import logging

from arb_files import audit, store as store_mod
from arb_files.audit import default_audit_sink
from arb_files.mcp import door_wire

FULL_ENV = {
    "ARB_FILES_ENDPOINT": "https://e",
    "ARB_FILES_REGION": "lon1",
    "ARB_FILES_BUCKET": "arb-files",
    "ARB_FILES_ACCESS_KEY": "AK",
    "ARB_FILES_SECRET_KEY": "SK",
}


def test_default_audit_sink_emits_structured_log(caplog):
    with caplog.at_level(logging.INFO, logger="arb_files.audit"):
        default_audit_sink({"op": "delete", "name": "a", "actor": "seat-1"})
    assert any("arb_files_audit" in r.message and "delete" in r.message for r in caplog.records)


def test_door_wire_wires_default_audit_sink(monkeypatch):
    """The prod door entrypoint must build FilesStore WITH the audit sink — otherwise the audit
    half of delete-safety is dormant in prod (merged != wired)."""
    captured = {}

    class _RecordingStore:
        def __init__(self, settings, *, audit_sink=None):
            captured["audit_sink"] = audit_sink

    monkeypatch.setattr(store_mod, "FilesStore", _RecordingStore)

    class _Server:
        def add_tool(self, *a, **k):
            pass

    ok = door_wire.register_file_tools(_Server(), FULL_ENV)
    assert ok is True
    assert captured["audit_sink"] is default_audit_sink
