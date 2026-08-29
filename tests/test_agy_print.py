from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_redis_bridge.engines.agy_print import AgyPrintEngine, AgySqliteTranscript, get_field, read_varint
from agent_redis_bridge.engines.base import EngineError


FIXTURE_DB = Path(__file__).parent / "fixtures" / "agy_conversation.db"


class FakeProcess:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0, pid: int = 4242) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = pid
        self.terminated = False
        self.killed = False
        self.waited = False

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: int | None = None) -> int:
        self.waited = True
        return self.returncode


class AgyPrintEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        # Hermetic conversations root: engine tests must not depend on the
        # runner's real ~/.gemini tree (present locally, absent in CI).
        self._root_td = tempfile.TemporaryDirectory()
        self.addCleanup(self._root_td.cleanup)
        self.conversations_root = Path(self._root_td.name)

    def _make_engine(self, process: FakeProcess) -> tuple[AgyPrintEngine, list[dict]]:
        captured: list[dict] = []

        def factory(args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            return process

        engine = AgyPrintEngine(
            cwd="/tmp/project",
            popen_factory=factory,
            conversations_root=self.conversations_root,
        )
        return engine, captured

    def test_run_turn_builds_print_command_with_trusted_policy(self) -> None:
        process = FakeProcess(stdout="report text\n")
        engine, captured = self._make_engine(process)

        result = engine.run_turn_with_progress("review the PR", timeout=120, policy="trusted", on_event=None)

        self.assertTrue(result.ok)
        self.assertEqual(result.result, "report text")
        self.assertEqual(
            captured[0]["args"],
            [
                "agy",
                "--print",
                mock.ANY,
                "--print-timeout",
                "120s",
                "--add-dir",
                "/tmp/project",
                "--dangerously-skip-permissions",
            ],
        )
        self.assertIn("review the PR", captured[0]["args"][2])
        self.assertIn("AGY_PRINT_NONCE_", captured[0]["args"][2])
        self.assertEqual(captured[0]["kwargs"]["cwd"], "/tmp/project")

    def test_run_turn_omits_add_dir_when_cwd_unset(self) -> None:
        process = FakeProcess(stdout="ok")
        captured: list[dict] = []

        def factory(args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            return process

        engine = AgyPrintEngine(cwd="", popen_factory=factory)
        engine.run_turn_with_progress("hi", timeout=60, policy="trusted", on_event=None)

        self.assertNotIn("--add-dir", captured[0]["args"])

    def test_run_turn_omits_skip_permissions_for_human_policy(self) -> None:
        process = FakeProcess(stdout="ok")
        engine, captured = self._make_engine(process)

        engine.run_turn_with_progress("hi", timeout=60, policy="human", on_event=None)

        self.assertNotIn("--dangerously-skip-permissions", captured[0]["args"])

    def test_run_turn_emits_started_and_completed_events(self) -> None:
        process = FakeProcess(stdout="done")
        engine, _ = self._make_engine(process)
        events: list[tuple[str, dict]] = []

        engine.run_turn_with_progress("hi", timeout=60, policy="trusted", on_event=lambda n, d: events.append((n, d)))

        names = [name for name, _ in events]
        self.assertEqual(names, ["turn_started", "model_text", "turn_completed"])
        self.assertEqual(events[1][1]["delta"], "done")
        self.assertEqual(events[1][1]["turn_id"], "4242")
        self.assertEqual(events[1][1]["item_id"], "4242:text")
        self.assertEqual(events[1][1]["kind"], "model_text")
        self.assertIsInstance(events[1][1]["seq"], int)
        self.assertTrue(events[2][1]["ok"])
        self.assertEqual(events[2][1]["exit_code"], 0)

    def test_turn_start_announces_dark_when_capture_off(self) -> None:
        # AGY-2 v2.1 (D1): capture switched off is a KNOWN-dark turn — refine the
        # bridge's default "unproven" blind reason at turn start.
        process = FakeProcess(stdout="done")
        engine, _ = self._make_engine(process)
        events: list[tuple[str, dict]] = []

        with mock.patch.dict("os.environ", {"ARB_TRANSCRIPT_CAPTURE": "off"}):
            engine.run_turn_with_progress("hi", timeout=60, policy="trusted", on_event=lambda n, d: events.append((n, d)))

        channel = [d for n, d in events if n == "progress_channel"]
        self.assertEqual(channel, [{"state": "dark", "reason": "capture-off"}])

    def test_turn_start_announces_dark_and_warns_when_root_missing(self) -> None:
        # AGY-2 v2.1 (D2a): a nonexistent conversations root was FULLY silent —
        # announce dark and log a warning naming the path, once per turn.
        process = FakeProcess(stdout="done")
        captured: list[dict] = []
        missing = self.conversations_root / "nope"
        engine = AgyPrintEngine(
            cwd="/tmp/project",
            popen_factory=lambda args, **kwargs: process,
            conversations_root=missing,
        )
        events: list[tuple[str, dict]] = []

        with self.assertLogs("agent_redis_bridge.engines.agy_print", level="WARNING") as logs:
            engine.run_turn_with_progress("hi", timeout=60, policy="trusted", on_event=lambda n, d: events.append((n, d)))

        channel = [d for n, d in events if n == "progress_channel"]
        self.assertEqual(channel, [{"state": "dark", "reason": "root-missing"}])
        self.assertTrue(any(str(missing) in line for line in logs.output))

    def test_turn_start_emits_no_channel_event_when_capture_on_and_root_exists(self) -> None:
        # The bridge's blind-by-default covers "unproven"; the engine only speaks
        # up when it KNOWS the channel is dark.
        process = FakeProcess(stdout="done")
        engine, _ = self._make_engine(process)
        events: list[tuple[str, dict]] = []

        engine.run_turn_with_progress("hi", timeout=60, policy="trusted", on_event=lambda n, d: events.append((n, d)))

        self.assertEqual([d for n, d in events if n == "progress_channel"], [])

    def test_non_zero_exit_marks_failure_and_returns_stderr(self) -> None:
        process = FakeProcess(stdout="", stderr="boom", returncode=1)
        engine, _ = self._make_engine(process)

        result = engine.run_turn_with_progress("hi", timeout=60, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "boom")

    def test_empty_stdout_with_exit_zero_fails_loudly(self) -> None:
        # agy exits 0 with no stdout when unauthenticated in non-TTY --print
        # mode; the adapter must not report that as a successful turn.
        process = FakeProcess(stdout="   ", pid=99)
        engine, _ = self._make_engine(process)
        events: list[tuple[str, dict]] = []

        result = engine.run_turn_with_progress(
            "hi", timeout=60, policy="trusted", on_event=lambda n, d: events.append((n, d))
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.result, "agy --print pid 99 produced no output.")
        self.assertIn("unauthenticated", result.error or "")
        self.assertFalse(events[-1][1]["ok"])

    def test_empty_stdout_failure_keeps_stderr_in_error(self) -> None:
        process = FakeProcess(stdout="", stderr="quota exceeded", pid=99)
        engine, _ = self._make_engine(process)

        result = engine.run_turn_with_progress("hi", timeout=60, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertIn("quota exceeded", result.error or "")

    def test_timeout_kills_process_and_returns_timeout_error(self) -> None:
        class TimeoutProcess(FakeProcess):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def communicate(self, timeout: int | None = None) -> tuple[str, str]:
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(cmd="agy", timeout=timeout or 0)
                return "partial", ""

        process = TimeoutProcess()
        engine, _ = self._make_engine(process)

        result = engine.run_turn_with_progress("hi", timeout=60, policy="trusted", on_event=None)

        self.assertFalse(result.ok)
        self.assertTrue(process.killed)
        self.assertIn("timeout", (result.error or "").lower())

    def test_steer_raises(self) -> None:
        engine = AgyPrintEngine(cwd="/tmp/project")
        with self.assertRaises(EngineError):
            engine.steer("nope")

    def test_interrupt_without_active_turn_raises(self) -> None:
        engine = AgyPrintEngine(cwd="/tmp/project")
        with self.assertRaises(EngineError):
            engine.interrupt()

    def test_interrupt_terminates_active_process(self) -> None:
        process = FakeProcess(pid=77)
        engine = AgyPrintEngine(cwd="/tmp/project")
        engine.active_process = process

        pid = engine.interrupt()

        self.assertEqual(pid, "77")
        self.assertTrue(process.terminated)

    def test_stop_when_idle_is_noop(self) -> None:
        engine = AgyPrintEngine(cwd="/tmp/project")
        engine.stop()


class AgyPrintSqliteTranscriptTest(unittest.TestCase):
    def test_varint_and_field_decoder_handles_valid_and_truncated_blobs(self) -> None:
        self.assertEqual(read_varint(bytes([0xAC, 0x02]), 0), (300, 2))
        self.assertEqual(read_varint(bytes([0x80]), 0), (None, 1))
        blob = bytes([0x0A, 0x03]) + b"abc"
        self.assertEqual(get_field(blob, 1), b"abc")
        self.assertIsNone(get_field(bytes([0x0A, 0x05]) + b"ab", 1))

    def test_fixture_db_maps_model_text_and_tool_events(self) -> None:
        tracker = AgySqliteTranscript(
            conversations_root=FIXTURE_DB.parent,
            before=set(),
            nonce="fixture",
            turn_id="turn-fixture",
        )
        tracker.bound_db = FIXTURE_DB

        events = tracker.poll()

        names = [name for name, _ in events]
        self.assertIn("model_text", names)
        self.assertIn("command_started", names)
        self.assertIn("command_output", names)
        self.assertIn("command_finished", names)
        model = next(data for name, data in events if name == "model_text")
        self.assertEqual(model["seq"], 2)
        self.assertEqual(model["kind"], "model_text")
        self.assertIn("LIVEPROBE2", model["delta"])
        outputs_by_tool: dict[str, list[str]] = {}
        for name, data in events:
            if name == "command_output":
                outputs_by_tool.setdefault(data["tool_name"], []).append(data["content"])
        self.assertTrue(any("LIVEPROBE2" in out for out in outputs_by_tool["run_command"]))
        self.assertTrue(any("# Agent Redis Bridge" in out for out in outputs_by_tool["view_file"]))
        self.assertTrue(any("run.py" in out for out in outputs_by_tool["list_dir"]))
        self.assertTrue(any("def _seq_key" in out for out in outputs_by_tool["grep_search"]))
        started = [data for name, data in events if name == "command_started"]
        self.assertTrue(any('"CommandLine":"echo LIVEPROBE2"' in data["content"] for data in started))
        seqs = [data["seq"] for _, data in events]
        self.assertEqual(seqs, sorted(seqs))

    def test_unknown_tool_uses_largest_non_call_result_fallback(self) -> None:
        tracker = AgySqliteTranscript(
            conversations_root=FIXTURE_DB.parent,
            before=set(),
            nonce="fixture",
            turn_id="turn-fixture",
        )
        with sqlite3.connect(f"file:{FIXTURE_DB}?mode=ro", uri=True) as conn:
            payload = conn.execute(
                "SELECT step_payload FROM steps WHERE step_type = 21 LIMIT 1"
            ).fetchone()[0]
        from agent_redis_bridge.engines import agy_print as module

        old_map = dict(module.TOOL_RESULT_FIELDS)
        try:
            module.TOOL_RESULT_FIELDS.clear()
            parsed = tracker._tool_call(payload)
        finally:
            module.TOOL_RESULT_FIELDS.clear()
            module.TOOL_RESULT_FIELDS.update(old_map)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[0], "run_command")
        self.assertIn("LIVEPROBE2", parsed[2])

    def test_unknown_tool_fallback_omits_non_text_binary_output(self) -> None:
        from agent_redis_bridge.engines import agy_print as module

        inner = _field(2, b"mystery_tool") + _field(3, b'{"x":1}')
        payload = _field(5, _field(4, inner)) + _field(99, b"\xff\xfe\xfd\xfc\xfb")
        tracker = AgySqliteTranscript(
            conversations_root=FIXTURE_DB.parent,
            before=set(),
            nonce="fixture",
            turn_id="turn-binary",
        )

        events = tracker._map_step(module._AgyStep(7, 101, 3, payload))

        self.assertEqual([name for name, _ in events], ["command_started", "command_finished"])
        self.assertEqual(events[-1][1]["status"], "DONE")

    def test_unknown_tool_fallback_omits_low_printable_decodable_output(self) -> None:
        # Round-1 P2-1 adversarial case: a UTF-8-DECODABLE but low-printable blob
        # (ASCII control bytes). _decode_text yields a non-empty control-char string,
        # so the empty-result guard does not fire; only a printable-ratio gate on the
        # line-143 fallback suppresses it.
        from agent_redis_bridge.engines import agy_print as module

        inner = _field(2, b"mystery_tool") + _field(3, b'{"x":1}')
        payload = _field(5, _field(4, inner)) + _field(99, b"\x01\x02\x03\x04\x05")
        tracker = AgySqliteTranscript(
            conversations_root=FIXTURE_DB.parent,
            before=set(),
            nonce="fixture",
            turn_id="turn-lowprint",
        )

        events = tracker._map_step(module._AgyStep(7, 101, 3, payload))

        self.assertEqual([name for name, _ in events], ["command_started", "command_finished"])

    def test_unknown_tool_fallback_omits_low_printable_field3_direct_result(self) -> None:
        # codex-contributor residual: the line-143 fallback gate is bypassed by the
        # earlier field-3 direct_result branch (>100 bytes) in _best_result_text. For an
        # UNKNOWN tool, _largest_non_call_field can hand a sub-blob whose field 3 is long
        # and low-printable; it must be gated too, while KNOWN-tool mapped output stays verbatim.
        from agent_redis_bridge.engines import agy_print as module

        inner = _field(2, b"mystery_tool") + _field(3, b'{"x":1}')
        result_inner = _field(3, b"\x01" * 101)  # field 3 > 100B, low-printable
        payload = _field(5, _field(4, inner)) + _field(99, result_inner)
        tracker = AgySqliteTranscript(
            conversations_root=FIXTURE_DB.parent,
            before=set(),
            nonce="fixture",
            turn_id="turn-field3",
        )

        events = tracker._map_step(module._AgyStep(9, 101, 3, payload))

        self.assertEqual([name for name, _ in events], ["command_started", "command_finished"])

    def test_model_step_emits_thinking_from_field_20_3(self) -> None:
        # agy's step_type=15 packs answer text in field 20.1 AND reasoning in field 20.3.
        # The poller dropped the thinking entirely — surface it as a model_thinking event.
        from agent_redis_bridge.engines import agy_print as module

        payload = _field(20, _field(1, b"the answer") + _field(3, b"my reasoning"))
        tracker = AgySqliteTranscript(
            conversations_root=FIXTURE_DB.parent, before=set(), nonce="fixture", turn_id="turn-think"
        )

        events = tracker._map_step(module._AgyStep(7, 15, 3, payload))

        names = [name for name, _ in events]
        self.assertIn("model_thinking", names)
        self.assertIn("model_text", names)
        self.assertLess(names.index("model_thinking"), names.index("model_text"))
        thinking = next(data for name, data in events if name == "model_thinking")
        self.assertEqual(thinking["content"], "my reasoning")
        self.assertEqual(thinking["kind"], "model_thinking")
        self.assertEqual(thinking["seq"], 7)
        text = next(data for name, data in events if name == "model_text")
        self.assertEqual(text["content"], "the answer")

    def test_model_step_thinking_only_emits_thinking_no_text(self) -> None:
        # A reasoning-only model step (field 20.1 absent, 20.3 present) — common for agy turns
        # that think then call a tool. Must still surface the thinking, not be dropped.
        from agent_redis_bridge.engines import agy_print as module

        payload = _field(20, _field(3, b"just reasoning"))
        tracker = AgySqliteTranscript(
            conversations_root=FIXTURE_DB.parent, before=set(), nonce="fixture", turn_id="turn-think2"
        )

        events = tracker._map_step(module._AgyStep(2, 15, 3, payload))

        self.assertEqual([name for name, _ in events], ["model_thinking"])
        self.assertEqual(events[0][1]["content"], "just reasoning")

    def test_transient_sqlite_read_error_retries_without_disabling(self) -> None:
        class FlakyTranscript(AgySqliteTranscript):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.calls = 0

            def _read_steps(self, db_path: Path, *, after: int):
                self.calls += 1
                if self.calls == 1:
                    raise sqlite3.OperationalError("checkpoint race")
                return super()._read_steps(db_path, after=after)

        tracker = FlakyTranscript(
            conversations_root=FIXTURE_DB.parent,
            before=set(),
            nonce="fixture",
            turn_id="turn-flaky",
        )
        tracker.bound_db = FIXTURE_DB

        with self.assertLogs("agent_redis_bridge.engines.agy_print", level="WARNING"):
            self.assertEqual(tracker.poll(), [])
        self.assertFalse(tracker.disabled)
        self.assertTrue(tracker.poll())

    def test_sqlite_connect_uses_wal_aware_read_only_uri_without_immutable(self) -> None:
        from agent_redis_bridge.engines import agy_print as module

        seen: list[tuple[str, bool]] = []
        real_connect = module.sqlite3.connect

        def spy(database, *args, **kwargs):
            seen.append((database, kwargs.get("uri")))
            return real_connect(database, *args, **kwargs)

        try:
            module.sqlite3.connect = spy
            with AgySqliteTranscript._connect(FIXTURE_DB):
                pass
        finally:
            module.sqlite3.connect = real_connect

        self.assertEqual(seen[0][1], True)
        self.assertIn("mode=ro", seen[0][0])
        self.assertNotIn("immutable=1", seen[0][0])

    def test_command_finished_reflects_step_status(self) -> None:
        from agent_redis_bridge.engines import agy_print as module

        inner = _field(2, b"mystery_tool") + _field(3, b'{"x":1}')
        payload = _field(5, _field(4, inner)) + _field(42, b"failure text")
        tracker = AgySqliteTranscript(
            conversations_root=FIXTURE_DB.parent,
            before=set(),
            nonce="fixture",
            turn_id="turn-failed",
        )

        events = tracker._map_step(module._AgyStep(8, 101, 9, payload))

        finished = next(data for name, data in events if name == "command_finished")
        self.assertEqual(finished["status"], "FAILED")
        self.assertEqual(finished["exit_code"], 1)

    def test_nonce_correlation_binds_only_matching_new_db(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            before_db = root / "before.db"
            other_db = root / "other.db"
            owned_db = root / "owned.db"
            self._create_nonce_db(before_db, "before prompt")
            self._create_nonce_db(other_db, "other prompt")
            self._create_nonce_db(owned_db, "hello AGY_PRINT_NONCE_owned")
            tracker = AgySqliteTranscript(
                conversations_root=root,
                before={before_db},
                nonce="AGY_PRINT_NONCE_owned",
                turn_id="turn-owned",
            )

            self.assertEqual(tracker._find_owned_db(), owned_db)

    def test_ambiguous_nonce_match_disables_without_binding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "first.db"
            second = root / "second.db"
            self._create_nonce_db(first, "AGY_PRINT_NONCE_dup")
            self._create_nonce_db(second, "AGY_PRINT_NONCE_dup")
            tracker = AgySqliteTranscript(
                conversations_root=root,
                before=set(),
                nonce="AGY_PRINT_NONCE_dup",
                turn_id="turn-dup",
            )

            with self.assertLogs("agent_redis_bridge.engines.agy_print", level="WARNING"):
                self.assertEqual(tracker.poll(), [("progress_channel", {"state": "dark", "reason": "tracker-disabled"})])
            self.assertTrue(tracker.disabled)
            self.assertEqual(tracker.poll(), [])  # announced once
            self.assertIsNone(tracker.bound_db)

    def test_schema_drift_missing_steps_logs_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "bad.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE not_steps (idx integer)")
            tracker = AgySqliteTranscript(
                conversations_root=db.parent,
                before=set(),
                nonce="unused",
                turn_id="turn-bad",
            )
            tracker.bound_db = db

            with self.assertLogs("agent_redis_bridge.engines.agy_print", level="WARNING"):
                self.assertEqual(tracker.poll(), [("progress_channel", {"state": "dark", "reason": "tracker-disabled"})])
            self.assertTrue(tracker.disabled)
            self.assertEqual(tracker.poll(), [])  # announced once

    def test_schema_drift_missing_column_disables_not_retries(self) -> None:
        # NEW-1: an older agy schema whose steps table lacks the 'status' column
        # raises OperationalError("no such column: status"). That is permanent drift,
        # not a transient read race, so it must DISABLE (fail loud) rather than
        # retry-forever at the poll interval.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "drift.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE steps (idx integer, step_type integer, step_payload blob)")
                conn.execute("INSERT INTO steps VALUES (0, 14, ?)", (_field(19, b"x"),))
            tracker = AgySqliteTranscript(
                conversations_root=db.parent,
                before=set(),
                nonce="unused",
                turn_id="turn-drift",
            )
            tracker.bound_db = db

            with self.assertLogs("agent_redis_bridge.engines.agy_print", level="WARNING"):
                self.assertEqual(tracker.poll(), [("progress_channel", {"state": "dark", "reason": "tracker-disabled"})])
            self.assertTrue(tracker.disabled)
            self.assertEqual(tracker.poll(), [])  # announced once

    def test_schema_drift_during_read_disables_not_retries(self) -> None:
        # NEW-1, poll-level handler: schema drift surfacing as a SQL error during the
        # row read (not the schema self-check) must also disable rather than retry.
        class DriftTranscript(AgySqliteTranscript):
            def _schema_ok(self, db_path: Path) -> bool | None:
                return True

            def _read_steps(self, db_path: Path, *, after: int):
                raise sqlite3.OperationalError("no such column: status")

        tracker = DriftTranscript(
            conversations_root=FIXTURE_DB.parent,
            before=set(),
            nonce="fixture",
            turn_id="turn-drift-read",
        )
        tracker.bound_db = FIXTURE_DB

        with self.assertLogs("agent_redis_bridge.engines.agy_print", level="WARNING"):
            self.assertEqual(tracker.poll(), [("progress_channel", {"state": "dark", "reason": "tracker-disabled"})])
        self.assertTrue(tracker.disabled)
        self.assertEqual(tracker.poll(), [])  # announced once

    def test_schema_self_check_disables_on_tool_decode_failure(self) -> None:
        # R2: the _schema_ok TOOL-step guard (tool_call decode None -> disable) had no
        # red-flipping test. A TOOL step (step_type in TOOL_STEPS) whose payload carries no
        # decodable tool_call must disable granular capture (fail loud), not pass silently.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "bad-tool.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE steps (idx integer, step_type integer, status integer, step_payload blob)")
                # step_type 101 is in TOOL_STEPS; payload has no field-5 tool_call -> _tool_call None
                conn.execute("INSERT INTO steps VALUES (0, 101, 3, ?)", (b"\x01\x02 not a tool call",))
            tracker = AgySqliteTranscript(
                conversations_root=db.parent, before=set(), nonce="unused", turn_id="turn-badtool",
            )
            tracker.bound_db = db

            with self.assertLogs("agent_redis_bridge.engines.agy_print", level="WARNING"):
                self.assertEqual(tracker.poll(), [("progress_channel", {"state": "dark", "reason": "tracker-disabled"})])
            self.assertTrue(tracker.disabled)
            self.assertEqual(tracker.poll(), [])  # announced once

    def test_schema_self_check_disables_on_user_input_decode_failure(self) -> None:
        # R2: the _schema_ok USER_INPUT guard (missing field 19 -> disable) had no
        # red-flipping test. A USER_INPUT step lacking USER_INPUT_FIELD must disable.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "bad-user.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE steps (idx integer, step_type integer, status integer, step_payload blob)")
                # step_type 14 is USER_INPUT_STEP; payload has field 1 but NOT field 19
                conn.execute("INSERT INTO steps VALUES (0, 14, 3, ?)", (_field(1, b"missing field 19"),))
            tracker = AgySqliteTranscript(
                conversations_root=db.parent, before=set(), nonce="unused", turn_id="turn-baduser",
            )
            tracker.bound_db = db

            with self.assertLogs("agent_redis_bridge.engines.agy_print", level="WARNING"):
                self.assertEqual(tracker.poll(), [("progress_channel", {"state": "dark", "reason": "tracker-disabled"})])
            self.assertTrue(tracker.disabled)
            self.assertEqual(tracker.poll(), [])  # announced once

    def test_engine_with_no_owned_db_keeps_stdout_only_model_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            process = FakeProcess(stdout="stdout result")
            events: list[tuple[str, dict]] = []
            engine = AgyPrintEngine(
                cwd="/tmp/project",
                conversations_root=Path(td),
                popen_factory=lambda *args, **kwargs: process,
                poll_s=0.01,
            )

            result = engine.run_turn_with_progress(
                "hi",
                timeout=60,
                policy="trusted",
                on_event=lambda name, data: events.append((name, data)),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.result, "stdout result")
        self.assertEqual([name for name, _ in events], ["turn_started", "model_text", "turn_completed"])

    def test_engine_schema_self_check_failure_falls_back_to_stdout_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            process = FakeProcess(stdout="stdout result")
            events: list[tuple[str, dict]] = []

            def factory(args, **kwargs):
                nonce = _extract_nonce(args[2])
                db = root / "bad-owned.db"
                with sqlite3.connect(db) as conn:
                    conn.execute("CREATE TABLE steps (idx integer, step_type integer, status integer, step_payload blob)")
                    conn.execute("INSERT INTO steps VALUES (0, 14, 3, ?)", (_field(19, nonce.encode("utf-8")),))
                    conn.execute("INSERT INTO steps VALUES (1, 15, 3, ?)", (_field(1, b"missing field 20"),))
                return process

            engine = AgyPrintEngine(
                cwd="/tmp/project",
                conversations_root=root,
                popen_factory=factory,
                poll_s=0.01,
            )

            with self.assertLogs("agent_redis_bridge.engines.agy_print", level="WARNING"):
                result = engine.run_turn_with_progress(
                    "hi",
                    timeout=60,
                    policy="trusted",
                    on_event=lambda name, data: events.append((name, data)),
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.result, "stdout result")
        names = [name for name, _ in events]
        # the mid-turn tracker disable now announces darkness once (AGY-2 v2.1)
        self.assertEqual(names.count("progress_channel"), 1)
        self.assertEqual([n for n in names if n != "progress_channel"], ["turn_started", "model_text", "turn_completed"])
        text_events = [d for n, d in events if n == "model_text"]
        self.assertEqual(text_events[0]["item_id"], "4242:text")

    def test_engine_exit_drain_emits_final_sqlite_steps_before_stdout_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            process = FakeProcess(stdout="stdout result")
            events: list[tuple[str, dict]] = []

            def factory(args, **kwargs):
                nonce = _extract_nonce(args[2])
                db = root / "owned.db"
                with sqlite3.connect(db) as conn:
                    conn.execute("CREATE TABLE steps (idx integer, step_type integer, status integer, step_payload blob)")
                    conn.execute("INSERT INTO steps VALUES (0, 14, 3, ?)", (_field(19, nonce.encode("utf-8")),))
                    conn.execute(
                        "INSERT INTO steps VALUES (1, 15, 3, ?)",
                        (_field(20, _field(1, b"sqlite final text")),),
                    )
                return process

            engine = AgyPrintEngine(
                cwd="/tmp/project",
                conversations_root=root,
                popen_factory=factory,
                poll_s=60,
            )

            result = engine.run_turn_with_progress(
                "hi",
                timeout=60,
                policy="trusted",
                on_event=lambda name, data: events.append((name, data)),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.result, "stdout result")
        model_events = [data for name, data in events if name == "model_text"]
        self.assertEqual(len(model_events), 1)
        self.assertEqual(model_events[0]["delta"], "sqlite final text")
        self.assertEqual(model_events[0]["item_id"], "4242:planner:1")

    def test_thinking_only_step_emits_thinking_and_keeps_stdout_fallback(self) -> None:
        # Convergent panel P2 (codex + cold-Opus): pin, through the full engine path, that a
        # thinking-only model step (field 20.3, no 20.1) emits model_thinking WITHOUT setting
        # emitted_model_text — so the end-of-turn stdout fallback still fires. Guards the fallback
        # invariant the new _map_step unit tests don't exercise.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            process = FakeProcess(stdout="stdout result")
            events: list[tuple[str, dict]] = []

            def factory(args, **kwargs):
                nonce = _extract_nonce(args[2])
                db = root / "owned.db"
                with sqlite3.connect(db) as conn:
                    conn.execute("CREATE TABLE steps (idx integer, step_type integer, status integer, step_payload blob)")
                    conn.execute("INSERT INTO steps VALUES (0, 14, 3, ?)", (_field(19, nonce.encode("utf-8")),))
                    conn.execute(
                        "INSERT INTO steps VALUES (1, 15, 3, ?)",
                        (_field(20, _field(3, b"only reasoning")),),
                    )
                return process

            engine = AgyPrintEngine(
                cwd="/tmp/project",
                conversations_root=root,
                popen_factory=factory,
                poll_s=60,
            )

            result = engine.run_turn_with_progress(
                "hi",
                timeout=60,
                policy="trusted",
                on_event=lambda name, data: events.append((name, data)),
            )

        self.assertTrue(result.ok)
        thinking = [data for name, data in events if name == "model_thinking"]
        self.assertEqual(len(thinking), 1)
        self.assertEqual(thinking[0]["delta"], "only reasoning")
        # emitted_model_text stayed False (thinking doesn't set it) -> stdout fallback still fired
        model_texts = [data for name, data in events if name == "model_text"]
        self.assertEqual(len(model_texts), 1)
        self.assertEqual(model_texts[0]["delta"], "stdout result")

    def test_poll_does_not_leak_sqlite_connections(self) -> None:
        # FD-exhaustion root cause: `with sqlite3.connect() as conn` commits but does NOT close,
        # so every poll (schema check + read) leaked connections until the seat ran out of FDs
        # (breaking DNS/sqlite/subprocess/the Valkey tee). poll() must not grow open FDs.
        import os

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "owned.db"
            with sqlite3.connect(db) as c:
                c.execute("CREATE TABLE steps (idx integer, step_type integer, status integer, step_payload blob)")
                c.execute("INSERT INTO steps VALUES (0, 14, 3, ?)", (_field(19, b"NONCELEAK"),))
                c.execute("INSERT INTO steps VALUES (1, 15, 3, ?)", (_field(20, _field(1, b"hi")),))
            tracker = AgySqliteTranscript(
                conversations_root=root, before=set(), nonce="NONCELEAK", turn_id="t"
            )
            tracker.poll()  # bind + initial read
            base = len(os.listdir("/dev/fd"))
            for _ in range(40):
                tracker.poll()
            leaked = len(os.listdir("/dev/fd")) - base
            self.assertLessEqual(leaked, 2, f"poll() leaked {leaked} fds over 40 polls")

    @staticmethod
    def _create_nonce_db(db: Path, text: str) -> None:
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE steps (idx integer, step_type integer, status integer, step_payload blob)")
            conn.execute("INSERT INTO steps VALUES (0, 14, 3, ?)", (_field(19, text.encode("utf-8")),))


def _extract_nonce(task: str) -> str:
    marker = "AGY_PRINT_NONCE_"
    start = task.index(marker)
    end = task.index("]", start)
    return task[start:end]


def _field(num: int, value: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(value)) + value


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            return bytes(out)


if __name__ == "__main__":
    unittest.main()


class ConversationsRootWiringTest(unittest.TestCase):
    def test_build_engine_passes_agy_conversations_root(self) -> None:
        from types import SimpleNamespace

        from agent_redis_bridge.bridge import build_engine

        args = SimpleNamespace(engine="agy-print", model=None, agy_conversations_root=Path("/custom/agy/convs"))
        engine = build_engine(args, cwd="/tmp/project")
        self.assertEqual(engine.conversations_root, Path("/custom/agy/convs"))

    def test_build_engine_defaults_root_when_unset(self) -> None:
        from types import SimpleNamespace

        from agent_redis_bridge.bridge import build_engine

        args = SimpleNamespace(engine="agy-print", model=None)
        engine = build_engine(args, cwd="/tmp/project")
        self.assertEqual(engine.conversations_root, Path.home() / ".gemini" / "antigravity-cli" / "conversations")

    def test_resolve_agy_conversations_root_prefers_env_file_then_process_env(self) -> None:
        from agent_redis_bridge.bridge import resolve_agy_conversations_root

        self.assertEqual(
            resolve_agy_conversations_root({"BRIDGE_AGY_CONVERSATIONS_ROOT": "/from/envfile"}),
            Path("/from/envfile"),
        )
        with mock.patch.dict("os.environ", {"BRIDGE_AGY_CONVERSATIONS_ROOT": "/from/process"}):
            self.assertEqual(resolve_agy_conversations_root({}), Path("/from/process"))
        self.assertIsNone(resolve_agy_conversations_root({}))
