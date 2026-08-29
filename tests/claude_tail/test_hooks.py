import json

from scripts.claude_tail_hooks import session_end, session_start


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_session_start_upserts_service_registry_record(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    transcript = tmp_path / "transcript.jsonl"
    cwd = tmp_path / "Bridge Dev"
    transcript.write_text("", encoding="utf-8")
    cwd.mkdir()
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_WORKSPACE", "pi-fidelity")

    rc = session_start.main(
        [
            json.dumps(
                {
                    "session_id": "sess-1",
                    "transcript_path": str(transcript),
                    "cwd": str(cwd),
                }
            )
        ]
    )

    assert rc == 0
    assert _read_json(registry) == [
        {
            "session_id": "sess-1",
            "transcript_path": str(transcript),
            "seat_id": "claude-bridge-dev-pi-fidelity",
            "run_id": "sess-1",
        }
    ]


def test_session_start_project_env_override_pins_seat_id(tmp_path, monkeypatch):
    # cwd basename "<workspace>" slugs to "workspace-dev"; the env overrides pin the
    # seat to the established bridge convention claude-bridge-dev regardless.
    registry = tmp_path / "registry.json"
    transcript = tmp_path / "transcript.jsonl"
    cwd = tmp_path / "<workspace>"
    transcript.write_text("", encoding="utf-8")
    cwd.mkdir()
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_PROJECT", "bridge")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_WORKSPACE", "dev")

    rc = session_start.main(
        [json.dumps({"session_id": "sess-1", "transcript_path": str(transcript), "cwd": str(cwd)})]
    )

    assert rc == 0
    assert _read_json(registry)[0]["seat_id"] == "claude-bridge-dev"


def test_session_start_replaces_existing_record_and_session_end_removes(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    transcript_a = tmp_path / "a.jsonl"
    transcript_b = tmp_path / "b.jsonl"
    transcript_a.write_text("", encoding="utf-8")
    transcript_b.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))

    assert session_start.main([json.dumps({"session_id": "sess-1", "transcript_path": str(transcript_a), "project": "bridge", "workspace": "dev"})]) == 0
    assert session_start.main([json.dumps({"session_id": "sess-1", "transcript_path": str(transcript_b), "project": "bridge", "workspace": "dev"})]) == 0
    assert len(_read_json(registry)) == 1
    assert _read_json(registry)[0]["transcript_path"] == str(transcript_b)

    assert session_end.main([json.dumps({"session_id": "sess-1"})]) == 0

    assert _read_json(registry) == []


def test_session_start_and_end_update_redis_registry(monkeypatch, tmp_path):
    class FakeRedisClient:
        def __init__(self):
            self.hashes = {}
            self.values = {}

        def hget(self, key, field):
            return self.hashes.get(key, {}).get(field)

        def hgetall(self, key):
            return self.hashes.get(key, {})

        def hset(self, key, field, value):
            self.hashes.setdefault(key, {})[field] = value

        def hdel(self, key, field):
            self.hashes.setdefault(key, {}).pop(field, None)

        def set(self, key, value, ex=None):
            self.values[key] = value

    class FakeRedisFactory:
        client = FakeRedisClient()

        @staticmethod
        def from_url(url, **kwargs):
            return FakeRedisFactory.client

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.delenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("AGENT_REDIS_URL", "redis://example/0")
    monkeypatch.setenv("AGENT_REDIS_PREFIX", "p:")
    monkeypatch.setattr("redis.Redis", FakeRedisFactory)

    assert session_start.main([json.dumps({"session_id": "sess-1", "transcript_path": str(transcript), "project": "bridge", "workspace": "dev"})]) == 0

    key = "p:claude:registry"
    assert json.loads(FakeRedisFactory.client.hashes[key]["sess-1"]) == {
        "session_id": "sess-1",
        "transcript_path": str(transcript),
        "seat_id": "claude-bridge-dev",
        "run_id": "sess-1",
    }

    assert session_end.main([json.dumps({"session_id": "sess-1"})]) == 0
    assert FakeRedisFactory.client.hashes[key] == {}


def test_session_end_keeps_other_redis_sessions(monkeypatch, tmp_path):
    # sess-1 and sess-2 are different seats (different workspace) — unrelated to
    # the same-seat eviction behaviour exercised below. This test is specifically
    # about session_end only ever touching its own record.
    class FakeRedisClient:
        def __init__(self):
            self.hashes = {}
            self.values = {}

        def hget(self, key, field):
            return self.hashes.get(key, {}).get(field)

        def hgetall(self, key):
            return self.hashes.get(key, {})

        def hset(self, key, field, value):
            self.hashes.setdefault(key, {})[field] = value

        def hdel(self, key, field):
            self.hashes.setdefault(key, {}).pop(field, None)

        def set(self, key, value, ex=None):
            self.values[key] = value

    class FakeRedisFactory:
        client = FakeRedisClient()

        @staticmethod
        def from_url(url, **kwargs):
            return FakeRedisFactory.client

    transcript_a = tmp_path / "a.jsonl"
    transcript_b = tmp_path / "b.jsonl"
    transcript_a.write_text("", encoding="utf-8")
    transcript_b.write_text("", encoding="utf-8")
    monkeypatch.delenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("AGENT_REDIS_PREFIX", raising=False)
    monkeypatch.setenv("AGENT_REDIS_URL", "redis://example/0")
    monkeypatch.setattr("redis.Redis", FakeRedisFactory)

    assert session_start.main([json.dumps({"session_id": "sess-1", "transcript_path": str(transcript_a), "project": "bridge", "workspace": "dev"})]) == 0
    assert session_start.main([json.dumps({"session_id": "sess-2", "transcript_path": str(transcript_b), "project": "bridge", "workspace": "other"})]) == 0
    assert session_end.main([json.dumps({"session_id": "sess-1"})]) == 0

    key = "agent_scratch:claude:registry"
    assert sorted(FakeRedisFactory.client.hashes[key]) == ["sess-2"]


def test_session_start_evicts_stale_same_seat_record(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry.json"
    transcript_a = tmp_path / "a.jsonl"
    transcript_b = tmp_path / "b.jsonl"
    transcript_a.write_text("", encoding="utf-8")
    transcript_b.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))

    assert session_start.main([json.dumps({"session_id": "sess-1", "transcript_path": str(transcript_a), "project": "bridge", "workspace": "dev"})]) == 0
    assert session_start.main([json.dumps({"session_id": "sess-2", "transcript_path": str(transcript_b), "project": "bridge", "workspace": "dev"})]) == 0

    records = _read_json(registry)
    assert [r["session_id"] for r in records] == ["sess-2"]

    captured = capsys.readouterr()
    assert "evicted stale registry entry sess-1 for seat claude-bridge-dev" in captured.err


def test_session_start_does_not_evict_different_seat_records(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    transcript_a = tmp_path / "a.jsonl"
    transcript_b = tmp_path / "b.jsonl"
    transcript_a.write_text("", encoding="utf-8")
    transcript_b.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))

    assert session_start.main([json.dumps({"session_id": "sess-1", "transcript_path": str(transcript_a), "project": "bridge", "workspace": "dev"})]) == 0
    assert session_start.main([json.dumps({"session_id": "sess-2", "transcript_path": str(transcript_b), "project": "other-project", "workspace": "dev"})]) == 0

    records = _read_json(registry)
    assert sorted(r["session_id"] for r in records) == ["sess-1", "sess-2"]


def test_session_start_evicts_stale_same_seat_redis_record(monkeypatch, tmp_path, capsys):
    class FakeRedisClient:
        def __init__(self):
            self.hashes = {}

        def hgetall(self, key):
            return self.hashes.get(key, {})

        def hset(self, key, field, value):
            self.hashes.setdefault(key, {})[field] = value

        def hdel(self, key, field):
            self.hashes.setdefault(key, {}).pop(field, None)

    class FakeRedisFactory:
        client = FakeRedisClient()

        @staticmethod
        def from_url(url, **kwargs):
            return FakeRedisFactory.client

    transcript_a = tmp_path / "a.jsonl"
    transcript_b = tmp_path / "b.jsonl"
    transcript_a.write_text("", encoding="utf-8")
    transcript_b.write_text("", encoding="utf-8")
    monkeypatch.delenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("AGENT_REDIS_URL", "redis://example/0")
    monkeypatch.setattr("redis.Redis", FakeRedisFactory)

    assert session_start.main([json.dumps({"session_id": "sess-1", "transcript_path": str(transcript_a), "project": "bridge", "workspace": "dev"})]) == 0
    assert session_start.main([json.dumps({"session_id": "sess-2", "transcript_path": str(transcript_b), "project": "bridge", "workspace": "dev"})]) == 0

    key = "agent_scratch:claude:registry"
    assert sorted(FakeRedisFactory.client.hashes[key]) == ["sess-2"]

    captured = capsys.readouterr()
    assert "evicted stale registry entry sess-1 for seat claude-bridge-dev" in captured.err


def test_session_start_mirrors_cold_output_files(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    cold_source = tmp_path / "agent-1.output"
    cold_dir = tmp_path / "tasks"
    cold_source.write_text("cold output", encoding="utf-8")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir))

    rc = session_start.main(
        [
            json.dumps(
                {
                    "session_id": "sess-1",
                    "transcript_path": str(tmp_path / "warm.jsonl"),
                    "project": "bridge",
                    "workspace": "dev",
                    "cold_output_paths": [str(cold_source)],
                }
            )
        ]
    )

    mirrored = cold_dir / "agent-1.output"
    assert rc == 0
    assert mirrored.exists()
    assert mirrored.read_text(encoding="utf-8") == "cold output"


def test_hooks_fail_soft_on_bad_input(monkeypatch, capsys):
    monkeypatch.delenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("AGENT_REDIS_URL", raising=False)

    assert session_start.main(["{bad-json"]) == 0
    assert session_end.main([json.dumps({})]) == 0

    captured = capsys.readouterr()
    assert "claude-tail hook error:" in captured.err
    assert "Traceback" not in captured.err


def test_mirror_skips_self_and_existing_real_file(tmp_path, monkeypatch):
    cold_dir = tmp_path / "tasks"
    cold_dir.mkdir()
    source = cold_dir / "agent-1.output"
    source.write_text("original", encoding="utf-8")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(tmp_path / "registry.json"))

    assert session_start.main(
        [
            json.dumps(
                {
                    "session_id": "sess-1",
                    "transcript_path": str(tmp_path / "warm.jsonl"),
                    "project": "bridge",
                    "workspace": "dev",
                    "cold_output_paths": [str(source)],
                }
            )
        ]
    ) == 0

    assert source.read_text(encoding="utf-8") == "original"


def test_session_end_writes_draining_record_before_removing_registry(monkeypatch):
    # spec §A (panel r6 codex P1): hook-side write-then-delete closes the
    # crash window between registry removal and the daemon's next tick.
    from scripts.claude_tail_hooks import common, session_end

    class Client:
        def __init__(self):
            self.hashes = {
                "agent_scratch:claude:registry": {
                    "s1": json.dumps(
                        {"session_id": "s1", "transcript_path": "/tmp/t.jsonl"}
                    )
                }
            }
            self.values = {}
            self.ops = []

        def hget(self, key, field):
            self.ops.append(("hget", key, field))
            return self.hashes.get(key, {}).get(field)

        def hdel(self, key, field):
            self.ops.append(("hdel", key, field))
            self.hashes.get(key, {}).pop(field, None)

        def set(self, key, value, ex=None):
            self.ops.append(("set", key))
            self.values[key] = value

    client = Client()
    monkeypatch.delenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("AGENT_REDIS_URL", "redis://ignored")
    monkeypatch.setattr(common, "redis_client", lambda: client)
    monkeypatch.setattr(session_end, "redis_client", lambda: client)

    rc = session_end.main([json.dumps({"session_id": "s1"})])

    assert rc == 0
    assert client.values["agent_scratch:claude:draining:s1"]  # record written
    assert "s1" not in client.hashes["agent_scratch:claude:registry"]  # then removed
    set_idx = client.ops.index(("set", "agent_scratch:claude:draining:s1"))
    hdel_idx = client.ops.index(("hdel", "agent_scratch:claude:registry", "s1"))
    assert set_idx < hdel_idx  # WRITE-THEN-DELETE ordering is the whole point
