"""CLI-boundary regression tests for scripts/panel-run.

The library helpers are covered by test_panel_run.py; these lock the CLI's
exit-code contract — the operator-facing fail-loud paths that protect the
audit/vote evidence plane:
  - `vote`    with no parseable fenced stance  -> exit 3, NO vote emitted
  - `verdict` that does not reconcile          -> exit 4, NO verdict emitted (anti-laundering)
  - `verdict` with malformed --stances JSON    -> exit 2, NO verdict emitted
  - `verdict` happy path (reconcile ok)        -> exit 0, verdict emitted
  - every `verdict` invocation warns that it is local-only break-glass
Mirrors tests/arb_memory/test_arb_panel_vote.py: load the extensionless script
via SourceFileLoader and inject fake redis/conn so nothing touches a real bus.
"""
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path

_path = Path(__file__).parents[1] / "scripts" / "panel-run"
_spec = importlib.util.spec_from_file_location(
    "panel_run_cli", _path,
    loader=importlib.machinery.SourceFileLoader("panel_run_cli", str(_path)))
pr_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_cli)


class FakeRedis:
    def __init__(self):
        self.adds = []

    def xadd(self, stream, fields, **kw):
        self.adds.append((stream, fields))
        return b"1-0"

    def incr(self, k):
        return 1

    def expire(self, k, s):
        return True


def test_vote_no_fenced_stance_exits_3_no_emit(capsys):
    r = FakeRedis()
    rc = pr_cli.main(["vote", "--run-id", "R", "--actor", "seat:cold-opus"],
                     stdin=io.StringIO("just prose, no vote block"),
                     redis_factory=lambda: r)
    assert rc == 3
    assert r.adds == []  # never invents a vote
    assert "no valid fenced vote" in capsys.readouterr().err


def test_verdict_refuses_when_not_reconciled_exit_4_no_emit(monkeypatch, capsys):
    r = FakeRedis()
    monkeypatch.setattr(pr_cli.panel_run, "reconcile",
                        lambda conn, rid, payload, **kw: {"ok": False, "incomplete": False,
                                                          "gaps": ["seat 'agy' never voted"]})
    rc = pr_cli.main(["verdict", "--run-id", "R", "--roster", "codex,agy",
                      "--stances", '{"codex":"approve","agy":"block"}'],
                     redis_factory=lambda: r, conn_factory=lambda: object())
    assert rc == 4
    assert r.adds == []  # a non-reconciling verdict is NEVER emitted (anti-laundering)
    stderr = capsys.readouterr().err
    assert "break-glass" in stderr
    assert "REFUSED" in stderr


def test_verdict_malformed_stances_json_exits_2_no_emit(capsys):
    r = FakeRedis()
    rc = pr_cli.main(["verdict", "--run-id", "R", "--roster", "codex",
                      "--stances", "{not valid json"],
                     redis_factory=lambda: r, conn_factory=lambda: object())
    assert rc == 2
    assert r.adds == []
    assert "not valid JSON" in capsys.readouterr().err


def test_verdict_emits_when_reconciled_exit_0(monkeypatch, capsys):
    r = FakeRedis()
    monkeypatch.setattr(pr_cli.panel_run, "reconcile",
                        lambda conn, rid, payload, **kw: {"ok": True, "incomplete": False, "gaps": []})
    rc = pr_cli.main(["verdict", "--run-id", "R", "--roster", "codex",
                      "--stances", '{"codex":"approve"}', "--overall", "APPROVE"],
                     redis_factory=lambda: r, conn_factory=lambda: object())
    assert rc == 0
    assert len(r.adds) == 1
    fields = r.adds[0][1]
    assert fields["kind"] == "verdict"
    assert json.loads(fields["payload"])["stances"] == {"codex": "approve"}
    assert "break-glass" in capsys.readouterr().err
