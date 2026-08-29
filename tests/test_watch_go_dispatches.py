from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Iterable


SCRIPT = Path(__file__).parents[1] / "scripts" / "watch-go-dispatches"


def _run(
    tmp_path: Path,
    reply: dict | None,
    state: str,
    *,
    rows: Iterable[tuple[str, str, dict | None]] | None = None,
    trailing_newline: bool = True,
) -> subprocess.CompletedProcess[str]:
    manifest = tmp_path / "manifest"
    manifest_rows: list[str] = []
    for index, (label, row_state, row_reply) in enumerate(rows or [("seat", state, reply)]):
        out = tmp_path / f"seat-{index}.out"
        if row_reply is not None:
            out.write_text(json.dumps(row_reply), encoding="utf-8")
        manifest_rows.append(f"{label} 0 task-{index} {out}")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    redis = bindir / "redis-cli"
    state_cases = "\n".join(
        f"  *task-{index}:status*) echo {row_state} ;;"
        for index, (_, row_state, _) in enumerate(rows or [("seat", state, reply)])
    )
    redis.write_text(f"#!/bin/sh\ncase \"$*\" in\n{state_cases}\n  *) echo ;;\nesac\n", encoding="utf-8")
    redis.chmod(0o755)
    text = "\n".join(manifest_rows)
    manifest.write_text(text + ("\n" if trailing_newline else ""), encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "WATCH_SLEEP_SECS": "0",
        "WATCH_MIN_WAIT_SECS": "0",
        "WATCH_MAX_WAIT_SECS": "2",
    }
    return subprocess.run([str(SCRIPT), str(manifest)], text=True, capture_output=True, env=env, check=False)


def test_ok_false_is_terminal_failure_not_success(tmp_path: Path) -> None:
    result = _run(tmp_path, {"ok": False, "error": "sender rejected"}, "queued")

    assert result.returncode != 0
    assert "FAILED" in result.stdout


def test_ok_true_requires_terminal_success_state(tmp_path: Path) -> None:
    result = _run(tmp_path, {"ok": True, "result": "done"}, "completed")

    assert result.returncode == 0
    assert "ok=1" in result.stdout


def test_ok_true_with_nonterminal_state_never_succeeds(tmp_path: Path) -> None:
    result = _run(tmp_path, {"ok": True, "result": "done"}, "running")

    assert result.returncode == 124
    assert "ok=1" not in result.stdout


def test_empty_manifest_is_rejected(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest"
    manifest.write_text("# no seats\n", encoding="utf-8")

    result = subprocess.run([str(SCRIPT), str(manifest)], text=True, capture_output=True, check=False)

    assert result.returncode == 2
    assert "no seats" in result.stderr


def test_pending_first_seat_does_not_hide_later_failure(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        None,
        "",
        rows=[
            ("pending", "running", {"ok": True}),
            ("failed", "queued", {"ok": False, "error": "rejected"}),
        ],
    )

    assert result.returncode == 1
    assert "failed" in result.stdout


def test_terminal_failure_without_output_fails_immediately(tmp_path: Path) -> None:
    result = _run(tmp_path, None, "failed")

    assert result.returncode == 1
    assert "FAILED" in result.stdout


def test_partial_json_is_pending_until_redis_is_terminal(tmp_path: Path) -> None:
    out = tmp_path / "seat-0.out"
    out.write_text('{"ok":', encoding="utf-8")
    result = _run(tmp_path, None, "running")

    assert result.returncode == 124


def test_manifest_last_row_without_newline_is_checked(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"ok": False},
        "failed",
        trailing_newline=False,
    )

    assert result.returncode == 1
