"""Target-bound publish-and-enqueue authority (Slice 1d-iv Task 4).

Instruments the real RedisCli.rpush primitive used by the authority — not a
dispatcher callback. Stage 1d-iv is dual-accept / receive-only: the authority
must select legacy for every target that lacks brief_hydrate=v1 (and no seat
advertises that yet).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

import pytest

from agent_redis_bridge.redis_io import RedisCli, RedisConfig


TARGET = "codex-bridge-dev"
VANTAGE = "mac-host-dev"
GEN = "owner-gen-1"


def _valid_brief(vantage: str = VANTAGE) -> str:
    assumptions = {
        "items": [
            {
                "statement": "bus is up",
                "status": "assumed",
                "vantage": vantage,
            }
        ]
    }
    return (
        "# Dispatch brief — authority test\n\n"
        "## Assumptions\n```json\n"
        + json.dumps(assumptions, indent=2)
        + "\n```\n\n## Instructions\n\nRun the probe and report.\n"
    )


@dataclass
class FakeRedisClient:
    strings: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, dict[str, str]] = field(default_factory=dict)
    lists: dict[str, list[str]] = field(default_factory=dict)
    rpush_calls: list[tuple[str, str]] = field(default_factory=list)

    def hset(self, key: str, mapping: dict[str, str] | None = None, **kwargs) -> None:
        data = dict(mapping or {})
        data.update(kwargs)
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in data.items()})

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    def rpush(self, key: str, body: str) -> int:
        self.rpush_calls.append((key, body))
        self.lists.setdefault(key, []).append(body)
        return len(self.lists[key])

    def eval(self, script: str, numkeys: int, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if numkeys == 1 and "if not current" in script:
            key = keys[0]
            current = self.strings.get(key)
            owner = argv[0]
            if current is None:
                self.strings[key] = owner
                return 1
            if current == owner:
                return 1
            return current
        if numkeys == 2:
            current = self.strings.get(keys[0])
            if current == argv[0]:
                self.strings[keys[1]] = argv[1]
                return 1
            return current or ""
        return 1

    def ttl(self, key: str) -> int:
        return 60 if key in self.strings else -2

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.strings.pop(key, None)
            self.hashes.pop(key, None)
            self.lists.pop(key, None)


def make_cli() -> tuple[RedisCli, FakeRedisClient, RedisConfig]:
    config = RedisConfig(host="127.0.0.1", port="6390", db="12", prefix="agent_scratch:")
    cli = RedisCli(config)
    client = FakeRedisClient()
    cli.client = client  # type: ignore[assignment]
    return cli, client, config


def register_target(
    cli: RedisCli,
    *,
    agent_id: str = TARGET,
    vantage: str = VANTAGE,
    generation: str = GEN,
    task_wire: str = "legacy-or-ref-v1",
    brief_hydrate: str = "",
    owner_token: str = GEN,
) -> None:
    cli.register(
        agent_id=agent_id,
        tool="codex",
        project="bridge",
        workspace="dev",
        branch="dev",
        path="/tmp/repo",
        registered_at="now",
        pid=1,
        owner_token=owner_token,
        ttl=60,
        worker_vantage=vantage,
        task_wire=task_wire,
        brief_hydrate=brief_hydrate,
        registration_generation=generation,
    )


def _receipt_for(
    brief: str,
    *,
    artefact_id: str = "art-brief-1",
    version: int = 1,
    target: str = TARGET,
    generation: str = GEN,
    vantage: str = VANTAGE,
):
    from agent_redis_bridge.dispatch_authority import (
        HarnessPublishReceipt,
        content_hash_for_brief,
    )

    return HarnessPublishReceipt(
        artefact_id=artefact_id,
        version=version,
        target_agent_id=target,
        registration_generation=generation,
        worker_vantage=vantage,
        content_hash=content_hash_for_brief(brief),
    )


def _pre_minted(brief: str, receipt=None, **overrides):
    from agent_redis_bridge.dispatch_authority import PreMintedSource

    receipt = receipt or _receipt_for(brief)
    return PreMintedSource(
        artefact_id=overrides.get("artefact_id", receipt.artefact_id),
        version=overrides.get("version", receipt.version),
        receipt=receipt,
        legacy_brief_bytes=brief.encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Closed source / identity matrix
# ---------------------------------------------------------------------------


class TestSourceIdentityMatrix:
    def test_faba_draft_allowed_with_credential(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")

        def fake_publish(**kwargs):
            return _receipt_for(brief)

        result = da.publish_and_enqueue(
            da.DraftSource(brief_text=brief, artefact_id="art-brief-1"),
            target_agent_id=TARGET,
            operation="request",
            run_id="run-1",
            caller_identity="faba",
            redis_cli=cli,
            sender="orch",
            branch="dev",
            redis_url="redis://memory/0",
            publish_fn=fake_publish,
        )
        assert result["artefact_id"] == "art-brief-1"
        assert len(client.rpush_calls) == 1
        body = json.loads(client.rpush_calls[0][1])
        assert body["payload"]["task"] == brief  # legacy selection

    def test_bash_draft_forbidden(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                da.DraftSource(brief_text=_valid_brief()),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        # Exact identity refuse — not a later credential/publish failure that
        # would still fire if the draft gate were deleted.
        assert "non-faba draft forbidden" in str(exc.value).lower()
        assert client.rpush_calls == []

    @pytest.mark.parametrize("identity", ["bash", "go", "ctl", "warm", "other"])
    def test_non_faba_with_publish_credential_forbidden(self, identity, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                _pre_minted(brief),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity=identity,
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "non-faba harness publish credential forbidden" in str(exc.value)
        assert client.rpush_calls == []

    def test_ref_without_receipt_forbidden(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                {
                    "kind": "pre_minted",
                    "artefact_id": "art-1",
                    "version": 1,
                    "legacy_brief_bytes": _valid_brief().encode("utf-8"),
                },
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "ref without receipt" in str(exc.value)
        assert client.rpush_calls == []

    def test_receipt_for_other_target_forbidden(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        register_target(cli, agent_id="other-seat", generation="gen-b", owner_token="gen-b")
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        bad = _receipt_for(brief, target="other-seat", generation="gen-b")
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                _pre_minted(brief, receipt=bad),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert client.rpush_calls == []
        assert "receipt target mismatch" in str(exc.value)

    def test_receipt_wrong_generation_forbidden(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        bad = _receipt_for(brief, generation="stale-gen")
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                _pre_minted(brief, receipt=bad),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "receipt registration_generation mismatch" in str(exc.value)
        assert client.rpush_calls == []

    def test_receipt_wrong_vantage_forbidden(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        bad = _receipt_for(brief, vantage="other-vantage")
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                _pre_minted(brief, receipt=bad),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "receipt vantage mismatch" in str(exc.value)
        assert client.rpush_calls == []

    def test_legacy_bytes_hash_mismatch_forbidden(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        receipt = _receipt_for(brief)
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                da.PreMintedSource(
                    artefact_id=receipt.artefact_id,
                    version=receipt.version,
                    receipt=receipt,
                    legacy_brief_bytes=b"# different bytes\n\n## Assumptions\n```json\n{\"items\":[]}\n```\n\n## Instructions\nx\n",
                ),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "legacy bytes content hash mismatch" in str(exc.value)
        assert client.rpush_calls == []

    def test_no_ambient_publish_credential_fallback(self, monkeypatch):
        """Non-FABA must not obtain publish material from ambient env."""
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                da.DraftSource(brief_text=_valid_brief()),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="ctl",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "non-faba harness publish credential forbidden" in str(exc.value)
        assert client.rpush_calls == []


# ---------------------------------------------------------------------------
# Target freeze + zero-enqueue
# ---------------------------------------------------------------------------


class TestTargetFreezeAndEnqueue:
    def test_valid_pre_minted_enqueues_once_legacy_pointer_return(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, config = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        result = da.publish_and_enqueue(
            _pre_minted(brief),
            target_agent_id=TARGET,
            operation="request",
            run_id="run-1",
            caller_identity="bash",
            redis_cli=cli,
            sender="orch",
            branch="dev",
        )
        assert set(result) == {
            "request_id",
            "artefact_id",
            "version",
            "target_agent_id",
            "change_summary",
        }
        assert result["artefact_id"] == "art-brief-1"
        assert result["version"] == 1
        assert result["target_agent_id"] == TARGET
        assert "legacy_brief" not in result
        assert "brief" not in result or result.get("brief") is None
        assert len(client.rpush_calls) == 1
        key, body = client.rpush_calls[0]
        assert key == config.inbox_key(TARGET)
        envelope = json.loads(body)
        assert envelope["to"] == TARGET
        assert envelope["run_id"] == "run-1"
        # Stage 1d-iv: legacy wire — full brief string, not a ref object.
        assert envelope["payload"]["task"] == brief
        assert not isinstance(envelope["payload"]["task"], dict)

    def test_missing_target_zero_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                _pre_minted(brief),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "missing target" in str(exc.value)
        assert client.rpush_calls == []

    def test_stale_target_zero_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, config = make_cli()
        register_target(cli)
        # Alive registration exists, but status key is stale/missing.
        client.strings.pop(config.status_key(TARGET), None)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                _pre_minted(brief),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "stale target" in str(exc.value)
        assert client.rpush_calls == []

    def test_blank_worker_vantage_zero_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli, vantage="")
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                _pre_minted(brief),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        # Exact freeze refuse — not a later receipt mismatch that would still
        # fire if blank vantage were filled from caller text.
        assert "blank/missing worker_vantage" in str(exc.value).lower()
        assert client.rpush_calls == []

    def test_invalid_brief_zero_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        bad_brief = "# Title only\n\nno assumptions section\n"
        # Craft a receipt that matches the bad bytes so failure is validation.
        receipt = _receipt_for(bad_brief)
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                da.PreMintedSource(
                    artefact_id=receipt.artefact_id,
                    version=receipt.version,
                    receipt=receipt,
                    legacy_brief_bytes=bad_brief.encode("utf-8"),
                ),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "invalid brief" in str(exc.value)
        assert client.rpush_calls == []

    def test_publish_exception_zero_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")

        def boom(**kwargs):
            raise RuntimeError("store down")

        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                da.DraftSource(brief_text=brief, artefact_id="art-x"),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="faba",
                redis_cli=cli,
                sender="orch",
                branch="dev",
                redis_url="redis://memory/0",
                publish_fn=boom,
            )
        assert "publish exception" in str(exc.value)
        assert "RuntimeError" in str(exc.value)
        assert "store down" in str(exc.value)
        assert client.rpush_calls == []

    def test_failed_receipt_zero_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")

        def fail_receipt(**kwargs):
            raise da.DispatchAuthorityError("failed/wrong receipt")

        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                da.DraftSource(brief_text=brief, artefact_id="art-x"),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="faba",
                redis_cli=cli,
                sender="orch",
                branch="dev",
                redis_url="redis://memory/0",
                publish_fn=fail_receipt,
            )
        assert "failed/wrong receipt" in str(exc.value)
        assert client.rpush_calls == []

    def test_publish_timeout_zero_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")

        def timeout_publish(**kwargs):
            raise TimeoutError("publish receipt wait timed out")

        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                da.DraftSource(brief_text=brief, artefact_id="art-x"),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="faba",
                redis_cli=cli,
                sender="orch",
                branch="dev",
                redis_url="redis://memory/0",
                publish_fn=timeout_publish,
            )
        assert "publish exception" in str(exc.value)
        assert "TimeoutError" in str(exc.value)
        assert client.rpush_calls == []

    def test_generation_change_before_enqueue_zero(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)

        # After freeze, mutate generation so recheck fails.
        original_freeze = da.freeze_target
        calls = {"n": 0}

        def flaky_freeze(redis_cli, target_agent_id):
            calls["n"] += 1
            frozen = original_freeze(redis_cli, target_agent_id)
            if calls["n"] >= 2:
                frozen = dict(frozen)
                frozen["registration_generation"] = "mutated"
            return frozen

        with mock.patch.object(da, "freeze_target", side_effect=flaky_freeze):
            with pytest.raises(da.DispatchAuthorityError) as exc:
                da.publish_and_enqueue(
                    _pre_minted(brief),
                    target_agent_id=TARGET,
                    operation="request",
                    run_id="run-1",
                    caller_identity="bash",
                    redis_cli=cli,
                    sender="orch",
                    branch="dev",
                )
        assert "target registration_generation changed since freeze" in str(exc.value)
        assert client.rpush_calls == []

    def test_worker_vantage_change_before_enqueue_zero(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        original_freeze = da.freeze_target
        calls = {"n": 0}

        def flaky_freeze(redis_cli, target_agent_id):
            calls["n"] += 1
            frozen = original_freeze(redis_cli, target_agent_id)
            if calls["n"] >= 2:
                frozen = dict(frozen)
                frozen["worker_vantage"] = "mutated-vantage"
            return frozen

        with mock.patch.object(da, "freeze_target", side_effect=flaky_freeze):
            with pytest.raises(da.DispatchAuthorityError) as exc:
                da.publish_and_enqueue(
                    _pre_minted(brief),
                    target_agent_id=TARGET,
                    operation="request",
                    run_id="run-1",
                    caller_identity="bash",
                    redis_cli=cli,
                    sender="orch",
                    branch="dev",
                )
        assert "target worker_vantage changed since freeze" in str(exc.value)
        assert client.rpush_calls == []

    def test_task_wire_change_before_enqueue_zero(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        original_freeze = da.freeze_target
        calls = {"n": 0}

        def flaky_freeze(redis_cli, target_agent_id):
            calls["n"] += 1
            frozen = original_freeze(redis_cli, target_agent_id)
            if calls["n"] >= 2:
                frozen = dict(frozen)
                frozen["task_wire"] = "mutated-wire"
            return frozen

        with mock.patch.object(da, "freeze_target", side_effect=flaky_freeze):
            with pytest.raises(da.DispatchAuthorityError) as exc:
                da.publish_and_enqueue(
                    _pre_minted(brief),
                    target_agent_id=TARGET,
                    operation="request",
                    run_id="run-1",
                    caller_identity="bash",
                    redis_cli=cli,
                    sender="orch",
                    branch="dev",
                )
        assert "target task_wire changed since freeze" in str(exc.value)
        assert client.rpush_calls == []

    def test_brief_hydrate_change_before_enqueue_zero(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        original_freeze = da.freeze_target
        calls = {"n": 0}

        def flaky_freeze(redis_cli, target_agent_id):
            calls["n"] += 1
            frozen = original_freeze(redis_cli, target_agent_id)
            if calls["n"] >= 2:
                frozen = dict(frozen)
                frozen["brief_hydrate"] = "v1"
            return frozen

        with mock.patch.object(da, "freeze_target", side_effect=flaky_freeze):
            with pytest.raises(da.DispatchAuthorityError) as exc:
                da.publish_and_enqueue(
                    _pre_minted(brief),
                    target_agent_id=TARGET,
                    operation="request",
                    run_id="run-1",
                    caller_identity="bash",
                    redis_cli=cli,
                    sender="orch",
                    branch="dev",
                )
        assert "target brief_hydrate changed since freeze" in str(exc.value)
        assert client.rpush_calls == []

    def test_redis_freeze_recheck_outage_zero_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        original_freeze = da.freeze_target
        calls = {"n": 0}

        def outage_freeze(redis_cli, target_agent_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return original_freeze(redis_cli, target_agent_id)
            raise ConnectionError("redis freeze/recheck outage")

        with mock.patch.object(da, "freeze_target", side_effect=outage_freeze):
            with pytest.raises(ConnectionError, match="redis freeze/recheck outage"):
                da.publish_and_enqueue(
                    _pre_minted(brief),
                    target_agent_id=TARGET,
                    operation="request",
                    run_id="run-1",
                    caller_identity="bash",
                    redis_cli=cli,
                    sender="orch",
                    branch="dev",
                )
        assert client.rpush_calls == []

    def test_gated_and_exempt_stop_identically_on_missing_target(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        for lane in ("gated", "exempt"):
            client.rpush_calls.clear()
            with pytest.raises(da.DispatchAuthorityError) as exc:
                da.publish_and_enqueue(
                    _pre_minted(brief),
                    target_agent_id=TARGET,
                    operation="request",
                    run_id=f"run-{lane}",
                    caller_identity="bash",
                    redis_cli=cli,
                    sender="orch",
                    branch="dev",
                    lane=lane,
                )
            assert "missing target" in str(exc.value)
            assert client.rpush_calls == []

    def test_retarget_a_to_b_rejects_a_receipt(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli, agent_id="seat-a", generation="gen-a", owner_token="gen-a")
        register_target(cli, agent_id="seat-b", generation="gen-b", owner_token="gen-b")
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        a_receipt = _receipt_for(brief, target="seat-a", generation="gen-a")
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                _pre_minted(brief, receipt=a_receipt),
                target_agent_id="seat-b",
                operation="request",
                run_id="run-1",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        # Must refuse on target/generation binding — A's receipt cannot authorize B.
        msg = str(exc.value).lower()
        assert "target" in msg or "generation" in msg or "receipt" in msg
        assert client.rpush_calls == []

    def test_faba_draft_invokes_publish_before_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")
        order: list[str] = []

        def fake_publish(**kwargs):
            order.append("publish")
            assert client.rpush_calls == []
            return _receipt_for(brief)

        original_rpush = RedisCli.rpush

        def tracking_rpush(self, agent_id, body):
            order.append("enqueue")
            return original_rpush(self, agent_id, body)

        with mock.patch.object(RedisCli, "rpush", tracking_rpush):
            da.publish_and_enqueue(
                da.DraftSource(brief_text=brief, artefact_id="art-brief-1"),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="faba",
                redis_cli=cli,
                sender="orch",
                branch="dev",
                redis_url="redis://memory/0",
                publish_fn=fake_publish,
            )
        assert order == ["publish", "enqueue"]

    @pytest.mark.parametrize("identity", ["bash", "go", "ctl", "warm"])
    def test_non_faba_zero_publish_calls(self, identity, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        publish_calls: list[Any] = []

        def should_not_run(**kwargs):
            publish_calls.append(kwargs)
            raise AssertionError("non-FABA must not publish")

        da.publish_and_enqueue(
            _pre_minted(brief),
            target_agent_id=TARGET,
            operation="request",
            run_id="run-1",
            caller_identity=identity,
            redis_cli=cli,
            sender="orch",
            branch="dev",
            publish_fn=should_not_run,
        )
        assert publish_calls == []
        assert len(client.rpush_calls) == 1

    def test_extra_payload_does_not_outrank_typed_params(self, monkeypatch):
        """Typed key NAMES are reserved: extra_payload cannot set task/operation/lane/worktree*."""
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        da.publish_and_enqueue(
            _pre_minted(brief),
            target_agent_id=TARGET,
            operation="worktree_run",
            run_id="run-1",
            caller_identity="bash",
            redis_cli=cli,
            sender="orch",
            branch="dev",
            worktree_lease="lease-1",
            lane="exempt",
            extra_payload={
                "task": "SHOULD_NOT_WIN",
                "operation": "SHOULD_NOT_WIN",
                "worktree_lease": "SHOULD_NOT_WIN",
                "lane": "SHOULD_NOT_WIN",
                "expected_artifacts": ["report.md"],
            },
        )
        envelope = json.loads(client.rpush_calls[0][1])
        payload = envelope["payload"]
        assert payload["task"] == brief
        assert payload["operation"] == "worktree_run"
        assert payload["worktree_lease"] == "lease-1"
        assert payload["lane"] == "exempt"
        assert payload["expected_artifacts"] == ["report.md"]

    def test_extra_payload_typed_keys_dropped_at_defaults(self, monkeypatch):
        """Typed key names stay reserved; typed-path worktree MUST land.

        Split claim (rem3 W2a):
        - when ``worktree=`` is the typed param, payload carries that spec;
        - extra_payload-injected worktree (and other typed keys) stays dropped,
          exercised with a distinguishable injected name so the two sources
          cannot be confused.
        """
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        typed_wt = {"name": "typed-iso-wt", "cleanup": "keep", "base_ref": "origin/dev"}
        da.publish_and_enqueue(
            _pre_minted(brief),
            target_agent_id=TARGET,
            operation="request",
            run_id="run-1",
            caller_identity="bash",
            redis_cli=cli,
            sender="orch",
            branch="dev",
            worktree=typed_wt,
            extra_payload={
                "operation": "SHOULD_NOT_WIN",
                "lane": "SHOULD_NOT_WIN",
                "worktree": {"name": "INJECTED_SHOULD_NOT_WIN", "cleanup": "delete"},
                "worktree_lease": "SHOULD_NOT_WIN",
                "expected_artifacts": ["report.md"],
            },
        )
        envelope = json.loads(client.rpush_calls[0][1])
        payload = envelope["payload"]
        assert payload["task"] == brief
        # Typed-path worktree lands; injection does not win.
        assert payload["worktree"] == typed_wt
        assert payload["worktree"]["name"] == "typed-iso-wt"
        assert payload["worktree"]["name"] != "INJECTED_SHOULD_NOT_WIN"
        # Other typed keys still omitted at defaults / not injected.
        assert "operation" not in payload
        assert "lane" not in payload
        assert "worktree_lease" not in payload
        assert payload["expected_artifacts"] == ["report.md"]

    def test_extra_payload_worktree_dropped_when_typed_absent(self, monkeypatch):
        """extra_payload-injected worktree stays dropped when typed param is absent."""
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        da.publish_and_enqueue(
            _pre_minted(brief),
            target_agent_id=TARGET,
            operation="request",
            run_id="run-1",
            caller_identity="bash",
            redis_cli=cli,
            sender="orch",
            branch="dev",
            extra_payload={
                "operation": "SHOULD_NOT_WIN",
                "lane": "SHOULD_NOT_WIN",
                "worktree": {"name": "SHOULD_NOT_WIN"},
                "worktree_lease": "SHOULD_NOT_WIN",
                "expected_artifacts": ["report.md"],
            },
        )
        envelope = json.loads(client.rpush_calls[0][1])
        payload = envelope["payload"]
        assert payload["task"] == brief
        assert "operation" not in payload
        assert "lane" not in payload
        assert "worktree" not in payload
        assert "worktree_lease" not in payload
        assert payload["expected_artifacts"] == ["report.md"]

    def test_worktree_json_cli_seam_enqueues_payload_worktree(
        self, monkeypatch, tmp_path
    ):
        """Caller-shaped --worktree-json argv must land worktree on the rpush payload.

        Seam coverage (rem3 W2b): argv → dispatch_cli.main → envelope payload.
        """
        from agent_redis_bridge import dispatch_cli

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        receipt = _receipt_for(brief)
        brief_path = tmp_path / "brief.md"
        receipt_path = tmp_path / "receipt.json"
        brief_path.write_text(brief, encoding="utf-8")
        receipt_path.write_text(json.dumps(receipt.to_dict()), encoding="utf-8")

        # Instrument the production RedisCli constructed by main().
        def _cli_factory(config):  # noqa: ARG001
            return cli

        monkeypatch.setattr(dispatch_cli, "RedisCli", _cli_factory)
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        monkeypatch.setenv("AGENT_REDIS_HOST", "127.0.0.1")
        monkeypatch.setenv("AGENT_REDIS_PORT", "6390")
        monkeypatch.setenv("AGENT_REDIS_DB", "12")
        monkeypatch.setenv("AGENT_REDIS_PREFIX", "agent_scratch:")

        wt_spec = {"name": "cli-seam-wt", "cleanup": "keep", "base_ref": "origin/dev"}
        argv = [
            "--caller-identity",
            "bash",
            "--target-agent-id",
            TARGET,
            "--sender",
            "orch",
            "--branch",
            "dev",
            "--run-id",
            "run-seam-1",
            "--artefact-id",
            receipt.artefact_id,
            "--version",
            str(receipt.version),
            "--receipt",
            str(receipt_path),
            "--brief",
            str(brief_path),
            "--worktree-json",
            json.dumps(wt_spec),
            "--extra-payload-json",
            json.dumps({"expected_artifacts": ["report.md"]}),
        ]
        rc = dispatch_cli.main(argv)
        assert rc == 0, "dispatch_cli.main should succeed with caller-shaped argv"
        assert len(client.rpush_calls) == 1, "exactly one rpush expected"
        envelope = json.loads(client.rpush_calls[0][1])
        payload = envelope["payload"]
        assert payload["worktree"]["name"] == "cli-seam-wt"
        assert payload["worktree"]["cleanup"] == "keep"
        assert payload["worktree"]["base_ref"] == "origin/dev"
        assert payload["expected_artifacts"] == ["report.md"]

    def test_malformed_worktree_json_returns_structured_error(self, monkeypatch, capsys):
        """Malformed --worktree-json must yield single-line JSON error, not traceback (rem4 P2-4)."""
        from agent_redis_bridge import dispatch_cli

        monkeypatch.setenv("AGENT_REDIS_HOST", "127.0.0.1")
        monkeypatch.setenv("AGENT_REDIS_PORT", "6390")
        monkeypatch.setenv("AGENT_REDIS_DB", "12")
        monkeypatch.setenv("AGENT_REDIS_PREFIX", "agent_scratch:")
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)

        argv = [
            "--caller-identity",
            "bash",
            "--target-agent-id",
            TARGET,
            "--sender",
            "orch",
            "--branch",
            "dev",
            "--run-id",
            "run-bad-wt",
            "--artefact-id",
            "art-x",
            "--version",
            "1",
            "--receipt",
            "/tmp/does-not-matter.json",
            "--brief",
            "/tmp/does-not-matter.md",
            "--worktree-json",
            "{not-valid-json",
        ]
        rc = dispatch_cli.main(argv)
        assert rc == 2
        err = capsys.readouterr().err.strip()
        # Single-line JSON error object — not a Python traceback.
        assert "Traceback" not in err
        parsed = json.loads(err)
        assert parsed.get("ok") is False
        assert "worktree-json" in str(parsed.get("error", "")).lower() or "json" in str(
            parsed.get("error", "")
        ).lower()

    def test_parse_only_target_selects_legacy_not_ref(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        # dual-parse capability but NO brief_hydrate advertisement
        register_target(cli, task_wire="legacy-or-ref-v1", brief_hydrate="")
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        da.publish_and_enqueue(
            _pre_minted(brief),
            target_agent_id=TARGET,
            operation="request",
            run_id="run-1",
            caller_identity="bash",
            redis_cli=cli,
            sender="orch",
            branch="dev",
        )
        envelope = json.loads(client.rpush_calls[0][1])
        assert envelope["payload"]["task"] == brief
        assert not isinstance(envelope["payload"]["task"], dict)

    def test_selection_machinery_would_select_ref_only_with_both_caps(self):
        from agent_redis_bridge.dispatch_authority import select_wire

        assert select_wire({"task_wire": "legacy-or-ref-v1", "brief_hydrate": ""}) == "legacy"
        assert select_wire({"task_wire": "", "brief_hydrate": ""}) == "legacy"
        assert (
            select_wire({"task_wire": "legacy-or-ref-v1", "brief_hydrate": "v1"}) == "ref"
        )

    def test_warm_path_has_no_store_write_imports(self):
        """Static shape: dispatch_authority must not call store writers itself
        outside the injectable FABA publish_fn path."""
        import ast
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agent_redis_bridge"
            / "dispatch_authority.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        forbidden_calls = {"psycopg.connect", "memory_write", "store_artefact", "publish_artefact_and_gate"}
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ""
                if isinstance(node.func, ast.Attribute):
                    # obj.attr
                    parts = []
                    cur = node.func
                    while isinstance(cur, ast.Attribute):
                        parts.append(cur.attr)
                        cur = cur.value
                    if isinstance(cur, ast.Name):
                        parts.append(cur.id)
                    name = ".".join(reversed(parts))
                elif isinstance(node.func, ast.Name):
                    name = node.func.id
                for banned in forbidden_calls:
                    if name == banned or name.endswith("." + banned.split(".")[-1]):
                        # allow name only if it's a parameter default reference string
                        found.add(name)
        # publish_artefact_and_gate may be referenced for harness script import
        # path documentation, but warm publish_and_enqueue body must not call it
        # unconditionally. The AST check is soft: ensure no psycopg.connect /
        # memory_write / store_artefact in the module at all.
        assert "psycopg.connect" not in found
        assert "memory_write" not in found
        assert "store_artefact" not in found




    def test_parse_plus_hydrate_selects_ref(self, monkeypatch):
        """Authority emits ref only when both parse and hydration are advertised."""
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli, task_wire="legacy-or-ref-v1", brief_hydrate="v1")
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
        da.publish_and_enqueue(
            _pre_minted(brief),
            target_agent_id=TARGET,
            operation="request",
            run_id="run-ref-1",
            caller_identity="bash",
            redis_cli=cli,
            sender="orch",
            branch="dev",
        )
        envelope = json.loads(client.rpush_calls[0][1])
        task = envelope["payload"]["task"]
        assert isinstance(task, dict)
        assert task["artefact_id"] == "art-brief-1"
        assert task["version"] == 1
        # Body sentinel / full brief must not be on the wire as task when ref selected.
        assert brief not in json.dumps(envelope["payload"]["task"])

    def test_target_generation_change_between_ready_and_enqueue_zero(self, monkeypatch):
        """Recheck catches generation change after freeze (hydration-ready target)."""
        from agent_redis_bridge import dispatch_authority as da

        cli, client, config = make_cli()
        register_target(
            cli, task_wire="legacy-or-ref-v1", brief_hydrate="v1", generation=GEN
        )
        brief = _valid_brief()
        monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)

        real_recheck = da._recheck_frozen

        def flapping_recheck(redis_cli, frozen):
            # Mutate frozen registry field in-place (same owner) so recheck sees drift.
            reg = config.registry_key(TARGET)
            client.hashes[reg]["registration_generation"] = "gen-CHANGED"
            return real_recheck(redis_cli, frozen)

        monkeypatch.setattr(da, "_recheck_frozen", flapping_recheck)
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                _pre_minted(brief),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-gen-change",
                caller_identity="bash",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "changed since freeze" in str(exc.value)
        assert client.rpush_calls == []

class TestMutationAndCredentialKills:
    """Credential tripwire + pointer to failure-path zero-enqueue polarity.

    Guard polarity (raise on a failure path coincides with ``rpush_calls == []``)
    is asserted by the zero-enqueue suite (e.g.
    ``test_missing_target_zero_enqueue``) — not re-duplicated here. The Step-5
    production-mutation procedure (enqueue-then-raise in production) is run
    out-of-band in the worktree, not as an in-repo fake-mutation test.
    """

    def test_credential_tripwire_before_enqueue(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://leaked/0")
        with pytest.raises(da.DispatchAuthorityError) as exc:
            da.publish_and_enqueue(
                da.DraftSource(brief_text=_valid_brief()),
                target_agent_id=TARGET,
                operation="request",
                run_id="run-1",
                caller_identity="go",
                redis_cli=cli,
                sender="orch",
                branch="dev",
            )
        assert "non-faba harness publish credential forbidden" in str(exc.value)
        assert client.rpush_calls == []


class TestHarnessPublishScript:
    def test_script_exists_and_is_executable_entrypoint(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "arb-memory-harness-publish"
        assert script.is_file()
        text = script.read_text(encoding="utf-8")
        assert "publish" in text.lower()
        assert "enqueue" not in text.lower() or "never enqueue" in text.lower() or "does not enqueue" in text.lower()

    def test_harness_publish_stdout_is_flat_receipt_contract(
        self, monkeypatch, capsys, tmp_path
    ):
        """R1 producer-side contract: stdout is the flat receipt, not wrapped.

        Exercises the script's actual success path (``main()`` → the live
        ``print(...)`` at the emit call site) with publish/Redis deps stubbed.
        RED if the call site re-wraps as ``{ok, receipt}`` while the helper
        stays flat — the exact gap the rem1 helper-only pin missed.
        """
        import json
        from pathlib import Path

        from agent_redis_bridge import dispatch_authority as da

        root = Path(__file__).resolve().parents[1]
        script_path = root / "scripts" / "arb-memory-harness-publish"
        brief = _valid_brief()
        brief_path = tmp_path / "brief.md"
        brief_path.write_text(brief, encoding="utf-8")
        receipt = _receipt_for(brief)

        def fake_publish(**kwargs):
            return receipt

        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")
        monkeypatch.setenv("AGENT_REDIS_HOST", "127.0.0.1")
        monkeypatch.setenv("AGENT_REDIS_PORT", "6390")
        monkeypatch.setenv("AGENT_REDIS_DB", "12")
        monkeypatch.setenv("AGENT_REDIS_PREFIX", "agent_scratch:")
        # Patch before main's late import binds the name.
        monkeypatch.setattr(da, "harness_publish", fake_publish)

        ns: dict = {
            "__name__": "arb_memory_harness_publish",
            "__file__": str(script_path),
        }
        exec(compile(script_path.read_text(encoding="utf-8"), str(script_path), "exec"), ns)
        assert "main" in ns

        rc = ns["main"](
            [
                "--target-agent-id",
                TARGET,
                "--brief",
                str(brief_path),
                "--redis-url",
                "redis://memory/0",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)

        # (i) parses through HarnessPublishReceipt.from_dict
        reconstructed = da.HarnessPublishReceipt.from_dict(parsed)
        assert reconstructed.artefact_id == receipt.artefact_id
        assert reconstructed.version == receipt.version
        assert reconstructed.content_hash == receipt.content_hash

        # (ii) top-level artefact_id (jq -r .artefact_id contract)
        assert "artefact_id" in parsed
        assert parsed["artefact_id"] == receipt.artefact_id
        # Must NOT be the wrapped shape consumers cannot read.
        assert "receipt" not in parsed or not isinstance(parsed.get("receipt"), dict)
        assert "ok" not in parsed

    def test_harness_publish_returns_receipt_without_rpush(self, monkeypatch):
        from agent_redis_bridge import dispatch_authority as da

        cli, client, _ = make_cli()
        register_target(cli)
        brief = _valid_brief()
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")

        def fake_gate(**kwargs):
            return _receipt_for(brief, artefact_id=kwargs.get("artefact_id") or "art-brief-1")

        receipt = da.harness_publish(
            brief_text=brief,
            target_agent_id=TARGET,
            redis_cli=cli,
            redis_url="redis://memory/0",
            artefact_id="art-brief-1",
            publish_fn=fake_gate,
        )
        assert receipt.artefact_id == "art-brief-1"
        assert receipt.target_agent_id == TARGET
        assert receipt.worker_vantage == VANTAGE
        assert receipt.registration_generation == GEN
        assert client.rpush_calls == []  # never enqueues
