"""A receiver that discards what it rejects cannot tell malformed from artifact.

`agent-inbox-watcher-reliable` never fabricated phantom messages the way
`agent-inbox-watcher` and `codex-inbox-once` did — its `parse_envelope` returns
None and the caller drops. But every one of those drop paths LREM-s the element
off `:processing`, and the script has not written it to the inbox dir (it could
not parse an id to name the file with). So the bytes left disk and bus at the
same instant, and one path — the startup re-drain — dropped with no emit at all.

That is the half of the 2026-08-11 defect that cost a peer a hunt for a sender
that never existed. Every test here fails against the pre-fix script.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "agent-inbox-watcher-reliable"
AGENT = "claude-arbcomms-arbbuzz"
INBOX = f"agent_scratch:agent:{AGENT}:inbox"
PROCESSING = f"{INBOX}:processing"

# The filter the docs tell operators to put on the Monitor invocation. A
# rejection the operator cannot see is only half-fixed.
DOCUMENTED_MONITOR_FILTER = re.compile(
    r"^\[inbox-meta\]|^\[watcher\]|ERROR|FATAL|NOAUTH|WRONGPASS|denied|rejected"
    r"|connection refused|reconnecting|sender-rejected|envelope-invalid"
)

FAKE_REDIS = '''#!/usr/bin/env python3
import json, os, pathlib, sys

argv = sys.argv[1:]
def arg_after(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None

state = pathlib.Path(os.environ["FAKE_REDIS_STATE"])
script = json.loads(pathlib.Path(os.environ["FAKE_REDIS_SCRIPT"]).read_text())

# Record every command so a test can assert the LREM actually happened.
log = pathlib.Path(os.environ["FAKE_REDIS_LOG"])
with log.open("a") as handle:
    handle.write(json.dumps(argv) + "\\n")

if "LRANGE" in argv:
    sys.stdout.write("\\n".join(script.get("processing", [])))
    raise SystemExit(0)
if "BLMOVE" in argv:
    try:
        index = int(state.read_text())
    except FileNotFoundError:
        index = 0
    state.write_text(str(index + 1))
    elements = script.get("blmove", [])
    item = elements[index] if index < len(elements) else None
    if item is None:
        sys.stdout.write("null")          # RESP nil == timeout, under --json
        raise SystemExit(0)
    if isinstance(item, dict):            # a scripted command/transport error
        sys.stderr.write(item.get("stderr", ""))
        raise SystemExit(item.get("rc", 1))
    sys.stdout.write(json.dumps(item))    # --json returns the element as a JSON string
    raise SystemExit(0)
if "LREM" in argv:
    sys.stdout.write("1")
    raise SystemExit(0)
raise SystemExit(0)   # SET heartbeat etc.
'''


def _run(tmp_path: Path, *, processing=(), blmove=(), iterations=1):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command = fake_bin / "redis-cli"
    command.write_text(FAKE_REDIS)
    command.chmod(0o755)
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"processing": list(processing), "blmove": list(blmove)}))

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AGENT_ID": AGENT,
        "AGENT_REDIS_HOST": "unused",
        "AGENT_REDIS_PORT": "6379",
        "AGENT_REDIS_DB": "12",
        "AGENT_REDIS_PREFIX": "agent_scratch:",
        "AGENT_BRIDGE_INBOX_DIR": str(tmp_path / "inbox"),
        "AGENT_INBOX_REJECT_DIR": str(tmp_path / "rejects"),
        "WATCHER_BLMOVE_TIMEOUT": "1",
        "WATCHER_MAX_ITERATIONS": str(iterations),
        "FAKE_REDIS_STATE": str(tmp_path / "calls"),
        "FAKE_REDIS_SCRIPT": str(script),
        "FAKE_REDIS_LOG": str(tmp_path / "commands.log"),
    }
    result = subprocess.run(
        ["python3", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=60, check=False
    )
    return result


def _rejects(tmp_path: Path, pattern: str = "*") -> list[Path]:
    directory = tmp_path / "rejects"
    return sorted(directory.glob(pattern)) if directory.exists() else []


def _lrem_calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "commands.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if "LREM" in line]


def test_malformed_element_is_preserved_before_it_leaves_the_bus(tmp_path):
    result = _run(tmp_path, blmove=["this-is-not-json"], iterations=1)

    kept = _rejects(tmp_path, "malformed-element-*.bin")
    assert len(kept) == 1, "the only copy of the element was deleted with no retention"
    assert kept[0].read_bytes() == b"this-is-not-json"
    assert _lrem_calls(tmp_path), "element was not acked off :processing"
    assert result.returncode == 0


def test_element_without_an_id_is_preserved(tmp_path):
    """Parseable but unusable: previously dropped with a bare no-id line."""
    body = json.dumps({"from": "somebody", "kind": "notify", "payload": {"event": "x"}})
    _run(tmp_path, blmove=[body], iterations=1)

    kept = _rejects(tmp_path, "element-without-id-*.bin")
    assert len(kept) == 1
    assert json.loads(kept[0].read_text())["from"] == "somebody"


def test_startup_redrain_no_longer_drops_silently(tmp_path):
    """The worst path: it had no emit at all, so this vanished without trace."""
    result = _run(tmp_path, processing=["{not-json-either"], iterations=0)

    kept = _rejects(tmp_path, "redrain-undeliverable-*.bin")
    assert len(kept) == 1, "startup re-drain still deletes undeliverable elements silently"
    assert kept[0].read_bytes() == b"{not-json-either"
    assert "startup re-drain" in result.stdout


@pytest.mark.parametrize(
    "scenario",
    [
        {"blmove": ["not-json"], "iterations": 1},
        {"blmove": [json.dumps({"from": "x", "kind": "notify"})], "iterations": 1},
        {"processing": ["{bad"], "iterations": 0},
    ],
    ids=["malformed", "no-id", "redrain"],
)
def test_every_rejection_surfaces_through_the_documented_monitor_filter(tmp_path, scenario):
    """Preserving bytes the operator is never told about is only half a fix.

    The pre-fix lines (`[watcher-error] envelope-malformed`, `envelope-no-id`)
    matched NONE of the documented alternatives: `^\\[watcher\\]` needs that
    exact literal and `ERROR` is case-sensitive.
    """
    result = _run(tmp_path, **scenario)

    surfaced = [ln for ln in result.stdout.splitlines() if DOCUMENTED_MONITOR_FILTER.search(ln)]
    assert any("rejected" in ln for ln in surfaced), (
        "no rejection line survives the documented Monitor filter; the operator "
        f"would see nothing. stdout was: {result.stdout!r}"
    )


def test_preserved_bytes_are_not_world_readable(tmp_path):
    """A rejected envelope can still carry a sealed payload."""
    _run(tmp_path, blmove=["not-json"], iterations=1)

    kept = _rejects(tmp_path, "*.bin")
    assert len(kept) == 1
    assert kept[0].stat().st_mode & 0o777 == 0o600
    assert kept[0].parent.stat().st_mode & 0o777 == 0o700


def test_repeated_identical_rejections_converge_on_one_file(tmp_path):
    """N repeats of one fault must not read as N distinct lost messages."""
    _run(tmp_path, blmove=["not-json", "not-json", "not-json"], iterations=3)

    assert len(_rejects(tmp_path, "malformed-element-*.bin")) == 1


def test_a_good_envelope_is_still_delivered_normally(tmp_path):
    """Control: the fix must not turn valid traffic into rejections."""
    body = json.dumps({"id": "good-1", "from": "peer", "kind": "notify", "payload": {"event": "e"}})
    result = _run(tmp_path, blmove=[body], iterations=1)

    assert "[inbox-meta] id=good-1" in result.stdout
    assert _rejects(tmp_path) == [], "a valid envelope was rejected"
    assert (tmp_path / "inbox" / "good-1.json").read_text() == body


# --- The (nil) substring misclassification --------------------------------
#
# `if not out or "(nil)" in out or not out.strip(): continue` treated ANY
# output containing the substring as a BLMOVE timeout. In a workstream that
# discusses nil returns constantly, an envelope mentioning (nil) is not
# hypothetical. It was not lost -- already in :processing, recovered by the
# next startup re-drain -- but it never surfaced in real time, which is the
# quiet half of the failure.


def test_an_envelope_mentioning_nil_is_delivered_not_read_as_a_timeout(tmp_path):
    body = json.dumps({
        "id": "mentions-nil",
        "from": "codex-arbmem-prod",
        "kind": "notify",
        "payload": {"event": "probe", "data": {"note": "GET returned (nil) for that key"}},
    })
    result = _run(tmp_path, blmove=[body], iterations=1)

    assert "[inbox-meta] id=mentions-nil" in result.stdout, (
        "an envelope whose payload merely mentions (nil) was swallowed as a timeout"
    )
    assert (tmp_path / "inbox" / "mentions-nil.json").read_text() == body
    assert _rejects(tmp_path) == []


def test_a_json_null_is_the_only_thing_treated_as_a_timeout(tmp_path):
    """Control for the test above: a real timeout must still be silent."""
    result = _run(tmp_path, blmove=[], iterations=1)

    assert "[inbox-meta]" not in result.stdout
    assert _rejects(tmp_path) == []
    assert not _lrem_calls(tmp_path), "a timeout must not ack anything"


def test_a_command_error_is_not_mistaken_for_a_message_and_is_visible(tmp_path):
    """`--json` alone exits 0 on NOPERM and prints error:"..." to stdout.

    Without `-e` that would parse as a malformed message. With it, the branch
    is a transport fault -- and it must surface, since the documented Monitor
    filter matches neither `[watcher-error]` nor a lowercase "error".
    """
    error = {"rc": 1, "stderr": "NOPERM User x has no permissions to run the 'blmove' command"}
    result = _run(tmp_path, blmove=[error], iterations=1)

    assert "[inbox-meta]" not in result.stdout, "a command error was rendered as a message"
    assert _rejects(tmp_path, "malformed-element-*.bin") == [], (
        "a permissions failure was misfiled as a malformed envelope"
    )
    surfaced = [ln for ln in result.stdout.splitlines() if DOCUMENTED_MONITOR_FILTER.search(ln)]
    assert any("blmove fault" in ln for ln in surfaced), (
        f"the fault never reached the operator. stdout was: {result.stdout!r}"
    )
