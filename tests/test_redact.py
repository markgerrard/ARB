from __future__ import annotations

from agent_redis_bridge.redact import REDACTED, redact


def test_redact_scrubs_corpus() -> None:
    corpus = [
        "AWS_SECRET=AKIAIOSFODNN7EXAMPLE",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature",
        "export TOKEN=super-secret-value-12345",
        "SECRET=hunter2",
        "API_KEY=abc123abc123abc123",
        "password = \"p@ss\"",
        "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    ]

    for line in corpus:
        scrubbed = redact(line)
        assert REDACTED in scrubbed
        assert "AKIAIOSFODNN7EXAMPLE" not in scrubbed
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in scrubbed
        assert "super-secret-value-12345" not in scrubbed
        assert "BEGIN PRIVATE KEY" not in scrubbed
        assert "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" not in scrubbed

    assert redact("ordinary progress text with no secrets") == "ordinary progress text with no secrets"


def test_redact_preserves_benign_assignments() -> None:
    benign = [
        "MAX_RETRIES = 5",
        "DEBUG=True",
        "timeout = 30",
        "ordinary progress text with no secrets",
    ]

    for line in benign:
        assert redact(line) == line
