"""Mutation sweep for muse_runner: prove its tests can fail.

Run:
    /Volumes/<workspace>/repos/ARB/.venv/bin/python tests/mutation_check_muse_runner.py

Every test in tests/test_muse_runner.py was written after the implementation, so
none of them ever ran red against missing code. This converts that claim into a
measurement. The four design decisions most worth pinning are the ones a future
edit would "tidy" without noticing:

* interrupt KEEPS the session id (design §4) -- rotating it is the safe-looking
  change that silently discards conversation context.
* apply_system_prompt is LOUD (design §5.4) -- making it a no-op is the exact
  regression acp_server.py:263-280 records costing a live-test round with codex.
* connect persists the id BEFORE any process (design §3.1) -- moving it to
  turn-end reproduces the runner.py coupling this runtime does not have.
* the prompt travels as a FILE, never argv -- the repo's recorded `\\n`
  shell-quoting failure class.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _mutation_lib import sweep  # noqa: E402

TARGET = "arb_warm_orch/muse_runner.py"
TEST_FILE = "tests/test_muse_runner.py"

MUTATIONS = [
    (
        "abandoned stream_turn stops reaping the child (F1 regression)",
        # Indentation shifted +4 on 2026-08-07 when stream_turn moved under
        # `async with self._turn_lock:` (panel P1). A multi-line anchor is
        # indentation-sensitive, so it stopped matching and the sweep reported
        # SKIP/unproven rather than a false pass — which is the whole point of
        # counting a missing anchor as a failure.
        "                if proc.returncode is None:\n"
        "                    try:\n"
        "                        proc.terminate()\n"
        "                    except ProcessLookupError:\n"
        "                        pass\n"
        "                    await proc.wait()\n",
        "",
        "test_abandoning_stream_turn_still_reaps_the_child",
    ),
    (
        "F4: non-zero exit no longer raises (silent empty success returns)",
        "        if proc.returncode not in (0, None):",
        "        if False:",
        "test_nonzero_exit_raises_rather_than_returning_empty",
    ),
    (
        "F4: a turn with no terminal event no longer raises",
        "        if mapper.terminal is None:",
        "        if False:",
        "test_missing_terminal_raises",
    ),
    (
        # P2 (second wave, 2026-08-07, grok). Note the companion hazard this
        # check created: it also refuses `terminal is None`, which made the
        # neighbouring missing-terminal test unable to fail until that test was
        # changed to assert its SPECIFIC message. This sweep is what caught it.
        "P2: a non-completed terminal no longer raises (silent partial success)",
        "            if mapper.terminal != TERMINAL_OK:",
        "            if False:",
        "test_non_completed_terminal_raises_even_on_exit_zero",
    ),
    (
        "F4: stderr drain removed (the ~16-64KB pipe deadlock returns)",
        '        drainer = asyncio.ensure_future(self._drain(getattr(proc, "stderr", None)))',
        "        drainer = asyncio.ensure_future(self._drain(None))",
        "test_stderr_is_drained",
    ),
    (
        "F2: turn serialisation removed (concurrent execs on one session-id)",
        # `if True:` rather than `pass`: the lock is now a block statement, so
        # deleting it outright would dedent the whole turn body and fail to
        # compile — a syntax error is not a mutation, it is a vacuous mutant
        # (the F5 lesson). `if True:` keeps the block shape and removes ONLY
        # the serialisation, which is the behaviour under test.
        "        async with self._turn_lock:",
        "        if True:",
        "test_concurrent_turns_never_overlap",
    ),
    # NO MUTATION for the P1 lock-leak fix, deliberately — recorded here so the
    # gap is visible rather than looking like an oversight.
    #
    # The obvious mutant (swap `async with` back for a bare `acquire()`) removes
    # the release from EVERY path, not just the setup-failure path the fix is
    # about. The suite then deadlocks instead of failing: this sweep has no
    # per-test timeout, so it hangs indefinitely and the harness becomes
    # unusable. A mutation that hangs is strictly worse than no mutation — it
    # converts a signal into a lost afternoon (observed 2026-08-07; the sweep
    # was killed at ~2min with a wedged pytest child).
    #
    # The fix is pinned by direct RED→GREEN evidence instead, which is stronger
    # for this defect than a mutant would be: all three tests in
    # TestTurnLockIsAlwaysReleased were observed FAILING against the
    # acquire-then-try-later code and PASSING after the `async with` change.
    # The "F2: turn serialisation removed" mutant above still proves the
    # concurrency test can fail, which is the neighbouring guarantee.
    #
    # If this sweep ever grows a per-test timeout, add the mutant back.
    (
        "interrupt ROTATES the session id (silent context loss)",
        "        proc = self._proc\n        if proc is None:\n            return",
        "        import uuid as _u\n"
        "        self.session_id = str(_u.uuid4()).lower()\n"
        "        proc = self._proc\n        if proc is None:\n            return",
        "test_interrupt_terminates_child_and_keeps_session_id",
    ),
    (
        "apply_system_prompt becomes a silent no-op (the codex regression)",
        "        raise NotImplementedError(",
        "        return None\n        raise NotImplementedError(",
        "test_apply_system_prompt_is_loud_not_a_silent_noop",
    ),
    (
        "connect stops persisting -- id would only exist in memory",
        "            existing = str(uuid.uuid4()).lower()\n"
        "            self._persist_session_id(existing)",
        "            existing = str(uuid.uuid4()).lower()",
        "test_connect_persists_session_id_before_any_process",
    ),
    # VACUOUS MUTANT, CORRECTED 2026-08-06 (panel finding F5, cold-opus).
    # The original injected `self._last_prompt_text` -- an attribute that
    # exists NOWHERE in src/. The mutant therefore raised AttributeError and
    # the test died for the wrong reason, while the sweep reported it "caught
    # by test_prompt_reaches_a_file_not_argv". It proved nothing about argv
    # leakage. A mutation that references a non-existent name is not a
    # mutation, it is a syntax bomb -- exactly the can't-fail check this file
    # exists to prevent, committed by this file.
    #
    # The replacement appends the real, in-scope prompt text to argv, which is
    # the actual regression the test claims to guard.
    (
        "prompt appended to argv as well as --prompt-file (quoting hazard)",
        "        argv = self.build_argv(prompt_file)",
        "        argv = self.build_argv(prompt_file) + [text]",
        "test_prompt_reaches_a_file_not_argv",
    ),
    (
        "build_argv adds --yolo (blast-radius widening)",
        '        if self.config.model:',
        '        argv.append("--yolo")\n        if self.config.model:',
        "test_argv_never_widens_the_blast_radius",
    ),
    (
        # Was NOT CAUGHT until 2026-08-06: the original turn() test used a
        # fixture with no tool traffic, so "join everything" and "join text
        # only" produced identical output. TestReplyIsolation exists because
        # this mutation survived.
        "turn() joins every event, leaking tool traffic into the reply",
        "            if isinstance(event, TextDelta)",
        "            if True",
        "test_tool_events_are_emitted_but_excluded_from_the_reply",
    ),
    (
        "task.lifecycle.failed made terminal at the runner layer",
        "        self.last_terminal = mapper.terminal",
        "        self.last_terminal = "
        '"failed" if mapper.failures else mapper.terminal',
        "test_turn_returns_joined_text_despite_failed_events",
    ),
    (
        "build_argv no longer requires connect() (id could be None on the wire)",
        "        if self.session_id is None:\n"
        '            raise RuntimeError("connect() before build_argv()")',
        "        pass",
        "test_build_argv_before_connect_is_an_error",
    ),
]


if __name__ == "__main__":
    raise SystemExit(sweep(TARGET, TEST_FILE, MUTATIONS))
