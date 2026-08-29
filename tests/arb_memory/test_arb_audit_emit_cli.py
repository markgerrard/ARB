import importlib.machinery, importlib.util, json
from pathlib import Path
import pytest

_path = Path(__file__).parents[2] / "scripts" / "arb-audit-emit"
_spec = importlib.util.spec_from_file_location(
    "arb_audit_emit", _path, loader=importlib.machinery.SourceFileLoader("arb_audit_emit", str(_path)))
_mod = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_mod)


def test_emit_writes_audit_event(redis_bus):
    prefix = redis_bus.prefix
    _mod.emit(redis_bus, run_id="run-1", source="orchestrator", kind="dispatch",
              payload={"actor": "seat:codex", "task_id": "t1"}, prefix=prefix)
    entries = redis_bus.xrange(f"{prefix}arbmem:audit")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["run_id"] == "run-1"
    assert fields["kind"] == "dispatch"
    assert json.loads(fields["payload"])["actor"] == "seat:codex"


def test_cli_rejects_vote_kind_and_emits_nothing(redis_bus):
    with pytest.raises(SystemExit):
        _mod.main(
            ["--run-id", "run-vote", "--kind", "vote", "--payload", '{"stance":"approve"}'],
            redis_factory=lambda: redis_bus,
        )
    assert redis_bus.xrange(f"{redis_bus.prefix}arbmem:audit") == []
