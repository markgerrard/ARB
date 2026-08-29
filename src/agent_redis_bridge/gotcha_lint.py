"""gotcha-lint — graduate a recurring gotcha from "reviewers must remember it"
to a mechanical check.

Pattern D (docs/orchestrator-patterns.md) captures classes of bug that recur and
briefs reviewers on them. That is discipline: it relies on the orchestrator
remembering to brief, and the reviewer remembering to look. This module is the
structural half — the graduation:

    recurs >= GRADUATE_AT times  AND  is expressible as a pattern
        -> add it to the registry with enforce:true
        -> this linter fails CI when the pattern reappears

So a graduated gotcha is caught by construction, not by memory.

Registry: JSON, default `.gotchas.json` at the repo root.
    {
      "gotchas": [
        {
          "id": "pest-tothrow-throwable",
          "description": "Pest toThrow(\\Throwable) falls back to substring matching",
          "hint": "Use a concrete exception class in toThrow().",
          "pattern": "toThrow\\(\\\\?Throwable",   # Python regex
          "include": ["*.php"],                      # fnmatch globs (default: all)
          "exclude": ["*/vendor/*"],
          "occurrences": 3,                          # how many times it has bitten
          "enforce": true                            # graduated -> fail on match
        }
      ]
    }

Non-`enforce` gotchas are "briefing" stage: reported as warnings (exit 0) and
meant to be injected into review briefs (see scripts/review-brief --gotchas).

Scan target:
  --diff RANGE   scan only the ADDED lines of `git diff RANGE` (CI: catch NEW
                 occurrences without flagging pre-existing debt)
  (default)      scan all git-tracked files under --root

Exit code: 1 if any ENFORCED gotcha matches; 0 otherwise. `--list` prints the
registry; `--check-graduation` flags briefing gotchas whose occurrences have
reached the threshold (they should be promoted to enforce:true).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

GRADUATE_AT = 3  # the "bitten thrice" threshold Pattern D names


class RegistryError(Exception):
    pass


@dataclass
class Gotcha:
    id: str
    description: str
    pattern: str
    hint: str = ""
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    occurrences: int = 0
    enforce: bool = False

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern)

    def applies_to(self, path: str) -> bool:
        inc = not self.include or any(fnmatch.fnmatch(path, g) for g in self.include)
        exc = any(fnmatch.fnmatch(path, g) for g in self.exclude)
        return inc and not exc


@dataclass
class Hit:
    gotcha: Gotcha
    path: str
    lineno: int
    text: str


def load_registry(path: Path) -> list[Gotcha]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"registry not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry is not valid JSON: {exc}") from exc
    out: list[Gotcha] = []
    for i, raw in enumerate(data.get("gotchas", [])):
        for required in ("id", "description", "pattern"):
            if required not in raw:
                raise RegistryError(f"gotcha #{i} missing required field {required!r}")
        try:
            re.compile(raw["pattern"])
        except re.error as exc:
            raise RegistryError(f"gotcha {raw['id']!r} has an invalid pattern: {exc}") from exc
        out.append(
            Gotcha(
                id=raw["id"],
                description=raw["description"],
                pattern=raw["pattern"],
                hint=raw.get("hint", ""),
                include=tuple(raw.get("include", ())),
                exclude=tuple(raw.get("exclude", ())),
                occurrences=int(raw.get("occurrences", 0)),
                enforce=bool(raw.get("enforce", False)),
            )
        )
    return out


def iter_diff_added_lines(diff_text: str):
    """Yield (path, new_lineno, text) for each ADDED line in a unified diff."""
    path = None
    new_lineno = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else target[2:] if target[:2] in ("b/", "a/") else target
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            if path is not None:
                yield path, new_lineno, line[1:]
            new_lineno += 1
        elif not line.startswith("-"):
            new_lineno += 1


def scan_lines(gotchas: list[Gotcha], items) -> list[Hit]:
    hits: list[Hit] = []
    for path, lineno, text in items:
        for g in gotchas:
            if g.applies_to(path) and g.regex.search(text):
                hits.append(Hit(g, path, lineno, text.rstrip("\n")))
    return hits


def collect_diff(root: Path, diff_range: str):
    out = subprocess.run(
        ["git", "-C", str(root), "diff", "--unified=3", diff_range],
        capture_output=True, text=True, check=True,
    ).stdout
    yield from iter_diff_added_lines(out)


def collect_tree(root: Path):
    files = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    for rel in files:
        fpath = root / rel
        try:
            text = fpath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary / unreadable
        for i, line in enumerate(text.splitlines(), start=1):
            yield rel, i, line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gotcha-lint", description="Enforce graduated recurring gotchas.")
    ap.add_argument("--registry", default=".gotchas.json", help="path to the gotcha registry JSON")
    ap.add_argument("--root", default=".", help="repo root to scan (default: cwd)")
    ap.add_argument("--diff", metavar="RANGE", help="scan only added lines of `git diff RANGE`")
    ap.add_argument("--list", action="store_true", help="print the registry and exit")
    ap.add_argument("--check-graduation", action="store_true",
                    help="exit 1 if a briefing gotcha has reached the graduation threshold")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        gotchas = load_registry(Path(args.registry) if Path(args.registry).is_absolute() else root / args.registry)
    except RegistryError as exc:
        print(f"gotcha-lint: {exc}", file=sys.stderr)
        return 2

    if args.list:
        for g in gotchas:
            tag = "ENFORCED" if g.enforce else "briefing"
            print(f"[{tag}] {g.id} (seen {g.occurrences}x): {g.description}")
        return 0

    if args.check_graduation:
        due = [g for g in gotchas if not g.enforce and g.occurrences >= GRADUATE_AT]
        for g in due:
            print(f"gotcha-lint: '{g.id}' has recurred {g.occurrences}x (>= {GRADUATE_AT}) "
                  f"but is still briefing-only — graduate it (set enforce:true).", file=sys.stderr)
        return 1 if due else 0

    items = collect_diff(root, args.diff) if args.diff else collect_tree(root)
    hits = scan_lines(gotchas, items)

    enforced_hits = [h for h in hits if h.gotcha.enforce]
    briefing_hits = [h for h in hits if not h.gotcha.enforce]

    for h in briefing_hits:
        print(f"::warning:: [{h.gotcha.id}] {h.path}:{h.lineno}: {h.text.strip()}"
              + (f"  ({h.gotcha.hint})" if h.gotcha.hint else ""))
    for h in enforced_hits:
        print(f"{h.path}:{h.lineno}: gotcha '{h.gotcha.id}' — {h.gotcha.description}"
              + (f"\n    fix: {h.gotcha.hint}" if h.gotcha.hint else ""), file=sys.stderr)

    if enforced_hits:
        print(f"\ngotcha-lint: {len(enforced_hits)} enforced-gotcha match(es). "
              f"These bug-classes have graduated to a check — fix them.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
