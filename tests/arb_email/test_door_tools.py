from dataclasses import replace

import anyio
import pytest

from arb_email.config import load_settings
from arb_email.mcp.door_tools import EmailTools


BASE = {"ARB_EMAIL_POSTMARK_TOKEN": "tok"}


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
        return {"sent": True, "message_id": "mid-1", "to": to, "stream": "arb"}


def _tools(settings=None, require_scope=None, now=None, audit=None):
    client = FakeClient()
    events = [] if audit is None else audit

    def allow(scope):
        if require_scope is not None:
            return require_scope(scope)
        return None

    tools = EmailTools(
        client,
        settings or load_settings(BASE),
        require_scope=allow,
        actor=lambda: "client-1",
        now=now,
        audit_sink=events.append if isinstance(events, list) else events,
    )
    return tools, client, events


def _run_email(tools, **kwargs):
    async def call():
        return await tools.email_send(**kwargs)

    return anyio.run(call)


@pytest.mark.parametrize(
    ("label", "kwargs", "settings", "require_scope", "exc_type"),
    [
        ("empty_subject", {"subject": "", "text_body": "x"}, None, None, ValueError),
        (
            "oversize_subject",
            {"subject": "x" * 256, "text_body": "x"},
            None,
            None,
            ValueError,
        ),
        (
            "control_subject",
            {"subject": "ok\r\nBcc: v@x", "text_body": "x"},
            None,
            None,
            ValueError,
        ),
        ("missing_body", {"subject": "Hi"}, None, None, ValueError),
        (
            "oversize_body",
            {"subject": "Hi", "text_body": "é" * 60000},
            None,
            None,
            ValueError,
        ),
        (
            "malformed_to",
            {"subject": "Hi", "text_body": "x", "to": "a@f.com, b@evil.com"},
            None,
            None,
            ValueError,
        ),
        (
            "non_allowlisted_to",
            {"subject": "Hi", "text_body": "x", "to": "evil@example.com"},
            None,
            None,
            ValueError,
        ),
        (
            "missing_scope",
            {"subject": "Hi", "text_body": "x"},
            None,
            lambda _scope: (_ for _ in ()).throw(PermissionError("email.send required")),
            PermissionError,
        ),
        (
            "rate_min",
            {"subject": "Hi", "text_body": "x"},
            replace(load_settings(BASE), rate_per_min=0),
            None,
            ValueError,
        ),
        (
            "rate_day",
            {"subject": "Hi", "text_body": "x"},
            replace(load_settings(BASE), rate_per_day=0),
            None,
            ValueError,
        ),
    ],
)
def test_rejects_emit_denial_audit_and_never_call_client(
    label, kwargs, settings, require_scope, exc_type
):
    tools, client, events = _tools(settings=settings, require_scope=require_scope, now=lambda: 10.0)
    with pytest.raises(exc_type):
        _run_email(tools, **kwargs)
    assert client.calls == []
    assert len(events) == 1
    assert events[0]["op"] == "email_send_denied"
    assert events[0]["actor"] == "client-1"
    _EXPECTED_REASON = {
        "empty_subject": "invalid subject", "oversize_subject": "invalid subject",
        "control_subject": "invalid subject", "missing_body": "body required",
        "oversize_body": "body too large", "malformed_to": "invalid recipient",
        "non_allowlisted_to": "recipient not allowlisted", "missing_scope": "missing scope",
        "rate_min": "rate limit exceeded", "rate_day": "rate limit exceeded",
    }
    assert events[0]["reason"] == _EXPECTED_REASON[label], (label, events[0]["reason"])
    # ts must be a WALL-CLOCK ISO string, not the monotonic rate clock (float)
    assert isinstance(events[0]["ts"], str)


def test_denial_audit_sink_failure_preserves_user_error():
    def boom(_event):
        raise RuntimeError("audit down")

    tools, client, _events = _tools(audit=boom)
    with pytest.raises(ValueError, match="recipient"):
        _run_email(tools, subject="Hi", text_body="x", to="evil@example.com")
    assert client.calls == []


def test_happy_path_delegates_with_normalized_to():
    tools, client, events = _tools()
    out = _run_email(tools, subject="Hi", text_body="x", to=" arb@example.com ")
    assert out["sent"]
    assert client.calls == [
        {
            "subject": "Hi",
            "html_body": None,
            "text_body": "x",
            "to": "arb@example.com",
            "actor": "client-1",
        }
    ]
    assert events == []


def test_rate_minute_sliding_window_recovers():
    current = [0.0]
    settings = replace(load_settings(BASE), rate_per_min=1)
    tools, client, _events = _tools(settings=settings, now=lambda: current[0])
    _run_email(tools, subject="Hi", text_body="x")
    current[0] = 10.0
    with pytest.raises(ValueError):
        _run_email(tools, subject="Hi", text_body="x")
    current[0] = 61.0
    _run_email(tools, subject="Hi", text_body="x")
    assert len(client.calls) == 2


def test_rate_day_sliding_window_recovers():
    current = [0.0]
    settings = replace(load_settings(BASE), rate_per_min=10, rate_per_day=1)
    tools, client, _events = _tools(settings=settings, now=lambda: current[0])
    _run_email(tools, subject="Hi", text_body="x")
    current[0] = 3600.0
    with pytest.raises(ValueError):
        _run_email(tools, subject="Hi", text_body="x")
    current[0] = 86401.0
    _run_email(tools, subject="Hi", text_body="x")
    assert len(client.calls) == 2


def test_postmark_failure_emits_email_send_failed_audit():
    # A Postmark-rejected send (RuntimeError from client) leaves a forensic email_send_failed record.
    class FailClient:
        def send(self, *a, **k):
            raise RuntimeError("postmark error 406: inactive recipient")

    events = []
    tools = EmailTools(
        FailClient(), load_settings(BASE), require_scope=lambda _s: None,
        actor=lambda: "client-1", audit_sink=events.append,
    )
    with pytest.raises(RuntimeError, match="406"):
        _run_email(tools, subject="Hi", text_body="x")
    assert len(events) == 1
    assert events[0]["op"] == "email_send_failed"
    assert events[0]["to"] == "arb@example.com"
    assert isinstance(events[0]["ts"], str)
