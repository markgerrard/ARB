import anyio
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from arb_email.config import load_settings
from arb_email.mcp.local_server import build_local_server


BASE = {"ARB_EMAIL_POSTMARK_TOKEN": "tok", "ARB_EMAIL_SEND_ENABLED": "1"}


class FakeClient:
    def __init__(self):
        self.calls = []

    def send(self, subject, html_body, text_body, *, to, actor):
        self.calls.append(
            {
                "subject": subject,
                "html_body": html_body,
                "text_body": text_body,
                "to": to,
                "actor": actor,
            }
        )
        return {"sent": True, "message_id": "mid-local", "to": to, "stream": "arb"}


def _server(env=None, now=None):
    client = FakeClient()
    events = []
    server = build_local_server(
        load_settings(env or BASE),
        client=client,
        seat_id="seat-A",
        audit_sink=events.append,
        now=now,
    )
    return server, client, events


def _tool_names(server):
    return {tool.name for tool in anyio.run(server.list_tools)}


def _call(server, args):
    return anyio.run(server.call_tool, "email_send", args)


def test_build_local_server_registers_email_send():
    server, _client, _events = _server()
    assert "email_send" in _tool_names(server)


def test_build_local_server_refuses_when_send_disabled():
    settings = load_settings({"ARB_EMAIL_POSTMARK_TOKEN": "tok", "ARB_EMAIL_SEND_ENABLED": "0"})
    with pytest.raises(ValueError, match="disabled"):
        build_local_server(settings, client=FakeClient(), seat_id="seat-A")


@pytest.mark.parametrize(
    "args",
    [
        {"subject": "Hi", "text_body": "x", "to": "evil@example.com"},
        {"subject": "Hi"},
    ],
)
def test_local_rejects_before_fake_client_and_emits_denial(args):
    server, client, events = _server()
    with pytest.raises(ToolError):
        _call(server, args)
    assert client.calls == []
    assert len(events) == 1
    assert events[0]["op"] == "email_send_denied"
    assert events[0]["actor"] == "seat-A"


def test_local_rate_trip_before_fake_client_and_emits_denial():
    server, client, events = _server(
        env={"ARB_EMAIL_POSTMARK_TOKEN": "tok", "ARB_EMAIL_SEND_ENABLED": "1", "ARB_EMAIL_RATE_PER_MIN": "0"},
        now=lambda: 10.0,
    )
    with pytest.raises(ToolError, match="rate"):
        _call(server, {"subject": "Hi", "text_body": "x"})
    assert client.calls == []
    assert events[0]["reason"] == "rate limit exceeded"


def test_local_happy_path_calls_client_with_seat_actor():
    server, client, events = _server()
    _call(server, {"subject": "Hi", "text_body": "x", "to": " arb@example.com "})
    assert client.calls[0]["actor"] == "seat-A"
    assert client.calls[0]["to"] == "arb@example.com"
    assert events == []
