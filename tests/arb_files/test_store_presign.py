from __future__ import annotations

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


def _store():
    fake = FakeS3()
    settings = load_settings(BASE)
    return FilesStore(settings, client_factory=lambda: fake), fake


def test_presign_get_requires_existing_object():
    st, _fake = _store()
    with pytest.raises(KeyError):
        st.presign_get("missing")


def test_presign_get_returns_url_and_meta():
    st, _fake = _store()
    st.put_bytes("a.txt", b"hi", "text/plain", uploaded_by="s")
    out = st.presign_get("a.txt")
    assert out["method"] == "GET"
    assert out["expires_in"] == 900
    assert "agent-files/a.txt" in out["url"]
    assert out["size"] == 2
    assert out["content_type"] == "text/plain"


def test_presign_put_nonforce_returns_required_headers_and_params():
    st, fake = _store()
    out = st.presign_put("b.bin", content_type="application/octet-stream", uploaded_by="client-1")
    assert out["method"] == "PUT"
    assert out["headers"]["If-None-Match"] == "*"
    assert out["headers"]["Content-Type"] == "application/octet-stream"
    assert out["headers"]["x-amz-meta-uploaded-by"] == "client-1"
    params = fake.presigned[-1]["Params"]
    assert params["IfNoneMatch"] == "*"
    assert params["Metadata"]["uploaded-by"] == "client-1"


def test_presign_put_force_omits_conditional_header():
    st, fake = _store()
    out = st.presign_put("b.bin", uploaded_by="client-1", force=True)
    assert "If-None-Match" not in out["headers"]
    assert "IfNoneMatch" not in fake.presigned[-1]["Params"]


def _audited_store():
    fake = FakeS3()
    events = []
    settings = load_settings(BASE)
    return FilesStore(settings, client_factory=lambda: fake, audit_sink=events.append), fake, events


def test_presign_put_force_trashes_prior_version_before_issuing_the_url():
    """The write happens out-of-band at S3, so the recovery copy must be taken at ISSUE time.

    Without this, a presigned force PUT overwrites a live object with no recovery copy at all —
    the delete-safety plane is bypassed entirely by the presigned path.
    """
    st, fake, events = _audited_store()
    st.put_bytes("b.bin", b"v1", "application/octet-stream", uploaded_by="s")
    prior_etag = st.head("b.bin")["etag"]

    st.presign_put("b.bin", uploaded_by="client-1", force=True)

    trash_key = st._trash_key("b.bin", prior_etag)
    assert trash_key in fake.objects, "no recovery copy was taken before issuing a force URL"
    assert fake.objects[trash_key]["Body"] == b"v1"


def test_presign_put_force_emits_an_overwrite_audit_event_marked_as_presigned():
    st, fake, events = _audited_store()
    st.put_bytes("b.bin", b"v1", "application/octet-stream", uploaded_by="s")
    prior_etag = st.head("b.bin")["etag"]

    st.presign_put("b.bin", uploaded_by="client-1", force=True)

    assert len(events) == 1, f"expected exactly one audit event, got {events}"
    event = events[0]
    assert event["op"] == "overwrite"
    assert event["name"] == "b.bin"
    assert event["actor"] == "client-1"
    assert event["etag"] == prior_etag
    assert event["recovery_key"] == st._trash_key("b.bin", prior_etag)
    # The write is out-of-band: this event records an AUTHORISED overwrite, not an observed one.
    # An audit reader must be able to tell the difference.
    assert event["via"] == "presign"


def test_presign_put_force_pins_if_match_to_the_version_it_trashed():
    """Race guard, mirroring put_bytes: the out-of-band PUT must only land on the version we
    trashed. Otherwise a concurrent writer's version is clobbered with no recovery copy of it."""
    st, fake, events = _audited_store()
    st.put_bytes("b.bin", b"v1", "application/octet-stream", uploaded_by="s")
    prior_etag = st.head("b.bin")["etag"]

    out = st.presign_put("b.bin", uploaded_by="client-1", force=True)

    assert prior_etag == '"deadbeef"'
    assert out["headers"]["If-Match"] == "deadbeef"
    assert fake.presigned[-1]["Params"]["IfMatch"] == "deadbeef"


def test_presign_put_force_on_a_missing_object_trashes_nothing_and_audits_nothing():
    st, fake, events = _audited_store()
    out = st.presign_put("absent.bin", uploaded_by="client-1", force=True)
    assert events == []
    assert not any(".trash/" in k for k in fake.objects)
    assert "If-Match" not in out["headers"]
