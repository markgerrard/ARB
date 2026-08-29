from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CORPUS_ROOT = ROOT / "h2_corpus"
REQUIRED = {"files", "diff", "changed_paths", "phase_input", "expected"}


def load_case(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        case = json.load(fh)
    validate_case(case)
    return case


def iter_corpus() -> Iterator[tuple[str, dict]]:
    for path in sorted(CORPUS_ROOT.glob("*/*/case.json")):
        yield path.relative_to(CORPUS_ROOT).parent.as_posix(), load_case(path)


def validate_case(case: dict) -> None:
    missing = REQUIRED - set(case)
    if missing:
        raise AssertionError(f"case missing required keys: {sorted(missing)}")
    if not isinstance(case["files"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in case["files"].items()):
        raise AssertionError("files must be a string map")
    if not isinstance(case["changed_paths"], list) or not all(isinstance(path, str) for path in case["changed_paths"]):
        raise AssertionError("changed_paths must be a list[str]")
    if not isinstance(case["phase_input"], dict):
        raise AssertionError("phase_input must be an object")
    expected = case["expected"]
    if not isinstance(expected, dict):
        raise AssertionError("expected must be an object")
    for key in ("status", "derived", "record", "graduation"):
        if key not in expected:
            raise AssertionError(f"expected missing {key}")


def assert_changed_paths_match_diff(case: dict) -> None:
    headers = set()
    for line in case["diff"].splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                headers.add(parts[3].removeprefix("b/"))
        elif line.startswith("+++ b/"):
            headers.add(line.removeprefix("+++ b/"))
    changed = set(case["changed_paths"])
    if headers != changed:
        raise AssertionError(f"diff headers {sorted(headers)} != changed_paths {sorted(changed)}")
