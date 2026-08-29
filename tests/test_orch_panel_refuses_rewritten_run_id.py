"""arb-orch-panel must refuse a --run-id it is going to rewrite.

The tool dispatches each seat under `f"{run_id}-{slug}"` and has no `arb-audit-emit` or
`arb-audit-close-request` call site, so a caller who supplies a run_id gets N independent
single-vote runs -- no manifest, no verdict -- while every seat reports success. The
reconcile gate cannot catch it: with per-seat runs there is no roster and no close is ever
requested, so there is nothing to refuse.

Measured before the fix: 92 orphan seat runs across 28 panel bases from 2026-07-18, none
carrying a dispatch or verdict row. Write-up:
docs/defect-classes/run-id-silently-rewritten.md

Driven as a SUBPROCESS on purpose. Importing this script executes its CLI, so an in-process
test cannot reach the predicate -- and the CLI contract (exit code + actionable stderr) is
the thing callers actually meet. The negative cases carry as much weight as the positive
one: a refusal that also fired without --run-id, or under --no-audit-panel where no votes
are written at all, would break ordinary unaudited parallel dispatch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
PANEL = REPO / "scripts" / "arb-orch-panel"
REFUSAL = "refusing --run-id"


def _run(*args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    artifact = tmp_path / "subject.md"
    artifact.write_text("# subject\n", encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(PANEL), "--stage", "design", "--round", "1",
         "--artifact", str(artifact), "--repo", str(REPO), *args],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )


def test_explicit_run_id_with_audit_enabled_is_refused(tmp_path):
    r = _run("--run-id", "panel-acceptance-20260810T111117Z-rf7b0ec", tmp_path=tmp_path)
    assert REFUSAL in r.stderr, f"expected a refusal, got rc={r.returncode}\n{r.stderr[:400]}"
    assert r.returncode == 2, f"refusal must exit 2, got {r.returncode}"


def test_refusal_names_the_rewrite_and_the_working_procedure(tmp_path):
    """A refusal that does not say what to do instead just moves the dead end."""
    err = _run("--run-id", "anything", tmp_path=tmp_path).stderr
    assert "<run-id>-<seat-slug>" in err, "must state that the id is rewritten per seat"
    assert "no manifest and no verdict" in err, "must state what is missing from the record"
    for step in ("arb-audit-emit", "agent-dispatch --audit-panel", "arb-audit-close-request"):
        assert step in err, f"refusal must name the working procedure step: {step}"


@pytest.mark.parametrize(
    "args,why",
    [
        ((), "no run_id: the auto-generated label promises the caller nothing"),
        (("--run-id", "some-id", "--no-audit-panel"),
         "--no-audit-panel writes no votes, so no orphan run can be created"),
        (("--no-audit-panel",), "neither a run_id nor votes"),
    ],
)
def test_does_not_refuse_ordinary_unaudited_dispatch(args, why, tmp_path):
    """These must fail LATER (roster, seats, whatever) -- never with this refusal."""
    r = _run(*args, tmp_path=tmp_path)
    assert REFUSAL not in r.stderr, f"{why}\nunexpected refusal:\n{r.stderr[:400]}"


def test_refusal_precedes_artifact_validation(tmp_path):
    """Proves the guard runs before any work, including the --detach re-invocation.

    A nonexistent artifact would normally die with 'artifact missing'. If the run_id
    refusal is wired first, it wins -- which is what stops a detached child from being
    spawned under an id that will not survive.
    """
    r = subprocess.run(
        [sys.executable, str(PANEL), "--stage", "design", "--round", "1",
         "--artifact", str(tmp_path / "does-not-exist.md"), "--repo", str(REPO),
         "--run-id", "x", "--detach"],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )
    assert REFUSAL in r.stderr, f"guard must precede artifact validation\n{r.stderr[:400]}"
    assert "artifact missing" not in r.stderr
