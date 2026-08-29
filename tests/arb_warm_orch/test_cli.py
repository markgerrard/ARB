"""Warm-orch test-drive slice: CLI driver.

The CLI is the pre-ACP cockpit: lines in, replies out, one warm channel per
invocation. It must be drivable non-interactively (pipe a prompt in, read the
reply out) because the kill-mid-arc resume test IS two separate processes.
The runner is injected via factory so these tests never touch the SDK. One
test per named behavior, red-first.
"""
from __future__ import annotations

import io

from pathlib import Path

import pytest

from arb_warm_orch.cli import (
    build_codex_runner,
    build_muse_runner,
    main,
    parse_args,
)


class SyncFakeRunner:
    """The codex runner is synchronous — the drive loop must handle both."""

    def __init__(self):
        self.turns: list[str] = []
        self.disconnected = False

    def turn(self, text: str) -> str:
        self.turns.append(text)
        return f"sync-reply-to:{text}"

    def disconnect(self) -> None:
        self.disconnected = True


class FakeRunner:
    def __init__(self):
        self.turns: list[str] = []
        self.disconnected = False

    async def turn(self, text: str) -> str:
        self.turns.append(text)
        return f"reply-to:{text}"

    async def disconnect(self) -> None:
        self.disconnected = True


def _main(argv, lines):
    runner = FakeRunner()
    out = io.StringIO()
    code = main(
        argv,
        input_lines=iter(lines),
        output=out,
        runner_factory=lambda config: runner,
    )
    return code, runner, out.getvalue()


def test_channel_is_required():
    runner = FakeRunner()
    out = io.StringIO()
    code = main([], input_lines=iter([]), output=out, runner_factory=lambda c: runner)
    assert code == 2
    assert "channel" in out.getvalue()


def test_each_input_line_becomes_one_turn():
    code, runner, _ = _main(["--channel", "c1"], ["hello", "again"])
    assert code == 0
    assert runner.turns == ["hello", "again"]


def test_replies_are_written_to_output():
    _, _, out = _main(["--channel", "c1"], ["hello"])
    assert "reply-to:hello" in out


def test_blank_lines_are_skipped():
    _, runner, _ = _main(["--channel", "c1"], ["", "  ", "real"])
    assert runner.turns == ["real"]


def test_runner_disconnects_at_end_of_input():
    _, runner, _ = _main(["--channel", "c1"], ["hello"])
    assert runner.disconnected is True


def test_config_carries_channel_and_cwd(tmp_path):
    captured = {}

    def factory(config):
        captured["config"] = config
        return FakeRunner()

    code = main(
        ["--channel", "chan-x", "--cwd", str(tmp_path), "--session-root", str(tmp_path / "s")],
        input_lines=iter([]),
        output=io.StringIO(),
        runner_factory=factory,
    )
    assert code == 0
    assert captured["config"].channel == "chan-x"
    assert captured["config"].cwd == str(tmp_path)


# ------------------------------------------------------------- live wiring


def test_default_dispatcher_is_the_stub():
    from arb_warm_orch.cli import build_dispatcher, parse_args
    from arb_warm_orch.dispatch import StubSeatDispatcher

    args = parse_args(["--channel", "c"])
    assert isinstance(build_dispatcher(args), StubSeatDispatcher)


def test_live_flag_builds_subprocess_dispatcher_with_config(tmp_path):
    from arb_warm_orch.cli import build_dispatcher, parse_args
    from arb_warm_orch.subprocess_dispatch import SubprocessSeatDispatcher

    args = parse_args(
        [
            "--channel", "c",
            "--live",
            "--bridge-clone", str(tmp_path / "bridge"),
            "--from-agent-id", "claude-bridge-dev",
            "--dispatch-branch", "dev",
            "--brief-dir", str(tmp_path / "briefs"),
            "--bus-env-file", str(tmp_path / "bus.env"),
            "--dispatch-timeout", "900",
        ]
    )
    dispatcher = build_dispatcher(args)
    assert isinstance(dispatcher, SubprocessSeatDispatcher)
    assert dispatcher.config.from_agent_id == "claude-bridge-dev"
    assert dispatcher.config.timeout == 900
    assert str(dispatcher.config.bridge_clone).endswith("bridge")


def test_live_flag_requires_bridge_clone(tmp_path):
    from arb_warm_orch.cli import build_dispatcher, parse_args

    args = parse_args(["--channel", "c", "--live"])
    import pytest as _pytest
    from arb_warm_orch.subprocess_dispatch import DispatchError

    with _pytest.raises(DispatchError, match="dispatch-live-config-missing"):
        build_dispatcher(args)


# ------------------------------------------------------- codex runtime
#
# The second runtime is SYNCHRONOUS (JSON-RPC over stdio) while the Claude
# runner is async. The cockpit drives both, so the loop awaits only what is
# actually awaitable — a sync runner must not be silently skipped or crash on
# `await`. Its live posture is also different: codex approvals BLOCK, so the
# approval policy mode is what makes the merge/close gate reachable at all.


def _sync_main(argv, lines):
    runner = SyncFakeRunner()
    out = io.StringIO()
    code = main(argv, input_lines=iter(lines), output=out, runner_factory=lambda c: runner)
    return code, runner, out.getvalue()


def test_codex_runtime_drives_a_synchronous_runner():
    code, runner, out = _sync_main(["--channel", "c1", "--runtime", "codex"], ["hello"])
    assert code == 0
    assert runner.turns == ["hello"]
    assert "sync-reply-to:hello" in out


def test_codex_runtime_disconnects_the_synchronous_runner():
    _, runner, _ = _sync_main(["--channel", "c1", "--runtime", "codex"], ["hello"])
    assert runner.disconnected is True


def test_runtime_defaults_to_claude():
    assert parse_args(["--channel", "c1"]).runtime == "claude"


def test_codex_runner_is_built_with_the_configured_channel_and_cwd(tmp_path):
    args = parse_args(
        [
            "--channel", "codex-1",
            "--runtime", "codex",
            "--cwd", str(tmp_path),
            "--session-root", str(tmp_path / "s"),
        ]
    )
    runner = build_codex_runner(args)
    assert runner.config.channel == "codex-1"
    assert runner.config.cwd == str(tmp_path)


def test_codex_runner_carries_approval_policy_and_sandbox_flags(tmp_path):
    args = parse_args(
        [
            "--channel", "codex-1",
            "--runtime", "codex",
            "--session-root", str(tmp_path / "s"),
            "--approval-policy", "untrusted",
            "--sandbox", "workspace-write",
        ]
    )
    runner = build_codex_runner(args)
    assert runner.config.approval_policy_mode == "untrusted"
    assert runner.config.sandbox_mode == "workspace-write"


def test_codex_runner_gate_refuses_merge_by_default(tmp_path):
    # Same posture as the Claude cockpit: no close-consumer is wired, so
    # merge/close must refuse. Asserted through the policy, not by inspection.
    args = parse_args(
        ["--channel", "c", "--runtime", "codex", "--session-root", str(tmp_path / "s")]
    )
    runner = build_codex_runner(args)
    answer = runner.approval_policy.answer(
        "item/commandExecution/requestApproval",
        {"itemId": "i", "threadId": "t", "turnId": "u", "startedAtMs": 0,
         "command": "git merge topic-x"},
    )
    assert answer.result == {"decision": "decline"}
    assert runner.approval_policy.records[-1].code == "merge-close-evidence-unresolved"


# ------------------------------------------------------------- ACP serving
#
# The graduation seam: the SAME runner, driven by an ACP client instead of the
# line loop. `--acp` selects who holds the loop, and nothing else.


def test_acp_flag_defaults_off_so_the_line_cockpit_is_unchanged():
    assert parse_args(["--channel", "c"]).acp is False


def test_acp_flag_selects_the_acp_server():
    assert parse_args(["--channel", "c", "--acp"]).acp is True


def test_acp_mode_serves_acp_and_never_reads_the_line_loop():
    """`--acp` hands the turn loop to the client. If the line loop also ran,
    stdin would be consumed by two readers and prompts would go missing."""
    runner = FakeRunner()
    served: dict = {}

    def fake_serve(served_runner, *, channel):
        served["runner"] = served_runner
        served["channel"] = channel
        return 0

    code = main(
        ["--channel", "warm-chan", "--acp"],
        input_lines=iter(["this line must not be consumed\n"]),
        output=io.StringIO(),
        runner_factory=lambda config: runner,
        acp_serve=fake_serve,
    )

    assert code == 0
    assert served["channel"] == "warm-chan"
    assert served["runner"] is runner
    assert runner.turns == []


# ------------------------------------------------------- grok warm runtime


def test_runtime_accepts_grok():
    assert parse_args(["--channel", "c", "--runtime", "grok"]).runtime == "grok"


def test_grok_runner_is_built_with_a_gate_wired():
    """A runtime reachable from the CLI without a gate would ship the gate
    unreachable — the exact defect the panel found in the codex policy."""
    from arb_warm_orch.cli import build_grok_runner

    runner = build_grok_runner(parse_args(["--channel", "c", "--runtime", "grok"]))
    assert runner.evidence_resolver is not None
    assert runner.config.channel == "c"


# -------------------------------------------------------- pi warm runtime


def test_runtime_accepts_pi():
    assert parse_args(["--channel", "c", "--runtime", "pi"]).runtime == "pi"


def test_pi_runner_is_built_with_a_gate_wired():
    from arb_warm_orch.cli import build_pi_runner

    runner = build_pi_runner(parse_args(["--channel", "c", "--runtime", "pi"]))
    assert runner.evidence_resolver is not None
    assert runner.config.channel == "c"


# --- P3 seat parity: the muse runtime ---------------------------------------
# muse_runner.py landed in P1 but nothing constructed it: --runtime accepted
# claude|codex|grok|pi only, so the runner was unreachable from the CLI. That is
# the whole of "the muse seat is not stood up".


def test_runtime_muse_is_selectable():
    args = parse_args(["--channel", "c", "--runtime", "muse"])
    assert args.runtime == "muse"


def test_build_muse_runner_wires_config_from_args(tmp_path):
    from arb_warm_orch.muse_runner import MuseRunner

    args = parse_args([
        "--channel", "c",
        "--runtime", "muse",
        "--cwd", str(tmp_path),
        "--session-root", str(tmp_path / "sessions"),
        "--model", "muse-spark-1.2",
        "--effort", "high",
        "--muse-bin", "/somewhere/muse",
    ])
    runner = build_muse_runner(args)
    assert isinstance(runner, MuseRunner)
    assert runner.config.cwd == Path(tmp_path)
    assert runner.config.model == "muse-spark-1.2"
    assert runner.config.reasoning_effort == "high"
    assert runner.config.muse_bin == "/somewhere/muse"
    # session_dir must live under session_root, not cwd: the session id is the
    # continuity mechanism and must survive a worktree being removed.
    assert Path(tmp_path / "sessions") in Path(runner.config.session_dir).parents or \
        Path(runner.config.session_dir) == Path(tmp_path / "sessions")


def test_muse_runtime_refuses_a_system_prompt_loudly(tmp_path):
    """G2 is UNRUN: muse exec has no --system-prompt seam, and whether a
    top-level --agents reaches an exec run is unmeasured. apply_system_prompt
    therefore raises. This asserts the CLI does not paper over it -- a seat that
    silently drops a composed prompt is the codex regression acp_server.py
    records, and it is the reason the method is loud in the first place."""
    args = parse_args([
        "--channel", "c", "--runtime", "muse",
        "--cwd", str(tmp_path), "--session-root", str(tmp_path / "s"),
    ])
    runner = build_muse_runner(args)
    with pytest.raises(NotImplementedError):
        runner.apply_system_prompt("be a reviewer")
