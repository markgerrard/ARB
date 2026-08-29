import importlib.machinery, importlib.util, io, json, os
from pathlib import Path
import pytest

_path = Path(__file__).parents[2] / "scripts" / "arb-panel-vote"
_spec = importlib.util.spec_from_file_location(
    "arb_panel_vote", _path, loader=importlib.machinery.SourceFileLoader("arb_panel_vote", str(_path)))
apv = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(apv)

class FakeRedis:
    def __init__(self): self.adds = []
    def xadd(self, stream, fields, **kw): self.adds.append((stream, fields)); return b"1-0"
    def incr(self, k): return 1
    def expire(self, k, s): return True

VALID = 'review...\n```vote\n{"stance":"block","severity":"P0","refs":[],"note":"x"}\n```'

def test_valid_reply_emits_vote(capsys):
    r = FakeRedis()
    rc = apv.main(["--run-id","R","--actor","seat:codex"], stdin=io.StringIO(VALID),
                  redis_factory=lambda: r)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "voted seat:codex" in captured.err
    stream, fields = r.adds[0]
    assert fields["kind"] == "vote"
    p = json.loads(fields["payload"])
    assert p["actor"] == "seat:codex" and p["stance"] == "block"

def test_timed_out_emits_without_stdin():
    r = FakeRedis()
    rc = apv.main(["--run-id","R","--actor","seat:m3","--timed-out"], stdin=io.StringIO(""),
                  redis_factory=lambda: r)
    assert rc == 0
    assert json.loads(r.adds[0][1]["payload"])["stance"] == "timed-out"

def test_malformed_reply_fails_loud_no_emit():
    r = FakeRedis()
    rc = apv.main(["--run-id","R","--actor","seat:codex"], stdin=io.StringIO("no vote block"),
                  redis_factory=lambda: r)
    assert rc != 0
    assert r.adds == []


def test_cli_resolves_audit_bus_exactly_like_the_daemon(monkeypatch):
    """ARB_AUDIT_* must win over ARB_MEMORY_*, as bridge.resolve_audit_redis does.

    Before this, the CLIs read ARB_MEMORY_* only. A host with the two pointed at
    different buses -- what a mid-migration host looks like -- split its audit plane:
    daemon votes on one bus, CLI votes on the other, surfacing only as
    refused_reconcile at verdict close with no hint of the cause.
    """
    from arb_memory.audit import resolve_audit_env

    env = {
        "ARB_AUDIT_REDIS_URL": "rediss://audit-emitter@selfhosted/5",
        "ARB_MEMORY_REDIS_URL": "rediss://default@managed/5",
        "ARB_AUDIT_PREFIX": "audit-",
        "ARB_MEMORY_PREFIX": "mem-",
    }
    assert resolve_audit_env(env) == ("rediss://audit-emitter@selfhosted/5", "audit-")

    # Fallback: hosts that set only the historical vars keep working unchanged.
    assert resolve_audit_env({"ARB_MEMORY_REDIS_URL": "rediss://m/5"}) == ("rediss://m/5", "")
    assert resolve_audit_env({"ARB_MEMORY_REDIS_URL": "u", "ARB_MEMORY_PREFIX": "p-"}) == ("u", "p-")
    # Neither set: URL is None so the caller can emit its own error text.
    assert resolve_audit_env({}) == (None, "")


def test_vote_stream_and_seq_use_the_audit_prefix(monkeypatch):
    """The prefix must follow the same precedence, or the daemon and CLI write to
    differently-named streams on the SAME bus -- a subtler split than the URL one."""
    monkeypatch.setenv("ARB_AUDIT_REDIS_URL", "rediss://unused/5")
    monkeypatch.setenv("ARB_MEMORY_PREFIX", "mem-")
    monkeypatch.setenv("ARB_AUDIT_PREFIX", "audit-")
    r = FakeRedis()
    rc = apv.main(["--run-id", "R", "--actor", "seat:x"], stdin=io.StringIO(VALID),
                  redis_factory=lambda: r)
    assert rc == 0
    stream, _fields = r.adds[0]
    assert stream.startswith("audit-"), stream
