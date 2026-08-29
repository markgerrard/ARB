"""Lint helpers for real-codebase fixture generation."""
from __future__ import annotations

import subprocess
from pathlib import Path

from arb_eval import boundary


class LintError(ValueError):
    pass


def assert_marker_free(repo: Path, patch_paths: list[Path], ids: list[str]) -> None:
    hits: list[str] = []
    for patch in patch_paths:
        text = patch.read_text(errors="ignore")
        hits.extend(f"{patch.name}:{marker}" for marker in ids if marker in text)
    commands = {
        "commit": ["git", "-C", str(repo), "log", "--all", "--format=%B"],
        "tag": ["git", "-C", str(repo), "tag", "--list"],
        "branch": ["git", "-C", str(repo), "branch", "--all", "--format=%(refname:short)"],
    }
    for surface, cmd in commands.items():
        text = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False).stdout
        hits.extend(f"{surface}:{marker}" for marker in ids if marker in text)
    if hits:
        raise LintError("fixture marker leak: " + ", ".join(sorted(set(hits))))


def same_enclosing_function(repo: Path, seed: dict, control: dict) -> bool:
    seed_loc = seed.get("location") or {}
    control_loc = control.get("location") or {}
    if seed.get("class") != control.get("class") or seed_loc.get("file") != control_loc.get("file"):
        return False
    seed_symbol = boundary.enclosing_symbol(repo, seed_loc.get("file"), seed_loc.get("line"))
    control_symbol = boundary.enclosing_symbol(repo, control_loc.get("file"), control_loc.get("line"))
    return bool(seed_symbol.symbol and seed_symbol.symbol == control_symbol.symbol)


def effective_clusters(controls: list[dict]) -> dict[str, int]:
    nominal = len({c.get("cluster") or c.get("id") for c in controls})
    effective = len({c.get("why_clean") or c.get("cluster") or c.get("id") for c in controls})
    return {"nominal": nominal, "effective": min(nominal, effective)}
