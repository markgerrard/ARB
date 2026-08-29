from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from arb_email.addresses import parse_single_recipient, recipient_allowed


@dataclass(frozen=True)
class Settings:
    token: str
    api_url: str = "https://api.postmarkapp.com/email"
    sender: str = "arb@example.com"
    stream: str = "arb"
    default_to: str = "arb@example.com"
    to_allowlist: tuple[str, ...] = ("arb@example.com",)
    send_enabled: bool = True
    subject_max: int = 255
    body_max: int = 102400
    rate_per_min: int = 10
    rate_per_day: int = 100


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    value = env.get(key)
    return int(value) if value else default


def load_settings(env: Mapping[str, str]) -> Settings:
    token = env.get("ARB_EMAIL_POSTMARK_TOKEN")
    if not token:
        raise ValueError("ARB_EMAIL_POSTMARK_TOKEN required")

    default_to = parse_single_recipient(env.get("ARB_EMAIL_DEFAULT_TO", "arb@example.com"))
    raw_allowlist = env.get("ARB_EMAIL_TO_ALLOWLIST")
    if raw_allowlist:
        allow = [entry.strip().lower() for entry in raw_allowlist.split(",")]
    else:
        allow = [default_to]
    allow = [entry for entry in allow if entry]
    if not allow:
        raise ValueError("ARB_EMAIL_TO_ALLOWLIST empty")
    if not recipient_allowed(default_to, allow):
        raise ValueError("ARB_EMAIL_DEFAULT_TO not in allowlist")

    return Settings(
        token=token,
        api_url=env.get("ARB_EMAIL_API_URL", "https://api.postmarkapp.com/email"),
        sender=env.get("ARB_EMAIL_FROM", "arb@example.com"),
        stream=env.get("ARB_EMAIL_STREAM", "arb"),
        default_to=default_to,
        to_allowlist=tuple(allow),
        send_enabled=env.get("ARB_EMAIL_SEND_ENABLED", "1") == "1",
        subject_max=_int(env, "ARB_EMAIL_SUBJECT_MAX", 255),
        body_max=_int(env, "ARB_EMAIL_BODY_MAX", 102400),
        rate_per_min=_int(env, "ARB_EMAIL_RATE_PER_MIN", 10),
        rate_per_day=_int(env, "ARB_EMAIL_RATE_PER_DAY", 100),
    )
