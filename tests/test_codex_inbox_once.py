from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "codex-inbox-once"
AGENT = "codex-arbmem-prod"
INBOX = f"agent_scratch:agent:{AGENT}:inbox"


def _fake_redis(tmp_path: Path, responses: list[tuple[int, str, str]]) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "calls"
    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps(responses))
    command = fake_bin / "redis-cli"
    command.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ["FAKE_REDIS_STATE"])
fixture = pathlib.Path(os.environ["FAKE_REDIS_RESPONSES"])
try:
    index = int(state.read_text())
except FileNotFoundError:
    index = 0
state.write_text(str(index + 1))
responses = json.loads(fixture.read_text())
status, stdout, stderr = responses[min(index, len(responses) - 1)]
pathlib.Path(os.environ["FAKE_REDIS_ARGS"]).write_text(json.dumps(sys.argv[1:]))
sys.stdout.write(stdout)
sys.stderr.write(stderr)
raise SystemExit(status)
"""
    )
    command.chmod(0o755)
    return fake_bin, fixture


def _run(tmp_path: Path, responses: list[tuple[int, str, str]]) -> subprocess.CompletedProcess[str]:
    fake_bin, fixture = _fake_redis(tmp_path, responses)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AGENT_ID": AGENT,
        "AGENT_REDIS_HOST": "unused",
        "AGENT_REDIS_PORT": "6379",
        "AGENT_REDIS_DB": "12",
        "AGENT_REDIS_PREFIX": "agent_scratch:",
        "AGENT_BLPOP_TIMEOUT": "1",
        "CODEX_INBOX_REJECT_DIR": str(tmp_path / "rejects"),
        "FAKE_REDIS_STATE": str(tmp_path / "calls"),
        "FAKE_REDIS_RESPONSES": str(fixture),
        "FAKE_REDIS_ARGS": str(tmp_path / "args.json"),
    }
    return subprocess.run(
        [str(SCRIPT)], env=env, text=True, capture_output=True, timeout=10, check=False
    )


def _hit(value: str, key: str = INBOX) -> str:
    return json.dumps([key, value]) + "\n"


def test_single_line_transport_artifact_is_preserved_but_not_consumed_as_message(tmp_path):
    envelope = json.dumps({"id": "ok", "to": AGENT, "kind": "notify", "payload": {}})
    result = _run(
        tmp_path,
        [(0, "synthetic-single-line\n", ""), (0, _hit(envelope), "")],
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["id"] == "ok"
    assert "invalid redis-cli response" in result.stderr
    preserved = list((tmp_path / "rejects").glob("transport-shape-*.json"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "synthetic-single-line\n"
    args = json.loads((tmp_path / "args.json").read_text())
    assert "--json" in args and "-e" in args


def test_real_malformed_list_value_is_retained_before_exit_65(tmp_path):
    result = _run(tmp_path, [(0, _hit("not-json"), "")])

    assert result.returncode == 65
    assert "invalid envelope" in result.stderr
    bodies = list((tmp_path / "rejects").glob("rejected-body-*.bin"))
    raw = list((tmp_path / "rejects").glob("rejected-redis-*.json"))
    assert len(bodies) == len(raw) == 1
    assert bodies[0].read_bytes() == b"not-json"
    assert json.loads(raw[0].read_text()) == [INBOX, "not-json"]
    assert bodies[0].stat().st_mode & 0o777 == 0o600
    assert bodies[0].parent.stat().st_mode & 0o777 == 0o700


def test_wrong_recipient_is_retained_before_exit_65(tmp_path):
    envelope = json.dumps({"id": "wrong", "to": "someone-else", "kind": "notify"})
    result = _run(tmp_path, [(0, _hit(envelope), "")])

    assert result.returncode == 65
    body = next((tmp_path / "rejects").glob("rejected-body-*.bin"))
    assert json.loads(body.read_text())["id"] == "wrong"


def test_redis_command_error_is_preserved_and_retried_internally(tmp_path):
    envelope = json.dumps({"id": "after-reconnect", "to": AGENT, "kind": "notify"})
    result = _run(
        tmp_path,
        [(1, "", "synthetic connection failure\n"), (0, _hit(envelope), "")],
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["id"] == "after-reconnect"
    assert "redis-cli rc=1" in result.stderr
    diagnostic = next((tmp_path / "rejects").glob("transport-*.txt"))
    assert diagnostic.read_text() == "synthetic connection failure\n"


def test_blpop_timeout_json_null_is_not_spooled_as_an_item(tmp_path):
    envelope = json.dumps({"id": "after-timeout", "to": AGENT, "kind": "notify"})
    result = _run(tmp_path, [(0, "null\n", ""), (0, _hit(envelope), "")])

    assert result.returncode == 0
    assert json.loads(result.stdout)["id"] == "after-timeout"
    assert not (tmp_path / "rejects").exists()
