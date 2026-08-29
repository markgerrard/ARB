"""The alarm's runner layer: fetch, remember where we got to, and say so loudly.

Exit codes are asserted SPECIFICALLY rather than as "non-zero". A monitor whose
failure and its alarm both surface as "exit 1" cannot be triaged, and on a
default-deny path a bare non-zero is the ambient outcome -- see
docs/defect-classes/refusal-is-ambient-assert-the-code.md.
"""
import json

import pytest

from arb_warm_orch.remote_alarm import (
    EXIT_ALARM,
    EXIT_ERROR,
    EXIT_QUIET,
    fetch_activity,
    load_cursor,
    main,
    save_cursor,
)


class FakeRun:
    """Stands in for subprocess.run over `gh`. Records what it was asked to do."""

    def __init__(self, returncode=0, stdout="[]", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return self


def event(event_id, activity_type="push", ref="refs/heads/main"):
    return {
        "id": event_id,
        "before": "a" * 40,
        "after": "b" * 40,
        "ref": ref,
        "timestamp": "2026-08-03T12:00:00Z",
        "activity_type": activity_type,
        "actor": {"login": "markgerrard"},
    }


# --- cursor persistence ----------------------------------------------------

def test_a_missing_state_file_reads_as_no_cursor(tmp_path):
    assert load_cursor(tmp_path / "nope.json") is None


def test_the_cursor_round_trips(tmp_path):
    state = tmp_path / "state.json"

    save_cursor(state, 12345)

    assert load_cursor(state) == 12345


def test_a_corrupt_state_file_reads_as_no_cursor_rather_than_crashing(tmp_path):
    """A truncated write must degrade to 'establish a new baseline', not to a
    traceback that stops the alarm running at all."""
    state = tmp_path / "state.json"
    state.write_text("{not json")

    assert load_cursor(state) is None


# --- fetching --------------------------------------------------------------

def test_fetch_asks_github_for_the_activity_feed():
    run = FakeRun(stdout=json.dumps([event(1)]))

    events = fetch_activity("owner/repo", run=run)

    assert [e["id"] for e in events] == [1]
    (cmd,) = run.calls
    assert cmd[:2] == ["gh", "api"]
    assert "repos/owner/repo/activity" in cmd[2]


def test_fetch_raises_when_gh_fails_rather_than_returning_nothing():
    """Returning [] on failure would read as 'all quiet' -- a monitor that goes
    silent exactly when it is broken."""
    run = FakeRun(returncode=1, stdout="", stderr="HTTP 404")

    with pytest.raises(RuntimeError, match="HTTP 404"):
        fetch_activity("owner/repo", run=run)


# --- end to end, through main ---------------------------------------------

def test_a_quiet_run_exits_quiet_and_says_nothing_alarming(tmp_path, capsys):
    state = tmp_path / "state.json"
    save_cursor(state, 1)

    code = main(
        ["--repo", "owner/repo", "--state", str(state)],
        fetch=lambda repo: [event(2, "push"), event(1, "push")],
    )

    assert code == EXIT_QUIET
    assert "ALARM" not in capsys.readouterr().out


def test_an_alarming_run_exits_alarm_and_names_what_moved(tmp_path, capsys):
    state = tmp_path / "state.json"
    save_cursor(state, 1)

    code = main(
        ["--repo", "owner/repo", "--state", str(state)],
        fetch=lambda repo: [event(2, "pr_merge"), event(1, "push")],
    )

    out = capsys.readouterr().out
    assert code == EXIT_ALARM
    assert "ALARM" in out
    assert "pr_merge" in out
    assert "refs/heads/main" in out


def test_a_broken_fetch_exits_error_and_is_distinguishable_from_an_alarm(tmp_path, capsys):
    state = tmp_path / "state.json"
    save_cursor(state, 1)

    def boom(repo):
        raise RuntimeError("gh exploded")

    code = main(["--repo", "owner/repo", "--state", str(state)], fetch=boom)

    assert code == EXIT_ERROR
    assert code != EXIT_ALARM
    assert "gh exploded" in capsys.readouterr().err


def test_a_broken_fetch_leaves_the_cursor_untouched(tmp_path):
    """Advancing on failure would skip whatever happened during the outage."""
    state = tmp_path / "state.json"
    save_cursor(state, 7)

    def boom(repo):
        raise RuntimeError("gh exploded")

    main(["--repo", "owner/repo", "--state", str(state)], fetch=boom)

    assert load_cursor(state) == 7


def test_the_cursor_advances_so_the_same_alarm_does_not_repeat_forever(tmp_path):
    state = tmp_path / "state.json"
    save_cursor(state, 1)
    events = [event(2, "pr_merge"), event(1, "push")]

    first = main(["--repo", "owner/repo", "--state", str(state)], fetch=lambda r: events)
    second = main(["--repo", "owner/repo", "--state", str(state)], fetch=lambda r: events)

    assert (first, second) == (EXIT_ALARM, EXIT_QUIET)


def test_alarms_are_appended_to_a_durable_log_not_only_printed(tmp_path):
    """stdout goes wherever cron sends it, which may be nowhere. The log is the
    record that survives nobody watching."""
    state = tmp_path / "state.json"
    log = tmp_path / "alarms.jsonl"
    save_cursor(state, 1)

    main(
        ["--repo", "owner/repo", "--state", str(state), "--log", str(log)],
        fetch=lambda r: [event(2, "force_push"), event(1, "push")],
    )

    (line,) = log.read_text().splitlines()
    assert json.loads(line)["activity_type"] == "force_push"


def test_the_first_run_is_a_baseline_and_does_not_alarm(tmp_path):
    state = tmp_path / "state.json"

    code = main(
        ["--repo", "owner/repo", "--state", str(state)],
        fetch=lambda r: [event(2, "pr_merge"), event(1, "push")],
    )

    assert code == EXIT_QUIET
    assert load_cursor(state) == 2


def test_watched_refs_can_be_overridden(tmp_path):
    state = tmp_path / "state.json"
    save_cursor(state, 1)

    code = main(
        ["--repo", "owner/repo", "--state", str(state), "--ref", "refs/heads/release"],
        fetch=lambda r: [event(2, "force_push", ref="refs/heads/release"),
                         event(1, "push", ref="refs/heads/release")],
    )

    assert code == EXIT_ALARM


def test_a_gap_is_reported_loudly_and_is_not_mistaken_for_quiet(tmp_path, capsys):
    """Missing events unnoticed is the exact failure this whole mechanism exists
    to prevent, so a gap must not exit quiet."""
    state = tmp_path / "state.json"
    save_cursor(state, 1)

    code = main(
        ["--repo", "owner/repo", "--state", str(state)],
        fetch=lambda r: [event(9000, "push")],
    )

    assert code == EXIT_ALARM
    assert "GAP" in capsys.readouterr().out


def test_fetch_honours_per_page():
    """The GAP message tells the reader to raise --per-page, so the knob it names
    has to exist and has to reach the request. An error message that recommends a
    flag the tool does not have is prose claiming more than the mechanism does."""
    run = FakeRun(stdout="[]")

    fetch_activity("owner/repo", run=run, per_page=25)

    assert "per_page=25" in run.calls[0][2]


def test_the_per_page_flag_is_accepted_and_defaults_to_a_full_page():
    from arb_warm_orch.remote_alarm import _parse_args

    assert _parse_args(["--repo", "o/r", "--state", "s"]).per_page == 100
    assert _parse_args(["--repo", "o/r", "--state", "s", "--per-page", "25"]).per_page == 25


def test_the_exit_codes_are_distinct():
    assert len({EXIT_QUIET, EXIT_ALARM, EXIT_ERROR}) == 3
