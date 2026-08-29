import asyncio
import json

import anyio
import httpx
from starlette.testclient import TestClient

from arb_memory.writer import build_writer_app


class _FakeBus:
    def __init__(self, boom=False):
        self.boom = boom
        self.calls = []

    def xadd(self, stream, fields, **k):
        if self.boom:
            raise RuntimeError("bus down")
        self.calls.append((stream, fields))
        return "1-0"


class _FakeAsyncRedis:
    def __init__(self, response=None):
        self.response = response
        self.calls = []
        self.closed = False

    async def blpop(self, key, timeout=0):
        self.calls.append((key, timeout))
        return self.response

    async def aclose(self):
        self.closed = True


class _BlockingAsyncRedis(_FakeAsyncRedis):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def blpop(self, key, timeout=0):
        self.calls.append((key, timeout))
        self.entered.set()
        await self.release.wait()
        return key, json.dumps({"artefact_outcome": "stored", "artefact_id": None, "version": None, "hints_stored": 1, "duplicate": False})


def test_writer_rejects_bad_token():
    c = TestClient(build_writer_app(_FakeBus(), token="secret"))
    r = c.post(
        "/publish",
        json={"artefact": None, "hints": [{"text": "x"}]},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_writer_rejects_missing_token():
    c = TestClient(build_writer_app(_FakeBus(), token="secret"))
    r = c.post("/publish", json={"artefact": None, "hints": [{"text": "x"}]})
    assert r.status_code == 401


def test_writer_publishes_with_valid_token():
    fake = _FakeBus()
    c = TestClient(build_writer_app(fake, token="secret"))
    r = c.post(
        "/publish",
        json={"artefact": None, "hints": [{"text": "hello"}], "author": "cid-1"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200 and "ulid" in r.json()
    assert fake.calls


def test_writer_returns_503_on_bus_error():
    c = TestClient(build_writer_app(_FakeBus(boom=True), token="secret"))
    r = c.post(
        "/publish",
        json={"artefact": None, "hints": [{"text": "x"}], "author": "c"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 503


def test_writer_await_returns_channel_envelope_and_honors_clamped_timeout():
    sync_bus = _FakeBus()
    async_bus = _FakeAsyncRedis(
        ("arbmem:write_result:minted", json.dumps({
            "artefact_outcome": "stored",
            "artefact_id": "d.md",
            "version": 1,
            "hints_stored": 1,
            "duplicate": False,
        }))
    )
    c = TestClient(build_writer_app(sync_bus, token="secret", async_redis_client=async_bus))

    r = c.post(
        "/publish",
        json={"artefact": None, "hints": [{"text": "hello"}], "await": True, "timeout": 999},
        headers={"Authorization": "Bearer secret"},
    )

    assert r.json() == {
        "ulid": r.json()["ulid"],
        "artefact_outcome": "stored",
        "artefact_id": "d.md",
        "version": 1,
        "hints_stored": 1,
        "duplicate": False,
    }
    _, fields = sync_bus.calls[0]
    request_id = fields["request_id"]
    assert len(request_id) == 32
    assert async_bus.calls == [(f"arbmem:write_result:{request_id}", 30)]


def test_writer_rejects_client_supplied_request_id():
    c = TestClient(build_writer_app(_FakeBus(), token="secret", async_redis_client=_FakeAsyncRedis()))

    r = c.post(
        "/publish",
        json={"artefact": None, "hints": [], "await": True, "request_id": "attacker-chosen"},
        headers={"Authorization": "Bearer secret"},
    )

    assert r.status_code == 400


def test_writer_rejects_expected_version_without_await():
    """AC7: artefact.expected_version with no await → 400."""
    fake = _FakeBus()
    c = TestClient(build_writer_app(fake, token="secret", async_redis_client=_FakeAsyncRedis()))

    r = c.post(
        "/publish",
        json={
            "artefact": {"artefact_id": "doc", "content": "C", "expected_version": 1},
            "hints": [],
        },
        headers={"Authorization": "Bearer secret"},
    )

    assert r.status_code == 400
    assert r.json() == {"error": "expected_version requires await"}
    assert fake.calls == []


def test_writer_allows_expected_version_null_without_await():
    """AC12 (writer half): expected_version: null is not a 400 (is not None check)."""
    fake = _FakeBus()
    c = TestClient(build_writer_app(fake, token="secret"))

    r = c.post(
        "/publish",
        json={
            "artefact": {"artefact_id": "doc", "content": "C", "expected_version": None},
            "hints": [],
        },
        headers={"Authorization": "Bearer secret"},
    )

    assert r.status_code == 200
    assert "ulid" in r.json()
    assert fake.calls


def test_writer_shorter_await_timeout_and_timeout_response():
    async_bus = _FakeAsyncRedis()
    c = TestClient(build_writer_app(_FakeBus(), token="secret", async_redis_client=async_bus))

    r = c.post(
        "/publish",
        json={"artefact": None, "hints": [], "await": True, "timeout": 0.1},
        headers={"Authorization": "Bearer secret"},
    )

    assert async_bus.calls[0][1] == 0.1
    assert r.json() == {"ulid": r.json()["ulid"], "artefact_outcome": "unknown", "timed_out": True}


def test_writer_result_channel_raise_is_unknown_not_a_failed_write():
    """A raise in the result channel must not surface as a write failure.

    The XADD commits before the BLPOP, so past that point "not stored" is unprovable and was
    observed false in production (art-8679a50030cf5d41). Regression guard: this used to escape
    as a 500, which the door reported as "item NOT stored".
    """

    class _BoomAsyncRedis(_FakeAsyncRedis):
        async def blpop(self, key, timeout=0):
            raise ConnectionError("result channel down")

    sync_bus = _FakeBus()
    c = TestClient(build_writer_app(sync_bus, token="secret", async_redis_client=_BoomAsyncRedis()))

    r = c.post(
        "/publish",
        json={"artefact": None, "hints": [], "await": True},
        headers={"Authorization": "Bearer secret"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["artefact_outcome"] == "unknown" and body["result_channel_error"] is True
    # The write really did commit — the response must not imply otherwise.
    assert sync_bus.calls


def test_writer_non_positive_await_timeouts_are_positive_and_capped():
    for requested in (0, -5):
        async_bus = _FakeAsyncRedis()
        c = TestClient(build_writer_app(_FakeBus(), token="secret", async_redis_client=async_bus))

        r = c.post(
            "/publish",
            json={"artefact": None, "hints": [], "await": True, "timeout": requested},
            headers={"Authorization": "Bearer secret"},
        )

        assert r.status_code == 200
        forwarded = async_bus.calls[0][1]
        assert 0 < forwarded <= 30


def test_writer_await_does_not_block_a_concurrent_non_await_publish():
    async def exercise():
        sync_bus = _FakeBus()
        async_bus = _BlockingAsyncRedis()
        app = build_writer_app(sync_bus, token="secret", async_redis_client=async_bus)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://writer") as client:
            waiting = asyncio.create_task(
                client.post(
                    "/publish",
                    json={"artefact": None, "hints": [], "await": True},
                    headers={"Authorization": "Bearer secret"},
                )
            )
            await async_bus.entered.wait()
            with anyio.fail_after(0.2):
                immediate = await client.post(
                    "/publish",
                    json={"artefact": None, "hints": []},
                    headers={"Authorization": "Bearer secret"},
                )
            assert immediate.status_code == 200 and "ulid" in immediate.json()
            async_bus.release.set()
            assert (await waiting).status_code == 200

    anyio.run(exercise)
