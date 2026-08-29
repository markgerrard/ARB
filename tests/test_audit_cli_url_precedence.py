"""ARB_AUDIT_* must win over ARB_MEMORY_* in EVERY audit CLI, not just the ones we noticed.

`a79f14bf` fixed `arb-audit-emit` and `arb-panel-vote` and missed `arb-audit-close-request`,
which kept resolving `ARB_MEMORY_REDIS_URL` while writing into the `arbmem:audit:*` namespace.
That was invisible while one shared `default` user served both planes and became a hard NOPERM
the morning per-role ACLs landed, blocking a live cutover at step 3.

The existing CLI tests all inject `redis_factory`, so the URL-resolution branch never executed
and could not fail. These tests drive the real branch by faking `redis.from_url`, and they are
parameterised over the whole family so a fourth CLI cannot be added — or a third fixed — while
leaving a sibling behind.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
AUDIT_URL = "redis://audit-emitter:pw@bus.example:6379/5"
MEMORY_URL = "redis://memory-writer:pw@bus.example:6379/5"


def _load(name: str):
    loader = SourceFileLoader(name.replace("-", "_"), str(SCRIPTS / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRedis:
    """Accepts every call the audit CLIs make; records nothing anyone asserts on here."""

    def __init__(self):
        self.seq = 0

    def incr(self, *_a, **_k):
        self.seq += 1
        return self.seq

    def expire(self, *_a, **_k):
        return True

    def xadd(self, *_a, **_k):
        return "1-0"

    def blpop(self, key, timeout):  # close-request waits for the consumer's reply
        return key, json.dumps({"outcome": "emitted", "exit_code": 0, "gaps": []})


def _invoke(name: str, tmp_path: Path):
    module = _load(name)
    if name == "arb-audit-close-request":
        payload = tmp_path / "verdict.json"
        payload.write_text('{"kind":"verdict"}', encoding="utf-8")
        return module, ["--run-id", "run-1", "--payload-file", str(payload), "--timeout", "1"]
    if name == "arb-audit-emit":
        return module, ["--run-id", "run-1", "--kind", "dispatch", "--source", "orch"]
    # arb-panel-vote reads the seat's reply from stdin; --timed-out supplies a stance
    # without one, keeping this test about URL resolution rather than stance parsing.
    return module, ["--run-id", "run-1", "--actor", "seat:x", "--timed-out"]


@pytest.fixture()
def captured_urls(monkeypatch):
    """Fake `redis.from_url` so the real resolution branch runs and records its URL."""
    urls: list[str] = []
    fake = _FakeRedis()

    real_redis = sys.modules.get("redis")
    stub = type(sys)("redis")
    stub.from_url = lambda url, **_k: (urls.append(url), fake)[1]
    stub.exceptions = getattr(real_redis, "exceptions", None)
    monkeypatch.setitem(sys.modules, "redis", stub)
    return urls


@pytest.mark.parametrize(
    "script", ["arb-audit-emit", "arb-panel-vote", "arb-audit-close-request"]
)
def test_audit_url_wins_over_memory_url(script, tmp_path, monkeypatch, captured_urls):
    """The bug: close-request took MEMORY_URL here and hit NOPERM on the audit namespace."""
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", MEMORY_URL)
    monkeypatch.setenv("ARB_AUDIT_REDIS_URL", AUDIT_URL)
    module, argv = _invoke(script, tmp_path)

    module.main(argv)

    assert captured_urls == [AUDIT_URL], f"{script} resolved {captured_urls} not the audit URL"


@pytest.mark.parametrize(
    "script", ["arb-audit-emit", "arb-panel-vote", "arb-audit-close-request"]
)
def test_memory_url_is_the_fallback_when_audit_url_is_unset(
    script, tmp_path, monkeypatch, captured_urls
):
    """Hosts that never split their planes must keep working on ARB_MEMORY_REDIS_URL alone."""
    monkeypatch.delenv("ARB_AUDIT_REDIS_URL", raising=False)
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", MEMORY_URL)
    module, argv = _invoke(script, tmp_path)

    module.main(argv)

    assert captured_urls == [MEMORY_URL]


@pytest.mark.parametrize(
    "script", ["arb-audit-emit", "arb-panel-vote", "arb-audit-close-request"]
)
def test_neither_url_set_is_refused_rather_than_connected(
    script, tmp_path, monkeypatch, captured_urls
):
    monkeypatch.delenv("ARB_AUDIT_REDIS_URL", raising=False)
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    module, argv = _invoke(script, tmp_path)

    assert module.main(argv) == 2
    assert captured_urls == []
