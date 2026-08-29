"""Primitives the Mac seat depends on, and which of them are not stock macOS.

Deliberately NOT a test module. tests/test_macos_primitives.py asserts the
behaviour of everything named here; tests/test_script_portability.py enforces
the static rules that consume it. Keeping the sets in a third file gives both
pairs a legal mutation target — a module that tests the platform has no repo
code to mutate, and the no-legal-target exemption is owner-authored.
"""

from __future__ import annotations

# Every external binary a shell script in scripts/ may invoke in command
# position. Adding a dependency without a behavioural test here fails the
# coverage guard in tests/test_script_portability.py.
COVERED: frozenset[str] = frozenset(
    {"grep", "chmod", "sed", "stat", "date", "sha256sum", "shasum", "awk", "wc"}
)

# Binaries NOT guaranteed by a stock macOS install, mapped to the stock
# equivalent. Every call site of one of these must be guarded with
# `command -v <name>`; scripts/arb-pi-orch:48 is the pattern.
#
# sha256sum is the proving case: it exists on mini-dev at /sbin/sha256sum,
# which is not part of a stock install, so "the binary is present" is exactly
# the check that passes here and fails on a clean Mac.
NON_STOCK: dict[str, str] = {"sha256sum": "shasum -a 256"}
