import importlib.machinery, importlib.util, io, json
from pathlib import Path
import pytest

_path = Path(__file__).parents[2] / "scripts" / "arb-audit-emit"
_spec = importlib.util.spec_from_file_location(
    "arb_audit_emit", _path, loader=importlib.machinery.SourceFileLoader("arb_audit_emit", str(_path)))
aae = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(aae)

class FakeRedis:
    def __init__(self): self.adds = []
    def xadd(self, stream, fields, **kw): self.adds.append((stream, fields)); return b"1-0"
    def incr(self, k): return 1
    def expire(self, k, s): return True

def test_verdict_refused_on_gap_no_xadd(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(aae, "reconcile", lambda *a, **k: {"ok": False, "incomplete": False, "gaps": ["seat X never voted"]})
    rc = aae.main(["--run-id","R","--kind","verdict","--payload",'{"kind":"verdict","roster":[],"stances":{}}'],
                  redis_factory=lambda: r, conn_factory=lambda: object())
    assert rc != 0 and r.adds == []

def test_verdict_emitted_when_reconcile_ok(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(aae, "reconcile", lambda *a, **k: {"ok": True, "incomplete": False, "gaps": []})
    rc = aae.main(["--run-id","R","--kind","verdict","--payload",'{"kind":"verdict","roster":[],"stances":{}}'],
                  redis_factory=lambda: r, conn_factory=lambda: object())
    assert rc == 0 and r.adds and r.adds[0][1]["kind"] == "verdict"

def test_dispatch_kind_still_emits_without_reconcile(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(aae, "reconcile", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reconcile dispatch")))
    rc = aae.main(["--run-id","R","--kind","dispatch","--payload",'{"roster":["seat:x"]}'],
                  redis_factory=lambda: r, conn_factory=lambda: object())
    assert rc == 0 and r.adds[0][1]["kind"] == "dispatch"
