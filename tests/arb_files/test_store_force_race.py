from datetime import datetime, timezone

import pytest

from arb_files.config import load_settings
import arb_files.store as store_module
from arb_files.store import FilesStore
from tests.arb_files.fakes import FakeClientError, FakeS3

BASE = {
    "ARB_FILES_ENDPOINT": "https://e",
    "ARB_FILES_REGION": "lon1",
    "ARB_FILES_BUCKET": "arb-files",
    "ARB_FILES_ACCESS_KEY": "AK",
    "ARB_FILES_SECRET_KEY": "SK",
}


def _store(fake=None):
    fake = fake or FakeS3()
    fixed = lambda: datetime(2026, 6, 29, tzinfo=timezone.utc)
    return FilesStore(load_settings(BASE), client_factory=lambda: fake, now=fixed), fake


def test_force_overwrite_sends_if_match_of_prior_etag():
    st, fake = _store()
    captured = {}
    orig = fake.put_object

    def spy(**kwargs):
        captured.update(kwargs)
        return orig(**kwargs)

    st.put_bytes("x", b"v1", "text/plain", uploaded_by="s")          # prior, etag "deadbeef"
    fake.put_object = spy                                            # spy only the force PUT
    st.put_bytes("x", b"v2", "text/plain", uploaded_by="s", force=True)
    assert captured.get("IfMatch") == "deadbeef"                     # race guard present
    assert "IfNoneMatch" not in captured                             # force omits create-guard


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('"abc"', "abc"),
        ('"abc-3"', "abc-3"),
        ("abc", "abc"),
        ('W/"abc"', 'W/"abc"'),
        ('""abc""', '"abc"'),
        (" abc ", " abc "),
    ],
)
def test_put_if_match_etag_serializes_exactly_one_quote_pair(value, expected):
    serializer = getattr(store_module, "_put_if_match_etag")
    assert serializer(value) == expected


@pytest.mark.parametrize("value", ['""', "", None, 123])
def test_put_if_match_etag_rejects_invalid_values(value):
    serializer = getattr(store_module, "_put_if_match_etag")
    with pytest.raises(RuntimeError, match="^files backend returned invalid ETag$"):
        serializer(value)


def test_force_paths_use_same_unquoted_if_match():
    st, fake = _store()
    captured = {}
    orig = fake.put_object

    st.put_bytes("x", b"v1", "text/plain", uploaded_by="s")

    def spy(**kwargs):
        captured.update(kwargs)
        return orig(**kwargs)

    fake.put_object = spy
    st.put_bytes("x", b"v2", "text/plain", uploaded_by="s", force=True)
    out = st.presign_put("x", uploaded_by="client-1", force=True)

    assert captured["IfMatch"] == "deadbeef"
    assert fake.presigned[-1]["Params"]["IfMatch"] == "deadbeef"
    assert out["headers"]["If-Match"] == "deadbeef"


def test_weak_etag_is_attached_at_both_force_wire_locations():
    st, fake = _store()
    captured = {}
    orig = fake.put_object

    st.put_bytes("x", b"v1", "text/plain", uploaded_by="s")
    fake.objects["agent-files/x"]["ETag"] = 'W/"abc"'

    def spy(**kwargs):
        captured.update(kwargs)
        return orig(**kwargs)

    fake.put_object = spy
    st.put_bytes("x", b"v2", "text/plain", uploaded_by="s", force=True)
    fake.objects["agent-files/x"]["ETag"] = 'W/"abc"'
    out = st.presign_put("x", uploaded_by="client-1", force=True)

    assert captured["IfMatch"] == 'W/"abc"'
    assert fake.presigned[-1]["Params"]["IfMatch"] == 'W/"abc"'
    assert out["headers"]["If-Match"] == 'W/"abc"'


@pytest.mark.parametrize("bad_etag", [None, "", 123])
@pytest.mark.parametrize("path", ["direct", "presign"])
def test_force_rejects_invalid_etag_before_any_side_effect(path, bad_etag):
    fake = FakeS3()
    events = []
    st, _ = _store(fake=fake)
    st.audit_sink = events.append
    st.put_bytes("x", b"v1", "text/plain", uploaded_by="s")
    fake.objects["agent-files/x"]["ETag"] = bad_etag

    def unexpected(**_kwargs):
        raise AssertionError("backend side effect happened before ETag validation")

    fake.copy_object = unexpected
    fake.put_object = unexpected
    fake.generate_presigned_url = unexpected

    with pytest.raises(RuntimeError, match="^files backend returned invalid ETag$"):
        if path == "direct":
            st.put_bytes("x", b"v2", "text/plain", uploaded_by="s", force=True)
        else:
            st.presign_put("x", uploaded_by="client-1", force=True)

    assert events == []


class _StaleFake(FakeS3):
    """Simulates a concurrent writer moving the etag between head() and put(): any conditional
    force-PUT (IfMatch present) fails as if the object changed underneath."""

    def put_object(self, **kwargs):
        if kwargs.get("IfMatch") is not None:
            raise FakeClientError("PreconditionFailed")
        return super().put_object(**kwargs)


def test_force_overwrite_stale_etag_raises_retryable():
    st, _ = _store(fake=_StaleFake())
    st.put_bytes("x", b"v1", "text/plain", uploaded_by="s")
    with pytest.raises(RuntimeError, match="stale write"):
        st.put_bytes("x", b"v2", "text/plain", uploaded_by="s", force=True)


def test_force_create_when_absent_has_no_condition():
    st, fake = _store()
    captured = {}
    orig = fake.put_object
    fake.put_object = lambda **kw: (captured.update(kw) or orig(**kw))
    st.put_bytes("fresh", b"v", "text/plain", uploaded_by="s", force=True)  # no prior
    assert "IfMatch" not in captured and "IfNoneMatch" not in captured
