from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_redis_bridge.engines.agy_tmux import AgyTmuxEngine
from agent_redis_bridge.engines.base import EngineError


FIXTURE = Path(__file__).parent / "fixtures" / "agy_transcript.jsonl"


class FakeTmux:
    def __init__(self, *, frames: list[str], brain_root: Path, create_brain: bool = True) -> None:
        self.frames = frames
        self.brain_root = brain_root
        self.create_brain = create_brain
        self.commands: list[list[str]] = []
        self.command_kwargs: list[dict] = []
        self.session_alive = True
        self.frame_index = 0
        self.brain_dir = brain_root / "conv-test"
        self.transcript = self.brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
        self.loaded_buffers: dict[str, str] = {}
        self.submit_count = 0

    @staticmethod
    def _strip_socket(args: list[str]) -> list[str]:
        """Drop private ``-L <socket>`` so command matchers stay engine-agnostic."""
        out = list(args)
        if len(out) >= 3 and out[0] == "tmux" and out[1] == "-L":
            return [out[0], *out[3:]]
        return out

    def run(self, args, **kwargs):
        self.commands.append(list(args))
        self.command_kwargs.append(dict(kwargs))
        args = self._strip_socket(list(args))
        if args[:2] == ["tmux", "new-session"] and self.create_brain:
            self.transcript.parent.mkdir(parents=True, exist_ok=True)
            self.transcript.write_text("", encoding="utf-8")
        if args[:2] == ["tmux", "load-buffer"]:
            self.loaded_buffers[args[args.index("-b") + 1]] = kwargs.get("input") or ""
        if args[:2] == ["tmux", "paste-buffer"]:
            buffer_name = args[args.index("-b") + 1]
            text = self.loaded_buffers.get(buffer_name, "")
            self.submit_count += 1
            self.transcript.parent.mkdir(parents=True, exist_ok=True)
            with self.transcript.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "content": text}) + "\n")
        if args[:2] == ["tmux", "capture-pane"]:
            frame = self.frames[min(self.frame_index, len(self.frames) - 1)]
            self.frame_index += 1
            return _Completed(stdout=frame, returncode=0)
        if args[:2] == ["tmux", "has-session"]:
            return _Completed(returncode=0 if self.session_alive else 1)
        if args[:2] == ["tmux", "kill-session"]:
            self.session_alive = False
        return _Completed(returncode=0)


class _Completed:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _engine(fake: FakeTmux, cwd: str, **kwargs) -> AgyTmuxEngine:
    return AgyTmuxEngine(
        cwd=cwd,
        command="agy",
        tmux_command="tmux",
        run_command=fake.run,
        poll_s=0.01,
        startup_timeout_s=0.2,
        brain_root=fake.brain_root,
        **kwargs,
    )


class AgyTranscriptMappingTest(unittest.TestCase):
    def test_fixture_maps_to_codex_parity_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain_root = Path(td) / "brain"
            fake = FakeTmux(frames=["? for shortcuts", "esc to cancel", "? for shortcuts"], brain_root=brain_root)
            engine = _engine(fake, "/tmp/project")
            events: list[tuple[str, dict]] = []

            for event in engine.events_from_transcript(FIXTURE, turn_id="turn-fixture"):
                events.append(event)

        names = [name for name, _ in events]
        self.assertIn("model_text", names)
        self.assertIn("command_started", names)
        self.assertIn("command_output", names)
        self.assertIn("command_finished", names)
        self.assertNotIn("USER_INPUT", names)

        first_model = next(data for name, data in events if name == "model_text")
        self.assertEqual(first_model["seq"], 2)
        self.assertEqual(first_model["kind"], "model_text")
        self.assertEqual(first_model["turn_id"], "turn-fixture")
        self.assertIn("available permissions", first_model["delta"])

        first_tool_triplet = [(name, data) for name, data in events if data.get("tool_name") == "GENERIC"][:3]
        self.assertEqual([name for name, _ in first_tool_triplet], ["command_started", "command_output", "command_finished"])
        self.assertEqual(first_tool_triplet[0][1]["kind"], "command_started")
        self.assertEqual(first_tool_triplet[1][1]["kind"], "command_output")
        self.assertEqual(first_tool_triplet[2][1]["kind"], "command_finished")
        self.assertEqual(first_tool_triplet[0][1]["seq"], 3)
        self.assertEqual(first_tool_triplet[2][1]["status"], "DONE")
        self.assertIn("read and write access", first_tool_triplet[1][1]["delta"])

    def test_live_tail_emits_incremental_appends_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "transcript.jsonl"
            lines = FIXTURE.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[0] + "\n", encoding="utf-8")
            fake = FakeTmux(frames=["? for shortcuts"], brain_root=Path(td) / "brain")
            engine = _engine(fake, "/tmp/project")

            first = list(engine.tail_transcript(path, turn_id="turn-live"))
            self.assertEqual(first, [])

            with path.open("a", encoding="utf-8") as f:
                f.write(lines[2] + "\n")
                f.write(lines[3] + "\n")

            second = list(engine.tail_transcript(path, turn_id="turn-live"))
            self.assertEqual([name for name, _ in second], ["model_text", "command_started", "command_output", "command_finished"])
            self.assertEqual(list(engine.tail_transcript(path, turn_id="turn-live")), [])


class AgyTmuxDrivingTest(unittest.TestCase):
    def test_run_turn_drives_fresh_tmux_tails_transcript_and_cleans_brain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain_root = Path(td) / "brain"
            fake = FakeTmux(frames=["? for shortcuts", "esc to cancel Generating", "? for shortcuts"], brain_root=brain_root)
            engine = _engine(fake, "/tmp/project")

            def append_fixture() -> None:
                while not fake.transcript.exists():
                    time.sleep(0.005)
                lines = FIXTURE.read_text(encoding="utf-8").splitlines()
                with fake.transcript.open("a", encoding="utf-8") as f:
                    f.write(lines[2] + "\n")
                    f.write(lines[3] + "\n")
                    f.write(lines[-1] + "\n")

            threading.Thread(target=append_fixture, daemon=True).start()
            events: list[tuple[str, dict]] = []
            result = engine.run_turn_with_progress(
                "hello from test",
                timeout=2,
                policy="trusted",
                on_event=lambda name, data: events.append((name, data)),
            )

            self.assertTrue(result.ok)
            self.assertIn("wrote report", result.result)
            self.assertFalse(fake.brain_dir.exists())
            commands = [FakeTmux._strip_socket(cmd) for cmd in fake.commands]
            self.assertTrue(any(cmd[:2] == ["tmux", "new-session"] for cmd in commands))
            self.assertTrue(any(cmd[:3] == ["tmux", "send-keys", "-t"] and cmd[-1] == "Enter" for cmd in commands))
            self.assertEqual(fake.submit_count, 1)
            self.assertTrue(any(cmd[:2] == ["tmux", "load-buffer"] for cmd in commands))
            # Private socket + scrubbed env on every client call.
            self.assertTrue(
                any(
                    len(cmd) >= 3 and cmd[0] == "tmux" and cmd[1] == "-L"
                    for cmd in fake.commands
                )
            )
            self.assertTrue(all("env" in kw for kw in fake.command_kwargs))
            self.assertEqual(events[0][0], "turn_started")
            self.assertIn("command_output", [name for name, _ in events])
            self.assertEqual(events[-1][0], "turn_completed")

    def test_brain_ownership_requires_nonce_and_preserves_other_brains(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain_root = Path(td) / "brain"
            fake = FakeTmux(frames=["? for shortcuts", "esc to cancel Generating", "? for shortcuts"], brain_root=brain_root)
            other_brain = brain_root / "conv-other"
            other_transcript = other_brain / ".system_generated" / "logs" / "transcript.jsonl"
            other_transcript.parent.mkdir(parents=True)
            other_transcript.write_text(
                json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "other seat prompt"}) + "\n",
                encoding="utf-8",
            )
            engine = _engine(fake, "/tmp/project")

            def append_fixture() -> None:
                while not fake.transcript.exists() or "AGY_TMUX_NONCE_" not in fake.transcript.read_text(encoding="utf-8"):
                    time.sleep(0.005)
                lines = FIXTURE.read_text(encoding="utf-8").splitlines()
                with fake.transcript.open("a", encoding="utf-8") as f:
                    f.write(lines[2] + "\n")
                    f.write(lines[-1] + "\n")

            threading.Thread(target=append_fixture, daemon=True).start()
            result = engine.run_turn_with_progress("own this brain", timeout=2, policy="trusted", on_event=None)

            self.assertTrue(result.ok)
            self.assertFalse(fake.brain_dir.exists())
            self.assertTrue(other_brain.exists())

    def test_multiline_task_is_pasted_as_single_submission(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain_root = Path(td) / "brain"
            fake = FakeTmux(frames=["? for shortcuts", "esc to cancel Generating", "? for shortcuts"], brain_root=brain_root)
            engine = _engine(fake, "/tmp/project")

            def append_result() -> None:
                while "line one\nline two" not in "\n".join(fake.loaded_buffers.values()):
                    time.sleep(0.005)
                with fake.transcript.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "done"}) + "\n")

            threading.Thread(target=append_result, daemon=True).start()
            result = engine.run_turn_with_progress("line one\nline two", timeout=2, policy="trusted", on_event=None)

            self.assertTrue(result.ok)
            self.assertEqual(fake.submit_count, 1)
            self.assertFalse(
                any(
                    cmd[:2] == ["tmux", "send-keys"] and "-l" in cmd
                    for cmd in (FakeTmux._strip_socket(c) for c in fake.commands)
                )
            )
            loaded = "\n".join(fake.loaded_buffers.values())
            self.assertIn("line one\nline two", loaded)

    def test_fast_turn_completes_from_idle_plus_transcript_progress_without_busy_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain_root = Path(td) / "brain"
            fake = FakeTmux(frames=["? for shortcuts", "? for shortcuts", "? for shortcuts"], brain_root=brain_root)
            engine = _engine(fake, "/tmp/project")

            def append_result() -> None:
                while not fake.transcript.exists() or "AGY_TMUX_NONCE_" not in fake.transcript.read_text(encoding="utf-8"):
                    time.sleep(0.005)
                with fake.transcript.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "fast done"}) + "\n")

            threading.Thread(target=append_result, daemon=True).start()
            events: list[tuple[str, dict]] = []
            result = engine.run_turn_with_progress(
                "fast",
                timeout=2,
                policy="trusted",
                on_event=lambda name, data: events.append((name, data)),
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.result, "fast done")
            self.assertIn("model_text", [name for name, _ in events])

    def test_stop_kills_session_and_deletes_owned_brain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain_root = Path(td) / "brain"
            fake = FakeTmux(frames=["? for shortcuts"], brain_root=brain_root)
            engine = _engine(fake, "/tmp/project")
            fake.transcript.parent.mkdir(parents=True)
            fake.transcript.write_text("", encoding="utf-8")
            engine.active_session = "agy-tmux-stop-test"
            engine.active_brain_dir = fake.brain_dir

            engine.stop()

            self.assertFalse(fake.session_alive)
            self.assertFalse(fake.brain_dir.exists())

    def test_startup_without_idle_marker_fails_loud_and_preserves_unowned_brain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain_root = Path(td) / "brain"
            fake = FakeTmux(frames=["loading", "still loading", "no prompt"], brain_root=brain_root)
            engine = _engine(fake, "/tmp/project")

            with self.assertRaises(EngineError):
                engine.run_turn_with_progress("hi", timeout=1, policy="trusted", on_event=None)

            self.assertTrue(fake.brain_dir.exists())
            self.assertFalse(engine.healthy)

    def test_timeout_sends_escape_marks_unhealthy_and_deletes_brain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain_root = Path(td) / "brain"
            fake = FakeTmux(
                frames=["? for shortcuts", "esc to cancel Generating", "esc to cancel Generating"],
                brain_root=brain_root,
            )
            engine = _engine(fake, "/tmp/project")

            result = engine.run_turn_with_progress("hi", timeout=0.05, policy="trusted", on_event=None)

            self.assertFalse(result.ok)
            self.assertIn("timed out", result.error or "")
            self.assertFalse(engine.healthy)
            self.assertFalse(fake.brain_dir.exists())
            self.assertTrue(
                any(
                    cmd[:3] == ["tmux", "send-keys", "-t"] and cmd[-1] == "Escape"
                    for cmd in (FakeTmux._strip_socket(c) for c in fake.commands)
                )
            )


class AgyTmuxBridgeWiringTest(unittest.TestCase):
    def test_build_engine_returns_agy_tmux(self) -> None:
        from agent_redis_bridge.bridge import ENGINE_TO_TOOL, build_engine

        self.assertEqual(ENGINE_TO_TOOL["agy-tmux"], "agy-tmux")
        args = argparse.Namespace(engine="agy-tmux", model=None)
        engine = build_engine(args, cwd="/tmp/project")
        self.assertIsInstance(engine, AgyTmuxEngine)


if __name__ == "__main__":
    unittest.main()


class ToolOnlyTurnLegibilityTest(unittest.TestCase):
    def test_tool_only_turn_failure_carries_an_explicit_error(self) -> None:
        # Audit AGY-3 (panel-confirmed): a clean turn whose transcript ends on a
        # tool event (no PLANNER_RESPONSE) returned ok=False with error=None —
        # a failure with no explanation anywhere downstream.
        with tempfile.TemporaryDirectory() as td:
            brain_root = Path(td) / "brain"
            fake = FakeTmux(frames=["? for shortcuts", "? for shortcuts", "? for shortcuts"], brain_root=brain_root)
            engine = _engine(fake, "/tmp/project")

            def append_tool_only() -> None:
                while not fake.transcript.exists() or "AGY_TMUX_NONCE_" not in fake.transcript.read_text(encoding="utf-8"):
                    time.sleep(0.005)
                with fake.transcript.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"step_index": 1, "type": "TOOL_CALL", "tool": "run_command", "content": "ls"}) + "\n")

            threading.Thread(target=append_tool_only, daemon=True).start()
            result = engine.run_turn_with_progress("tool only", timeout=2, policy="trusted", on_event=None)

            self.assertFalse(result.ok)
            self.assertTrue(result.error, "ok=False must carry an explicit error message")
            self.assertIn("final response", result.error)
