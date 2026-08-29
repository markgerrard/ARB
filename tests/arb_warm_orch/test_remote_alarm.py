"""The cheap alarm: does a watched integration ref move in a way that is not an ordinary push?

Design note: the remote-observer design note (not included in this repository).
This is the "cheapest step" of option R -- observation WITHOUT per-tool-call bracketing.
It detects; it does not prevent and it does not attribute to a tool call.

Two properties this corpus is built to hold, both learned the expensive way:

* The alarm keys on the EVENT TYPE, and treats "ordinary push" as the only quiet
  one. An allowlist of alarming types would silently ignore any type GitHub adds
  later; §5.1 of the design note records a real instance of exactly that shape,
  where filtering on `push` alone missed `branch_creation` entirely.
* Newness is decided by EVENT ID, never by comparing timestamps against the local
  clock. Measured 2026-08-03: this host's clock was ~10h adrift from GitHub's mid
  session and later resynced, so any "events in the last N minutes" logic would
  have been wrong twice. Event ids were verified monotonic with time across 1240
  real events (0 inversions between events with differing timestamps).
"""
import pytest

from arb_warm_orch.remote_alarm import (
    DEFAULT_WATCHED_REFS,
    SAFE_ACTIVITY_TYPES,
    scan,
)

WATCHED = ("refs/heads/main", "refs/heads/dev")


def event(
    event_id: int,
    activity_type: str = "push",
    ref: str = "refs/heads/main",
    *,
    actor: str = "markgerrard",
    before: str = "a" * 40,
    after: str = "b" * 40,
    timestamp: str = "2026-08-03T12:00:00Z",
) -> dict:
    """One activity event in the shape the real API returns (fields verified live)."""
    return {
        "id": event_id,
        "before": before,
        "after": after,
        "ref": ref,
        "timestamp": timestamp,
        "activity_type": activity_type,
        "actor": {"login": actor},
    }


# --- what must alarm -------------------------------------------------------

@pytest.mark.parametrize(
    "activity_type",
    ["pr_merge", "force_push", "branch_deletion", "branch_creation"],
)
def test_non_push_event_on_a_watched_ref_alarms(activity_type):
    result = scan([event(100, activity_type)], cursor=1, watched_refs=WATCHED)

    assert [a.activity_type for a in result.alarms] == [activity_type]
    assert result.alarms[0].ref == "refs/heads/main"


def test_an_activity_type_github_has_not_invented_yet_alarms():
    """Fail loud on the unknown. An allowlist of KNOWN-BAD types would wave this
    through, which is how §5.1's branch_creation gap happened."""
    result = scan([event(100, "some_future_type")], cursor=1, watched_refs=WATCHED)

    assert [a.activity_type for a in result.alarms] == ["some_future_type"]


def test_the_alarm_carries_enough_to_investigate():
    result = scan(
        [event(100, "pr_merge", before="c" * 40, after="d" * 40, actor="someone")],
        cursor=1,
        watched_refs=WATCHED,
    )

    (alarm,) = result.alarms
    assert (alarm.event_id, alarm.actor, alarm.before, alarm.after) == (
        100, "someone", "c" * 40, "d" * 40,
    )


# --- what must stay quiet --------------------------------------------------

def test_an_ordinary_push_does_not_alarm():
    result = scan([event(100, "push")], cursor=1, watched_refs=WATCHED)

    assert result.alarms == ()


def test_an_event_on_an_unwatched_ref_is_ignored():
    result = scan(
        [event(100, "force_push", ref="refs/heads/some-worker-branch")],
        cursor=1,
        watched_refs=WATCHED,
    )

    assert result.alarms == ()


def test_an_event_already_seen_does_not_alarm_twice():
    already_seen = event(100, "pr_merge")

    result = scan([already_seen], cursor=100, watched_refs=WATCHED)

    assert result.alarms == ()


def test_the_first_run_establishes_a_baseline_rather_than_alarming_on_all_history():
    """1227 events existed on the real repo when this was written. A first run that
    alarmed on every one of them would be indistinguishable from noise, and would
    train the reader to ignore it."""
    result = scan(
        [event(100, "pr_merge"), event(99, "force_push")],
        cursor=None,
        watched_refs=WATCHED,
    )

    assert result.alarms == ()
    assert result.cursor == 100


# --- the cursor ------------------------------------------------------------

def test_a_quiet_run_still_advances_the_cursor():
    result = scan([event(100, "push"), event(99, "push")], cursor=1, watched_refs=WATCHED)

    assert result.alarms == ()
    assert result.cursor == 100


def test_the_cursor_never_moves_backwards():
    """A short page of older events must not rewind the cursor and re-alarm history."""
    result = scan([event(50, "push")], cursor=100, watched_refs=WATCHED)

    assert result.cursor == 100


def test_an_unwatched_ref_still_advances_the_cursor():
    """Otherwise a busy worker branch keeps the cursor pinned and every run re-reads
    the same window forever."""
    result = scan(
        [event(100, "force_push", ref="refs/heads/worker")],
        cursor=1,
        watched_refs=WATCHED,
    )

    assert result.alarms == ()
    assert result.cursor == 100


def test_only_events_newer_than_the_cursor_alarm():
    events = [event(102, "pr_merge"), event(101, "push"), event(100, "force_push")]

    result = scan(events, cursor=100, watched_refs=WATCHED)

    assert [a.event_id for a in result.alarms] == [102]
    assert result.cursor == 102


# --- the gap: did the feed reach back to where we left off? ----------------

def test_a_feed_that_does_not_reach_back_to_the_cursor_reports_a_gap():
    """The feed returns a bounded page. If more happened between runs than fits,
    the unseen events are skipped -- silently, unless this is detected. A monitor
    that misses without saying so is worse than none."""
    result = scan([event(500), event(499)], cursor=1, watched_refs=WATCHED)

    assert result.gap is True


def test_no_gap_when_the_feed_overlaps_the_cursor():
    result = scan([event(101), event(100), event(99)], cursor=100, watched_refs=WATCHED)

    assert result.gap is False


def test_the_baseline_run_is_not_a_gap():
    """First run has nothing to have missed."""
    result = scan([event(500)], cursor=None, watched_refs=WATCHED)

    assert result.gap is False


def test_an_empty_feed_is_not_a_gap():
    result = scan([], cursor=100, watched_refs=WATCHED)

    assert result.gap is False


# --- the constants are part of the contract --------------------------------

def test_push_is_the_only_quiet_activity_type():
    assert SAFE_ACTIVITY_TYPES == frozenset({"push"})


def test_the_watched_refs_default_to_the_integration_branches():
    assert DEFAULT_WATCHED_REFS == ("refs/heads/main", "refs/heads/dev")
