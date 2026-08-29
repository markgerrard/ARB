"""The thin own ACP server (ARB-B25) — server side of the cockpit chain.

The contract under test is NOT "what ACP allows" but "what the shipped
consumer enforces". Every expectation here is traceable to buzz-acp's client
(`/Volumes/<workspace>/repos/buzz-upstream/crates/buzz-acp/src/acp.rs`), cited
inline, because this slice does not own that code and cannot certify claims
about it by assertion. Design pass: an internal orchestration log.

These tests drive the real JSON-RPC surface with real dict messages. The
runner beneath is a fake, but the protocol is not simulated.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from arb_warm_orch.acp_server import AcpServer, BlockingWriter, serve
from arb_warm_orch.turn_events import TextDelta, ToolCallCompleted, ToolCallStarted


@dataclass
class FakeRunner:
    """A runner that streams a scripted event sequence."""

    events: list = field(default_factory=lambda: [TextDelta(text="warm reply")])
    prompts: list[str] = field(default_factory=list)
    disconnected: bool = False
    interrupted: bool = False

    async def stream_turn(self, text: str):
        self.prompts.append(text)
        for event in self.events:
            yield event

    async def disconnect(self) -> None:
        self.disconnected = True

    async def interrupt(self) -> None:
        # Present because the REAL runtimes have it. A fake that omits part of
        # the contract is how the cancel P0 stayed green.
        self.interrupted = True


def _server(runner: FakeRunner | None = None, channel: str = "warm-chan"):
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    server = AcpServer(
        runner if runner is not None else FakeRunner(),
        channel=channel,
        send=send,
    )
    return server, sent


def _handle(server: AcpServer, message: dict):
    return asyncio.run(server.handle(message))


# ------------------------------------------------------------- initialize


def test_initialize_answers_with_protocol_version_two():
    """buzz pins protocolVersion 2 (acp.rs:124-133, :599-601)."""
    server, _ = _server()
    response = _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": 2, "clientCapabilities": {}},
        },
    )
    assert response["result"]["protocolVersion"] == 2


# ------------------------------------------------------------ session/new


def _new_session(server: AcpServer, request_id: int = 1):
    return _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/new",
            "params": {"cwd": "/tmp", "mcpServers": []},
        },
    )


def test_session_new_returns_the_durable_channel_as_the_session_id():
    """The channel is the identity; sessions rotate beneath it (entry 27)."""
    server, _ = _server(channel="warm-chan")
    response = _new_session(server)
    assert response["result"]["sessionId"] == "warm-chan"


def test_session_new_re_attaches_rather_than_minting_a_fresh_session():
    """A reconnecting client must land back on the SAME warm channel.

    buzz never calls `session/load` — zero occurrences crate-wide — so a
    reconnect is always a second `session/new`. If that minted a new identity,
    warmth would be lost on every relay restart, which is the one property the
    warm orchestrator exists to provide.
    """
    server, _ = _server(channel="warm-chan")
    first = _new_session(server, request_id=1)
    second = _new_session(server, request_id=2)
    assert first["result"]["sessionId"] == second["result"]["sessionId"]


# ------------------------------------------------- session/new systemPrompt


class PromptAdoptingRunner(FakeRunner):
    """A runner that can hold a system prompt — the Agent SDK shape."""

    def __init__(self, *, cold: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.adopted: list[str] = []
        self._cold = cold

    def apply_system_prompt(self, system_prompt: str) -> bool:
        if not self._cold:
            return False
        self.adopted.append(system_prompt)
        return True


def _new_session_with(server: AcpServer, params: dict, request_id: int = 1):
    return _handle(
        server,
        {"jsonrpc": "2.0", "id": request_id, "method": "session/new", "params": params},
    )


def test_session_new_adopts_the_clients_composed_system_prompt():
    """Answering protocolVersion 2 makes buzz deliver the prompt HERE, once.

    buzz picks the modern path on a v2 agent and writes the composed prompt into
    `session/new` params (`pool.rs:186-196` -> `acp.rs:640-642`) rather than
    re-injecting it per turn. Ignoring params dropped `[Base]`, `[System]`,
    `[Team Instructions]`, agent memory and the channel canvas silently — and
    with them the instruction to post replies via `buzz messages send`, without
    which the channel hears nothing at all (#2698).
    """
    runner = PromptAdoptingRunner()
    server, _ = _server(runner)
    _new_session_with(server, {"cwd": "/tmp", "systemPrompt": "[Base]\nbe useful"})
    assert runner.adopted == ["[Base]\nbe useful"]


def test_session_new_without_a_system_prompt_adopts_nothing():
    """The negative control. Absent/empty/non-string must not reach the runtime."""
    for params in (
        {"cwd": "/tmp"},
        {"cwd": "/tmp", "systemPrompt": ""},
        {"cwd": "/tmp", "systemPrompt": None},
        {"cwd": "/tmp", "systemPrompt": {"not": "a string"}},
    ):
        runner = PromptAdoptingRunner()
        server, _ = _server(runner)
        _new_session_with(server, params)
        assert runner.adopted == [], params


def test_session_new_tolerates_a_runtime_that_cannot_hold_a_system_prompt():
    """`codex_runner` and `grok_runner` have no system-prompt concept.

    A runtime that cannot hold one must no-op, not raise — otherwise adding this
    would break two of the four shipped runtimes at `session/new`.
    """
    runner = FakeRunner()  # deliberately has no apply_system_prompt
    server, _ = _server(runner)
    response = _new_session_with(server, {"cwd": "/tmp", "systemPrompt": "[Base]\nx"})
    assert response["result"]["sessionId"] == "warm-chan"


def test_session_new_still_re_attaches_when_a_system_prompt_is_supplied():
    """Adopting a prompt must not disturb channel-as-identity."""
    runner = PromptAdoptingRunner(cold=False)  # warm: refuses the prompt
    server, _ = _server(runner, channel="warm-chan")
    first = _new_session_with(server, {"systemPrompt": "a"}, request_id=1)
    second = _new_session_with(server, {"systemPrompt": "b"}, request_id=2)
    assert first["result"]["sessionId"] == second["result"]["sessionId"] == "warm-chan"
    assert runner.adopted == []  # warm runtime keeps the prompt it was created with


# --------------------------------------------------------- session/prompt


def _prompt(server: AcpServer, *blocks: str, request_id: int = 2):
    return _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/prompt",
            "params": {
                "sessionId": "warm-chan",
                "prompt": [{"type": "text", "text": b} for b in blocks],
            },
        },
    )


def test_prompt_forwards_every_content_block_to_the_runner():
    """buzz sends MULTIPLE blocks, not one wrapped block (acp.rs:745-746).

    The harness sends `["/cmd args", "<buzz context>"]` so that agents which
    detect slash commands see the `/` at the start of the first block. A server
    that read only `prompt[0]` would silently drop the context buzz attached.
    """
    runner = FakeRunner()
    server, _ = _server(runner)
    _prompt(server, "/review this", "buzz context here")
    assert runner.prompts == ["/review this\nbuzz context here"]


def test_prompt_streams_text_as_agent_message_chunk_notifications():
    """The discriminator is `sessionUpdate`, NOT `type` (acp.rs:1695)."""
    runner = FakeRunner(events=[TextDelta(text="alpha"), TextDelta(text="beta")])
    server, sent = _server(runner)
    _prompt(server, "go")
    updates = [m for m in sent if m.get("method") == "session/update"]
    assert [u["params"]["update"]["content"]["text"] for u in updates] == [
        "alpha",
        "beta",
    ]
    assert {u["params"]["update"]["sessionUpdate"] for u in updates} == {
        "agent_message_chunk"
    }


def test_prompt_notifications_carry_the_session_id_and_no_request_id():
    """A notification with an `id` is a REQUEST — the client would await a
    reply that never comes. JSON-RPC 2.0 distinguishes them by that field
    alone."""
    server, sent = _server()
    _prompt(server, "go")
    updates = [m for m in sent if m.get("method") == "session/update"]
    assert updates, "expected at least one session/update"
    for update in updates:
        assert update["params"]["sessionId"] == "warm-chan"
        assert "id" not in update


def test_prompt_emits_tool_call_updates_so_the_client_stops_seeing_silence():
    """buzz resets its 900s idle clock on `tool_call` (acp.rs:1721-1731) and a
    seat dispatch may run 1800s (cli.py:62). Drop this emission and every
    dispatch past fifteen minutes is killed by the consumer."""
    runner = FakeRunner(
        events=[ToolCallStarted(tool_call_id="tu-1", title="Bash", kind="execute")]
    )
    server, sent = _server(runner)
    _prompt(server, "go")
    calls = [
        m["params"]["update"]
        for m in sent
        if m.get("method") == "session/update"
        and m["params"]["update"]["sessionUpdate"] == "tool_call"
    ]
    assert calls == [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tu-1",
            "title": "Bash",
            "kind": "execute",
            "status": "in_progress",
        }
    ]


def test_prompt_reports_a_failed_tool_call_as_failed():
    """buzz reads `status` off `tool_call_update` (acp.rs:1733-1741). Reporting
    a failure as completed would misrepresent the turn in the cockpit."""
    runner = FakeRunner(
        events=[ToolCallCompleted(tool_call_id="tu-1", status="failed")]
    )
    server, sent = _server(runner)
    _prompt(server, "go")
    updates = [
        m["params"]["update"]
        for m in sent
        if m.get("method") == "session/update"
        and m["params"]["update"]["sessionUpdate"] == "tool_call_update"
    ]
    assert updates == [
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tu-1",
            "status": "failed",
        }
    ]


def test_prompt_answers_with_a_stop_reason_the_client_can_parse():
    """buzz parses `stopReason` against a CLOSED set and errors on anything
    else (acp.rs:46-75, :1943-1949) — a missing or novel value is a protocol
    error, not a warning."""
    server, _ = _server()
    response = _prompt(server, "go")
    assert response["result"]["stopReason"] == "end_turn"


def test_the_server_never_asks_the_client_for_permission():
    """The merge/close gate must NOT live on this wire (entry 25).

    buzz answers `session/request_permission` by finding the `allow_once`
    option by kind and selecting it, with no human and no policy involved
    (acp.rs:1888-1900). A gate moved onto this wire would therefore be granted
    every time by the shipped consumer — the party the gate exists to
    constrain. The refusal stays in the runner's PreToolUse hook; this test
    fails the moment someone adds permission plumbing here.
    """
    runner = FakeRunner(
        events=[
            ToolCallStarted(tool_call_id="tu-1", title="Bash", kind="execute"),
            ToolCallCompleted(tool_call_id="tu-1", status="completed"),
        ]
    )
    server, sent = _server(runner)
    _prompt(server, "git merge topic")
    assert not [m for m in sent if m.get("method") == "session/request_permission"]


# --------------------------------------------------------- session/cancel


@dataclass
class BlockingRunner:
    """Streams one event, then blocks until interrupted.

    Models the real shape of a cancel: the client interrupts a turn that is
    still producing. `interrupt()` releases the stream, which then ENDS
    NORMALLY rather than being abandoned mid-flight.
    """

    started: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    drained_to_completion: bool = False

    async def stream_turn(self, text: str):
        yield TextDelta(text="partial")
        self.started.set()
        await self.released.wait()
        # Reached only because the stream was released rather than abandoned;
        # in the real runner this is where the ResultMessage lands and the
        # channel's session id is persisted.
        self.drained_to_completion = True

    async def interrupt(self) -> None:
        self.interrupted = True
        self.released.set()

    async def disconnect(self) -> None:
        pass


def _cancel_message():
    return {
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": "warm-chan"},
    }


def _run_cancel_scenario(runner: BlockingRunner):
    server, sent = _server(runner)

    async def scenario():
        prompt = asyncio.create_task(
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": "warm-chan",
                        "prompt": [{"type": "text", "text": "go"}],
                    },
                }
            )
        )
        await asyncio.wait_for(runner.started.wait(), timeout=5)
        cancel_response = await server.handle(_cancel_message())
        # Bounded on purpose: a cancel that fails to release the turn is a
        # DEADLOCK, and a test that hangs on regression is worse than one that
        # fails — CI would stall rather than report.
        return await asyncio.wait_for(prompt, timeout=5), cancel_response

    response, cancel_response = asyncio.run(scenario())
    return response, cancel_response, sent


def test_cancel_is_a_notification_and_gets_no_response():
    """`session/cancel` carries no `id` (acp.rs:810-823). Answering it would
    put an unmatched response on the wire."""
    _, cancel_response, _ = _run_cancel_scenario(BlockingRunner())
    assert cancel_response is None


def test_a_cancelled_prompt_answers_cancelled():
    """The client expects the in-flight prompt to come back `cancelled`
    (acp.rs:810-815)."""
    response, _, _ = _run_cancel_scenario(BlockingRunner())
    assert response["result"]["stopReason"] == "cancelled"


@dataclass
class RaisingOnInterruptRunner:
    """Models the REAL codex runner: an interrupted turn RAISES.

    `codex_runner._stream_until_complete` treats any TurnStatus outside
    `COMPLETED_TURN_STATUSES` as a failure and raises — and TurnStatus includes
    `interrupted`, which is precisely what a cancel produces
    (`codex_runner.py:319-327`). So on the codex path the stream does not end
    quietly the way `BlockingRunner` does; it blows up, and every line after
    the `async for` in `_prompt` is unreachable.

    `BlockingRunner` cannot catch that, because a fake that ends its stream
    cleanly models the one runner shape where the bug does not occur. Same
    lesson as the note on `FakeRunner.interrupt` above: a fake that omits part
    of the contract is how the cancel path stayed green.
    """

    started: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False

    async def stream_turn(self, text: str):
        yield TextDelta(text="partial")
        self.started.set()
        await self.released.wait()
        raise RuntimeError(
            "codex-turn-not-completed: turn t1 ended with status 'interrupted'"
        )

    async def interrupt(self) -> None:
        self.interrupted = True
        self.released.set()

    async def disconnect(self) -> None:
        pass


def test_a_cancelled_prompt_answers_cancelled_even_when_the_runner_raises():
    """A cancel the SERVER asked for is an expected outcome, not an error.

    buzz parses `stopReason` against a CLOSED set and raises a protocol error
    on anything outside it (`acp.rs:46-75`, cited at `acp_server.py:47-51`), so
    letting the runner's exception escape puts a JSON-RPC -32603 on the wire
    where a `cancelled` stop reason belongs. The client then sees an internal
    error instead of the cancel it just asked for.
    """
    response, _, _ = _run_cancel_scenario(RaisingOnInterruptRunner())
    assert "error" not in response, response
    assert response["result"]["stopReason"] == "cancelled"


def test_an_unrequested_stream_failure_still_propagates():
    """The cancel path must not become a blanket exception swallow.

    Without this, "map a raising stream to `cancelled`" would hide every real
    turn failure behind a clean stop reason — the turn would look cancelled to
    the client when nobody cancelled it. Only a cancel the server ASKED for may
    be absorbed.
    """
    import pytest

    runner = RaisingOnInterruptRunner()
    server, _ = _server(runner)

    async def scenario():
        prompt = asyncio.create_task(
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": "warm-chan",
                        "prompt": [{"type": "text", "text": "go"}],
                    },
                }
            )
        )
        await asyncio.wait_for(runner.started.wait(), timeout=5)
        # Release the stream WITHOUT going through session/cancel, so the
        # server never sets its cancelled flag.
        runner.released.set()
        return await asyncio.wait_for(prompt, timeout=5)

    with pytest.raises(RuntimeError, match="codex-turn-not-completed"):
        asyncio.run(scenario())


def test_cancel_interrupts_the_runner_instead_of_abandoning_the_stream():
    """Warmth depends on this.

    The channel's session id is persisted when the stream ENDS — abandoning
    the generator mid-turn skips that, so the next process would resume from a
    stale id or none at all. Cancelling a turn must not silently cost the
    channel its continuity.
    """
    runner = BlockingRunner()
    _run_cancel_scenario(runner)
    assert runner.interrupted
    assert runner.drained_to_completion


def test_a_later_prompt_is_not_still_cancelled():
    """The cancel flag is per-turn. If it leaked, every subsequent turn on the
    channel would report `cancelled` while running perfectly well."""
    runner = BlockingRunner()
    _run_cancel_scenario(runner)
    server, _ = _server()
    assert _prompt(server, "next")["result"]["stopReason"] == "end_turn"


# ------------------------------------------------------- stdio serve loop


@dataclass
class CollectingWriter:
    chunks: list[bytes] = field(default_factory=list)

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        pass

    def messages(self) -> list[dict]:
        joined = b"".join(self.chunks).decode()
        return [json.loads(line) for line in joined.splitlines() if line.strip()]


def _line(payload: dict | str) -> bytes:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return (text + "\n").encode()


def _run_serve(lines: list[bytes], runner: object | None = None):
    """Drive the serve loop to EOF. The StreamReader is built INSIDE the
    coroutine — constructing one without a running loop raises."""
    writer = CollectingWriter()

    async def scenario():
        reader = asyncio.StreamReader()
        for line in lines:
            reader.feed_data(line)
        reader.feed_eof()
        return await asyncio.wait_for(
            serve(
                reader,
                writer,
                runner=runner if runner is not None else FakeRunner(),
                channel="warm-chan",
            ),
            timeout=5,
        )

    return writer, asyncio.run(scenario())


def test_serve_answers_a_request_line_with_a_response_line():
    writer, _ = _run_serve(
        [_line({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})]
    )
    assert writer.messages() == [
        {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": 2}}
    ]


def test_serve_keeps_reading_while_a_prompt_is_in_flight():
    """The whole point of the loop's shape.

    `session/prompt` is long-running and `session/cancel` arrives DURING it. A
    loop that awaited each handler before reading the next line could not
    deliver the cancel until the turn it cancels had already finished — the
    cancel path would deadlock, and no single-message test would notice.
    """
    runner = BlockingRunner()
    writer = CollectingWriter()

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "session/prompt",
                        "params": {
                            "sessionId": "warm-chan",
                            "prompt": [{"type": "text", "text": "go"}],
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        loop = asyncio.create_task(
            serve(reader, writer, runner=runner, channel="warm-chan")
        )
        await asyncio.wait_for(runner.started.wait(), timeout=5)
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/cancel",
                        "params": {"sessionId": "warm-chan"},
                    }
                )
                + "\n"
            ).encode()
        )
        reader.feed_eof()
        await asyncio.wait_for(loop, timeout=5)

    asyncio.run(scenario())
    responses = [m for m in writer.messages() if "id" in m]
    assert responses[-1]["result"]["stopReason"] == "cancelled"


def test_serve_survives_a_line_that_is_not_json():
    """A peer banner on stdout must not take the server down — but the line is
    RECORDED, because a stream quietly discarding everything looks identical to
    one that is merely quiet (same rule as codex_stdio.py)."""
    writer, skipped = _run_serve(
        [
            _line("starting up, not json"),
            _line({"jsonrpc": "2.0", "id": 0, "method": "initialize"}),
        ]
    )
    assert writer.messages()[0]["result"]["protocolVersion"] == 2
    assert skipped == ["starting up, not json"]


def test_stdout_writer_accepts_a_plain_file_not_only_a_pipe(tmp_path):
    """asyncio's `connect_write_pipe` REFUSES a regular file.

    Found live: a smoke that piped stdout through another process passed,
    while the same server crashed with `ValueError: Pipe transport is only for
    pipes, sockets and character devices` the moment stdout was redirected to
    a file. One stdout flavour passing is not evidence for the others.
    """
    target = tmp_path / "out.ndjson"
    with target.open("wb") as handle:
        writer = BlockingWriter(handle)
        writer.write(b'{"jsonrpc":"2.0"}\n')
        asyncio.run(writer.drain())
    assert target.read_text() == '{"jsonrpc":"2.0"}\n'


# ---- panel panel-warmorch-remediation-20260802T121813Z-06f457 ----------


@dataclass
class ExplodingRunner:
    """A runtime whose turn fails, which is now a REACHABLE state.

    grok and pi were changed to RAISE on a failed turn rather than return an
    empty string — correct in itself, but into a server with no error path.
    """

    async def stream_turn(self, text: str):
        raise RuntimeError("pi-turn-failed: stopReason='error' error='404'")
        yield  # pragma: no cover — makes this an async generator

    async def disconnect(self) -> None:
        pass

    async def interrupt(self) -> None:
        pass


def test_a_failing_turn_answers_the_client_instead_of_hanging_it():
    """`dispatch()` had no except and the module emitted no error responses, so
    a raising turn left the `session/prompt` request unanswered forever. The
    client cannot distinguish that from a slow turn — it just waits."""
    writer, _ = _run_serve(
        [_line({"jsonrpc": "2.0", "id": 7, "method": "session/prompt",
                "params": {"sessionId": "warm-chan",
                           "prompt": [{"type": "text", "text": "go"}]}})],
        runner=ExplodingRunner(),
    )
    replies = [m for m in writer.messages() if m.get("id") == 7]
    assert replies, "the client was never answered — it would hang"
    assert "error" in replies[0], f"expected a JSON-RPC error, got {replies[0]}"
    assert "pi-turn-failed" in json.dumps(replies[0]), "the cause must reach the client"


def test_an_unknown_method_is_answered_with_method_not_found():
    """An unhandled method returned None, which the loop treated as 'nothing to
    send' — so a client calling anything unimplemented hung too."""
    writer, _ = _run_serve(
        [_line({"jsonrpc": "2.0", "id": 9, "method": "session/set_model",
                "params": {"modelId": "x"}})]
    )
    replies = [m for m in writer.messages() if m.get("id") == 9]
    assert replies, "unknown method left the client unanswered"
    assert replies[0].get("error", {}).get("code") == -32601


def test_a_notification_still_gets_no_reply():
    """The counterpart: `session/cancel` has no id and must NOT be answered,
    or the client sees a response it never asked for."""
    writer, _ = _run_serve(
        [_line({"jsonrpc": "2.0", "method": "session/cancel",
                "params": {"sessionId": "warm-chan"}})]
    )
    assert writer.messages() == []
