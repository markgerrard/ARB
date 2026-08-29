"""Stdio transport for `codex app-server` — the real wire under the runner.

Framing verified from the shipping reference adapter (repos/codex-acp
StdUtils.ts:16,39): newline-delimited JSON, one message per line — NOT the
LSP-style `Content-Length` framing that "JSON-RPC over stdio" usually implies.
The same source shows the adapter DELETES the `jsonrpc` field on write and adds
it back on read, i.e. codex app-server neither wants nor emits it; this
transport therefore sends exactly what the runner hands it.

Streams are injected so these tests exercise the real framing without spawning
a process. One test per named behavior, red-first.
"""
from __future__ import annotations

import io

import pytest

from arb_warm_orch.codex_stdio import CodexStdioClosed, StdioTransport


class FakeStdin(io.StringIO):
    """A writable stream that records whether it was flushed."""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:  # pragma: no cover - trivial
        self.flushes += 1


def _transport(lines: str = "") -> tuple[StdioTransport, FakeStdin]:
    stdin = FakeStdin()
    transport = StdioTransport(stdin=stdin, stdout=io.StringIO(lines))
    return transport, stdin


# --------------------------------------------------------------- sending


def test_send_writes_one_json_line():
    transport, stdin = _transport()
    transport.send({"id": 1, "method": "initialize"})
    assert stdin.getvalue() == '{"id": 1, "method": "initialize"}\n'


def test_send_flushes_so_the_peer_is_not_left_waiting():
    transport, stdin = _transport()
    transport.send({"id": 1, "method": "initialize"})
    assert stdin.flushes >= 1


def test_send_does_not_add_a_jsonrpc_field():
    transport, stdin = _transport()
    transport.send({"id": 1, "method": "initialize"})
    assert "jsonrpc" not in stdin.getvalue()


# ------------------------------------------------------------- receiving


def test_receive_parses_one_message_per_line():
    transport, _ = _transport('{"id": 1, "result": {}}\n')
    assert transport.receive() == {"id": 1, "result": {}}


def test_receive_returns_messages_in_order():
    transport, _ = _transport('{"id": 1}\n{"id": 2}\n')
    assert transport.receive()["id"] == 1
    assert transport.receive()["id"] == 2


def test_blank_lines_are_skipped_not_returned():
    transport, _ = _transport('\n\n{"id": 7}\n')
    assert transport.receive() == {"id": 7}


def test_non_json_banner_lines_are_skipped_but_counted():
    transport, _ = _transport('starting app-server...\n{"id": 7}\n')
    assert transport.receive() == {"id": 7}
    assert transport.skipped_lines == ["starting app-server..."]


def test_eof_raises_a_specific_error_instead_of_hanging():
    transport, _ = _transport("")
    with pytest.raises(CodexStdioClosed) as excinfo:
        transport.receive()
    assert "codex-stdio-closed" in str(excinfo.value)


def test_eof_after_valid_messages_still_raises():
    transport, _ = _transport('{"id": 1}\n')
    transport.receive()
    with pytest.raises(CodexStdioClosed):
        transport.receive()


# ---------------------------------------------------------------- close


def test_close_runs_the_injected_teardown():
    closed: list[str] = []
    transport = StdioTransport(
        stdin=FakeStdin(), stdout=io.StringIO(""), on_close=lambda: closed.append("x")
    )
    transport.close()
    assert closed == ["x"]


def test_close_is_idempotent():
    closed: list[str] = []
    transport = StdioTransport(
        stdin=FakeStdin(), stdout=io.StringIO(""), on_close=lambda: closed.append("x")
    )
    transport.close()
    transport.close()
    assert closed == ["x"]


def test_send_after_close_refuses_with_a_specific_error():
    transport, _ = _transport()
    transport.close()
    with pytest.raises(CodexStdioClosed) as excinfo:
        transport.send({"id": 1})
    assert "codex-stdio-closed" in str(excinfo.value)


def test_spawn_stdio_agent_drives_an_arbitrary_command():
    """The transport was always generic NDJSON; only the spawn was codex-shaped.

    grok is launched as `grok agent stdio`, so the warm grok runtime needs the
    same framing with a different argv rather than a second transport.
    """
    from arb_warm_orch.codex_stdio import spawn_stdio_agent

    transport = spawn_stdio_agent("cat", [], cwd=None)
    try:
        transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert transport.receive() == {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    finally:
        transport.close()
