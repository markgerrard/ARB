from __future__ import annotations

import queue
import tempfile
from pathlib import Path
from unittest import mock

from agent_redis_bridge.bridge import Bridge, build_parser, resolve_trace_redis
from agent_redis_bridge.engines.cursor_acp import CursorAcpEngine, normalize_session_update
from agent_redis_bridge.transcript_flusher import TranscriptFlusher


class FakeTraceRedis:
    def __init__(self) -> None:
        self.xadds: list[tuple[str, dict, dict]] = []

    def xadd(self, key, fields, **kwargs):
        if "ttl" in kwargs:
            raise AssertionError("xadd must not receive ttl")
        self.xadds.append((key, fields, kwargs))
        return "1-0"


def _item(
    *,
    task_id: str = "task-1",
    run_id: str = "run-1",
    seat_id: str = "seat-1",
    orchestrator: str = "orch-1",
    event: str = "model_text",
    turn_id: str = "turn-1",
    item_id: str = "turn-1:text",
    seq: int = 1,
    kind: str = "model_text",
    delta: str = "",
    tool_name: str | None = None,
    command: str | None = None,
) -> dict:
    data = {
        "delta": delta,
        "turn_id": turn_id,
        "item_id": item_id,
        "kind": kind,
        "seq": seq,
    }
    if tool_name is not None:
        data["tool_name"] = tool_name
    if command is not None:
        data["command"] = command
    return {
        "task_id": task_id,
        "run_id": run_id,
        "seat_id": seat_id,
        "orchestrator": orchestrator,
        "event": event,
        "turn_id": turn_id,
        "item_id": item_id,
        "kind": kind,
        "seq": seq,
        "data": data,
    }


def _turn_end(task_id: str = "task-1") -> dict:
    return {
        "task_id": task_id,
        "run_id": "run-1",
        "seat_id": "seat-1",
        "orchestrator": "orch-1",
        "event": "turn_end",
        "turn_id": task_id,
        "item_id": f"{task_id}:turn_end",
        "kind": "turn_end",
        "seq": 99,
        "data": {},
    }


def test_redact_redis_config_resolves_from_env_mapping(monkeypatch) -> None:
    monkeypatch.delenv("ARB_TRACE_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_TRACE_PREFIX", raising=False)

    assert resolve_trace_redis({"ARB_TRACE_REDIS_URL": "redis://trace/5", "ARB_TRACE_PREFIX": "p:"}) == (
        "redis://trace/5",
        "p:",
    )

    monkeypatch.setenv("ARB_TRACE_REDIS_URL", "redis://override/6")
    monkeypatch.setenv("ARB_TRACE_PREFIX", "env:")
    assert resolve_trace_redis({"ARB_TRACE_REDIS_URL": "redis://trace/5", "ARB_TRACE_PREFIX": "p:"}) == (
        "redis://override/6",
        "env:",
    )


def test_flusher_coalesces_and_redacts() -> None:
    q: queue.Queue[dict] = queue.Queue()
    q.put(_item(delta="hello ", seq=1))
    q.put(_item(delta="Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig", seq=2))
    q.put(_turn_end())
    trace = FakeTraceRedis()

    flusher = TranscriptFlusher(q, trace, "trace:", redactor=lambda text: text.replace("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig", "‹redacted›"))
    flusher.flush_pending()

    assert len(trace.xadds) == 1
    key, fields, kwargs = trace.xadds[0]
    assert key == "trace:arbmem:trace"
    assert kwargs == {"maxlen": flusher.maxlen, "approximate": True}
    assert fields["run_id"] == "run-1"
    assert fields["task_id"] == "task-1"
    assert fields["seat_id"] == "seat-1"
    assert fields["orchestrator"] == "orch-1"
    assert fields["item_id"] == "turn-1:text"
    assert fields["seq"] == "1"
    assert fields["kind"] == "model_text"
    assert fields["content"] == "hello Bearer ‹redacted›"


def test_flusher_redacts_secret_bearing_tool_name() -> None:
    q: queue.Queue[dict] = queue.Queue()
    secret_command = 'curl -H "Authorization: Bearer eyJabcabcabcabcabcabcabcabcabcabc" https://example.invalid'
    q.put(
        {
            "task_id": "task-1",
            "run_id": "run-1",
            "seat_id": "seat-1",
            "orchestrator": "orch-1",
            "event": "command_started",
            "turn_id": "turn-1",
            "item_id": "turn-1:curl",
            "kind": "command_started",
            "seq": 3,
            "data": {
                "content": "started",
                "tool_name": secret_command,
                "turn_id": "turn-1",
                "item_id": "turn-1:curl",
                "kind": "command_started",
                "seq": 3,
            },
        }
    )
    q.put(_turn_end())
    trace = FakeTraceRedis()

    TranscriptFlusher(q, trace, "trace:").flush_pending()

    fields = trace.xadds[0][1]
    assert "eyJabcabcabcabcabcabcabcabcabcabc" not in fields["tool_name"]
    assert "‹redacted›" in fields["tool_name"]


def test_flusher_updates_tool_name_from_later_same_item_id_event() -> None:
    trace = FakeTraceRedis()
    flusher = TranscriptFlusher(queue.Queue(), trace, "trace:")
    item_id = "turn-1:edit"

    flusher._process(
        _item(
            event="command_started",
            item_id=item_id,
            kind="command_started",
            seq=1,
            tool_name="Edit File",
        )
    )
    flusher._process(
        _item(
            event="command_finished",
            item_id=item_id,
            kind="command_finished",
            seq=2,
            command="Edit File: /tmp/project/farewell.py",
        )
    )
    flusher._process(_turn_end())

    assert len(trace.xadds) == 1
    assert trace.xadds[0][1]["tool_name"] == "Edit File: /tmp/project/farewell.py"


def test_flusher_keeps_existing_tool_name_when_later_event_has_no_label() -> None:
    trace = FakeTraceRedis()
    flusher = TranscriptFlusher(queue.Queue(), trace, "trace:")
    item_id = "turn-1:edit"

    flusher._process(
        _item(
            event="command_started",
            item_id=item_id,
            kind="command_started",
            seq=1,
            tool_name="Edit File",
        )
    )
    flusher._process(
        _item(
            event="command_finished",
            item_id=item_id,
            kind="command_finished",
            seq=2,
            command="",
        )
    )
    flusher._process(_turn_end())

    assert len(trace.xadds) == 1
    assert trace.xadds[0][1]["tool_name"] == "Edit File"


def test_flusher_preserves_first_kind_when_later_same_item_id_kind_differs() -> None:
    trace = FakeTraceRedis()
    flusher = TranscriptFlusher(queue.Queue(), trace, "trace:")
    item_id = "turn-1:edit"

    flusher._process(
        _item(
            event="command_started",
            item_id=item_id,
            kind="command_started",
            seq=1,
            tool_name="Edit File",
        )
    )
    flusher._process(
        _item(
            event="command_finished",
            item_id=item_id,
            kind="command_finished",
            seq=2,
            command="Edit File: /tmp/project/farewell.py",
        )
    )
    flusher._process(_turn_end())

    assert len(trace.xadds) == 1
    fields = trace.xadds[0][1]
    assert fields["kind"] == "command_started"
    assert fields["tool_name"] == "Edit File: /tmp/project/farewell.py"


def test_cursor_acp_tool_call_full_chain_keeps_start_kind_and_enriched_tool_name() -> None:
    trace = FakeTraceRedis()
    flusher = TranscriptFlusher(queue.Queue(), trace, "trace:")
    engine = CursorAcpEngine(cwd="/tmp/project", model=None)
    engine.active_prompt_id = 42
    tool_titles: dict[str, str] = {}
    path = "/tmp/project/farewell.py"

    started = normalize_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tool-1",
            "status": "pending",
            "title": "Edit File",
            "kind": "edit",
        },
        tool_titles,
    )
    finished = normalize_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tool-1",
            "status": "completed",
            "content": [{"type": "diff", "path": path, "oldText": "", "newText": ""}],
        },
        tool_titles,
    )
    assert started is not None
    assert finished is not None

    for event_name, data in (started, finished):
        enriched = engine._with_progress_schema(event_name, data)
        flusher._process(
            {
                "task_id": "task-1",
                "run_id": "run-1",
                "seat_id": "cursor-seat",
                "orchestrator": "orch-1",
                "event": event_name,
                "turn_id": enriched["turn_id"],
                "item_id": enriched["item_id"],
                "kind": enriched["kind"],
                "seq": enriched["seq"],
                "data": enriched,
            }
        )
    flusher._process(_turn_end())

    assert len(trace.xadds) == 1
    fields = trace.xadds[0][1]
    assert fields["kind"] == "command_started"
    assert path in fields["tool_name"]


def test_turn_end_is_a_flush_boundary() -> None:
    q: queue.Queue[dict] = queue.Queue()
    repeated_item_id = "session-1:text"
    q.put(_item(turn_id="session-1", item_id=repeated_item_id, delta="turn A", seq=1))
    q.put(_turn_end())
    q.put(_item(turn_id="session-1", item_id=repeated_item_id, delta="turn B", seq=2))
    q.put(_turn_end())
    trace = FakeTraceRedis()

    TranscriptFlusher(q, trace, "trace:").flush_pending()

    assert [fields["content"] for _, fields, _ in trace.xadds] == ["turn A", "turn B"]
    assert [fields["item_id"] for _, fields, _ in trace.xadds] == [repeated_item_id, repeated_item_id]
    assert [fields["turn_index"] for _, fields, _ in trace.xadds] == ["0", "1"]


def test_flusher_survives_poison_item() -> None:
    q: queue.Queue[dict] = queue.Queue()
    q.put(None)  # type: ignore[arg-type]
    q.put({"task_id": "task-1", "event": "model_text", "data": "wrong"})
    q.put(_item(delta="valid", seq=7))
    q.put(_turn_end())
    trace = FakeTraceRedis()

    TranscriptFlusher(q, trace, "trace:").flush_pending()

    assert [fields["content"] for _, fields, _ in trace.xadds] == ["valid"]
    assert trace.xadds[0][1]["seq"] == "7"


def test_flusher_truncates_large_content() -> None:
    q: queue.Queue[dict] = queue.Queue()
    q.put(_item(delta="x" * (TranscriptFlusher.content_cap + 10)))
    q.put(_turn_end())
    trace = FakeTraceRedis()

    TranscriptFlusher(q, trace, "trace:").flush_pending()

    content = trace.xadds[0][1]["content"]
    assert len(content) <= TranscriptFlusher.content_cap + len("…‹truncated›")
    assert content.endswith("…‹truncated›")


def test_flusher_capture_off_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("ARB_TRACE_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_TRACE_PREFIX", raising=False)
    # Force the kill-switch OFF via os.environ (highest precedence in the
    # resolve order: os.environ > env-file). delenv alone is fragile under a
    # polluted suite where an earlier test leaks ARB_TRANSCRIPT_CAPTURE into
    # os.environ; setenv("off") models the prod kill-switch and is pollution-proof.
    monkeypatch.setenv("ARB_TRANSCRIPT_CAPTURE", "off")
    with tempfile.TemporaryDirectory() as temp_dir:
        env_file = Path(temp_dir) / ".env"
        workdir = Path(temp_dir) / "workdir"
        workdir.mkdir()
        env_file.write_text(
            "AGENT_REDIS_HOST=127.0.0.1\n"
            "AGENT_REDIS_PORT=6390\n"
            "AGENT_REDIS_DB=12\n"
            "AGENT_REDIS_PREFIX=agent_scratch:\n"
            "AGENT_WORKSPACE=dev\n"
            "AGENT_PROJECT=project-c\n"
            "ARB_TRACE_REDIS_URL=redis://127.0.0.1:6379/8\n"
            "ARB_TRANSCRIPT_CAPTURE=off\n",
            encoding="utf-8",
        )
        args = build_parser().parse_args(["--env-file", str(env_file), "--workdir", str(workdir)])
        # Patch from_url only to prevent any real connection attempt during init;
        # do NOT assert_not_called on it — Bridge.__init__ legitimately calls
        # redis.from_url for the eval/main bus when those are configured (and a
        # polluted suite can leak ARB_EVAL_REDIS_URL into os.environ). The precise
        # proof that the TRANSCRIPT path stayed off is the structural state below.
        with mock.patch("redis.from_url"):
            bridge = Bridge(args)

    assert bridge.trace_redis is None
    assert bridge._transcript_flusher is None
    assert bridge._transcript_thread is None


def test_flusher_streams_per_item_not_only_at_turn_end() -> None:
    """A new item_id flushes the previous (completed) item immediately, so the live
    view streams per item instead of dumping the whole turn at turn_end."""
    trace = FakeTraceRedis()
    flusher = TranscriptFlusher(queue.Queue(), trace, "trace:")

    # model_text deltas for item A — same item_id coalesces, no flush yet
    flusher._process(_item(item_id="turn-1:text", delta="hello ", seq=1))
    flusher._process(_item(item_id="turn-1:text", delta="world", seq=2))
    assert len(trace.xadds) == 0  # item A still open (coalescing)

    # a NEW item_id (a command) arrives -> item A is complete -> flushed NOW
    flusher._process(_item(item_id="turn-1:cmd", kind="command_started",
                           event="command_started", delta="", seq=3))
    assert len(trace.xadds) == 1
    assert trace.xadds[0][1]["item_id"] == "turn-1:text"
    assert trace.xadds[0][1]["content"] == "hello world"  # coalesced before the boundary flush

    # the last item flushes on turn_end as before
    flusher._process(_turn_end())
    assert [f["item_id"] for _, f, _ in trace.xadds] == ["turn-1:text", "turn-1:cmd"]


def test_flusher_interleaved_item_id_splits_into_ordered_rows() -> None:
    """Pins the documented behaviour: if an item_id interleaves (A → B → A), A is flushed
    at B and the resumed A becomes a SECOND ordered row (content preserved, ordered by seq;
    no loss). Engines emit sequentially today, so this guards against a silent change."""
    trace = FakeTraceRedis()
    flusher = TranscriptFlusher(queue.Queue(), trace, "trace:")

    flusher._process(_item(item_id="A", delta="a1", seq=1))
    flusher._process(_item(item_id="B", kind="command_started", event="command_started", delta="", seq=2))  # flushes A
    flusher._process(_item(item_id="A", delta="a2", seq=3))  # A resumes -> new pending row
    flusher._process(_turn_end())  # flushes B then the second A

    rows = [(f["item_id"], f["content"], f["seq"]) for _, f, _ in trace.xadds]
    assert rows[0] == ("A", "a1", "1")           # first A flushed at the B boundary
    assert ("A", "a2", "3") in rows              # resumed A is a separate, later-seq row
    assert all(int(rows[i][2]) <= int(rows[i + 1][2]) for i in range(len(rows) - 1))  # seq-ordered
