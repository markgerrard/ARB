"""Scrub secrets from any captured stream before printing/persisting.

Defends against a model running `env`/`echo $KEY` via the Bash tool.
"""
from __future__ import annotations

REDACTED = "[REDACTED]"


def scrub(text: str, secrets: list[str], var_names: list[str]) -> str:
    out = text
    for s in list(secrets) + list(var_names):
        s = (s or "").strip()
        if s:
            out = out.replace(s, REDACTED)
    return out
