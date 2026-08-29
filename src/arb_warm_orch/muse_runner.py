"""Warm-orch runtime 5 -- Muse Code (cold-process / warm-session).

Design: `docs/superpowers/specs/2026-08-06-muse-runner-design.md`.
Evidence: ARB Memory `findings-muse-code-seat-probe-20260806` v1.

WHAT MAKES THIS ONE DIFFERENT. codex, grok and pi all hold a process open
between turns. Muse does not: `muse exec` runs one prompt and exits, and
continuity comes from a persisted session UUID that two independent processes
share. So this module spawns per turn and owns an identity, not a connection.

THE SESSION-ID INVERSION (design §3.1) -- the one thing not to copy from
`runner.py`. That module's `interrupt` docstring (runner.py:238-250) records a
real coupling: the Claude SDK returns its session id at the END of a turn, so
`_persist_session_id` runs inside `stream_turn` (runner.py:293) and a caller
that ABANDONS the generator never advances the channel. Here WE mint the UUID,
so it is persisted by `connect()` before any process exists. Continuity
therefore survives a crash, a kill, and an abandoned generator, and the ordering
hazard simply does not arise. Copying the Claude ordering by reflex would have
imported a constraint that does not apply.

CONFORMANCE IS STRUCTURAL, NOT DECLARED. `acp_server.serve` takes
`runner: Any` (acp_server.py:78) -- there is no Protocol and no ABC, so nothing
type-checks this class. A missing method fails at call time in a live channel.
The method set is fixed by `WarmOrchRunner` (runner.py:226-295).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .muse_events import MuseEventMapper, parse_line
from .turn_events import TextDelta, TurnEvent

# Flags that widen a dispatched worker's blast radius. P1 has no evidence-gated
# story for any of them and P2 owns approvals, so `build_argv` must never emit
# one. Asserted by test rather than left to reviewer vigilance.
FORBIDDEN_FLAGS = frozenset(
    {
        "--yolo",
        "--disable-sandbox",
        "--disable-approval",
        "--trust-workspace",
        "--disable-write",
        "--disable-shell",
        "--allow-workspace-switch",
    }
)

SESSION_ID_FILENAME = "session-id"
PROMPT_FILENAME = "turn-prompt.md"

# The only `run.terminal.*` value attested against real Muse output: five
# occurrences across the committed fixtures, and every test. Named rather than
# inlined because it is an ALLOWLIST of one -- see the P2 check in stream_turn,
# which fails closed on anything else. Widen this only against captured bytes.
TERMINAL_OK = "completed"


class MuseTurnFailed(RuntimeError):
    """A turn did not complete. Raised rather than returning "".

    F4 (panel 2026-08-06). The runner previously ignored the child's exit
    status and never consumed the terminal it recorded, so a failed turn
    returned an empty string and the ACP wire reported `end_turn` — a seat that
    fails silently. `GrokTurnFailed` / `PiTurnFailed` are the precedent: both
    runtimes were changed to eliminate exactly this shape.
    """


@dataclass(frozen=True)
class MuseConfig:
    """Everything the runner needs. `muse_bin` is injectable for tests."""

    cwd: Path
    session_dir: Path
    model: str | None = None
    reasoning_effort: str | None = None
    muse_bin: str = "muse"


class MuseRunner:
    def __init__(self, config: MuseConfig) -> None:
        self.config = config
        self.session_id: str | None = None
        self._proc: Any | None = None
        self.last_exit_code: int | None = None
        # F2 (panel 2026-08-06). `acp_server.py:142-145` dispatches every
        # `session/prompt` as a TASK — documented as deliberate at :85-89, so a
        # cancel can be delivered mid-turn. Concurrent turns on one
        # --session-id are therefore reachable BY THE CONSUMER'S DESIGN, not
        # merely possible. Gate G4 (does concurrency corrupt a session?) is
        # unrun, so the runner enforces the assumption it documents instead of
        # trusting it: one turn at a time per runner.
        self._turn_lock = asyncio.Lock()

    # ------------------------------------------------------------ identity

    def _session_id_path(self) -> Path:
        return self.config.session_dir / SESSION_ID_FILENAME

    def _load_session_id(self) -> str | None:
        try:
            value = self._session_id_path().read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def _persist_session_id(self, session_id: str) -> None:
        path = self._session_id_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{session_id}\n", encoding="utf-8")

    async def connect(self) -> None:
        """Establish session IDENTITY. Spawns nothing.

        Idempotent: `stream_turn` calls it unconditionally, mirroring
        runner.py:256.
        """
        if self.session_id is not None:
            return
        existing = self._load_session_id()
        if existing is None:
            existing = str(uuid.uuid4()).lower()
            self._persist_session_id(existing)
        self.session_id = existing

    async def disconnect(self) -> None:
        """Reap any live child. The session id is deliberately KEPT --
        disconnecting is not the same as discarding the conversation."""
        await self.interrupt()

    # ------------------------------------------------------------ the turn

    def build_argv(self, prompt_file: Path) -> list[str]:
        """Split out from spawning so argv is assertable without a process."""
        if self.session_id is None:
            raise RuntimeError("connect() before build_argv()")
        argv = [
            self.config.muse_bin,
            "exec",
            "--json",
            "--session-id",
            self.session_id,
            "--prompt-file",
            str(prompt_file),
            "--workspace",
            str(self.config.cwd),
        ]
        if self.config.model:
            argv += ["--model", self.config.model]
        if self.config.reasoning_effort:
            argv += ["--reasoning-effort", self.config.reasoning_effort]
        return argv

    async def _spawn(self, argv: list[str], cwd: Path) -> Any:
        """Real spawn. Replaced wholesale in tests -- see test_muse_runner."""
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    def _write_prompt(self, text: str) -> Path:
        path = self.config.session_dir / PROMPT_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    async def _drain(stream) -> None:
        """Consume a pipe so the child can never block writing to it.

        F4 (panel 2026-08-06). `engines/_stdio.py:372` exists in this repo for
        exactly this reason: "a subprocess spawned with stderr=PIPE blocks on
        write() once the OS pipe buffer (~16-64 KB) fills if nothing reads it."
        Muse writes provider-retry telemetry to stderr on every turn, so a long
        turn could wedge the runner. Read and discard; never block.
        """
        if stream is None:
            return
        try:
            async for _ in stream:
                pass
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:  # noqa: BLE001 — a dead pipe must not fail the turn
            return

    async def stream_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        await self.connect()
        # F2: serialise. Held for the whole generator, so an abandoned turn
        # releases it rather than wedging the runner.
        #
        # P1 (panel second wave, 2026-08-07 — reproduced independently by agy,
        # grok and codex, each with its own executed repro). This was
        # `await self._turn_lock.acquire()` with the `try` opening only after
        # the spawn, leaving `_write_prompt`, `build_argv` and `_spawn` in the
        # gap between the acquire and the `finally` that released it. A raise
        # from any of them — missing binary, unwritable session_dir, bad cwd —
        # leaked the lock PERMANENTLY: every later turn on this runner blocked
        # forever, silently. `acp_server` dispatches every `session/prompt` as
        # a task, so one spawn failure wedged the whole seat with no local
        # recovery path.
        #
        # `async with` rather than a widened try/finally on purpose: it makes
        # the release structural instead of remembered. The bug it replaces was
        # introduced BY a fix (F2) that added an acquire and put the release
        # somewhere else — the exact mistake this form cannot make. Same defect
        # class as F1: a resource acquired outside the scope that frees it.
        #
        # Everything through the F4 raise stays INSIDE the lock, so the `last_*`
        # fields cannot be clobbered by a turn starting between release and read.
        async with self._turn_lock:
            prompt_file = self._write_prompt(text)
            argv = self.build_argv(prompt_file)

            mapper = MuseEventMapper()
            proc = await self._spawn(argv, self.config.cwd)
            self._proc = proc
            drainer = asyncio.ensure_future(self._drain(getattr(proc, "stderr", None)))
            try:
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace") if isinstance(
                        raw, (bytes, bytearray)
                    ) else raw
                    envelope = parse_line(line)
                    if envelope is None:
                        continue
                    for event in mapper.feed(envelope):
                        yield event
                await proc.wait()
                # The child has exited, so its stderr is at EOF and the drainer
                # finishes on its own. Await it so the pipe is fully consumed —
                # cancelling here would leave the tail unread and, worse, could
                # cancel the task before it was ever scheduled.
                await drainer
            finally:
                # F1 (panel 2026-08-06). This block runs on GeneratorExit too --
                # i.e. when a consumer ABANDONS the generator rather than draining
                # it, which `acp_server` does on a non-cancel re-raise. An earlier
                # version cleared `_proc` here WITHOUT reaping, orphaning the
                # child: `interrupt()` and `disconnect()` both early-return on
                # `_proc is None`, so nothing could ever kill it. The orphan holds
                # the session, and the next turn would spawn a CONCURRENT exec on
                # the same --session-id -- the unrun gate G4.
                #
                # Reap unconditionally, but only signal a child still alive: a
                # cleanly-finished turn must not be sent SIGTERM.
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
                if self._proc is proc:
                    self._proc = None
                drainer.cancel()
            # Outcome comes from run.terminal.* ONLY. `mapper.failures` may be
            # non-empty on a perfectly successful echo run (design §5.2).
            self.last_terminal = mapper.terminal
            self.last_terminal_text = mapper.terminal_text
            self.last_failures = list(mapper.failures)
            self.last_exit_code = proc.returncode
            # F4: the recorded outcome is now CONSUMED, not merely stored. Both
            # halves matter -- a non-zero exit is a failure even with a `completed`
            # terminal, and a zero exit with NO terminal means the turn never
            # finished. Either previously returned "" from `turn()`, which the ACP
            # wire reports as an ordinary `end_turn`.
            if proc.returncode not in (0, None):
                raise MuseTurnFailed(
                    f"muse exec exited {proc.returncode} "
                    f"(terminal={mapper.terminal!r}, failures={mapper.failures})"
                )
            if mapper.terminal is None:
                raise MuseTurnFailed(
                    "muse exec produced no run.terminal.* event; the turn did "
                    f"not complete (exit={proc.returncode!r}, "
                    f"failures={mapper.failures})"
                )
            # P2 (panel second wave, 2026-08-07, grok). The two checks above
            # left a hole: a terminal that is PRESENT but not a success --
            # `run.terminal.failed` with exit 0 -- passed both and returned its
            # partial text, which the ACP wire reports as an ordinary
            # `end_turn`. That is the same silent-success class F4 exists to
            # kill, just reached by a third route.
            #
            # Fails CLOSED against an allowlist, not open against a denylist:
            # `completed` is the ONLY terminal value attested anywhere in this
            # repo (five fixture occurrences, every test). Enumerating known-bad
            # values instead would silently admit the next unknown one, and
            # "silently admits an unknown outcome" is the precise defect being
            # removed. If a future build emits another success-shaped terminal,
            # this raises loudly and the allowlist gets widened against real
            # bytes -- a visible failure rather than an invisible wrong answer.
            if mapper.terminal != TERMINAL_OK:
                raise MuseTurnFailed(
                    f"muse exec reported terminal={mapper.terminal!r}, not "
                    f"{TERMINAL_OK!r} (exit={proc.returncode!r}, "
                    f"text={mapper.terminal_text!r}, "
                    f"failures={mapper.failures})"
                )

    async def turn(self, text: str) -> str:
        """Drain a whole turn and return its text.

        Unlike runner.py:226, draining is NOT load-bearing for continuity here
        -- the session id was persisted at connect(). It is just the reply.
        """
        parts = [
            event.text
            async for event in self.stream_turn(text)
            if isinstance(event, TextDelta)
        ]
        return "".join(parts)

    async def interrupt(self) -> None:
        """SIGTERM the live child; KEEP the session id.

        MEASURED — gate G1 RUN and PASSED 2026-08-07 (design §12). A turn
        SIGTERMed mid-flight (rc=143, no terminal event, 16 events against the
        usual 26) was followed by a clean recall of the pre-kill token on the
        same --session-id. Fixtures: `tests/fixtures/muse/killed-turn-sigterm
        .jsonl` and `killed-turn-then-recall.jsonl`. So retaining the id is
        licensed by evidence, not by the argument that used to stand here.

        This docstring said "HYPOTHESIS, NOT MEASURED" until 2026-08-07, three
        commits after G1 closed — flagged by the grok seat in the second-wave
        panel. Recorded because stale prose is a live claim: a reader trusts a
        docstring at the strength it asserts, and this one understated what the
        repo could prove.

        No SIGKILL escalation: whether Muse cleans up its session log on
        SIGTERM is still unmeasured — G1 covered resumability after a SIGTERM,
        not what a SIGKILL would leave behind. That remains a guess nobody has
        tested, so no kill path is added.
        """
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        finally:
            self._proc = None

    # ------------------------------------------------- the system-prompt seam

    def apply_system_prompt(self, text: str) -> None:
        """Deliberately LOUD. Design §5.4, and the reason is on the record.

        `acp_server.py:290` adopts a system prompt via
        `getattr(runner, "apply_system_prompt", None)` and NO-OPS when it is
        absent. That module's own docstring (acp_server.py:263-280) records what
        that cost with codex: a seat that "works perfectly and says nothing in
        the channel", plus a live-test round burned on a docstring that had
        asserted codex had no such seam at all.

        Muse almost certainly HAS a seam -- its echo provider errors with
        "provider does not support base instructions" -- but verified on
        2026-08-06, `muse exec` exposes no flag for it: a grep for
        --system/--instruction over the full exec flag list returns 0. The
        candidates (`--agents <JSON>`, `--preset`) are TOP-LEVEL flags absent
        from exec's list, so whether they compose with the subcommand at all is
        unresolved -- that is gate G2.

        Raising is the point. Defining this method silently would make Muse the
        fourth seat to drop the client's prompt without saying so; omitting it
        would do the same via the getattr no-op. Until G2 answers, a caller that
        tries to set a system prompt must find out.
        """
        raise NotImplementedError(
            "muse system-prompt seam is unresolved (gate G2): `muse exec` has "
            "no --system-prompt flag, and the --agents/--preset overlay is a "
            "top-level flag whose composition with `exec` is unmeasured. "
            "Refusing loudly rather than dropping the prompt silently."
        )
