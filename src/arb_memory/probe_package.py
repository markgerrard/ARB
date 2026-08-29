"""Slice 1e-i: probe-package (`probe_package_v: 1`) schema validator.

Design: docs/superpowers/specs/2026-08-01-slice1e-probe-package-design.md §2.
Gates SHAPE, not truth — the mechanical truth checks (excerpt-in-tree, red
actually red) live in the build tool and deep check. Idiom follows
tools/faba/faba_schema.py: accumulate ``problems``, never raise; duplicate
JSON keys refused; unknown keys refused per section.

Path rules are this module's own ``validate_package_path`` — the hostility
posture of ``brief_hydrate.validate_artefact_id`` (which rejects path
separators outright and so cannot validate relative paths), applied to
relative paths with allowed roots ``tests/``, ``fixtures/``, and exactly
``conftest.py``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

PACKAGE_MAX_BYTES = 512 * 1024

_TOP_KEYS = {
    "probe_package_v",
    "claim_id",
    "target",
    "files",
    "pytest_args",
    "runtime",
    "red",
    "defect",
    "notes",
}
_TARGET_KEYS = {"commit", "origin_hint"}
_FILE_KEYS = {"mode", "content"}
_RUNTIME_KEYS = {"python", "pytest"}
_RED_KEYS = {"expect_failed", "run_log", "tree_provenance_stamp", "exit_code"}
_DEFECT_KEYS = {"path", "excerpt"}

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_PACKAGE_ROOTS = ("tests", "fixtures")
_ALLOWED_BARE_ARGS = {"-q", "-x"}


@dataclass
class RecordCheck:
    ok: bool
    package: dict | None = None
    problems: list[str] = field(default_factory=list)


def _reject_duplicate_keys(pairs):
    """object_pairs_hook: refuse JSON objects with duplicate keys."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _segment_problems(path: str, label: str) -> list[str]:
    """Hostility rules shared by package paths and defect.path."""
    problems: list[str] = []
    if "\x00" in path:
        problems.append(f"NUL in {label}")
    if "\\" in path:
        problems.append(f"backslash in {label}")
    if path.startswith("/"):
        problems.append(f"absolute {label}")
    for segment in path.split("/"):
        if segment == "..":
            problems.append(f"traversal segment in {label}")
        elif segment in ("", "."):
            problems.append(f"empty or dot segment in {label}")
        elif segment.startswith("-"):
            problems.append(f"option-shaped segment in {label}")
    return sorted(set(problems))


def validate_package_path(path) -> list[str]:
    """Return problems for a package file path; empty list means valid.

    Rules (design §2.1): relative; no ``..``, NUL, absolute, backslash, or
    option-shaped segments; must live under ``tests/`` or ``fixtures/`` with
    at least one component below the root, or be exactly ``conftest.py``.
    """
    if not isinstance(path, str) or not path.strip():
        return ["package path must be a nonblank string"]
    problems = _segment_problems(path, "package path")
    if problems:
        return problems
    segments = path.split("/")
    in_root = segments[0] in _PACKAGE_ROOTS and len(segments) >= 2
    if not in_root and path != "conftest.py":
        problems.append(
            "package path outside allowed roots (tests/, fixtures/, conftest.py)"
        )
    return problems


def _nonblank_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _section(payload, key, allowed, problems) -> dict | None:
    """Fetch a required object section; report unknown/missing keys."""
    value = payload.get(key)
    if not isinstance(value, dict):
        problems.append(f"{key} must be an object")
        return None
    unknown = sorted(set(value) - allowed)
    if unknown:
        problems.append(f"unknown {key} key(s): {unknown}")
    missing = sorted(allowed - set(value))
    if missing:
        problems.append(f"missing {key} key(s): {missing}")
    return value


def _check_files(files, problems) -> None:
    if not isinstance(files, dict) or not files:
        problems.append("files must be a non-empty object")
        return
    for path, entry in files.items():
        prefix = f"files[{path!r}]"
        for path_problem in validate_package_path(path):
            problems.append(f"{prefix}: {path_problem}")
        if not isinstance(entry, dict):
            problems.append(f"{prefix}: entry must be an object")
            continue
        unknown = sorted(set(entry) - _FILE_KEYS)
        if unknown:
            problems.append(f"{prefix}: unknown key(s) {unknown}")
        missing = sorted(_FILE_KEYS - set(entry))
        if missing:
            problems.append(f"{prefix}: missing key(s) {missing}")
        if "mode" in entry and entry["mode"] not in ("create", "replace"):
            problems.append(
                f"{prefix}: mode must be create|replace, got {entry['mode']!r}"
            )
        if "content" in entry:
            content = entry["content"]
            encodable = isinstance(content, str)
            if encodable:
                try:
                    content.encode("utf-8")
                except UnicodeEncodeError:
                    encodable = False
            if not encodable:
                problems.append(
                    f"{prefix}: files_not_text — content must be UTF-8 text"
                )


def _check_pytest_args(args, problems) -> None:
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        problems.append("pytest_args must be a list of strings")
        return
    i = 0
    while i < len(args):
        token = args[i]
        if token in _ALLOWED_BARE_ARGS:
            i += 1
        elif token == "-p":
            if i + 1 < len(args) and args[i + 1] == "no:cacheprovider":
                i += 2
            else:
                problems.append(f"pytest_args_not_allowlisted: {token!r}")
                i += 1
        else:
            problems.append(f"pytest_args_not_allowlisted: {token!r}")
            i += 1


def _check_red(red, problems) -> None:
    if "expect_failed" in red:  # a missing key is _section's report, not ours
        expect_failed = red["expect_failed"]
        if (
            not isinstance(expect_failed, list)
            or not expect_failed
            or not all(_nonblank_str(n) for n in expect_failed)
        ):
            problems.append(
                "red.expect_failed must be a non-empty list of node ids"
            )
        else:
            for idx, node_id in enumerate(expect_failed):
                if "::" not in node_id:
                    problems.append(
                        f"red.expect_failed[{idx}] must be a node id containing '::'"
                    )
            if len(set(expect_failed)) != len(expect_failed):
                problems.append("red.expect_failed contains duplicate node id(s)")
    if "run_log" in red and not _nonblank_str(red["run_log"]):
        problems.append("red.run_log must be a nonblank string")
    if "tree_provenance_stamp" in red and not _nonblank_str(
        red["tree_provenance_stamp"]
    ):
        problems.append("red.tree_provenance_stamp must be a nonblank string")
    if "exit_code" in red:
        exit_code = red["exit_code"]
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            problems.append("red.exit_code must be an integer")


def _check_defect(defect, problems) -> None:
    path = defect.get("path")
    if "path" in defect:
        if not _nonblank_str(path):
            problems.append("defect.path must be a nonblank string")
        else:
            problems.extend(_segment_problems(path, "defect.path"))
    if "excerpt" in defect and not _nonblank_str(defect["excerpt"]):
        problems.append("defect.excerpt must be a nonblank string")


def validate_probe_package(text: str) -> RecordCheck:
    """Shape-gate a probe-package JSON document (design §2.1/§2.2)."""
    problems: list[str] = []

    size = len(text.encode("utf-8", errors="surrogatepass"))
    if size > PACKAGE_MAX_BYTES:
        problems.append(f"package_too_large: {size} bytes > {PACKAGE_MAX_BYTES}")

    payload = None
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        problems.append(f"malformed package JSON: {exc}")

    if payload is not None and not isinstance(payload, dict):
        problems.append("package must be a JSON object")
        payload = None
    if payload is None:
        return RecordCheck(ok=False, problems=problems)

    unknown = sorted(set(payload) - _TOP_KEYS)
    if unknown:
        problems.append(f"unknown package key(s): {unknown}")
    missing = sorted(_TOP_KEYS - set(payload))
    if missing:
        problems.append(f"missing package key(s): {missing}")

    if "probe_package_v" in payload and payload["probe_package_v"] != 1:
        problems.append(
            f"probe_package_v must be 1, got {payload['probe_package_v']!r}"
        )
    if "claim_id" in payload and not _nonblank_str(payload["claim_id"]):
        problems.append("claim_id must be a nonblank string")

    if "target" in payload:
        target = _section(payload, "target", _TARGET_KEYS, problems)
        if target is not None:
            commit = target.get("commit")
            if "commit" in target and (
                not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit)
            ):
                problems.append(
                    f"target.commit must be a 40-hex commit OID, got {commit!r}"
                )
            if "origin_hint" in target and not isinstance(
                target["origin_hint"], str
            ):
                problems.append("target.origin_hint must be a string")

    if "files" in payload:
        _check_files(payload["files"], problems)

    if "pytest_args" in payload:
        _check_pytest_args(payload["pytest_args"], problems)

    if "runtime" in payload:
        runtime = _section(payload, "runtime", _RUNTIME_KEYS, problems)
        if runtime is not None:
            for key in sorted(_RUNTIME_KEYS):
                if key in runtime and not _nonblank_str(runtime[key]):
                    problems.append(f"runtime.{key} must be a nonblank string")

    if "red" in payload:
        red = _section(payload, "red", _RED_KEYS, problems)
        if red is not None:
            _check_red(red, problems)

    if "defect" in payload:
        defect = _section(payload, "defect", _DEFECT_KEYS, problems)
        if defect is not None:
            _check_defect(defect, problems)

    if "notes" in payload and not isinstance(payload["notes"], str):
        problems.append("notes must be a string")

    ok = not problems
    return RecordCheck(ok=ok, package=payload if ok else None, problems=problems)
