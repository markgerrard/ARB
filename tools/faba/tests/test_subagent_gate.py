"""Unit tests for the SubagentStop record gate (subagent-FABA form, contract v2).

The gate script is executed in-process with FABA_POINTER_FILE pointing into
tmp_path. Contract v2: the gate validates decision-record.md content
(schema + prior-open coverage) — it needs no redis at all; publishing is the
driver's job. Tests cover the decision surface: no-op paths, block, pass,
fail-open loop protection, and the scoping regression from panel round 1.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
for p in (str(FABA), str(FABA / "subagent")):
    if p not in sys.path:
        sys.path.insert(0, p)

VALID_RECORD = """# FABA decision record — round 1
Subject: art-x | Prior record: none | Status: ok

**What the subject IS:** art-x is the toy subject artefact these tests bind to

## Round task
probe

## Findings
| id | severity | status | evidence (command, exit code, ref) | reopen-if |
|----|----------|--------|-------------------------------------|-----------|
| F1 | info | closed | ls, exit 0, abc123 | * |

## Recommendation
proceed

## Open items
none
"""


def run_gate(
    monkeypatch,
    tmp_path,
    *,
    pointer=None,
    hook_input=None,
    transcript=None,
    parent_transcript=None,
    record=None,
    artefact=None,
):
    """Execute gate main() with a controlled pointer/stdin/workspace record.

    ``transcript`` populates agent_transcript_path (the SUBAGENT's own transcript —
    the only valid scoping surface); ``parent_transcript`` populates the parent
    session's transcript_path, which the gate must IGNORE (panel r1 P1: it contains
    the brief, so it matches every sibling stop). ``record`` writes the workspace
    decision-record.md the content gate validates.
    """
    pointer_file = tmp_path / "pointer.json"
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    if record is not None:
        (workspace / "decision-record.md").write_text(record, encoding="utf-8")
    if artefact is not None:
        (workspace / "artefact.md").write_text(artefact, encoding="utf-8")
    if pointer is not None:
        pointer = dict(pointer)
        pointer.setdefault("workspace", str(workspace))
        pointer_file.write_text(json.dumps(pointer) + "\n", encoding="utf-8")
    monkeypatch.setenv("FABA_POINTER_FILE", str(pointer_file))

    payload = dict(hook_input or {})
    if transcript is not None:
        tpath = tmp_path / "agent-transcript.jsonl"
        tpath.write_text(transcript, encoding="utf-8")
        payload["agent_transcript_path"] = str(tpath)
    if parent_transcript is not None:
        ppath = tmp_path / "parent-transcript.jsonl"
        ppath.write_text(parent_transcript, encoding="utf-8")
        payload["transcript_path"] = str(ppath)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    sys.modules.pop("subagent_stop_gate", None)
    import subagent_stop_gate

    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)
    code = subagent_stop_gate.main()
    after = json.loads(pointer_file.read_text(encoding="utf-8")) if pointer_file.exists() else None
    return code, after, stderr.getvalue()


ARMED = {"request_id": "faba-sa-r1-abc123", "record_artefact_id": "art-faba-sa-abc123", "attempts": 0}
# The scoping token is the RECORD ARTEFACT ID: it is in the brief's round
# variables, so it reaches the round agent's transcript. The request_id does
# NOT reach the subagent under contract v2 (panel-faba-v2 grok F1).
OURS = "brief ... record artefact id the parent will publish under: art-faba-sa-abc123 ..."


def test_no_pointer_noops(monkeypatch, tmp_path):
    code, after, _ = run_gate(monkeypatch, tmp_path, pointer=None)
    assert code == 0
    assert after is None


def test_foreign_subagent_noops(monkeypatch, tmp_path):
    """A stop whose agent transcript lacks the minted request_id is not the FABA round."""
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(ARMED), transcript="some other subagent's transcript"
    )
    assert code == 0
    assert after["attempts"] == 0
    assert "gate" not in after


def test_parent_transcript_never_scopes(monkeypatch, tmp_path):
    """Regression for panel-faba-sa-20260718T060100Z-3037b6 P1 (3-seat convergent):
    the parent session's transcript_path contains the dispatch brief and thus the
    scoping token — a sibling subagent stop carrying only that field must NOT be gated."""
    code, after, _ = run_gate(
        monkeypatch,
        tmp_path,
        pointer=dict(ARMED),
        parent_transcript="parent session log ... art-faba-sa-abc123 ... (the brief)",
    )
    assert code == 0
    assert after["attempts"] == 0
    assert "gate" not in after


def test_request_id_alone_no_longer_scopes(monkeypatch, tmp_path):
    """Regression for panel-faba-v2-20260718T063314Z-c56303 grok F1: under
    contract v2 the request_id never reaches the subagent, so a transcript
    carrying ONLY the request_id (not the record id) is not evidence of our
    round — and more importantly, our own round agent's transcript carries the
    RECORD id, which must be sufficient to scope (covered by the OURS token in
    the pass/block tests above)."""
    code, after, _ = run_gate(
        monkeypatch,
        tmp_path,
        pointer=dict(ARMED),
        transcript="something mentioning faba-sa-r1-abc123 but not the record id",
    )
    assert code == 0
    assert "gate" not in after


def test_wrong_round_binding_blocks(monkeypatch, tmp_path):
    """panel-faba-v2 codex F1: a structurally valid record for the WRONG round
    must not pass when the pointer carries the binding expectation."""
    code, after, stderr = run_gate(
        monkeypatch,
        tmp_path,
        pointer=dict(ARMED, round=7, subject_artefact_id="art-x"),
        transcript=OURS,
        record=VALID_RECORD,  # says "round 1", subject art-x
    )
    assert code == 2
    assert "round" in stderr


def test_wrong_subject_binding_blocks(monkeypatch, tmp_path):
    code, after, stderr = run_gate(
        monkeypatch,
        tmp_path,
        pointer=dict(ARMED, round=1, subject_artefact_id="art-OTHER"),
        transcript=OURS,
        record=VALID_RECORD,
    )
    assert code == 2
    assert "subject" in stderr


def test_failed_status_record_is_stoppable(monkeypatch, tmp_path):
    """Crash equivalence: a schema-valid Status: failed record must let the
    agent STOP (the driver fails the round; the hook must not trap the agent)."""
    code, after, _ = run_gate(
        monkeypatch,
        tmp_path,
        pointer=dict(ARMED, round=1, subject_artefact_id="art-x"),
        transcript=OURS,
        record=VALID_RECORD.replace("Status: ok", "Status: failed"),
    )
    assert code == 0
    assert after["gate"] == "passed"


def test_unscopeable_stop_never_gates(monkeypatch, tmp_path):
    """No agent_transcript_path -> treated as foreign. Integrity lives in the
    driver (a never-fired gate publishes nothing), so unscopeable must not block."""
    code, after, _ = run_gate(monkeypatch, tmp_path, pointer=dict(ARMED))
    assert code == 0
    assert "gate" not in after


def test_valid_record_passes(monkeypatch, tmp_path):
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(ARMED), transcript=OURS, record=VALID_RECORD
    )
    assert code == 0
    assert after["gate"] == "passed"


def test_missing_record_blocks_with_exit_2(monkeypatch, tmp_path):
    code, after, stderr = run_gate(monkeypatch, tmp_path, pointer=dict(ARMED), transcript=OURS)
    assert code == 2
    assert after["attempts"] == 1
    assert "decision-record.md" in stderr


def test_schema_invalid_record_blocks_and_names_problems(monkeypatch, tmp_path):
    truncated = VALID_RECORD.split("## Recommendation")[0]
    code, after, stderr = run_gate(
        monkeypatch, tmp_path, pointer=dict(ARMED), transcript=OURS, record=truncated
    )
    assert code == 2
    assert "Recommendation" in stderr


def test_uncovered_prior_open_finding_blocks(monkeypatch, tmp_path):
    code, after, stderr = run_gate(
        monkeypatch,
        tmp_path,
        pointer=dict(ARMED, prior_open_ids=["PF9"]),
        transcript=OURS,
        record=VALID_RECORD,
    )
    assert code == 2
    assert "PF9" in stderr


def test_third_blocked_stop_fails_open(monkeypatch, tmp_path):
    """Loop protection: the gate never traps a round that cannot produce a record."""
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(ARMED, attempts=2), transcript=OURS
    )
    assert code == 0
    assert after["gate"] == "failed"
    assert after["attempts"] == 3


def test_passed_round_never_regates(monkeypatch, tmp_path):
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(ARMED, gate="passed"), transcript=OURS
    )
    assert code == 0
    assert after["gate"] == "passed"


def test_missing_workspace_fails_round_not_trap(monkeypatch, tmp_path):
    """A pointer with no workspace cannot be validated — fail the round loudly,
    never exit-2-trap the subagent."""
    pointer_file = tmp_path / "pointer.json"
    pointer_file.write_text(json.dumps(dict(ARMED, workspace=None)) + "\n", encoding="utf-8")
    monkeypatch.setenv("FABA_POINTER_FILE", str(pointer_file))
    tpath = tmp_path / "agent-transcript.jsonl"
    tpath.write_text(OURS, encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"agent_transcript_path": str(tpath)})))
    sys.modules.pop("subagent_stop_gate", None)
    import subagent_stop_gate

    code = subagent_stop_gate.main()
    after = json.loads(pointer_file.read_text(encoding="utf-8"))
    assert code == 0
    assert after["gate"] == "failed"
    assert "workspace" in after["gate_reason"]


def test_gate_log_residue_written(monkeypatch, tmp_path):
    """Every stop while armed appends to the round workspace's gate-log.jsonl."""
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(ARMED), transcript=OURS, record=VALID_RECORD
    )
    assert code == 0
    ws = Path(after["workspace"])
    logged = [json.loads(l) for l in (ws / "gate-log.jsonl").read_text().splitlines()]
    assert logged and "agent_transcript_path" in logged[0]["hook_input"]


def test_brief_carries_the_scoping_token():
    """panel-faba-v2 grok F1 structural pin: the composed production brief MUST
    contain the record artefact id (the gate's scoping token) — if this drifts,
    the gate treats our own round agent as foreign and every round dies."""
    import run_probe_round

    brief = run_probe_round.build_brief(
        "CONTRACT-BODY",
        workspace="/tmp/ws",
        round_number=1,
        artefact_id="art-x",
        subject_summary="art-x is the toy subject",
        prior_record_id="none",
        record_artefact_id="art-faba-sa-abc123",
        task="t",
        block_test=False,
    )
    assert "art-faba-sa-abc123" in brief
    assert "CONTRACT-BODY" in brief
    assert "art-x is the toy subject" in brief
    # NOTE: no "request_id absent" pin — the production workspace tempdir
    # prefix contains "faba-sa-r{round}-" so a substring check would be
    # misleading (panel-faba-v2-r2 cold-opus nit 1). Scoping correctness rests
    # on the full record id, pinned above.


def test_materialise_workspace_lists_prior_record(tmp_path):
    """panel-faba-v2-r2 codex F1 regression: a materialised prior record MUST
    be listed in round-input.json (the contract points the agent there) and its
    open ids become the coverage predicate."""
    import json as _json

    import run_probe_round

    prior = tmp_path / "prior.md"
    prior.write_text(
        "# FABA decision record — round 1\n"
        "Subject: art-x | Prior record: none | Status: ok\n\n"
        "## Round task\nt\n\n## Findings\n"
        "| id | severity | status | evidence (command, exit code, ref) |\n"
        "|----|----|----|----|\n"
        "| PF9 | P2 | open (noted) | cmd, exit 0, ref |\n\n"
        "## Recommendation\nr\n\n## Open items\nnone\n",
        encoding="utf-8",
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    open_ids = run_probe_round.materialise_workspace(
        ws,
        round_number=2,
        artefact_id="art-x",
        subject_summary="art-x is the toy subject",
        prior_record_id="art-prior",
        record_artefact_id="art-faba-sa-xyz",
        task="t",
        prior_record_file=prior,
    )
    round_input = _json.loads((ws / "round-input.json").read_text())
    assert round_input["prior_record_file"] == "prior-record.md"
    assert round_input["subject_summary"] == "art-x is the toy subject"
    assert (ws / "prior-record.md").exists()
    assert open_ids == ["PF9"]


def test_materialise_workspace_reopens_closed_finding_on_matching_change(tmp_path, monkeypatch):
    """Reopen consumer wiring (ADR open item #12): with --prior-basis given, a
    closed finding whose reopen-if scope matches a changed path joins the
    must-carry set and is surfaced to the agent in round-input.json."""
    import json as _json

    import faba_git
    import run_probe_round

    prior = tmp_path / "prior.md"
    prior.write_text(
        "# FABA decision record — round 1\n"
        "Subject: art-x | Prior record: none | Status: ok\n\n"
        "**What the subject IS:** toy\n\n"
        "## Round task\nt\n\n## Findings\n"
        "| id | severity | status | evidence (command, exit code, ref) | reopen-if |\n"
        "|----|----|----|----|----|\n"
        "| PF9 | P2 | open (noted) | cmd, exit 0, ref |  |\n"
        "| F1 | info | closed | pytest, exit 0, abc | tools/faba/ |\n\n"
        "## Recommendation\nr\n\n## Open items\nnone\n",
        encoding="utf-8",
    )
    # Pin the delta so the test doesn't depend on the process cwd's git state.
    monkeypatch.setattr(faba_git, "changed_paths_since", lambda basis, repo_root=None: ["tools/faba/x.py"])
    ws = tmp_path / "ws"
    ws.mkdir()
    must_carry = run_probe_round.materialise_workspace(
        ws,
        round_number=2,
        artefact_id="art-x",
        subject_summary="toy",
        prior_record_id="art-prior",
        record_artefact_id="art-faba-sa-xyz",
        task="t",
        prior_record_file=prior,
        prior_basis="HEAD~1",
    )
    round_input = _json.loads((ws / "round-input.json").read_text())
    assert must_carry == ["F1", "PF9"]  # closed F1 reopened by the tools/faba/ change
    assert round_input["must_carry_ids"] == ["F1", "PF9"]


def test_reopen_defaults_to_recorded_basis(tmp_path, monkeypatch):
    """Auto-basis (ADR open item #13): with no explicit --prior-basis, the reopen
    consumer defaults to the commit the prior record recorded in its Basis line."""
    import faba_git
    import run_probe_round

    prior = tmp_path / "prior.md"
    prior.write_text(
        "# FABA decision record — round 1\n"
        "Subject: art-x | Prior record: none | Status: ok\n"
        "Basis: DEADBEEF\n\n"
        "**What the subject IS:** toy\n\n"
        "## Round task\nt\n\n## Findings\n"
        "| id | severity | status | evidence (command, exit code, ref) | reopen-if |\n"
        "|----|----|----|----|----|\n"
        "| PF9 | P2 | open (noted) | cmd, exit 0, ref |  |\n"
        "| F1 | info | closed | pytest, exit 0, abc | tools/faba/ |\n\n"
        "## Recommendation\nr\n\n## Open items\nnone\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_changed(basis, repo_root=None):
        seen["basis"] = basis
        return ["tools/faba/x.py"] if basis == "DEADBEEF" else []

    monkeypatch.setattr(faba_git, "changed_paths_since", fake_changed)
    ws = tmp_path / "ws"
    ws.mkdir()
    must_carry = run_probe_round.materialise_workspace(
        ws,
        round_number=2,
        artefact_id="art-x",
        subject_summary="toy",
        prior_record_id="art-prior",
        record_artefact_id="art-faba-sa-xyz",
        task="t",
        prior_record_file=prior,
        prior_basis=None,  # no explicit basis -> falls back to the recorded one
    )
    assert seen["basis"] == "DEADBEEF"  # used the record's Basis line
    assert must_carry == ["F1", "PF9"]


def test_explicit_prior_basis_overrides_recorded(tmp_path, monkeypatch):
    """An explicit --prior-basis wins over the record's Basis line."""
    import faba_git
    import run_probe_round

    prior = tmp_path / "prior.md"
    prior.write_text(
        "# FABA decision record — round 1\n"
        "Subject: art-x | Prior record: none | Status: ok\n"
        "Basis: DEADBEEF\n\n"
        "**What the subject IS:** toy\n\n"
        "## Round task\nt\n\n## Findings\n"
        "| id | severity | status | evidence (command, exit code, ref) | reopen-if |\n"
        "|----|----|----|----|----|\n"
        "| F1 | info | closed | pytest, exit 0, abc | tools/faba/ |\n\n"
        "## Recommendation\nr\n\n## Open items\nnone\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_changed(basis, repo_root=None):
        seen["basis"] = basis
        return []

    monkeypatch.setattr(faba_git, "changed_paths_since", fake_changed)
    ws = tmp_path / "ws"
    ws.mkdir()
    run_probe_round.materialise_workspace(
        ws,
        round_number=2,
        artefact_id="art-x",
        subject_summary="toy",
        prior_record_id="art-prior",
        record_artefact_id="art-faba-sa-xyz",
        task="t",
        prior_record_file=prior,
        prior_basis="EXPLICIT",
    )
    assert seen["basis"] == "EXPLICIT"


def test_old_three_column_prior_record_still_yields_open_ids():
    """panel-faba-v2-r2 cold-opus nit 2: a pre-v2 3-column findings table must
    not silently produce an empty coverage predicate."""
    from faba_schema import open_finding_ids

    old = (
        "# FABA decision record — round 1\n"
        "Subject: art-x | Prior record: none | Status: ok\n\n"
        "## Findings\n"
        "| id | severity | status |\n"
        "|----|----|----|\n"
        "| F7 | P1 | open |\n"
    )
    assert open_finding_ids(old) == ["F7"]


# ---------------------------------------------------------------------------
# Author-round gate (Workflow C structural half, owner-directed 2026-07-19).
# Pointer kind="author": the content gate validates workspace artefact.md with
# the light authored-artefact check instead of the decision-record schema; no
# prior-open coverage, no round/subject binding. Absent kind = review — the
# entire existing suite above is the back-compat pin.

VALID_ARTEFACT = """# Design — toy subsystem frobnicator

**Change summary:** first draft against the certified round-2 record; adds the
frobnicator design with two alternatives considered and one rejected.

## Problem

Long enough body text that the stub floor does not reject this artefact. The
frobnicator must frob at least one nicator under load without dropping frobs.

## Design

Frob early, frob often. Rejected alternative: nicate first (fails under load).
"""

AUTHOR_ARMED = {
    "request_id": "faba-au-design-abc123",
    "record_artefact_id": "art-faba-au-abc123",
    "kind": "author",
    "attempts": 0,
}
AUTHOR_OURS = "brief ... artefact id the parent will publish under: art-faba-au-abc123 ..."


def test_author_valid_artefact_passes(monkeypatch, tmp_path):
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(AUTHOR_ARMED), transcript=AUTHOR_OURS, artefact=VALID_ARTEFACT
    )
    assert code == 0
    assert after["gate"] == "passed"


def test_author_missing_artefact_blocks_with_exit_2(monkeypatch, tmp_path):
    code, after, stderr = run_gate(
        monkeypatch, tmp_path, pointer=dict(AUTHOR_ARMED), transcript=AUTHOR_OURS
    )
    assert code == 2
    assert after["attempts"] == 1
    assert "artefact.md" in stderr


def test_author_stub_artefact_blocks(monkeypatch, tmp_path):
    code, after, stderr = run_gate(
        monkeypatch, tmp_path, pointer=dict(AUTHOR_ARMED), transcript=AUTHOR_OURS,
        artefact="# t\n\n**Change summary:** x\n\nshort",
    )
    assert code == 2
    assert "stub" in stderr or "short" in stderr


def test_author_artefact_without_change_summary_blocks(monkeypatch, tmp_path):
    no_summary = VALID_ARTEFACT.replace("**Change summary:**", "**Notes:**")
    code, after, stderr = run_gate(
        monkeypatch, tmp_path, pointer=dict(AUTHOR_ARMED), transcript=AUTHOR_OURS, artefact=no_summary
    )
    assert code == 2
    assert "change summary" in stderr.lower()


def test_author_artefact_without_title_blocks(monkeypatch, tmp_path):
    no_title = VALID_ARTEFACT.replace("# Design — toy subsystem frobnicator", "Design doc")
    code, after, stderr = run_gate(
        monkeypatch, tmp_path, pointer=dict(AUTHOR_ARMED), transcript=AUTHOR_OURS, artefact=no_title
    )
    assert code == 2
    assert "title" in stderr.lower()


def test_author_gate_ignores_decision_record(monkeypatch, tmp_path):
    """An author round with only a decision-record.md present is still artefact-less."""
    code, after, stderr = run_gate(
        monkeypatch, tmp_path, pointer=dict(AUTHOR_ARMED), transcript=AUTHOR_OURS, record=VALID_RECORD
    )
    assert code == 2
    assert "artefact.md" in stderr


def test_author_foreign_subagent_noops(monkeypatch, tmp_path):
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(AUTHOR_ARMED), transcript="unrelated subagent stop"
    )
    assert code == 0
    assert "gate" not in after


def test_author_third_blocked_stop_fails_open(monkeypatch, tmp_path):
    pointer = dict(AUTHOR_ARMED)
    pointer["attempts"] = 2
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=pointer, transcript=AUTHOR_OURS
    )
    assert code == 0
    assert after["gate"] == "failed"


def test_review_kind_explicit_still_validates_record(monkeypatch, tmp_path):
    pointer = dict(ARMED)
    pointer["kind"] = "review"
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=pointer, transcript=OURS, record=VALID_RECORD
    )
    assert code == 0
    assert after["gate"] == "passed"


# Revision hygiene (F14 route (c) hygiene tier, panel
# panel-f14c-design-20260720T033218Z-5ec74f, owner-scoped 2026-07-20): arming
# failures fail the round immediately (child can't fix them — no bounce);
# nothing-folded is child-fixable and joins the bounce loop. Non-revision
# author rounds above are the back-compat pin.

REVISION_ARMED = dict(AUTHOR_ARMED, revision=True)


def _stage_revision_workspace(tmp_path, *, prior=None, manifest=True):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    if prior is not None:
        (ws / "prior-record.md").write_text(prior, encoding="utf-8")
    if manifest:
        (ws / "staged-input-manifest.json").write_text(
            json.dumps({str(ws / "prior-record.md"): "0" * 64}) + "\n", encoding="utf-8"
        )
    return ws


def test_revision_without_prior_fails_immediately_no_bounce(monkeypatch, tmp_path):
    _stage_revision_workspace(tmp_path, prior=None)
    code, after, stderr = run_gate(
        monkeypatch, tmp_path, pointer=dict(REVISION_ARMED), transcript=AUTHOR_OURS,
        artefact=VALID_ARTEFACT + "\nfolded delta\n",
    )
    assert code == 0
    assert after["gate"] == "failed"
    assert "arming" in after["gate_reason"]
    assert after.get("attempts", 0) == 0
    assert "gate attempt" not in stderr


def test_revision_without_manifest_fails_immediately(monkeypatch, tmp_path):
    _stage_revision_workspace(tmp_path, prior=VALID_ARTEFACT, manifest=False)
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(REVISION_ARMED), transcript=AUTHOR_OURS,
        artefact=VALID_ARTEFACT + "\nfolded delta\n",
    )
    assert code == 0
    assert after["gate"] == "failed"
    assert "staged-input-manifest" in after["gate_reason"]


def test_revision_with_non_artefact_prior_fails_immediately(monkeypatch, tmp_path):
    _stage_revision_workspace(tmp_path, prior="just a note\n")
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(REVISION_ARMED), transcript=AUTHOR_OURS,
        artefact=VALID_ARTEFACT + "\nfolded delta\n",
    )
    assert code == 0
    assert after["gate"] == "failed"
    assert "publishable" in after["gate_reason"]


def test_revision_blemished_prior_is_admitted(monkeypatch, tmp_path):
    """The staged prior is the store as it IS — a v17/v19-class tail blemish in
    the CURRENT body must not fail the revision that exists to remove it."""
    _stage_revision_workspace(tmp_path, prior=VALID_ARTEFACT + "</content>\n")
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(REVISION_ARMED), transcript=AUTHOR_OURS,
        artefact=VALID_ARTEFACT + "\nfolded delta\n",
    )
    assert code == 0
    assert after["gate"] == "passed"


def test_revision_nothing_folded_bounces(monkeypatch, tmp_path):
    """Byte-identical output is child-fixable — bounce, don't hard-fail."""
    _stage_revision_workspace(tmp_path, prior=VALID_ARTEFACT)
    code, after, stderr = run_gate(
        monkeypatch, tmp_path, pointer=dict(REVISION_ARMED), transcript=AUTHOR_OURS,
        artefact=VALID_ARTEFACT,
    )
    assert code == 2
    assert after["attempts"] == 1
    assert "byte-identical" in stderr


def test_revision_valid_fold_passes(monkeypatch, tmp_path):
    _stage_revision_workspace(tmp_path, prior=VALID_ARTEFACT)
    code, after, _ = run_gate(
        monkeypatch, tmp_path, pointer=dict(REVISION_ARMED), transcript=AUTHOR_OURS,
        artefact=VALID_ARTEFACT + "\nChangelog v2: folded delta.\n",
    )
    assert code == 0
    assert after["gate"] == "passed"
    assert "revision-fold" in after["gate_reason"]


def test_author_brief_carries_the_scoping_token():
    """Same invariant as the review brief: the publish id must land in the
    author agent's transcript — it is the gate's only scoping surface."""
    import run_author_round

    brief = run_author_round.build_author_brief(
        "CONTRACT BODY",
        workspace="/tmp/ws",
        stage="design",
        subject_summary="toy",
        artefact_id="art-faba-au-tok123",
        prior_artefact_id="art-prior",
        prior_record_id="art-rec",
        task="write it",
    )
    assert "artefact id the parent will publish under: art-faba-au-tok123" in brief
    assert "CONTRACT BODY" in brief


def test_materialise_author_workspace_lists_prior_record(tmp_path):
    import run_author_round

    prior = tmp_path / "rec.md"
    prior.write_text("# FABA decision record — round 2\nstuff", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    run_author_round.materialise_author_workspace(
        ws,
        stage="spec",
        subject_summary="toy",
        artefact_id="art-au-1",
        prior_artefact_id="art-prior",
        prior_record_id="art-rec",
        task="t",
        prior_record_file=prior,
    )
    author_input = json.loads((ws / "author-input.json").read_text(encoding="utf-8"))
    assert author_input["prior_record_file"] == "prior-record.md"
    assert (ws / "prior-record.md").exists()
    assert author_input["artefact_id"] == "art-au-1"
    assert author_input["stage"] == "spec"
