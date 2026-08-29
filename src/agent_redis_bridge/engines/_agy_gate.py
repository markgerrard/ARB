"""Host gate for engines that drive the agy CLI.

agy authenticates with a personal Google OAuth. Google's account security
flags sign-ins and API traffic from datacenter IPs (observed 2026-08-15:
agy on DigitalOcean droplets tripped the flag, degrading the account on
every device). The agy engines are therefore hard-gated to macOS hosts —
the operator's MacBook Pro / Mac mini on residential IPs.

Deliberately no env-var override: a bypassable gate would defeat the
account protection it exists to provide. If agy must ever run elsewhere,
change this module in the repo so the decision is reviewed, not toggled.
"""

from __future__ import annotations

import platform

from .base import EngineError


def assert_agy_host_allowed(system: str | None = None) -> None:
    actual = platform.system() if system is None else system
    if actual != "Darwin":
        raise EngineError(
            "agy engines are gated to macOS hosts: agy runs under a personal "
            "Google OAuth and datacenter IPs flag the account "
            f"(this host: {actual}). Run agy seats on the MacBook Pro or Mac mini."
        )
