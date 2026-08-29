import json
import pytest
from psycopg.types.json import Jsonb
from arb_memory.panel_audit import reconcile

ROSTER = ["seat:codex", "seat:cold-opus"]

def _row(conn, run_id, seq, kind, payload):
    conn.execute(
        "INSERT INTO audit_events (run_id, seq, source, kind, payload) VALUES (%s,%s,%s,%s,%s)",
        (run_id, seq, "orchestrator", kind, Jsonb(payload)),
    )

def _good_panel(conn, run_id):
    _row(conn, run_id, 1, "dispatch", {"kind":"dispatch","roster":ROSTER,"task":"t","branch":"dev"})
    _row(conn, run_id, 2, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    _row(conn, run_id, 3, "vote", {"kind":"vote","actor":"seat:cold-opus","stance":"needs-changes"})

VERDICT = {"kind":"verdict","roster":ROSTER,
           "stances":{"seat:codex":"approve","seat:cold-opus":"needs-changes"}}

def test_clean_panel_reconciles(scratch):
    rid = "panel-clean"
    _good_panel(scratch, rid)
    out = reconcile(scratch, rid, VERDICT)
    assert out == {"ok": True, "incomplete": False, "gaps": []}

def test_missing_manifest_fails(scratch):
    rid = "panel-nomanifest"
    _row(scratch, rid, 1, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    out = reconcile(scratch, rid, VERDICT)
    assert out["ok"] is False and any("manifest" in g for g in out["gaps"])

def test_manifest_not_seq1_fails(scratch):
    rid = "panel-lateman"
    _row(scratch, rid, 1, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    _row(scratch, rid, 2, "vote", {"kind":"vote","actor":"seat:cold-opus","stance":"needs-changes"})
    _row(scratch, rid, 3, "dispatch", {"kind":"dispatch","roster":ROSTER,"task":"t","branch":"dev"})
    out = reconcile(scratch, rid, VERDICT)
    assert out["ok"] is False and any("seq" in g or "precede" in g for g in out["gaps"])

def test_missing_vote_fails(scratch):
    rid = "panel-missvote"
    _row(scratch, rid, 1, "dispatch", {"kind":"dispatch","roster":ROSTER,"task":"t","branch":"dev"})
    _row(scratch, rid, 2, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    out = reconcile(scratch, rid, VERDICT)
    assert out["ok"] is False and any("cold-opus" in g for g in out["gaps"])

def test_unrostered_vote_fails(scratch):
    rid = "panel-extra"
    _good_panel(scratch, rid)
    _row(scratch, rid, 4, "vote", {"kind":"vote","actor":"seat:ghost","stance":"approve"})
    out = reconcile(scratch, rid, VERDICT)
    assert out["ok"] is False and any("ghost" in g for g in out["gaps"])

def test_verdict_stance_mismatch_fails(scratch):
    rid = "panel-mismatch"
    _good_panel(scratch, rid)
    bad = {"kind":"verdict","roster":ROSTER,
           "stances":{"seat:codex":"approve","seat:cold-opus":"approve"}}  # voted needs-changes
    out = reconcile(scratch, rid, bad)
    assert out["ok"] is False and any("cold-opus" in g for g in out["gaps"])

def test_verdict_roster_mismatch_fails(scratch):
    rid = "panel-roster-mismatch"
    _good_panel(scratch, rid)
    bad = {"kind":"verdict","roster":["seat:codex"],
           "stances":{"seat:codex":"approve","seat:cold-opus":"needs-changes"}}
    out = reconcile(scratch, rid, bad)
    assert out["ok"] is False and any("roster" in g for g in out["gaps"])

def test_timed_out_vote_satisfies_roster(scratch):
    rid = "panel-timeout"
    _row(scratch, rid, 1, "dispatch", {"kind":"dispatch","roster":ROSTER,"task":"t","branch":"dev"})
    _row(scratch, rid, 2, "vote", {"kind":"vote","actor":"seat:codex","stance":"approve"})
    _row(scratch, rid, 3, "vote", {"kind":"vote","actor":"seat:cold-opus","stance":"timed-out"})
    v = {"kind":"verdict","roster":ROSTER,
         "stances":{"seat:codex":"approve","seat:cold-opus":"timed-out"}}
    out = reconcile(scratch, rid, v)
    assert out == {"ok": True, "incomplete": False, "gaps": []}
