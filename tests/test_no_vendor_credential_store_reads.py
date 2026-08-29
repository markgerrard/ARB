"""README § Authentication guarantee, as a test: nothing under src/, scripts/, tools/, pi-extensions/
or .pi/ reads a vendor credential store. Credentials reach ARB only through the seat's own
environment (operator-exported API key, or CLAUDE_CODE_OAUTH_TOKEN minted with `claude setup-token`).

Forbidden in CODE lines (comments and docstrings are ignored): any reference to a vendor credential
FILE or keychain lookup. Vendor config (`config.toml`) and memory dirs are fine and are not matched.
Added 2026-08-30 after two such fallbacks were found and removed (confined-authorbench.sh,
confined-review.sh copying ~/.codex/auth.json; arb_eval/pipeline.py reading ~/.pi/agent/auth.json).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("src", "scripts", "tools", "pi-extensions", ".pi")
CODE_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".mjs", ".js", ".ts", ".tsx", ".go"}
FORBIDDEN = re.compile(
    r"auth\.json"                       # ~/.codex/auth.json, ~/.pi/agent/auth.json
    r"|\.credentials\.json"             # ~/.claude/.credentials.json
    r"|security\s+find-(generic|internet)-password"  # macOS keychain
    r"|\.codex/auth\b|\.pi/agent/auth\b|\.claude/\.credentials\b",
    re.I,
)
SKIP_PARTS = {"node_modules", ".venv", "__pycache__", "fixtures", "corpus", "dist"}


def _is_code_file(p: Path) -> bool:
    if any(part in SKIP_PARTS for part in p.parts):
        return False
    if p.suffix in CODE_SUFFIXES:
        return True
    if p.suffix == "" and p.is_file():
        try:
            return p.open("rb").read(2).startswith(b"#!")
        except OSError:
            return False
    return False


def code_lines(text: str, suffix: str):
    """Yield (lineno, line) for lines that are not comments or (Python) docstring bodies."""
    in_doc = False
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if suffix == ".py":
            quotes = s.count('"""') + s.count("'''")
            if in_doc:
                if quotes:
                    in_doc = False
                continue
            if quotes == 1 and (s.startswith('"""') or s.startswith("'''") or s.startswith(("r'''", 'r"""'))):
                in_doc = True
                continue
            if quotes == 2 and (s.startswith('"""') or s.startswith("'''")):
                continue
        if s.startswith("#") or s.startswith("//") or s.startswith("*"):
            continue
        yield i, line


def find_violations(root: Path) -> list[str]:
    out: list[str] = []
    for d in SCAN_DIRS:
        base = root / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or not _is_code_file(p):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in code_lines(text, p.suffix):
                if FORBIDDEN.search(line):
                    out.append(f"{p.relative_to(root)}:{n}: {line.strip()[:100]}")
    return out


def test_checker_can_fail(tmp_path: Path):
    """The guarantee test must be able to fail: a planted read is caught, a comment is not."""
    (tmp_path / "src").mkdir()
    bad = tmp_path / "src" / "leak.py"
    bad.write_text(
        '"""docstring mentions auth.json harmlessly"""\n'
        "# comment: ~/.codex/auth.json\n"
        'p = Path("~/.pi/agent/auth.json").expanduser()\n'
    )
    v = find_violations(tmp_path)
    assert v == [f"src/leak.py:3: {bad.read_text().splitlines()[2]}"], v


def test_no_vendor_credential_store_reads():
    v = find_violations(REPO)
    assert not v, "vendor credential store read in code (README § Authentication):\n" + "\n".join(v)
