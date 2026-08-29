import pytest

from arb_email.client import EmailClient
from arb_email.config import load_settings


BASE = {"ARB_EMAIL_POSTMARK_TOKEN": "tok"}


def _client(env=None, resp=(200, {"ErrorCode": 0, "MessageID": "mid-1"}), audit=None):
    calls = {}

    def post(url, json, headers):
        calls["url"] = url
        calls["json"] = json
        calls["headers"] = headers
        return resp

    c = EmailClient(load_settings(env or BASE), http_post=post, audit_sink=audit)
    return c, calls


def test_send_ok_returns_message_id_and_fixed_from_stream():
    c, calls = _client()
    out = c.send("Hi", "<b>x</b>", None, to="arb@example.com", actor="seat")
    assert out["sent"] and out["message_id"] == "mid-1"
    assert calls["json"]["From"] == "arb@example.com"
    assert calls["json"]["MessageStream"] == "arb"
    assert calls["json"]["To"] == "arb@example.com"
    assert set(calls["json"]) <= {
        "From",
        "To",
        "Subject",
        "MessageStream",
        "HtmlBody",
        "TextBody",
    }


def test_payload_to_is_normalized():
    c, calls = _client()
    c.send("Hi", "x", None, to=" arb@example.com ", actor="s")
    assert calls["json"]["To"] == "arb@example.com"


def test_non_allowlisted_to_rejected_before_network():
    c, calls = _client()
    with pytest.raises(ValueError, match="allowlisted"):
        c.send("Hi", "x", None, to="evil@example.com", actor="s")
    assert calls == {}


def test_subject_control_chars_rejected_before_network():
    c, calls = _client()
    with pytest.raises(ValueError, match="subject"):
        c.send("ok\r\nBcc: v@x", "x", None, to="arb@example.com", actor="s")
    assert calls == {}


def test_postmark_error_is_runtime_not_silent():
    c, _ = _client(resp=(200, {"ErrorCode": 406, "Message": "inactive recipient"}))
    with pytest.raises(RuntimeError, match="406"):
        c.send("Hi", "x", None, to="arb@example.com", actor="s")


def test_non_2xx_runtime():
    c, _ = _client(resp=(500, {}))
    with pytest.raises(RuntimeError):
        c.send("Hi", "x", None, to="arb@example.com", actor="s")


def test_post_send_audit_failure_does_not_raise():
    def boom(_event):
        raise RuntimeError("sink down")

    c, _ = _client(audit=boom)
    out = c.send("Hi", "x", None, to="arb@example.com", actor="s")
    assert out["sent"]


def test_success_audit_event_emitted():
    events = []
    c, _ = _client(audit=events.append)
    c.send("Hi", "x", None, to="arb@example.com", actor="s")
    assert events[0]["op"] == "email_send"
    assert events[0]["actor"] == "s"
    assert events[0]["to"] == "arb@example.com"
    assert events[0]["message_id"] == "mid-1"


def test_body_cap_is_bytes():
    c, _ = _client()
    with pytest.raises(ValueError):
        c.send("Hi", "x" + "é" * 60000, None, to="arb@example.com", actor="s")
