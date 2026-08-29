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


def _store(fake=None, audit=None, list_max=None):
    fake = fake or FakeS3()
    env = dict(BASE)
    if list_max is not None:
        env["ARB_FILES_LIST_MAX"] = str(list_max)
    settings = load_settings(env)
    now = lambda: datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    return FilesStore(settings, client_factory=lambda: fake, audit_sink=audit, now=now), fake


def test_put_then_head_and_get():
    st, fake = _store()
    st.put_bytes("a/b.txt", b"hello", "text/plain", uploaded_by="seat-1")
    assert "agent-files/a/b.txt" in fake.objects
    h = st.head("a/b.txt")
    assert h["exists"] and h["size"] == 5 and h["uploaded_by"] == "seat-1"
    data, ct = st.get_bytes("a/b.txt")
    assert data == b"hello" and ct == "text/plain"


def test_clobber_guard_blocks_overwrite():
    st, _fake = _store()
    st.put_bytes("x", b"1", "text/plain", uploaded_by="s")
    with pytest.raises(ValueError):
        st.put_bytes("x", b"2", "text/plain", uploaded_by="s")
    st.put_bytes("x", b"2", "text/plain", uploaded_by="s", force=True)
    assert st.get_bytes("x")[0] == b"2"


def test_force_overwrite_trashes_and_audits():
    events = []
    st, fake = _store(audit=events.append)
    st.put_bytes("x", b"1", "text/plain", uploaded_by="s")
    st.put_bytes("x", b"2", "text/plain", uploaded_by="seat-2", force=True)
    assert fake.objects["agent-files/.trash/2026-06-29/deadbeef/x"]["Body"] == b"1"
    assert events[0]["op"] == "overwrite"
    assert events[0]["actor"] == "seat-2"
    assert events[0]["recovery_key"] == "agent-files/.trash/2026-06-29/deadbeef/x"


def test_force_overwrite_audit_failure_leaves_original_bytes():
    def boom(_event):
        raise RuntimeError("audit down")

    st, _fake = _store(audit=boom)
    st.put_bytes("x", b"original", "text/plain", uploaded_by="s")
    with pytest.raises(RuntimeError):
        st.put_bytes("x", b"new", "text/plain", uploaded_by="seat-2", force=True)
    assert st.get_bytes("x")[0] == b"original"


def test_head_absent():
    st, _fake = _store()
    assert st.head("nope")["exists"] is False


def test_list_includes_name_size_etag_and_truncation():
    st, _fake = _store()
    st.put_bytes("one.txt", b"1", "text/plain", uploaded_by="s")
    out = st.list()
    assert out["items"][0]["name"] == "one.txt"
    assert out["items"][0]["size"] == 1
    assert out["items"][0]["etag"] == '"deadbeef"'
    assert out["is_truncated"] is False


def test_list_excludes_trash():
    st, fake = _store()
    st.put_bytes("one.txt", b"1", "text/plain", uploaded_by="s")
    fake.objects["agent-files/.trash/2026-06-29/one.txt"] = dict(fake.objects["agent-files/one.txt"])
    assert [item["name"] for item in st.list()["items"]] == ["one.txt"]


def test_list_subprefix_ok():
    st, _fake = _store()
    st.put_bytes("sub/one.txt", b"1", "text/plain", uploaded_by="s")
    st.put_bytes("two.txt", b"2", "text/plain", uploaded_by="s")
    assert [item["name"] for item in st.list("sub/")["items"]] == ["sub/one.txt"]


def test_list_rejects_bad_prefix_before_s3():
    st, fake = _store()
    with pytest.raises(ValueError):
        st.list("../")
    assert fake.objects == {}


def test_list_truncation_signal():
    st, _fake = _store(list_max=1)
    st.put_bytes("a.txt", b"a", "text/plain", uploaded_by="s")
    st.put_bytes("b.txt", b"b", "text/plain", uploaded_by="s")
    out = st.list()
    assert out["is_truncated"] is True
    assert out["next_token"] == "tok"


def test_name_validation_before_s3():
    st, fake = _store()
    with pytest.raises(ValueError):
        st.put_bytes("../escape", b"x", "text/plain", uploaded_by="s")
    assert fake.objects == {}
