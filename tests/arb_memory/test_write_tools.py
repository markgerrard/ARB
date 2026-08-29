import anyio
import pytest

from arb_memory.mcp import tools
from arb_memory.mcp.config import Settings

S = Settings(
    public_base_url="https://x",
    mcp_dsn="postgresql://x",
    login_secret="l",
    totp_secret="t",
)


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    """Captures POSTs; returns 200 {ulid} by default."""

    def __init__(self, status_code=200, ulid="ulid-1", boom=False, payload=None):
        self.status_code = status_code
        self.ulid = ulid
        self.boom = boom
        self.payload = payload
        self.posts = []
        self.timeouts = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json, headers))
        self.timeouts.append(timeout)
        if self.boom:
            raise ConnectionError("writer down")
        return _Resp(self.status_code, self.payload or {"ulid": self.ulid})


def _mt(http, conn_factory=lambda: None):
    return tools.MemoryTools(
        S,
        conn_factory=conn_factory,
        embed=lambda t: [0.0],
        writer_url="http://writer:8800",
        writer_token="sek",
        http_client=http,
    )


def _grant(monkeypatch, scopes=("memory.read", "memory.write"), client_id="cid-1"):
    monkeypatch.setattr(
        tools,
        "get_access_token",
        lambda: type("T", (), {"scopes": list(scopes), "client_id": client_id})(),
    )


def test_memory_store_posts_mcp_artefact_with_bearer(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp()
    res = anyio.run(lambda: _mt(http).memory_store("hello", access_token="tok"))
    assert res["accepted"] is True and res["ulid"] == "ulid-1"
    url, body, headers = http.posts[0]
    assert url == "http://writer:8800/publish"
    assert headers["Authorization"] == "Bearer sek"
    assert body["artefact"]["source"] == "mcp" and body["artefact"]["author"] == "cid-1"
    assert body["author"] == "cid-1"


def test_memory_store_same_content_same_id(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp()
    mt = _mt(http)
    a = anyio.run(lambda: mt.memory_store("dup", access_token="t"))["artefact_id"]
    b = anyio.run(lambda: mt.memory_store("dup", access_token="t"))["artefact_id"]
    assert a == b and a.startswith("art-")


def test_memory_store_fails_loud_on_writer_5xx(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp(status_code=503)
    with pytest.raises(RuntimeError):
        anyio.run(lambda: _mt(http).memory_store("x", access_token="t"))


def test_memory_store_fails_loud_on_transport_error(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp(boom=True)
    with pytest.raises(RuntimeError):
        anyio.run(lambda: _mt(http).memory_store("x", access_token="t"))
    assert http.posts


@pytest.mark.parametrize("http", [_FakeHttp(boom=True), _FakeHttp(status_code=503)])
def test_memory_store_never_claims_not_stored_when_it_cannot_know(monkeypatch, http):
    """Transport faults and 5xx happen at or after the XADD, so delivery is genuinely unknown.

    The writer commits before it answers and mints its own request_id, so this door has no way
    to reconcile a failed round-trip. Claiming NOT stored here was wrong in production and the
    "retry" it invites bumps a phantom version.
    """
    _grant(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        anyio.run(lambda: _mt(http).memory_store("x", access_token="t"))
    assert "UNKNOWN" in str(excinfo.value)
    assert "NOT stored" not in str(excinfo.value)


def test_memory_store_says_not_stored_only_when_refused_before_the_bus(monkeypatch):
    """4xx is auth/validation, refused ahead of the XADD — the one case we may assert."""
    _grant(monkeypatch)
    for status in (400, 401, 403):
        with pytest.raises(RuntimeError) as excinfo:
            anyio.run(lambda: _mt(_FakeHttp(status_code=status)).memory_store("x", access_token="t"))
        assert "NOT stored" in str(excinfo.value)


def test_write_without_scope_denied(monkeypatch):
    _grant(monkeypatch, scopes=("memory.read",))
    with pytest.raises(PermissionError):
        anyio.run(lambda: _mt(_FakeHttp()).memory_store("x", access_token="t"))


def test_memory_remember_linked_requires_existing_artefact(monkeypatch, conn_factory, fake_embed):
    from arb_memory import store

    _grant(monkeypatch)
    conn = conn_factory()
    store.write_artefact_and_hints(
        conn,
        artefact={
            "artefact_id": "art-link",
            "content": "c",
            "mime": "text/plain",
            "source": "mcp",
            "author": "c",
        },
    )
    http = _FakeHttp()
    mt = tools.MemoryTools(
        S,
        conn_factory=conn_factory,
        embed=fake_embed,
        writer_url="http://writer:8800",
        writer_token="sek",
        http_client=http,
    )
    with pytest.raises(ValueError):
        anyio.run(lambda: mt.memory_remember("n", artefact_id="art-link", access_token="t"))
    with pytest.raises(ValueError):
        anyio.run(lambda: mt.memory_remember("n", artefact_id="art-link", artefact_version=99, access_token="t"))
    res = anyio.run(lambda: mt.memory_remember("n", artefact_id="art-link", artefact_version=1, access_token="t"))
    assert res["accepted"] is True and http.posts


def test_memory_remember_rejects_too_many_tags(monkeypatch):
    _grant(monkeypatch)
    with pytest.raises(ValueError):
        anyio.run(lambda: _mt(_FakeHttp()).memory_remember("n", tags=[f"t{i}" for i in range(17)], access_token="t"))


@pytest.mark.parametrize("bad_tag", ["x" * 65, 3])
def test_memory_remember_rejects_bad_tag(monkeypatch, bad_tag):
    _grant(monkeypatch)
    with pytest.raises(ValueError):
        anyio.run(lambda: _mt(_FakeHttp()).memory_remember("n", tags=["ok", bad_tag], access_token="t"))


def test_memory_remember_accepts_bounded_tags(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp()

    res = anyio.run(lambda: _mt(http).memory_remember("n", tags=["alpha", "beta"], access_token="t"))

    assert res["accepted"] is True
    _, body, _ = http.posts[0]
    assert body["hints"][0]["metadata"] == {"tags": ["alpha", "beta"]}


def test_memory_store_auto_indexes_for_search(monkeypatch):
    # memory_store must emit an indexing hint alongside the artefact so memory_search can find it
    _grant(monkeypatch)
    http = _FakeHttp()
    res = anyio.run(lambda: _mt(http).memory_store("searchable body text", access_token="t"))
    _, body, _ = http.posts[0]
    assert body["artefact"]["artefact_id"] == res["artefact_id"]
    assert len(body["hints"]) == 1
    assert body["hints"][0]["text"] == "searchable body text"
    assert body["hints"][0]["metadata"]["kind"] == "artefact_index"
    assert body["hints"][0]["metadata"]["artefact_id"] == res["artefact_id"]


def test_memory_store_index_hint_capped_for_embedding(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp()
    anyio.run(lambda: _mt(http).memory_store("x" * 20000, access_token="t"))
    _, body, _ = http.posts[0]
    assert len(body["hints"][0]["text"]) == 8000  # settings.write_index_chars


def test_memory_store_await_result_relays_receipt_with_a_timeout_over_the_writer_cap(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp(payload={
        "ulid": "ulid-await",
        "artefact_outcome": "stored",
        "artefact_id": "art-from-writer",
        "version": 1,
        "hints_stored": 1,
        "duplicate": False,
    })

    result = anyio.run(lambda: _mt(http).memory_store("hello", await_result=True, access_token="tok"))

    assert result == {
        "accepted": True,
        "ulid": "ulid-await",
        "artefact_id": "art-from-writer",
        "artefact_outcome": "stored",
        "version": 1,
        "hints_stored": 1,
        "duplicate": False,
    }
    assert http.posts[0][1]["await"] is True
    assert http.timeouts == [tools.WRITE_AWAIT_HTTP_TIMEOUT]
    assert http.timeouts[0] > 30


def test_memory_store_expected_version_requires_await_result(monkeypatch):
    """AC6: expected_version without await_result raises; posts empty."""
    _grant(monkeypatch)
    http = _FakeHttp()
    with pytest.raises(ValueError, match="expected_version requires await_result"):
        anyio.run(
            lambda: _mt(http).memory_store(
                "x", artefact_id="doc", expected_version=1, access_token="t"
            )
        )
    assert http.posts == []


def test_memory_store_refusal_accepted_false_distinct_from_transport(monkeypatch):
    """AC8: refusal envelope → accepted False; transport failure is RuntimeError."""
    _grant(monkeypatch)
    http = _FakeHttp(payload={
        "ulid": "ulid-refuse",
        "artefact_outcome": "refused_version_mismatch",
        "artefact_id": "doc",
        "version": 2,
        "hints_stored": 0,
        "duplicate": False,
    })
    result = anyio.run(
        lambda: _mt(http).memory_store(
            "C",
            artefact_id="doc",
            expected_version=1,
            await_result=True,
            access_token="t",
        )
    )
    assert result["accepted"] is False
    assert result["artefact_outcome"] == "refused_version_mismatch"
    assert result["version"] == 2

    # AC8's distinction is refusal-returns vs transport-raises, and that still holds. The
    # wording moved: a transport fault cannot know whether the write landed, so it reports
    # UNKNOWN rather than asserting NOT stored (which was observed false in production).
    boom = _FakeHttp(boom=True)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        anyio.run(
            lambda: _mt(boom).memory_store(
                "C",
                artefact_id="doc",
                expected_version=1,
                await_result=True,
                access_token="t",
            )
        )


def test_memory_store_guard_not_live_on_arithmetic_mismatch(monkeypatch):
    """AC9: old consumer stored+version=3 with expected_version=1 → guard_not_live."""
    _grant(monkeypatch)
    http = _FakeHttp(payload={
        "ulid": "ulid-old",
        "artefact_outcome": "stored",
        "artefact_id": "doc",
        "version": 3,
        "hints_stored": 1,
        "duplicate": False,
    })
    result = anyio.run(
        lambda: _mt(http).memory_store(
            "C",
            artefact_id="doc",
            expected_version=1,
            await_result=True,
            access_token="t",
        )
    )
    assert result["accepted"] is False
    assert result["artefact_outcome"] == "guard_not_live"


def test_memory_store_guard_not_live_on_deduped_version_mismatch(monkeypatch):
    """R-2.3: deduped + version != expected → guard_not_live, accepted False."""
    _grant(monkeypatch)
    http = _FakeHttp(payload={
        "ulid": "ulid-dedup-bad",
        "artefact_outcome": "deduped",
        "artefact_id": "doc",
        "version": 2,
        "hints_stored": 0,
        "duplicate": False,
    })
    result = anyio.run(
        lambda: _mt(http).memory_store(
            "C",
            artefact_id="doc",
            expected_version=1,
            await_result=True,
            access_token="t",
        )
    )
    assert result["accepted"] is False
    assert result["artefact_outcome"] == "guard_not_live"


def test_memory_store_guarded_happy_path_stored(monkeypatch):
    """R-4: stored → accepted true, version == expected_version + 1."""
    _grant(monkeypatch)
    http = _FakeHttp(payload={
        "ulid": "ulid-ok-stored",
        "artefact_outcome": "stored",
        "artefact_id": "doc",
        "version": 2,
        "hints_stored": 1,
        "duplicate": False,
    })
    result = anyio.run(
        lambda: _mt(http).memory_store(
            "C",
            artefact_id="doc",
            expected_version=1,
            await_result=True,
            access_token="t",
        )
    )
    assert result["accepted"] is True
    assert result["artefact_outcome"] == "stored"
    assert result["version"] == 2  # expected_version + 1


def test_memory_store_guarded_happy_path_deduped(monkeypatch):
    """R-4: deduped → accepted true, version == expected_version."""
    _grant(monkeypatch)
    http = _FakeHttp(payload={
        "ulid": "ulid-ok-dedup",
        "artefact_outcome": "deduped",
        "artefact_id": "doc",
        "version": 1,
        "hints_stored": 0,
        "duplicate": False,
    })
    result = anyio.run(
        lambda: _mt(http).memory_store(
            "C",
            artefact_id="doc",
            expected_version=1,
            await_result=True,
            access_token="t",
        )
    )
    assert result["accepted"] is True
    assert result["artefact_outcome"] == "deduped"
    assert result["version"] == 1


def test_memory_store_ordinary_write_untouched(monkeypatch):
    """AC12 (tool half): no expected_version → accepted True, no expected_version on wire."""
    _grant(monkeypatch)
    http = _FakeHttp(payload={
        "ulid": "ulid-ord",
        "artefact_outcome": "stored",
        "artefact_id": "art-ord",
        "version": 1,
        "hints_stored": 1,
    })
    result = anyio.run(lambda: _mt(http).memory_store("hello", await_result=True, access_token="t"))
    assert result["accepted"] is True
    assert result["artefact_outcome"] == "stored"
    assert "expected_version" not in http.posts[0][1]["artefact"]


def test_memory_store_accepted_false_on_unknown_and_failed(monkeypatch):
    """AC13: with expected_version set, unknown and failed yield accepted False."""
    _grant(monkeypatch)
    unknown = _FakeHttp(payload={
        "ulid": "u1",
        "artefact_outcome": "unknown",
        "timed_out": True,
    })
    r1 = anyio.run(
        lambda: _mt(unknown).memory_store(
            "C", artefact_id="doc", expected_version=1, await_result=True, access_token="t"
        )
    )
    assert r1["accepted"] is False
    assert r1["artefact_outcome"] == "unknown"

    failed = _FakeHttp(payload={
        "ulid": "u2",
        "artefact_outcome": "failed",
        "reason": "deadlettered",
    })
    r2 = anyio.run(
        lambda: _mt(failed).memory_store(
            "C", artefact_id="doc", expected_version=1, await_result=True, access_token="t"
        )
    )
    assert r2["accepted"] is False
    assert r2["artefact_outcome"] == "failed"


@pytest.mark.parametrize("bad_ev", [-1, True, "1"])
def test_memory_store_rejects_invalid_expected_version(monkeypatch, bad_ev):
    """AC14 (tool half): invalid expected_version / missing artefact_id → ValueError, no post."""
    _grant(monkeypatch)
    http = _FakeHttp()
    with pytest.raises(ValueError):
        anyio.run(
            lambda: _mt(http).memory_store(
                "C",
                artefact_id="doc",
                expected_version=bad_ev,
                await_result=True,
                access_token="t",
            )
        )
    assert http.posts == []


def test_memory_store_expected_version_requires_artefact_id(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp()
    with pytest.raises(ValueError, match="artefact_id"):
        anyio.run(
            lambda: _mt(http).memory_store(
                "C", expected_version=1, await_result=True, access_token="t"
            )
        )
    assert http.posts == []
