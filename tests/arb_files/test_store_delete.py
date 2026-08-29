from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

from arb_files.config import load_settings
from arb_files.store import FilesStore

sys.path.append(str(Path(__file__).parent))
from fakes import FakeS3  # noqa: E402


BASE = {
    "ARB_FILES_ENDPOINT": "https://e",
    "ARB_FILES_REGION": "lon1",
    "ARB_FILES_BUCKET": "arb-files",
    "ARB_FILES_ACCESS_KEY": "AK",
    "ARB_FILES_SECRET_KEY": "SK",
}


def _store(audit=None):
    fake = FakeS3()
    settings = load_settings(BASE)
    fixed = lambda: datetime(2026, 6, 29, tzinfo=timezone.utc)
    return FilesStore(settings, client_factory=lambda: fake, audit_sink=audit, now=fixed), fake


def test_delete_is_soft_and_recoverable():
    events = []
    st, fake = _store(audit=events.append)
    st.put_bytes("doomed.txt", b"x", "text/plain", uploaded_by="s")
    out = st.delete("doomed.txt", actor="seat-2")
    assert out["deleted"] and "recovery" in out
    assert out["recovery"]["trash_key"] == "agent-files/.trash/2026-06-29/deadbeef/doomed.txt"
    assert "agent-files/doomed.txt" not in fake.objects
    assert fake.objects[out["recovery"]["trash_key"]]["Body"] == b"x"


def test_delete_emits_audit_event_before_delete():
    events = []
    st, _fake = _store(audit=events.append)
    st.put_bytes("a", b"x", "text/plain", uploaded_by="s")
    st.delete("a", actor="seat-2")
    assert events and events[0]["op"] == "delete" and events[0]["actor"] == "seat-2"
    assert events[0]["recovery_key"] == "agent-files/.trash/2026-06-29/deadbeef/a"


def test_audit_failure_not_swallowed_and_live_object_remains():
    def boom(_event):
        raise RuntimeError("audit down")

    st, _fake = _store(audit=boom)
    st.put_bytes("a", b"x", "text/plain", uploaded_by="s")
    with pytest.raises(RuntimeError):
        st.delete("a", actor="seat-2")
    assert st.head("a")["exists"] is True


def test_delete_missing_raises_key_error():
    st, _fake = _store()
    with pytest.raises(KeyError):
        st.delete("missing", actor="seat-2")
