#!/usr/bin/env python3
"""Generate a rev-2 real correctness scenario after Task 10 corpus authoring."""
from __future__ import annotations

import json
from pathlib import Path

from real_corpus_lint import assert_marker_free, effective_clusters

CLS = "correctness"
REPO = Path(__file__).resolve().parent / "repos" / "floor-real-correctness"
SRC = Path(__file__).resolve().parent / "src" / "real-correctness"
OUT = Path(__file__).resolve().parents[1] / "scenarios" / "floor-real-correctness.json"


def main() -> int:
    patches = sorted(SRC.glob("*.patch"))
    assert_marker_free(REPO, patches, [])
    controls: list[dict] = []
    desc = effective_clusters(controls)
    scenario = {
        "schema_rev": 2,
        "id": "floor-real-correctness",
        "description": f"Real correctness corpus placeholder; nominal={desc['nominal']} effective={desc['effective']}",
        "subject": {"repo": "../fixtures/repos/floor-real-correctness", "base": "BASE", "head": "HEAD", "languages": ["python"]},
        "seeded_defects": [],
        "control_loci": controls,
        "panel": [{"seat": "codex", "model": "gpt-5.5", "harness": "codex"}],
    }
    OUT.write_text(json.dumps(scenario, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
