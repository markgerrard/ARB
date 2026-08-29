from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from . import report, rubric
from .factkey import load_factkey
from .normalize import stage_for_judges


class Recorder:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)

    def write(self, event: dict[str, Any]) -> None:
        report.guard(event)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        row = dict(event)
        row.setdefault("ts", time.time())
        row.setdefault("run_id", self.run_dir.name)
        with (self.run_dir / "events.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def memory_key(author_seat, author_model_version, corpus_version, run_id) -> str:
    return f"author-bench/{author_seat}/{author_model_version}/{corpus_version}/{run_id}"


def _plain(value):
    if is_dataclass(value):
        return {key: _plain(val) for key, val in asdict(value).items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(val) for key, val in value.items()}
    return value


def _bundle_dir(run_dir: Path, brief_id: str | None) -> Path:
    if brief_id is not None and (run_dir / "bundles" / brief_id).exists():
        return run_dir / "bundles" / brief_id
    return run_dir / "bundle"


def _mechanical_for(run_dir: Path, seat: str, brief_id: str | None, raw_path: Path) -> list[dict[str, Any]]:
    factkey = load_factkey(_bundle_dir(run_dir, brief_id) / "fact_key.yaml")
    effective_brief = brief_id or factkey.brief_id
    raw = raw_path.read_text(encoding="utf-8")
    staging = run_dir / "staging" / seat / effective_brief
    staged = stage_for_judges(raw, staging)
    normalized = (staging / "draft.md").read_text(encoding="utf-8") if staged.staged else ""
    r1 = rubric.check_r1(normalized, factkey, run_dir / "export")
    r4 = rubric.count_r4(normalized)
    return [
        {
            "event": "blinding_scan",
            "seat": seat,
            "hits": [],
            "result": "quarantined" if staged.quarantined else "clean",
        },
        {
            "event": "mechanical_r1",
            "seat": seat,
            "brief_id": factkey.brief_id,
            "cited": r1.cited,
            "true": r1.true,
            "fabricated": r1.fabricated,
            "citations": _plain(r1.citations),
        },
        {
            "event": "mechanical_r4",
            "seat": seat,
            "brief_id": factkey.brief_id,
            "obligations": r4.obligations,
            "with_red_condition": r4.with_red_condition,
        },
    ]


def rederive_mechanical(run_dir) -> dict[str, list[dict[str, Any]]]:
    run_dir = Path(run_dir)
    out: dict[str, list[dict[str, Any]]] = {}
    for draft_dir in sorted((run_dir / "drafts").iterdir()):
        if not draft_dir.is_dir():
            continue
        nested = sorted(
            brief_dir for brief_dir in draft_dir.iterdir()
            if brief_dir.is_dir() and (brief_dir / "raw.md").exists()
        )
        if nested:
            for brief_dir in nested:
                out.setdefault(draft_dir.name, []).extend(_mechanical_for(run_dir, draft_dir.name, brief_dir.name, brief_dir / "raw.md"))
        elif (draft_dir / "raw.md").exists():
            out[draft_dir.name] = _mechanical_for(run_dir, draft_dir.name, None, draft_dir / "raw.md")
    return out


def new_judging_run(run_dir, judging_run_id: str) -> Path:
    path = Path(run_dir) / "judging-reruns" / judging_run_id
    if path.exists():
        raise FileExistsError(path)
    path.mkdir(parents=True)
    return path
