import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    project_key_for_directory,
)
from claude_agent_sdk.types import (
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolPermissionContext,
)

from agent_redis_bridge.engines.agent_sdk import AgentSdkEngine
from agent_redis_bridge.engines.agent_sdk_session import _key_path
from agent_redis_bridge.engines.base import EngineError


class FakeClient:
    def __init__(
        self,
        messages=(),
        delay: float = 0,
        hang_after: bool = False,
        stop_fails: bool = False,
        gaps=None,
        raise_after: Exception | None = None,
    ) -> None:
        self.raise_after = raise_after
        # gaps: {index: seconds} — REAL awaits inside the generator before yielding messages[index],
        # so a cancelled __anext__ would finish the generator like the real SDK client's would.
        self.gaps = dict(gaps or {})
        self.messages = list(messages)
        self.delay = delay
        self.hang_after = hang_after
        self.stop_fails = stop_fails
        self.connected = False
        self.disconnected = False
        self.interrupted = False
        self.queries = []
        self.stopped_tasks = []

    async def connect(self):
        self.connected = True

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        for message in self.messages:
            yield message

    async def receive_messages(self):
        # Mirrors the real client: an open-ended stream that does NOT stop at ResultMessage.
        if self.delay:
            await asyncio.sleep(self.delay)
        for index, message in enumerate(self.messages):
            if index in self.gaps:
                await asyncio.sleep(self.gaps[index])
            yield message
        if self.raise_after is not None:
            raise self.raise_after
        if self.hang_after:
            await asyncio.Event().wait()

    async def stop_task(self, task_id):
        self.stopped_tasks.append(task_id)
        if self.stop_fails:
            raise RuntimeError("stop rejected")

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.disconnected = True



class ResponseOnlyFakeClient(FakeClient):
    # Minimal client shape: receive_response() only, no receive_messages().
    receive_messages = None


def _result(session_id="sid1", subtype="success", text="done", *, is_error=False):
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        result=text,
        stop_reason=subtype,
    )


def _tooluse(name, tool_id):
    return AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name, input={})], model="m")


def _text(value):
    return AssistantMessage(content=[TextBlock(text=value)], model="m")


def _task_started(task_id="t1"):
    return TaskStartedMessage(
        subtype="task_started", data={}, task_id=task_id, description="sleep", uuid="u", session_id="sid1"
    )


def _task_notification(task_id="t1", status="completed"):
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file="/tmp/out.txt",
        summary="done",
        uuid="u",
        session_id="sid1",
    )


def _task_updated(task_id="t1", status="completed"):
    return TaskUpdatedMessage(
        subtype="task_updated", data={}, task_id=task_id, patch={"status": status}, status=status
    )


class EngineTest(unittest.TestCase):
    def _engine(self, messages=(), **kwargs):
        self.temp = tempfile.TemporaryDirectory()
        delay = kwargs.pop("delay", 0)
        client_cls = kwargs.pop("client_cls", FakeClient)
        client = client_cls(
            messages,
            delay=delay,
            hang_after=kwargs.pop("hang_after", False),
            stop_fails=kwargs.pop("stop_fails", False),
            gaps=kwargs.pop("gaps", None),
            raise_after=kwargs.pop("raise_after", None),
        )
        keepalive = kwargs.pop("hold_keepalive", None)

        def factory(**factory_kwargs):
            self.captured_options = factory_kwargs["options"]
            return client

        engine = AgentSdkEngine(
            cwd=".",
            model="minimax-m3",
            tool_ceiling=kwargs.pop("tool_ceiling", "Read,Write,Bash"),
            key="K",
            session_root=Path(self.temp.name),
            startup_probe=kwargs.pop("startup_probe", False),
            client_factory=factory,
            **kwargs,
        )
        if keepalive is not None:
            engine._hold_keepalive_secs = keepalive
        engine.start()
        self.client = client
        return engine

    def tearDown(self):
        temp = getattr(self, "temp", None)
        if temp is not None:
            temp.cleanup()

    def test_engine_builds_failclosed_options(self):
        engine = self._engine(messages=[_result()])
        try:
            opts = self.captured_options
            self.assertEqual(opts.allowed_tools, [])
            self.assertEqual(opts.setting_sources, [])
            self.assertEqual(opts.permission_mode, "default")
            self.assertTrue(callable(opts.can_use_tool))
            self.assertIsNotNone(opts.session_store)
        finally:
            engine.stop()

    def test_engine_builds_created_claude_config_dir_under_session_root(self):
        engine = self._engine(messages=[_result()], agent_id="agent-one")
        try:
            opts = self.captured_options
            config_dir = Path(opts.env["CLAUDE_CONFIG_DIR"])
            self.assertTrue(config_dir.is_dir())
            self.assertEqual(config_dir.parent, Path(self.temp.name) / "agent-one" / "claude-config")
            self.assertEqual(opts.env["ANTHROPIC_BASE_URL"], "https://api.minimax.io/anthropic")
            self.assertEqual(opts.env["ANTHROPIC_API_KEY"], "K")
        finally:
            engine.stop()

    def test_distinct_agent_ids_get_distinct_claude_config_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            captured = []

            def factory(**factory_kwargs):
                captured.append(factory_kwargs["options"].env["CLAUDE_CONFIG_DIR"])
                return FakeClient([_result()])

            first = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=root,
                agent_id="agent-one",
                startup_probe=False,
                client_factory=factory,
            )
            second = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=root,
                agent_id="agent-two",
                startup_probe=False,
                client_factory=factory,
            )
            first.start()
            second.start()
            try:
                self.assertEqual(Path(captured[0]).parent, root / "agent-one" / "claude-config")
                self.assertEqual(Path(captured[1]).parent, root / "agent-two" / "claude-config")
                self.assertNotEqual(captured[0], captured[1])
                self.assertTrue(Path(captured[0]).is_dir())
                self.assertTrue(Path(captured[1]).is_dir())
            finally:
                first.stop()
                second.stop()

    def test_same_agent_id_engines_get_distinct_claude_config_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            captured = []

            def factory(**factory_kwargs):
                captured.append(factory_kwargs["options"].env["CLAUDE_CONFIG_DIR"])
                return FakeClient([_result()])

            engines = [
                AgentSdkEngine(
                    cwd=".",
                    model="minimax-m3",
                    tool_ceiling="Read",
                    key="K",
                    session_root=root,
                    agent_id="agent-one",
                    startup_probe=False,
                    client_factory=factory,
                )
                for _ in range(2)
            ]
            for engine in engines:
                engine.start()
            try:
                self.assertEqual(Path(captured[0]).parent, root / "agent-one" / "claude-config")
                self.assertEqual(Path(captured[1]).parent, root / "agent-one" / "claude-config")
                self.assertNotEqual(captured[0], captured[1])
            finally:
                for engine in engines:
                    engine.stop()

    def test_stream_without_result_message_is_failure_and_unhealthy(self):
        engine = self._engine(messages=[])
        try:
            result = engine.run_turn_with_progress("do x", timeout=30, policy="trusted", on_event=None)
            self.assertFalse(result.ok)
            self.assertIn("without result", (result.error or "").lower())
            self.assertFalse(engine.healthy)
        finally:
            engine.stop()

    def test_result_message_maps_to_turnresult(self):
        engine = self._engine(messages=[_text("hello"), _result(session_id="sid1", subtype="success", text="done")])
        try:
            result = engine.run_turn_with_progress("do x", timeout=30, policy="trusted", on_event=None)
            self.assertTrue(result.ok)
            self.assertEqual(result.result, "done")
            self.assertEqual(result.thread_id, "sid1")
            self.assertEqual(engine.session_id, "sid1")
            self.assertEqual(result.stop_reason, "success")
        finally:
            engine.stop()

    def test_completed_session_id_persists_without_autoresume_for_next_engine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_client = FakeClient([_result(session_id="sid-persist", text="done")])
            first = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=root,
                agent_id="agent-sdk-test",
                startup_probe=False,
                client_factory=lambda **kw: first_client,
            )
            first.start()
            try:
                result = first.run_turn_with_progress("do x", timeout=30, policy="trusted", on_event=None)
                self.assertEqual(result.thread_id, "sid-persist")
                self.assertEqual(first.session_id, "sid-persist")
            finally:
                first.stop()

            captured_resumes = []
            second_client = FakeClient([_result()])

            def factory(**factory_kwargs):
                captured_resumes.append(factory_kwargs["options"].resume)
                return second_client

            second = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=root,
                agent_id="agent-sdk-test",
                startup_probe=False,
                client_factory=factory,
            )
            self.assertEqual(second.session_id, "sid-persist")
            second.start()
            try:
                self.assertEqual(captured_resumes, [None])
            finally:
                second.stop()

    def test_vendor_oneshot_engine_does_not_autoresume_persisted_session_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_file = root / "agent-sdk-test" / "last-session-id"
            session_file.parent.mkdir(parents=True)
            session_file.write_text("sid-persist\n", encoding="utf-8")
            captured_resumes = []

            def factory(**factory_kwargs):
                captured_resumes.append(factory_kwargs["options"].resume)
                return FakeClient([_result()])

            engine = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=root,
                agent_id="agent-sdk-test",
                oneshot=True,
                startup_probe=False,
                client_factory=factory,
            )
            self.assertEqual(engine.session_id, "sid-persist")
            engine.start()
            try:
                self.assertEqual(captured_resumes, [None])
            finally:
                engine.stop()

    def test_no_persisted_session_id_starts_without_resume(self):
        engine = self._engine(messages=[_result()])
        try:
            self.assertIsNone(engine.session_id)
            self.assertIsNone(self.captured_options.resume)
        finally:
            engine.stop()

    def test_resume_thread_reconnects_with_resume(self):
        clients = []
        captured_resumes = []

        def factory(**factory_kwargs):
            captured_resumes.append(factory_kwargs["options"].resume)
            client = FakeClient([_result()])
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as temp_dir:
            session_id = "d16bbaf8-d177-4d76-ae5d-a7daab31d7b0"
            engine = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=Path(temp_dir),
                startup_probe=False,
                client_factory=factory,
            )
            session_path = _key_path(
                engine.session_root,
                engine.agent_id,
                {
                    "project_key": project_key_for_directory(engine.cwd),
                    "session_id": session_id,
                },
            )
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text('{"type":"user","message":"stored"}\n', encoding="utf-8")
            engine.start()
            try:
                self.assertEqual(engine.resume_thread(session_id), session_id)
                self.assertEqual(engine.session_id, session_id)
                self.assertEqual(captured_resumes, [None, session_id])
                self.assertTrue(clients[0].disconnected)
                self.assertTrue(clients[1].connected)
            finally:
                engine.stop()

    def test_nontrusted_mutation_turn_refused(self):
        engine = self._engine(messages=[_result()], tool_ceiling="Read,Write,Bash")
        try:
            result = engine.run_turn_with_progress("edit files", timeout=30, policy="human", on_event=None)
            self.assertFalse(result.ok)
            self.assertIn("non-trusted", result.error or "")
        finally:
            engine.stop()

    def test_tooluse_events_deduped_by_id(self):
        seen = []
        engine = self._engine(messages=[_tooluse("Read", "t1"), _tooluse("Read", "t1"), _result()])
        try:
            engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=lambda k, d: seen.append((k, d)))
            starts = [data for kind, data in seen if kind == "command_started"]
            self.assertEqual(len(starts), 1)
        finally:
            engine.stop()

    def test_command_started_kind_and_arg_label_render_in_transcript(self):
        # arb-watch keys off payload kind=="command_started" and shows tool_name/command; the seat
        # must emit that kind (not the bare tool name) AND fold the args in so the bullet isn't bare.
        seen = []
        block = AssistantMessage(content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "src/foo.py"})], model="m")
        engine = self._engine(messages=[block, _result()])
        try:
            engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=lambda k, d: seen.append((k, d)))
            starts = [data for kind, data in seen if kind == "command_started"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["kind"], "command_started")
            self.assertEqual(starts[0]["command"], "Read(src/foo.py)")
            # The label must NOT be in tool_name: eval allowlists tool_name (excludes raw args),
            # so a labelled tool_name would leak Bash args into the eval tee.
            self.assertNotIn("tool_name", starts[0])
        finally:
            engine.stop()

    def test_tool_result_output_flows_to_transcript_content(self):
        # Tool results arrive in a UserMessage (not AssistantMessage) — the engine must handle that
        # message type and emit command_output with the result content so arb-watch renders the ⎿
        # line. content is not eval-allowlisted → transcript-only.
        seen = []
        msg = UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="line A\nline B", is_error=False)])
        engine = self._engine(messages=[msg, _result()])
        try:
            engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=lambda k, d: seen.append((k, d)))
            outs = [data for kind, data in seen if kind == "command_output" and data.get("content")]
            self.assertEqual(len(outs), 1)
            self.assertEqual(outs[0]["content"], "line A\nline B")
            # Distinct :output item_id so the gateway doesn't merge it into the command_started frame.
            self.assertEqual(outs[0]["item_id"], "t1:output")
            self.assertNotIn("tool_name", outs[0])  # transcript-only, never the eval-allowlisted field
        finally:
            engine.stop()

    def test_timeout_disconnects_and_marks_unhealthy(self):
        engine = self._engine(messages=[_result()], delay=1)
        try:
            started = time.monotonic()
            result = engine.run_turn_with_progress("x", timeout=0.05, policy="trusted", on_event=None)
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertFalse(result.ok)
            self.assertFalse(engine.healthy)
            self.assertTrue(self.client.disconnected)
        finally:
            engine.stop()

    def test_startup_gate_passes_read_only_without_live_model_query(self):
        class ProbeClient(FakeClient):
            def __init__(self, options):
                super().__init__([_result()])
                self.options = options
                self._query = SimpleNamespace(can_use_tool=options.can_use_tool)

            async def query(self, prompt):
                raise AssertionError("deterministic startup gate must not run live model probe")

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=Path(temp_dir),
                client_factory=lambda **kw: ProbeClient(kw["options"]),
            )
            engine.start()
            engine.stop()

    def test_startup_gate_passes_mutation_ceiling_without_live_model_query(self):
        class ProbeClient(FakeClient):
            def __init__(self, options):
                super().__init__([_result()])
                self.options = options
                self._query = SimpleNamespace(can_use_tool=options.can_use_tool)

            async def query(self, prompt):
                raise AssertionError("deterministic startup gate must not run live model probe")

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Write,Edit",
                key="K",
                session_root=Path(temp_dir),
                client_factory=lambda **kw: ProbeClient(kw["options"]),
            )
            engine.start()
            engine.stop()

    def test_startup_gate_rejects_connected_client_without_callback_wiring(self):
        class ProbeClient(FakeClient):
            def __init__(self, options):
                super().__init__([_result()])
                self.options = options
                self._query = SimpleNamespace(can_use_tool=lambda *_args: None)

            async def query(self, prompt):
                await self.options.can_use_tool("Read", {}, ToolPermissionContext(tool_use_id="r1"))

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=Path(temp_dir),
                client_factory=lambda **kw: ProbeClient(kw["options"]),
            )
            with self.assertRaisesRegex(EngineError, "callback not wired"):
                engine.start()
            self.assertFalse(engine.healthy)
            engine.stop()

    def test_startup_probe_helper_requires_result_message(self):
        class ProbeClient(FakeClient):
            def __init__(self, options):
                super().__init__([])
                self.options = options

            async def query(self, prompt):
                await self.options.can_use_tool("Read", {}, type("Ctx", (), {"tool_use_id": "r1"})())

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=Path(temp_dir),
                startup_probe=False,
                client_factory=lambda **kw: ProbeClient(kw["options"]),
            )
            engine.start()
            with self.assertRaisesRegex(Exception, "startup probe.*ResultMessage"):
                engine.loop_thread.submit(engine._run_startup_probe()).result(timeout=10)
            self.assertFalse(engine.healthy)
            engine.stop()

    def test_live_startup_smoke_test_is_nonfatal_when_probe_has_no_result(self):
        class ProbeClient(FakeClient):
            def __init__(self, options):
                super().__init__([])
                self.options = options
                self._query = SimpleNamespace(can_use_tool=options.can_use_tool)

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=Path(temp_dir),
                live_smoke_test=True,
                client_factory=lambda **kw: ProbeClient(kw["options"]),
            )
            with self.assertLogs("agent_redis_bridge.engines.agent_sdk", level="INFO") as logs:
                engine.start()
            try:
                self.assertTrue(engine.healthy)
                self.assertTrue(any("did not fire cleanly" in line for line in logs.output))
            finally:
                engine.stop()

    def test_default_startup_does_not_run_live_smoke_test(self):
        class ProbeClient(FakeClient):
            def __init__(self, options):
                super().__init__([_result()])
                self.options = options
                self._query = SimpleNamespace(can_use_tool=options.can_use_tool)

            async def query(self, prompt):
                raise AssertionError("per-dispatch startup must not run live smoke test")

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = AgentSdkEngine(
                cwd=str(Path(temp_dir) / ".claude" / "worktrees" / "task"),
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=Path(temp_dir),
                client_factory=lambda **kw: ProbeClient(kw["options"]),
            )
            engine.start()
            engine.stop()

    def test_startup_gate_records_direct_allow_and_sentinel_deny(self):
        class ProbeClient(FakeClient):
            def __init__(self, options):
                super().__init__([_result()])
                self.options = options
                self._query = SimpleNamespace(can_use_tool=options.can_use_tool)

            async def query(self, prompt):
                raise AssertionError("deterministic startup gate must not run live model probe")

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = AgentSdkEngine(
                cwd=".",
                model="minimax-m3",
                tool_ceiling="Read",
                key="K",
                session_root=Path(temp_dir),
                client_factory=lambda **kw: ProbeClient(kw["options"]),
            )
            engine.start()
            self.assertIn(("Read", True, "allowed"), engine._gate_records)
            self.assertIn(("ARB_STARTUP_DENY_SENTINEL", False, "ARB_STARTUP_DENY_SENTINEL outside ceiling"), engine._gate_records)
            engine.stop()


    def test_gate_emits_tool_permission_decided_not_command_finished(self):
        # The permission gate is a DECISION, not a tool result — it must not masquerade as a finish.
        # The gate fires on the SDK can_use_tool path, so drive _gate() directly (it is async but awaits
        # nothing; asyncio.run completes it on the main thread, independent of the engine loop thread).
        import asyncio
        from types import SimpleNamespace
        seen = []
        engine = self._engine()
        try:
            engine._turn_on_event = lambda k, d: seen.append((k, d))
            engine._turn_policy = "trusted"
            asyncio.run(engine._gate("Read", {}, SimpleNamespace(tool_use_id="t1")))
            kinds = [k for k, _ in seen]
            self.assertIn("tool_permission_decided", kinds)
            self.assertNotIn("command_finished", kinds)   # the gate must NOT emit a finish
        finally:
            engine.stop()

    def test_real_command_finished_comes_from_tool_result_block(self):
        # A ToolResultBlock yields command_output AND a REAL command_finished carrying the tool's exit
        # status (mirrors the existing test_tool_result_output_flows_to_transcript_content stream shape).
        seen = []
        msg = UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="boom", is_error=True)])
        engine = self._engine(messages=[msg, _result()])
        try:
            engine.run_turn_with_progress("x", timeout=30, policy="trusted",
                                          on_event=lambda k, d: seen.append((k, d)))
            finishes = [d for k, d in seen if k == "command_finished"]
            self.assertEqual(len(finishes), 1)                 # exactly one, from the ToolResultBlock
            self.assertEqual(finishes[0]["tool_call_id"], "t1")
            self.assertEqual(finishes[0]["status"], "failed")
            self.assertEqual(finishes[0]["exit_code"], 1)
        finally:
            engine.stop()

    def test_deny_produces_exactly_one_decision_and_no_finish(self):
        # A denied mutating tool under a non-trusted policy → exactly one tool_permission_decided(deny),
        # and NO phantom command_finished. (decide(): Write is MUTATING → denied when policy != trusted.)
        import asyncio
        from types import SimpleNamespace
        seen = []
        engine = self._engine()
        try:
            engine._turn_on_event = lambda k, d: seen.append((k, d))
            engine._turn_policy = "human"
            asyncio.run(engine._gate("Write", {}, SimpleNamespace(tool_use_id="t9")))
            decisions = [d for k, d in seen if k == "tool_permission_decided"]
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["status"], "denied")
            self.assertEqual(decisions[0]["exit_code"], 1)
            self.assertNotIn("command_finished", [k for k, _ in seen])
        finally:
            engine.stop()

    def test_subscription_audit_payload_is_out_of_turn(self):
        # The audit is emitted BEFORE _run_turn (agent_sdk.py:498-501, ahead of turn_started); the bridge
        # lists agent_sdk_subscription_audit in OUT_OF_TURN (Task 10) so the DURABLE eval record never
        # carries a turn_index. Engine-level: the payload self-stamps no turn ordinal. (r2-fold: the
        # load-bearing no-turn_index property on the durable eval record is now proven on a REAL Bridge in
        # Task 10's test_real_bridge_shares_turn_index_and_coalesces_tool_call_id_on_both_edges — not the
        # mirror harness. The subscription EMIT path itself is env/slot-gated, so it is asserted
        # structurally here rather than by standing up a subscription seat.)
        engine = self._engine()
        try:
            payload = engine._subscription_audit_payload()
            self.assertEqual(payload["kind"], "agent_sdk_subscription_audit")
            self.assertNotIn("turn_index", payload)
        finally:
            engine.stop()


class GateTelemetryExceptionSafetyTest(unittest.TestCase):
    """Audit ASK-2 (panel-confirmed): _gate wrapped only decide(); the telemetry
    emission after it was bare, so a Redis blip inside _turn_on_event raised out of
    the SDK permission callback instead of returning an explicit Allow/Deny — the
    one guarantee the fail-closed gate design rests on."""

    def _engine(self):
        self.temp = tempfile.TemporaryDirectory()
        client = FakeClient([_result()])
        engine = AgentSdkEngine(
            cwd=".",
            model="minimax-m3",
            tool_ceiling="Read,Write,Bash",
            key="K",
            session_root=Path(self.temp.name),
            startup_probe=False,
            client_factory=lambda **kw: client,
        )
        engine.start()
        return engine

    def tearDown(self):
        temp = getattr(self, "temp", None)
        if temp is not None:
            temp.cleanup()

    def test_gate_returns_explicit_allow_when_telemetry_raises(self):
        from claude_agent_sdk.types import PermissionResultAllow

        engine = self._engine()
        try:
            engine._turn_policy = "trusted"
            engine._turn_on_event = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("valkey blip"))
            ctx = ToolPermissionContext(tool_use_id="t-allow")

            result = asyncio.run(engine._gate("Read", {}, ctx))

            self.assertIsInstance(result, PermissionResultAllow)
        finally:
            engine.stop()

    def test_gate_returns_explicit_deny_when_telemetry_raises(self):
        from claude_agent_sdk.types import PermissionResultDeny

        engine = self._engine()
        try:
            engine._turn_policy = "trusted"
            engine._turn_on_event = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("valkey blip"))
            ctx = ToolPermissionContext(tool_use_id="t-deny")

            result = asyncio.run(engine._gate("WebSearch", {}, ctx))

            self.assertIsInstance(result, PermissionResultDeny)
        finally:
            engine.stop()


class ControlHonestyTest(unittest.TestCase):
    """Audit ASK-3/ASK-6 (panel-confirmed): non-oneshot steer() returned a plain
    string that the bridge reported as steer_sent; interrupt() fire-and-forgot
    the cancel future so a failed SDK interrupt was reported as cancel_sent."""

    def _engine(self, client=None):
        self.temp = tempfile.TemporaryDirectory()
        client = client or FakeClient([_result()])
        engine = AgentSdkEngine(
            cwd=".",
            model="minimax-m3",
            tool_ceiling="Read,Grep",
            key="K",
            session_root=Path(self.temp.name),
            startup_probe=False,
            client_factory=lambda **kw: client,
        )
        engine.start()
        return engine

    def tearDown(self):
        temp = getattr(self, "temp", None)
        if temp is not None:
            temp.cleanup()

    def test_steer_raises_on_non_oneshot_instead_of_faking_success(self):
        engine = self._engine()
        try:
            self.assertFalse(engine.oneshot)
            with self.assertRaises(EngineError):
                engine.steer("change course")
        finally:
            engine.stop()

    def test_interrupt_failure_raises_instead_of_reporting_cancel_sent(self):
        class InterruptExplodingClient(FakeClient):
            async def interrupt(self):
                raise RuntimeError("sdk interrupt transport died")

        engine = self._engine(client=InterruptExplodingClient([_result()]))
        try:
            engine._active_turn = True
            engine._last_session_id = "sess-1"
            with self.assertRaises(EngineError):
                engine.interrupt()
        finally:
            engine._active_turn = False
            engine.stop()


class BackgroundTaskTest(EngineTest):
    """These tests pin the engine's handling of FAKE message orderings, not the CLI's behaviour.
    The premise (the CLI re-invokes the model on background-task completion inside the same SDK
    session) was observed once on macOS, SDK 0.2.117 — ARB Memory art-d17b2c72afaf7b15 v2."""

    def test_no_background_task_returns_at_first_result(self):
        engine = self._engine(messages=[_result(text="first"), _result(text="late")])
        try:
            result = engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=None)
            self.assertTrue(result.ok)
            self.assertEqual(result.result, "first")
        finally:
            engine.stop()

    def test_pending_task_holds_turn_until_final_result(self):
        events = []
        engine = self._engine(
            messages=[
                _tooluse("Bash", "call1"),
                _task_started("t1"),
                _text("STARTED"),
                _result(text="STARTED"),
                _task_notification("t1", "completed"),
                _tooluse("Read", "call2"),
                _text("digest"),
                _result(text="digest"),
            ]
        )
        try:
            result = engine.run_turn_with_progress(
                "x", timeout=30, policy="trusted", on_event=lambda k, p: events.append((k, p))
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.result, "digest")
            self.assertEqual(result.tool_calls, 2)
            kinds = [k for k, _ in events]
            self.assertEqual(kinds.count("turn_completed"), 1)
            self.assertEqual(kinds.count("turn_continued"), 1)
            continued = next(p for k, p in events if k == "turn_continued")
            self.assertEqual(continued["pending_tasks"], ["t1"])
        finally:
            engine.stop()

    def test_task_updated_terminal_patch_clears_pending_without_notification(self):
        engine = self._engine(
            messages=[
                _task_started("t1"),
                _result(text="STARTED"),
                _task_updated("t1", "killed"),
                _result(text="final"),
                _result(text="too-late"),
            ]
        )
        try:
            result = engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=None)
            self.assertEqual(result.result, "final")
        finally:
            engine.stop()

    def test_timeout_with_pending_tasks_stops_them_and_reports_ids(self):
        events = []
        # Task started, then the stream hangs BEFORE any ResultMessage: no hold has begun, so
        # the caller's hard timeout is the only exit and it must reap the pending task.
        engine = self._engine(messages=[_task_started("t1")], hang_after=True)
        try:
            result = engine.run_turn_with_progress(
                "x", timeout=0.3, policy="trusted", on_event=lambda k, p: events.append((k, p))
            )
            self.assertFalse(result.ok)
            self.assertIn("stop requested for background tasks: t1", result.error or "")
            self.assertEqual(self.client.stopped_tasks, ["t1"])
            self.assertTrue(self.client.disconnected)
            timeout_payload = next(p for k, p in events if k == "turn_timeout")
            self.assertEqual(timeout_payload["pending_tasks"], ["t1"])
        finally:
            engine.stop()

    def test_error_result_with_pending_tasks_reaps_and_reports(self):
        events = []
        engine = self._engine(
            messages=[
                _task_started("t1"),
                _result(text="interim-error", subtype="error_max_turns", is_error=True),
                _task_notification("t1", "completed"),
                _result(text="post-task-final"),
            ]
        )
        try:
            result = engine.run_turn_with_progress(
                "x", timeout=30, policy="trusted", on_event=lambda k, p: events.append((k, p))
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.result, "interim-error")
            self.assertEqual(self.client.stopped_tasks, ["t1"])
            self.assertIn("t1", result.error or "")
            completed = next(p for k, p in events if k == "turn_completed")
            self.assertEqual(completed["stop_requested_tasks"], ["t1"])
        finally:
            engine.stop()

    def test_stream_eof_with_pending_tasks_is_failure_and_reaps(self):
        engine = self._engine(messages=[_task_started("t1"), _result(text="STARTED")])
        try:
            result = engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=None)
            self.assertFalse(result.ok)
            self.assertEqual(result.result, "STARTED")
            self.assertIn("pending", (result.error or "").lower())
            self.assertEqual(self.client.stopped_tasks, ["t1"])
            self.assertFalse(engine.healthy)
        finally:
            engine.stop()

    def test_hold_grace_expiry_returns_interim_result_and_reaps(self):
        events = []
        engine = self._engine(
            messages=[_task_started("t1"), _text("STARTED"), _result(text="STARTED")],
            hang_after=True,
            background_grace=0.2,
        )
        try:
            started = time.monotonic()
            result = engine.run_turn_with_progress(
                "x", timeout=30, policy="trusted", on_event=lambda k, p: events.append((k, p))
            )
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(result.ok)
            self.assertEqual(result.result, "STARTED")
            self.assertEqual(self.client.stopped_tasks, ["t1"])
            # Work is still in flight in the session: the engine must be retired, not reused.
            self.assertFalse(engine.healthy)
            completed = next(p for k, p in events if k == "turn_completed")
            self.assertEqual(completed["abandoned_tasks"], ["t1"])
            self.assertEqual(completed["stop_requested_tasks"], ["t1"])
        finally:
            engine.stop()

    def test_hold_grace_is_capped_below_turn_timeout(self):
        # grace (10s) > timeout (0.5s): the hold must still yield before the hard timeout,
        # so the interim text is returned instead of the total-loss timeout path.
        engine = self._engine(
            messages=[_task_started("t1"), _result(text="STARTED")], hang_after=True, background_grace=10
        )
        try:
            started = time.monotonic()
            result = engine.run_turn_with_progress("x", timeout=0.5, policy="trusted", on_event=None)
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(result.ok)
            self.assertEqual(result.result, "STARTED")
            self.assertEqual(self.client.stopped_tasks, ["t1"])
        finally:
            engine.stop()

    def test_timeout_reports_failed_stops_separately(self):
        events = []
        engine = self._engine(
            messages=[_task_started("t1"), _task_started("t2")],
            hang_after=True,
            stop_fails=True,
        )
        try:
            result = engine.run_turn_with_progress(
                "x", timeout=0.2, policy="trusted", on_event=lambda k, p: events.append((k, p))
            )
            self.assertFalse(result.ok)
            self.assertNotIn("stopped", result.error or "")
            self.assertIn("stop_task failed for: t1, t2", result.error or "")
            payload = next(p for k, p in events if k == "turn_timeout")
            self.assertEqual(payload["stop_failed_tasks"], ["t1", "t2"])
            self.assertEqual(payload["stop_requested_tasks"], [])
        finally:
            engine.stop()

    def test_timeout_before_any_result_returns_empty(self):
        engine = self._engine(messages=[_text("partial answer"), _result(text="partial answer")], delay=0.4)
        try:
            result = engine.run_turn_with_progress("x", timeout=0.1, policy="trusted", on_event=None)
            self.assertFalse(result.ok)
            self.assertEqual(result.result, "")
        finally:
            engine.stop()

    def test_timeout_after_interim_result_returns_interim_text(self):
        # The hold is clamped to turn_budget = max(timeout-60, timeout*0.5) = 0.4s here, so the
        # grace path would fire first; a hanging stop_task keeps the loop thread busy past the
        # hard timeout, which is the only way to reach the caller-thread timeout after an interim.
        engine = self._engine(
            messages=[_task_started("t1"), _result(text="partial answer")],
            hang_after=True,
            background_grace=10,
        )

        async def hanging_stop(task_id):
            self.client.stopped_tasks.append(task_id)
            await asyncio.Event().wait()

        self.client.stop_task = hanging_stop
        try:
            result = engine.run_turn_with_progress("x", timeout=0.8, policy="trusted", on_event=None)
            self.assertFalse(result.ok)
            self.assertEqual(result.result, "partial answer")
            self.assertIn("timed out", result.error or "")
        finally:
            engine.stop()

    def test_hold_ends_when_tasks_complete_even_if_grace_would_expire_later(self):
        # Mirror of the keepalive test with the gap AFTER the notification: the task completes
        # quickly, then the model takes longer than the grace to produce its follow-up. The
        # grace must not guillotine a live turn once nothing is pending (re-panel P1-1).
        engine = self._engine(
            messages=[
                _task_started("t1"),
                _result(text="STARTED"),
                _task_notification("t1", "completed"),
                _text("FINAL"),
                _result(text="FINAL"),
            ],
            gaps={3: 0.4},
            hold_keepalive=0.05,
            background_grace=0.2,
        )
        try:
            result = engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=None)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.result, "FINAL")
            self.assertIsNone(result.error)
            self.assertEqual(self.client.stopped_tasks, [])
            self.assertTrue(engine.healthy)
        finally:
            engine.stop()

    def test_grace_expiry_marks_engine_unhealthy_and_sets_error_on_ok_result(self):
        engine = self._engine(
            messages=[_task_started("t1"), _result(text="STARTED")], hang_after=True, background_grace=0.1
        )
        try:
            result = engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=None)
            self.assertTrue(result.ok)
            self.assertEqual(result.result, "STARTED")
            # SPEC: error is set only when ok=false; truncation travels in stop_reason.
            self.assertIsNone(result.error)
            self.assertEqual(result.stop_reason, "background_hold_expired:t1")
            self.assertFalse(engine.healthy)
        finally:
            engine.stop()

    def test_hold_deadline_is_clamped_by_elapsed_turn_time(self):
        # First interim result arrives late in the turn budget: the hold must expire at
        # turn_start + budget, not interim + grace (re-panel P2-3).
        # timeout=1.0 => hold_grace = budget = 0.5s. The interim result lands at 0.6s, AFTER the
        # absolute budget: new behaviour expires the hold immediately (~0.6s, ok=True);
        # interim-relative behaviour would hold until 1.1s and hit the 1.0s hard timeout
        # (ok=False) — the two implementations diverge on ok, not only on elapsed time.
        engine = self._engine(
            messages=[_task_started("t1"), _result(text="STARTED")],
            gaps={0: 0.6},
            hang_after=True,
            background_grace=10,
        )
        try:
            started = time.monotonic()
            result = engine.run_turn_with_progress("x", timeout=1.0, policy="trusted", on_event=None)
            self.assertLess(time.monotonic() - started, 0.9)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.result, "STARTED")
            self.assertTrue((result.stop_reason or "").startswith("background_hold_expired"))
        finally:
            engine.stop()

    def test_two_tasks_partial_clear_keeps_holding(self):
        engine = self._engine(
            messages=[
                _task_started("t1"),
                _task_started("t2"),
                _result(text="STARTED"),
                _task_notification("t1", "completed"),
                _result(text="ONLY-T1"),
                _task_updated("t2", "completed"),
                _result(text="BOTH"),
            ]
        )
        try:
            result = engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=None)
            self.assertTrue(result.ok)
            self.assertEqual(result.result, "BOTH")
        finally:
            engine.stop()

    def test_receive_response_only_client_still_completes(self):
        engine = self._engine(messages=[_text("hi"), _result(text="done")], client_cls=ResponseOnlyFakeClient)
        try:
            result = engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=None)
            self.assertTrue(result.ok)
            self.assertEqual(result.result, "done")
        finally:
            engine.stop()

    def test_hold_survives_keepalive_wakeups_without_cancelling_the_stream(self):
        # The completion arrives AFTER several keepalive intervals. A read that gets cancelled
        # on each keepalive would finish the generator and misreport "stream ended".
        events = []
        engine = self._engine(
            messages=[
                _task_started("t1"),
                _result(text="STARTED"),
                _task_notification("t1", "completed"),
                _result(text="FINAL"),
            ],
            gaps={2: 0.3},
            hold_keepalive=0.05,
            background_grace=5,
        )
        try:
            result = engine.run_turn_with_progress(
                "x", timeout=30, policy="trusted", on_event=lambda k, p: events.append((k, p))
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.result, "FINAL")
            self.assertEqual(self.client.stopped_tasks, [])
            keepalives = [p for k, p in events if k == "turn_continued" and p.get("keepalive")]
            self.assertGreaterEqual(len(keepalives), 2)
        finally:
            engine.stop()

    def test_grace_expiry_with_failed_stop_is_failure_and_unhealthy(self):
        events = []
        engine = self._engine(
            messages=[_task_started("t1"), _result(text="STARTED")],
            hang_after=True,
            stop_fails=True,
            background_grace=0.1,
        )
        try:
            result = engine.run_turn_with_progress(
                "x", timeout=30, policy="trusted", on_event=lambda k, p: events.append((k, p))
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.result, "STARTED")
            self.assertIn("stop_task failed for: t1", result.error or "")
            self.assertFalse(engine.healthy)
            completed = next(p for k, p in events if k == "turn_completed")
            self.assertEqual(completed["stop_failed_tasks"], ["t1"])
            self.assertFalse(completed["ok"])
        finally:
            engine.stop()

    def test_stream_exception_with_pending_tasks_still_reaps_then_raises(self):
        engine = self._engine(
            messages=[_task_started("t1"), _result(text="STARTED")],
            raise_after=RuntimeError("stream transport failed"),
            background_grace=5,
        )
        try:
            with self.assertRaises(RuntimeError):
                engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=None)
            self.assertEqual(self.client.stopped_tasks, ["t1"])
            self.assertFalse(engine.healthy)
        finally:
            engine.stop()

    def test_timeout_reap_incomplete_is_labelled_separately(self):
        events = []
        engine = self._engine(messages=[_task_started("t1")], hang_after=True)

        async def boom():
            raise RuntimeError("reap exploded")

        engine._reap_pending = boom
        try:
            result = engine.run_turn_with_progress(
                "x", timeout=0.3, policy="trusted", on_event=lambda k, p: events.append((k, p))
            )
            self.assertFalse(result.ok)
            self.assertIn("reap did not complete for: t1", result.error or "")
            self.assertNotIn("stop_task failed", result.error or "")
            payload = next(p for k, p in events if k == "turn_timeout")
            self.assertEqual(payload["reap_incomplete_tasks"], ["t1"])
            self.assertEqual(payload["stop_failed_tasks"], [])
        finally:
            engine.stop()

    def test_task_completes_but_no_reinvocation_falls_to_hard_timeout(self):
        # Inverse of the guillotine case: once nothing is pending the hold is over, so if the
        # CLI never re-invokes the model the surviving bound is the caller's hard timeout, and
        # the interim text is kept.
        engine = self._engine(
            messages=[_task_started("t1"), _result(text="STARTED"), _task_notification("t1", "completed")],
            hang_after=True,
            background_grace=0.1,
        )
        try:
            result = engine.run_turn_with_progress("x", timeout=0.5, policy="trusted", on_event=None)
            self.assertFalse(result.ok)
            self.assertIn("timed out", result.error or "")
            self.assertEqual(result.result, "STARTED")
            self.assertEqual(self.client.stopped_tasks, [])
        finally:
            engine.stop()

    def test_grace_expiry_with_failed_stop_keeps_truncation_marker(self):
        engine = self._engine(
            messages=[_task_started("t1"), _result(text="STARTED")],
            hang_after=True,
            stop_fails=True,
            background_grace=0.1,
        )
        try:
            result = engine.run_turn_with_progress("x", timeout=30, policy="trusted", on_event=None)
            self.assertFalse(result.ok)
            self.assertIn("stop_task failed for: t1", result.error or "")
            self.assertEqual(result.stop_reason, "background_hold_expired:t1")
        finally:
            engine.stop()
