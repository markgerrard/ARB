import json

from scripts.claude_tail_hooks import common, subagent_start, subagent_stop


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_cold_dir_defaults_to_claude_tasks(monkeypatch):
    monkeypatch.delenv("ARB_CLAUDE_TAIL_COLD_DIR", raising=False)
    assert common.cold_dir() == (__import__("pathlib").Path("~/.claude/tasks").expanduser())


def test_cold_dir_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(tmp_path / "custom"))
    assert common.cold_dir() == tmp_path / "custom"


def test_cold_agent_types_defaults_to_code_reviewer_report_writer(monkeypatch):
    monkeypatch.delenv("ARB_CLAUDE_TAIL_COLD_AGENT_TYPES", raising=False)
    assert common.cold_agent_types() == {"code-reviewer-report-writer"}


def test_cold_agent_types_parses_comma_separated_override(monkeypatch):
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_AGENT_TYPES", "code-reviewer-report-writer, arb-design-panelist")
    assert common.cold_agent_types() == {"code-reviewer-report-writer", "arb-design-panelist"}


def test_lookup_registry_record_finds_match_in_file_registry(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [
        {"session_id": "sess-1", "transcript_path": "/x/sess-1.jsonl", "seat_id": "claude-bridge-dev"},
        {"session_id": "sess-2", "transcript_path": "/x/sess-2.jsonl", "seat_id": "claude-other-dev"},
    ])

    record = common.lookup_registry_record("sess-2")

    assert record == {"session_id": "sess-2", "transcript_path": "/x/sess-2.jsonl", "seat_id": "claude-other-dev"}


def test_lookup_registry_record_returns_none_on_miss(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [{"session_id": "sess-1", "transcript_path": "/x/sess-1.jsonl"}])

    assert common.lookup_registry_record("sess-missing") is None


def test_lookup_registry_record_uses_redis_when_no_registry_path(monkeypatch):
    class FakeRedisClient:
        def __init__(self):
            self.hashes = {}

        def hgetall(self, key):
            return self.hashes.get(key, {})

        def hset(self, key, field, value):
            self.hashes.setdefault(key, {})[field] = value

    class FakeRedisFactory:
        client = FakeRedisClient()

        @staticmethod
        def from_url(url, **kwargs):
            return FakeRedisFactory.client

    monkeypatch.delenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("AGENT_REDIS_URL", "redis://example/0")
    monkeypatch.setattr("redis.Redis", FakeRedisFactory)
    common.upsert_redis_record(FakeRedisFactory.client, {
        "session_id": "sess-9", "transcript_path": "/x/sess-9.jsonl", "seat_id": "claude-bridge-dev",
    })

    record = common.lookup_registry_record("sess-9")

    assert record["transcript_path"] == "/x/sess-9.jsonl"


def test_subagent_start_noop_for_disallowed_agent_type(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_AGENT_TYPES", "code-reviewer-report-writer")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(tmp_path / "registry.json"))

    rc = subagent_start.main([json.dumps({
        "session_id": "sess-1", "agent_id": "agent-1", "agent_type": "Explore", "cwd": "/Users/<user>/<workspace>",
    })])

    assert rc == 0
    assert not cold_dir_path.exists()


def test_subagent_start_noop_when_parent_session_not_in_registry(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [])

    rc = subagent_start.main([json.dumps({
        "session_id": "sess-missing", "agent_id": "agent-1",
        "agent_type": "code-reviewer-report-writer", "cwd": "/Users/<user>/<workspace>",
    })])

    assert rc == 0
    assert not (cold_dir_path / "agent-1.output").exists()


def test_subagent_start_creates_symlink_and_sidecar_for_allowed_type(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    registry = tmp_path / "registry.json"
    parent_transcript = tmp_path / "projects" / "-Users-mark-<workspace>" / "sess-1.jsonl"
    parent_transcript.parent.mkdir(parents=True)
    parent_transcript.write_text("", encoding="utf-8")
    # The real Claude Code layout nests subagent transcripts under a directory matching the
    # parent session id -- a SIBLING of the flat parent .jsonl file, not a child of its parent
    # directory. Create that real file independently of any path-construction formula under
    # test (no .parent / .with_suffix reuse here), so this assertion can't pass tautologically
    # against a buggy formula the way the original version of this test did.
    real_subagent_dir = tmp_path / "projects" / "-Users-mark-<workspace>" / "sess-1" / "subagents"
    real_subagent_dir.mkdir(parents=True)
    real_subagent_transcript = real_subagent_dir / "agent-agent-1.jsonl"
    real_subagent_transcript.write_text("", encoding="utf-8")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [{
        "session_id": "sess-1", "transcript_path": str(parent_transcript), "seat_id": "claude-bridge-dev",
    }])

    rc = subagent_start.main([json.dumps({
        "session_id": "sess-1", "agent_id": "agent-1",
        "agent_type": "code-reviewer-report-writer", "cwd": "/Users/<user>/<workspace>",
    })])

    assert rc == 0
    output_link = cold_dir_path / "agent-1.output"
    assert output_link.is_symlink()
    assert output_link.exists()  # NOT dangling -- mirrors what service._is_recent()'s stat() needs
    assert output_link.resolve() == real_subagent_transcript.resolve()
    sidecar = _read_json(cold_dir_path / "agent-1.arb-tail.json")
    assert sidecar == {"orchestrator": "claude-bridge-dev", "completed": False}


def test_subagent_start_is_idempotent(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    registry = tmp_path / "registry.json"
    parent_transcript = tmp_path / "projects" / "-Users-mark-<workspace>" / "sess-1.jsonl"
    parent_transcript.parent.mkdir(parents=True)
    parent_transcript.write_text("", encoding="utf-8")
    real_subagent_dir = tmp_path / "projects" / "-Users-mark-<workspace>" / "sess-1" / "subagents"
    real_subagent_dir.mkdir(parents=True)
    (real_subagent_dir / "agent-agent-1.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(registry))
    common.write_registry(registry, [{
        "session_id": "sess-1", "transcript_path": str(parent_transcript), "seat_id": "claude-bridge-dev",
    }])
    payload = [json.dumps({
        "session_id": "sess-1", "agent_id": "agent-1",
        "agent_type": "code-reviewer-report-writer", "cwd": "/Users/<user>/<workspace>",
    })]

    assert subagent_start.main(payload) == 0
    assert subagent_start.main(payload) == 0  # must not raise on re-invocation

    output_link = cold_dir_path / "agent-1.output"
    assert output_link.is_symlink()
    assert output_link.exists()  # NOT dangling


def test_subagent_start_fails_soft_on_bad_input(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ARB_CLAUDE_TAIL_REGISTRY_PATH", str(tmp_path / "registry.json"))

    rc = subagent_start.main(["{bad-json"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "claude-tail hook error:" in captured.err
    assert "Traceback" not in captured.err


def test_subagent_stop_rewrites_completed_true_without_deleting(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    cold_dir_path.mkdir()
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    output_link = cold_dir_path / "agent-1.output"
    output_link.symlink_to(tmp_path / "does-not-need-to-exist.jsonl")
    sidecar = cold_dir_path / "agent-1.arb-tail.json"
    sidecar.write_text(json.dumps({"orchestrator": "claude-bridge-dev", "completed": False}), encoding="utf-8")

    rc = subagent_stop.main([json.dumps({"agent_id": "agent-1"})])

    assert rc == 0
    assert output_link.is_symlink()  # not deleted
    assert _read_json(sidecar) == {"orchestrator": "claude-bridge-dev", "completed": True}


def test_subagent_stop_noop_when_sidecar_missing(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    cold_dir_path.mkdir()
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))

    rc = subagent_stop.main([json.dumps({"agent_id": "agent-unknown"})])

    assert rc == 0
    assert list(cold_dir_path.iterdir()) == []


def test_subagent_stop_is_idempotent(tmp_path, monkeypatch):
    cold_dir_path = tmp_path / "tasks"
    cold_dir_path.mkdir()
    monkeypatch.setenv("ARB_CLAUDE_TAIL_COLD_DIR", str(cold_dir_path))
    sidecar = cold_dir_path / "agent-1.arb-tail.json"
    sidecar.write_text(json.dumps({"orchestrator": "claude-bridge-dev", "completed": False}), encoding="utf-8")
    payload = [json.dumps({"agent_id": "agent-1"})]

    assert subagent_stop.main(payload) == 0
    assert subagent_stop.main(payload) == 0

    assert _read_json(sidecar) == {"orchestrator": "claude-bridge-dev", "completed": True}


def test_subagent_stop_fails_soft_on_bad_input(monkeypatch, capsys):
    rc = subagent_stop.main(["{bad-json"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "claude-tail hook error:" in captured.err
    assert "Traceback" not in captured.err
