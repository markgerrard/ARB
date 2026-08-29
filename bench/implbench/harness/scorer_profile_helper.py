"""Structural-tier scorer profile verifier.

The repository-owned helper makes the profile argument and digest part of the
actual exec chain without claiming Seatbelt enforcement. Task 14 replaces this
step with ``sandbox-exec -p`` through the same launcher object.
"""

from __future__ import annotations

import hashlib
import os
import sys


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) < 6 or values[0] != "--profile" or values[2] != "--digest" or values[4] != "--":
        return 125
    profile, digest, command = values[1], values[3], values[5:]
    if not command or hashlib.sha256(profile.encode()).hexdigest() != digest:
        return 125
    os.execvpe(command[0], command, dict(os.environ))
    return 125


if __name__ == "__main__":
    raise SystemExit(main())
