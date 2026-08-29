"""Pre-ACP cockpit CLI: lines in, warm-channel replies out.

Non-interactive by design — `echo "prompt" | python -m arb_warm_orch
--channel c` is one turn, and the kill-mid-arc resume proof is simply two
such processes against the same channel. The graduation chain (direct CLI →
ACP client → buzz) swaps who holds this loop; the runner underneath is
identical.

Test-drive wiring: the stub seat dispatcher answers every dispatch_seat call,
and the evidence resolver reports nothing resolvable — so the merge/close
gate DENIES gated actions with its specific code. That is the correct
test-drive posture: no close-consumer exists yet, so no merge/close evidence
can resolve.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from .acp_server import BlockingWriter, serve
from .codex_approvals import ApproveNonGated, CodexApprovalPolicy
from .codex_runner import CodexAppServerRunner, CodexOrchConfig
from .codex_stdio import spawn_codex_app_server, spawn_stdio_agent
from .grok_runner import GrokAcpRunner, GrokOrchConfig
from .muse_runner import MuseConfig, MuseRunner
from .pi_runner import PiOrchConfig, PiSdkRunner
from .dispatch import SeatDispatcher, StubSeatDispatcher
from .gates import EvidenceCheck
from .runner import WarmOrchConfig, WarmOrchRunner
from .subprocess_dispatch import (
    DispatchError,
    LiveDispatchConfig,
    SubprocessSeatDispatcher,
)

DEFAULT_SESSION_ROOT = Path.home() / ".arb-warm-orch" / "sessions"


class NoCloseConsumerYet:
    """Test-drive resolver: nothing resolves until a close-consumer exists."""

    def resolve(self, tool_name: str, tool_input: dict[str, Any]) -> EvidenceCheck:
        return EvidenceCheck(
            resolvable=False,
            detail="no close-consumer wired in the test-drive; merge/close stays refused",
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="arb-warm-orch")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--session-root", default=str(DEFAULT_SESSION_ROOT))
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--effort",
        default=None,
        help=(
            "pin reasoning effort for this seat (low|medium|high|xhigh|max|ultra); "
            "codex and muse runtimes. For codex, omit to follow ~/.codex/config.toml, "
            "which for an unpinned model falls back to the model default "
            "(gpt-5.6-sol: low). For muse it maps to `muse exec --reasoning-effort`; "
            "note gate G3 measured NO reasoning stream at `high`, so raising it buys "
            "no observability in the activity panel."
        ),
    )
    parser.add_argument("--system-prompt", default=None)
    parser.add_argument("--live", action="store_true", help="dispatch to real bridge seats")
    parser.add_argument("--bridge-clone", default=None)
    parser.add_argument("--from-agent-id", default="claude-bridge-dev")
    parser.add_argument("--dispatch-branch", default="dev")
    parser.add_argument("--brief-dir", default=None)
    parser.add_argument("--bus-env-file", default=None)
    parser.add_argument("--dispatch-timeout", type=int, default=1800)
    parser.add_argument(
        "--acp",
        action="store_true",
        help=(
            "serve ACP over stdio instead of the line cockpit. Same runner, "
            "same gates; only who holds the turn loop changes."
        ),
    )
    parser.add_argument(
        "--runtime",
        choices=("claude", "codex", "grok", "pi", "muse"),
        default="claude",
        help="which warm-orch runtime to drive",
    )
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--muse-bin", default="muse")
    parser.add_argument("--grok-bin", default="grok")
    parser.add_argument("--pi-host", default=None,
                        help="path to tools/pi-sdk-host/host.mjs")
    parser.add_argument("--node-bin", default="node")
    parser.add_argument(
        "--approval-policy",
        default=None,
        help=(
            "codex approval policy (e.g. untrusted). Load-bearing: under the "
            "default policy an exec may be auto-approved, so no approval "
            "request reaches the gate and it is never consulted."
        ),
    )
    parser.add_argument(
        "--sandbox",
        default=None,
        help="codex sandbox mode (read-only | workspace-write | danger-full-access)",
    )
    return parser.parse_args(argv)


def build_dispatcher(args: argparse.Namespace) -> SeatDispatcher:
    if not args.live:
        return StubSeatDispatcher()
    if not args.bridge_clone:
        raise DispatchError("dispatch-live-config-missing: --live requires --bridge-clone")
    bridge_clone = Path(args.bridge_clone)
    return SubprocessSeatDispatcher(
        LiveDispatchConfig(
            bridge_clone=bridge_clone,
            from_agent_id=args.from_agent_id,
            branch=args.dispatch_branch,
            brief_dir=Path(args.brief_dir) if args.brief_dir else Path(args.session_root) / "briefs",
            env_file=Path(args.bus_env_file) if args.bus_env_file else None,
            timeout=args.dispatch_timeout,
        )
    )


def build_codex_runner(args: argparse.Namespace) -> CodexAppServerRunner:
    """Wire the codex runtime with the same refusal posture as the Claude one.

    `dispatch_seat` is absent here on purpose: codex exposes client tools via a
    different seam (`item/tool/call` / client-provided MCP servers), which is
    its own slice. This runner is the protocol + gate half.
    """
    return CodexAppServerRunner(
        CodexOrchConfig(
            channel=args.channel,
            cwd=args.cwd,
            session_root=Path(args.session_root),
            model=args.model,
            developer_instructions=args.system_prompt,
            approval_policy_mode=args.approval_policy,
            sandbox_mode=args.sandbox,
            effort=args.effort,
        ),
        approval_policy=CodexApprovalPolicy(
            evidence=NoCloseConsumerYet(), base_policy=ApproveNonGated()
        ),
        transport_factory=lambda: spawn_codex_app_server(args.codex_bin, cwd=args.cwd),
    )


def build_grok_runner(args: argparse.Namespace) -> GrokAcpRunner:
    """Wire the grok runtime with the same refusal posture as the others.

    The gate is passed explicitly because `GrokAcpRunner` REQUIRES it — a
    runtime that can be constructed gate-less ships with the gate unreachable.
    Unlike the ACP server, gating here is on `session/request_permission`:
    we are the client, so that verdict is ours to give (log entry 36).
    """
    return GrokAcpRunner(
        GrokOrchConfig(
            channel=args.channel,
            cwd=args.cwd,
            session_root=Path(args.session_root),
            model=args.model,
        ),
        transport_factory=lambda: spawn_stdio_agent(
            args.grok_bin, ["agent", "stdio"], cwd=args.cwd
        ),
        evidence_resolver=NoCloseConsumerYet(),
    )


def build_muse_runner(args: argparse.Namespace) -> MuseRunner:
    """Wire the muse runtime. P3 seat parity; the runner shipped in P1.

    Muse is a COLD-PROCESS / WARM-SESSION seat: one `muse exec` per turn, no
    long-lived stdin loop, continuity carried by a persisted --session-id. Every
    other runtime here holds a process open, so there is no transport to build
    and no host script to spawn -- the config IS the wiring.

    session_dir goes under --session-root, not --cwd, on purpose. The session id
    is the ONLY continuity mechanism, and putting it in the workspace would mean
    a removed worktree silently starts a fresh conversation. It is minted at
    connect(), before any process exists, so it survives a crash, a kill and an
    abandoned generator (design §3.1).

    NOT WIRED, deliberately: --system-prompt. `muse exec` exposes no
    system-prompt flag and gate G2 (does a top-level --agents reach an exec
    run's model context?) is UNRUN, so `apply_system_prompt` raises rather than
    silently dropping a composed prompt. Passing --system-prompt with this
    runtime does nothing here and will raise loudly on the ACP path -- which is
    the intended behaviour, not an oversight. Put reviewer/role instructions in
    the prompt text until G2 lands.
    """
    session_root = Path(args.session_root)
    return MuseRunner(
        MuseConfig(
            cwd=Path(args.cwd),
            session_dir=session_root / "muse" / args.channel,
            model=args.model,
            reasoning_effort=args.effort,
            muse_bin=args.muse_bin,
        )
    )


def build_pi_runner(args: argparse.Namespace) -> PiSdkRunner:
    """Wire the pi runtime with the same refusal posture as the others.

    The runner asks host.mjs for a PERSISTED session and turns on the
    host->client approval wire; both are opt-in params the cold pi-sdk seat
    engine never sends, so seats are unaffected.
    """
    host_script = args.pi_host or str(
        Path(__file__).resolve().parents[2] / "tools" / "pi-sdk-host" / "host.mjs"
    )
    return PiSdkRunner(
        PiOrchConfig(
            channel=args.channel,
            cwd=args.cwd,
            session_root=Path(args.session_root),
            model=args.model,
            append_system_prompt=args.system_prompt,
        ),
        transport_factory=lambda: spawn_stdio_agent(
            args.node_bin, [host_script], cwd=args.cwd
        ),
        evidence_resolver=NoCloseConsumerYet(),
    )


def _default_runner_factory(config: WarmOrchConfig, dispatcher: SeatDispatcher | None = None) -> WarmOrchRunner:
    return WarmOrchRunner(
        config,
        dispatcher=dispatcher if dispatcher is not None else StubSeatDispatcher(),
        evidence_resolver=NoCloseConsumerYet(),
    )


def serve_acp_over_stdio(runner: Any, *, channel: str) -> int:
    """Bridge this process's stdin/stdout into the ACP serve loop."""

    async def drive() -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        # NOT connect_write_pipe: it rejects a regular file, so redirecting
        # stdout to one would crash the server (see BlockingWriter).
        writer = BlockingWriter(sys.stdout.buffer)
        try:
            await serve(reader, writer, runner=runner, channel=channel)
        finally:
            await _maybe_await_value(runner.disconnect())

    asyncio.run(drive())
    return 0


async def _maybe_await_value(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def main(
    argv: list[str] | None = None,
    *,
    input_lines: Iterator[str] | None = None,
    output: TextIO | None = None,
    runner_factory: Callable[[WarmOrchConfig], Any] | None = None,
    acp_serve: Callable[..., int] | None = None,
) -> int:
    out = output if output is not None else sys.stdout
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        out.write("error: --channel is required\n")
        return int(exc.code or 2)

    if runner_factory is None:
        if args.runtime == "codex":
            runner_factory = lambda config: build_codex_runner(args)
        elif args.runtime == "grok":
            runner_factory = lambda config: build_grok_runner(args)
        elif args.runtime == "pi":
            runner_factory = lambda config: build_pi_runner(args)
        elif args.runtime == "muse":
            runner_factory = lambda config: build_muse_runner(args)
        else:
            dispatcher = build_dispatcher(args)
            runner_factory = lambda config: _default_runner_factory(config, dispatcher)

    config = WarmOrchConfig(
        channel=args.channel,
        cwd=args.cwd,
        session_root=Path(args.session_root),
        model=args.model,
        system_prompt=args.system_prompt,
    )
    runner = runner_factory(config)

    if args.acp:
        # The client holds the turn loop now. Returning here matters: letting
        # the line loop also run would put two readers on stdin.
        serve_fn = acp_serve if acp_serve is not None else serve_acp_over_stdio
        return serve_fn(runner, channel=args.channel)

    lines = input_lines if input_lines is not None else sys.stdin

    # The Claude runner is async (SDK); the codex runner is sync (stdio
    # JSON-RPC). Await only what is actually awaitable rather than keeping two
    # drive loops in step with each other.
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def drive() -> None:
        try:
            for line in lines:
                text = line.strip()
                if not text:
                    continue
                reply = await _maybe_await(runner.turn(text))
                out.write(f"{reply}\n")
                out.flush()
        finally:
            await _maybe_await(runner.disconnect())

    asyncio.run(drive())
    return 0
