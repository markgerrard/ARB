"""Seatbelt (SBPL) profile rendering for the kimi surface probe — v4 §3.

The bootstrap profile (§3) is a coarse-but-real boundary used to CONTAIN kimi while
instrument A measures it: read broad, write only worktree + TMPDIR + ~/.kimi-code, and
it must be able to exec a binary (ARB's could not — rc=71, no /usr/lib/dyld allow).

Traps baked in from the arc's scars (§8):
  - `/tmp` is a symlink to `/private/tmp`; Seatbelt matches RESOLVED paths, so a
    `/tmp/...` subpath silently matches nothing. We REJECT any symlinked / non-absolute
    write path rather than let it fail closed-and-silent.
  - The allow-set includes the system read roots a Mach-O binary needs to launch, or
    the profile parses and denies everything.
"""
import os
from dataclasses import dataclass


class SandboxError(ValueError):
    """A profile request that cannot be rendered into a sound boundary."""


# Read roots a native binary needs to exec/link. Broad on purpose (bootstrap MEASURES
# reads; it does not constrain them). This is exactly what ARB's profile omitted.
_SYSTEM_READ_ROOTS = ("/usr", "/bin", "/sbin", "/System", "/Library", "/private/var",
                      "/opt", "/Applications", "/dev", "/etc", "/Volumes")


def _validate_write_path(name: str, path: str) -> None:
    if not os.path.isabs(path):
        raise SandboxError(f"{name} must be absolute: {path!r}")
    # Reject a path any of whose existing components is a symlink — Seatbelt would match
    # the resolved target, not this string, and silently deny.
    probe = path
    while probe not in ("/", ""):
        if os.path.islink(probe):
            raise SandboxError(f"{name} traverses a symlink ({probe}); use the resolved path")
        probe = os.path.dirname(probe)
    if path.startswith("/tmp/") or path == "/tmp":
        raise SandboxError(f"{name} uses /tmp (a symlink); use /private/tmp")


@dataclass
class BootstrapProfile:
    worktree: str
    tmpdir: str
    kimi_home: str

    def render(self) -> str:
        for name, p in (("worktree", self.worktree), ("tmpdir", self.tmpdir),
                        ("kimi_home", self.kimi_home)):
            _validate_write_path(name, p)
        return render(read_roots=_SYSTEM_READ_ROOTS,
                      write_paths=(self.worktree, self.tmpdir, self.kimi_home))


def render(*, read_roots, write_paths) -> str:
    lines = ["(version 1)", "(deny default)"]
    # dyld/launch reads the ROOT DIRECTORY itself (`deny file-read-data /` in the
    # sandbox violation log) — a subpath list never grants it, and its absence SIGABRTs
    # the launch (found by execution, not reasoning; §8 unified-log channel).
    lines.append('(allow file-read* (literal "/"))')
    # Reads: broad system roots (so binaries can exec/link) + explicit read of writes.
    for root in read_roots:
        lines.append(f'(allow file-read* (subpath "{root}"))')
    for wp in write_paths:
        lines.append(f'(allow file-read* (subpath "{wp}"))')
        lines.append(f'(allow file-write* (subpath "{wp}"))')
    # Exec + the process/sysctl/mach basics a launch needs.
    lines.append("(allow process-exec)")
    lines.append("(allow process-fork)")
    lines.append("(allow sysctl-read)")
    lines.append("(allow mach-lookup)")
    return "\n".join(lines) + "\n"
