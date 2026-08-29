"""Neutral artifact capture and collation."""

from __future__ import annotations

import hashlib
from pathlib import Path


def _sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def capture_artifact(path: str | Path) -> dict:
    artifact_path = Path(path)
    return {
        "path": str(artifact_path),
        "sha256": _sha256_path(artifact_path),
        "size": artifact_path.stat().st_size,
    }


def collate_artifacts(artifacts: list[dict]) -> dict:
    return {"artifacts": list(artifacts)}
