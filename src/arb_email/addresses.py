from __future__ import annotations

from email.utils import parseaddr


_BAD = {"<", ">", '"', ","}
_UNI_SEP = ("\x85", "\u2028", "\u2029")


def has_control_chars(s: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 or c in _UNI_SEP for c in s)


def parse_single_recipient(raw: str) -> str:
    if not raw or has_control_chars(raw) or any(c in raw for c in _BAD):
        raise ValueError("invalid recipient")
    stripped = raw.strip()
    name, addr = parseaddr(raw)
    if name or not addr or addr.strip().lower() != stripped.lower():
        raise ValueError("invalid recipient")
    if addr.count("@") != 1:
        raise ValueError("invalid recipient")
    local, host = addr.split("@")
    if not local or not host or "." not in host:
        raise ValueError("invalid recipient")
    return addr.strip().lower()


def recipient_allowed(addr: str, allowlist: list[str] | tuple[str, ...]) -> bool:
    if addr.count("@") != 1:
        return False
    host = addr.split("@")[1]
    if not host:
        return False
    for entry in allowlist:
        if entry.startswith("@"):
            if host == entry[1:]:
                return True
        elif addr == entry:
            return True
    return False

