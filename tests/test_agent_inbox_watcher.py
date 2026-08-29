"""Transport faults must never be rendered as peer messages.

The watcher used to read BLPOP output with `tail -n +2`, discard stderr and
swallow redis-cli's exit status. Any single-line stdout therefore became an
EMPTY body, which the split-filter spooled as a corrupt "UNPARSEABLE
envelope" — a phantom message manufactured from a connection hiccup
(observed 2026-08-11; the sibling defect in scripts/codex-inbox-once was
found and fixed the same day).

Every test here drives the real script through a fake redis-cli, and every
one of them fails against the pre-fix script.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[1]
WATCHER = REPO / "scripts" / "agent-inbox-watcher"
FILTER = REPO / "scripts" / "inbox-split-filter.py"
AGENT = "claude-arbcomms-arbbuzz"
INBOX = f"agent_scratch:agent:{AGENT}:inbox"

FAKE_REDIS = '''#!/usr/bin/env python3
import json, os, pathlib, sys

argv = sys.argv[1:]
# Heartbeat SETs are not part of the scripted conversation.
if "BLPOP" not in argv:
    raise SystemExit(0)

state = pathlib.Path(os.environ["FAKE_REDIS_STATE"])
fixture = pathlib.Path(os.environ["FAKE_REDIS_RESPONSES"])
try:
    index = int(state.read_text())
except FileNotFoundError:
    index = 0
state.write_text(str(index + 1))
responses = json.loads(fixture.read_text())
status, stdout, stderr = responses[min(index, len(responses) - 1)]
pathlib.Path(os.environ["FAKE_REDIS_ARGS"]).write_text(json.dumps(argv))
sys.stdout.write(stdout)
sys.stderr.write(stderr)
raise SystemExit(status)
'''


def _run(tmp_path: Path, responses: list, max_loops: int = 1):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps(responses))
    command = fake_bin / "redis-cli"
    command.write_text(FAKE_REDIS)
    command.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AGENT_ID": AGENT,
        "AGENT_REDIS_HOST": "unused",
        "AGENT_REDIS_PORT": "6379",
        "AGENT_REDIS_DB": "12",
        "AGENT_REDIS_PREFIX": "agent_scratch:",
        "AGENT_BLPOP_TIMEOUT": "1",
        "AGENT_BRIDGE_INBOX_DIR": str(tmp_path / "inbox"),
        "AGENT_INBOX_REJECT_DIR": str(tmp_path / "rejects"),
        "AGENT_WATCHER_MAX_LOOPS": str(max_loops),
        "AGENT_WATCHER_MAX_BACKOFF": "0",  # keep the suite fast; backoff is not under test
        "FAKE_REDIS_STATE": str(tmp_path / "calls"),
        "FAKE_REDIS_RESPONSES": str(fixture),
        "FAKE_REDIS_ARGS": str(tmp_path / "args.json"),
    }
    return subprocess.run(
        [str(WATCHER)], env=env, text=True, capture_output=True, timeout=60, check=False
    )


def _hit(value: str, key: str = INBOX) -> str:
    return json.dumps([key, value]) + "\n"


def _envelope(mid: str) -> str:
    return json.dumps({"id": mid, "from": "codex-arbmem-prod", "kind": "notify", "payload": {}})


def _inbox_lines(stdout: str) -> list[str]:
    return [ln for ln in stdout.splitlines() if ln.startswith("[inbox] ")]


def _rejects(tmp_path: Path, pattern: str) -> list[Path]:
    directory = tmp_path / "rejects"
    return sorted(directory.glob(pattern)) if directory.exists() else []


def test_single_line_response_is_preserved_and_never_becomes_an_envelope(tmp_path):
    """The exact 2026-08-11 phantom: one stdout line rendered as a message."""
    result = _run(
        tmp_path,
        [
            [0, "Error: Connection reset by peer\n", ""],
            [0, _hit(_envelope("real")), ""],
        ],
        max_loops=2,
    )

    emitted = _inbox_lines(result.stdout)
    assert len(emitted) == 1, f"transport artifact leaked as an envelope: {emitted}"
    assert json.loads(emitted[0][len("[inbox] "):])["id"] == "real"
    assert "rejected invalid redis-cli response" in result.stderr
    preserved = _rejects(tmp_path, "transport-shape-*.json")
    assert len(preserved) == 1
    assert preserved[0].read_text() == "Error: Connection reset by peer\n"


def test_transport_failure_is_preserved_and_retried_internally(tmp_path):
    result = _run(
        tmp_path,
        [
            [1, "", "synthetic connection failure\n"],
            [0, _hit(_envelope("after-reconnect")), ""],
        ],
        max_loops=2,
    )

    emitted = _inbox_lines(result.stdout)
    assert len(emitted) == 1
    assert json.loads(emitted[0][len("[inbox] "):])["id"] == "after-reconnect"
    assert "reconnecting after transport fault rc=1" in result.stderr
    preserved = _rejects(tmp_path, "transport-*.txt")
    assert len(preserved) == 1
    assert preserved[0].read_text() == "synthetic connection failure\n"


def test_blpop_timeout_null_is_silent_and_spools_nothing(tmp_path):
    result = _run(tmp_path, [[0, "null\n", ""]], max_loops=2)

    assert _inbox_lines(result.stdout) == []
    assert _rejects(tmp_path, "*") == []


def test_real_envelope_is_emitted_on_one_line(tmp_path):
    result = _run(tmp_path, [[0, _hit(_envelope("genuine")), ""]], max_loops=1)

    emitted = _inbox_lines(result.stdout)
    assert len(emitted) == 1
    assert json.loads(emitted[0][len("[inbox] "):])["id"] == "genuine"
    assert _rejects(tmp_path, "*") == []


def test_empty_list_value_is_rejected_rather_than_emitted_blank(tmp_path):
    result = _run(tmp_path, [[0, _hit(""), ""]], max_loops=1)

    assert _inbox_lines(result.stdout) == []
    assert "rejected empty inbox item" in result.stderr
    assert len(_rejects(tmp_path, "rejected-empty-*.json")) == 1


def test_key_mismatch_is_preserved_and_the_loop_continues(tmp_path):
    result = _run(
        tmp_path,
        [
            [0, _hit(_envelope("wrong"), key="agent_scratch:agent:someone-else:inbox"), ""],
            [0, _hit(_envelope("mine")), ""],
        ],
        max_loops=2,
    )

    emitted = _inbox_lines(result.stdout)
    assert len(emitted) == 1
    assert json.loads(emitted[0][len("[inbox] "):])["id"] == "mine"
    assert "rejected BLPOP key mismatch" in result.stderr
    body = _rejects(tmp_path, "rejected-body-*.bin")
    assert len(body) == 1
    assert json.loads(body[0].read_text())["id"] == "wrong"


def test_preserved_bytes_are_not_world_readable(tmp_path):
    _run(tmp_path, [[1, "", "diagnostic\n"]], max_loops=1)

    preserved = _rejects(tmp_path, "transport-*.txt")
    assert len(preserved) == 1
    assert preserved[0].stat().st_mode & 0o777 == 0o600
    assert preserved[0].parent.stat().st_mode & 0o777 == 0o700


def test_repeated_identical_faults_converge_on_one_file(tmp_path):
    """N faults must not look like N distinct messages."""
    _run(tmp_path, [[1, "", "same failure\n"]], max_loops=3)

    assert len(_rejects(tmp_path, "transport-*.txt")) == 1


def test_a_flapping_bus_reports_itself_without_one_line_per_poll(tmp_path):
    """Sustained faults are rate-limited to powers of two, not every poll."""
    result = _run(tmp_path, [[0, "Error: Connection reset by peer\n", ""]], max_loops=5)

    reported = [ln for ln in result.stderr.splitlines() if "invalid redis-cli response" in ln]
    assert 0 < len(reported) < 5, f"expected suppression, got {len(reported)} lines"
    assert "streak=1" in result.stderr


def _filter(tmp_path: Path, line: str):
    inbox_dir = tmp_path / "inbox"
    env = {**os.environ, "AGENT_BRIDGE_INBOX_DIR": str(inbox_dir)}
    result = subprocess.run(
        ["python3", str(FILTER)], input=line, env=env, text=True,
        capture_output=True, timeout=30, check=False,
    )
    spooled = sorted(inbox_dir.glob("*.json")) if inbox_dir.exists() else []
    return result, spooled


def test_filter_does_not_spool_a_phantom_for_an_empty_body(tmp_path):
    """Defence in depth: even handed an empty body, spool nothing.

    Every empty body hashes to the same id, so repeated faults would all
    collapse onto one file that reads as a received-but-corrupt message.
    """
    result, spooled = _filter(tmp_path, "[inbox] \n")

    assert "kind=EMPTY-BODY spooled=no" in result.stdout
    assert spooled == []


def test_filter_still_spools_a_genuinely_malformed_body(tmp_path):
    result, spooled = _filter(tmp_path, "[inbox] not-json\n")

    assert "kind=UNPARSEABLE spooled=yes" in result.stdout
    assert len(spooled) == 1
    assert spooled[0].read_text() == "not-json\n"
