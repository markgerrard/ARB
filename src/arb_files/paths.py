from __future__ import annotations

import os


def resolve_local_path(root: str | None, path: str) -> str:
    if not root:
        raise ValueError("ARB_FILES_LOCAL_ROOT/AGENT_WORKDIR must be set for local_path operations")
    if not os.path.isabs(path):
        raise ValueError("local path must be absolute")
    real = os.path.realpath(path)
    root_real = os.path.realpath(root)
    if not (real == root_real or real.startswith(root_real + os.sep)):
        raise ValueError("local path escapes allowed root")
    return real
