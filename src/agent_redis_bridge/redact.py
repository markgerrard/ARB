from __future__ import annotations

import re


REDACTED = "‹redacted›"


_SECRET_KEY_TERMS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "api-key",
    "auth",
    "credential",
    "access_key",
    "private_key",
    "client_secret",
)

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b[A-Fa-f0-9]{64,}\b"),
]

_ASSIGNMENT = re.compile(
    r"\b(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|[^\s]+)",
    re.IGNORECASE,
)


def _secret_assignment(match: re.Match[str]) -> str:
    key = match.group("key").lower()
    if any(term in key for term in _SECRET_KEY_TERMS):
        return REDACTED
    return match.group(0)


def redact(text: str) -> str:
    scrubbed = text
    for pattern in _PATTERNS:
        scrubbed = pattern.sub(REDACTED, scrubbed)
    return _ASSIGNMENT.sub(_secret_assignment, scrubbed)
