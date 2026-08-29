"""The cheap alarm — notice when an integration ref moves in a way that is not an ordinary push.

WHAT THIS ESTABLISHES, stated at the strength the mechanism enforces:

    "a watched ref changed by some means other than an ordinary push, and nobody
     had told this process about it yet"

It does NOT establish that the change was unauthorised, and it does not know what
evidence exists for it — there is no evidence resolution in this system today
(`cli.NoCloseConsumerYet` refuses everything unconditionally). It detects and
reports; it neither prevents nor attributes the change to a tool call.

This is the cheapest slice of "option R" in
`the remote-observer design note (not included in this repository)`:
remote observation WITHOUT per-tool-call bracketing. The expensive part of that
option is the bracket, and the bracket is only worth building once this alarm
shows the event actually happens. Measured when this was written: across the
repo's entire 98-day history there were 2 `pr_merge` and 1 `force_push` events.

TWO DESIGN CHOICES THAT ARE LOAD-BEARING, both learned the expensive way:

1. QUIET IS AN ALLOWLIST, NOISE IS THE DEFAULT. `SAFE_ACTIVITY_TYPES` names the
   one type that does not alarm; everything else does, including types GitHub has
   not invented yet. The inverse — listing the types that alarm — is how the
   design note's §5.1 gap happened: a filter on `push` alone silently ignored
   `branch_creation`, which is how a ref's FIRST push is typed.

2. NEWNESS IS DECIDED BY EVENT ID, NEVER BY THE LOCAL CLOCK. On 2026-08-03 this
   host's clock was observed ~10h adrift from GitHub's and later resynced, so any
   "events since T-minus-N-minutes" logic would have been wrong in both
   directions. Event ids were verified monotonic with time across 1240 real
   events (0 inversions among events with differing timestamps), so a numeric
   comparison against a stored cursor is sound where a clock comparison is not.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# Distinct on purpose. A monitor whose breakage and whose alarm both surface as
# "non-zero" cannot be triaged, and the ambient outcome of a failing cron job is
# already non-zero — so the codes have to separate "it fired" from "it broke".
EXIT_QUIET = 0
EXIT_ALARM = 1
EXIT_ERROR = 2

# The only activity type that does not alarm on a watched ref. Deliberately an
# allowlist of the QUIET, not a blocklist of the loud — see docstring note 1.
SAFE_ACTIVITY_TYPES: frozenset[str] = frozenset({"push"})

# The refs where a change becomes everyone's problem. Worker branches are not
# watched: work is isolated until it reaches one of these.
DEFAULT_WATCHED_REFS: tuple[str, ...] = ("refs/heads/main", "refs/heads/dev")


@dataclass(frozen=True)
class Alarm:
    """One ref change worth a human look. Carries enough to investigate without
    a second API call — the OIDs bracket exactly what moved."""

    event_id: int
    activity_type: str
    ref: str
    actor: str
    before: str
    after: str
    timestamp: str

    def describe(self) -> str:
        return (
            f"{self.timestamp} {self.activity_type} on {self.ref} "
            f"by {self.actor}: {self.before[:8]} -> {self.after[:8]}"
        )


@dataclass(frozen=True)
class ScanResult:
    alarms: tuple[Alarm, ...]
    cursor: int | None
    # True when the feed's OLDEST returned event is still newer than the cursor,
    # i.e. the page did not reach back to where the last run finished, so events
    # in between were never seen. Silent misses are the failure this whole
    # mechanism exists to prevent, so this is surfaced rather than swallowed.
    gap: bool = False


def _to_alarm(event: dict[str, Any]) -> Alarm:
    actor = event.get("actor") or {}
    return Alarm(
        event_id=int(event["id"]),
        activity_type=str(event.get("activity_type", "")),
        ref=str(event.get("ref", "")),
        actor=str(actor.get("login", "")),
        before=str(event.get("before", "")),
        after=str(event.get("after", "")),
        timestamp=str(event.get("timestamp", "")),
    )


def scan(
    events: Iterable[dict[str, Any]],
    *,
    cursor: int | None = None,
    watched_refs: Sequence[str] = DEFAULT_WATCHED_REFS,
) -> ScanResult:
    """Find alarming ref changes newer than `cursor`, and return the next cursor.

    `cursor=None` means "first run": it establishes a baseline and alarms on
    nothing. Alarming on all of history instead would produce hundreds of entries
    on first use, which trains the reader to ignore the alarm — the failure mode
    that makes a monitor worse than none.

    The cursor advances past events on UNWATCHED refs too. Otherwise a busy worker
    branch pins it and every later run re-reads the same window forever.
    """
    watched = frozenset(watched_refs)
    materialised = list(events)
    ids = [int(e["id"]) for e in materialised]
    highest = max(ids) if ids else None

    if cursor is None:
        return ScanResult((), highest)

    next_cursor = cursor if highest is None else max(cursor, highest)
    gap = bool(ids) and min(ids) > cursor
    alarms = tuple(
        _to_alarm(e)
        for e in materialised
        if int(e["id"]) > cursor
        and e.get("ref") in watched
        and e.get("activity_type") not in SAFE_ACTIVITY_TYPES
    )
    return ScanResult(alarms, next_cursor, gap)


# --- the runner layer ------------------------------------------------------


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def fetch_activity(
    repo: str,
    *,
    run: Callable[[list[str]], Any] | None = None,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """Read the repo activity feed. RAISES on failure rather than returning [].

    An empty list is indistinguishable from "all quiet", so swallowing an error
    here would make the monitor go silent at exactly the moment it is broken.
    """
    runner = run or _default_run
    result = runner(["gh", "api", f"repos/{repo}/activity?per_page={per_page}"])
    if result.returncode != 0:
        detail = (getattr(result, "stderr", "") or "").strip()[:300]
        raise RuntimeError(f"gh api activity failed for {repo}: {detail}")
    return json.loads(result.stdout or "[]")


def load_cursor(path: Path | str) -> int | None:
    """Missing or unreadable state reads as "no cursor", i.e. re-baseline.

    Degrading to a baseline is the safe direction: the alternative is a traceback
    that stops the alarm running at all, which is a silent monitor.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("cursor")
    return value if isinstance(value, int) else None


def save_cursor(path: Path | str, cursor: int | None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"cursor": cursor}))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="arb-remote-alarm",
        description="Alarm when a watched integration ref moves other than by an ordinary push.",
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--state", required=True, help="cursor file; remembers what was seen")
    parser.add_argument(
        "--ref",
        action="append",
        default=None,
        help=f"ref to watch, repeatable (default: {' '.join(DEFAULT_WATCHED_REFS)})",
    )
    parser.add_argument("--log", default=None, help="append alarms here as JSONL")
    parser.add_argument(
        "--per-page",
        type=int,
        default=100,
        help="events to read per run; raise it if GAP is reported (default: 100)",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    fetch: Callable[[str], list[dict[str, Any]]] | None = None,
) -> int:
    args = _parse_args(argv)
    fetcher = fetch or (lambda repo: fetch_activity(repo, per_page=args.per_page))

    cursor = load_cursor(args.state)
    try:
        events = fetcher(args.repo)
    except Exception as exc:  # noqa: BLE001 - any failure must exit ERROR, not ALARM
        print(f"arb-remote-alarm: fetch failed: {exc}", file=sys.stderr)
        return EXIT_ERROR

    result = scan(
        events,
        cursor=cursor,
        watched_refs=tuple(args.ref) if args.ref else DEFAULT_WATCHED_REFS,
    )
    # Only after a successful fetch: advancing on failure would skip whatever
    # happened during the outage.
    save_cursor(args.state, result.cursor)

    if result.gap:
        print(
            "GAP the activity feed did not reach back to the last cursor; "
            "events in between were never seen. Run more often, or raise --per-page."
        )

    for alarm in result.alarms:
        print(f"ALARM {alarm.describe()}")
        if args.log:
            with open(args.log, "a") as handle:
                handle.write(json.dumps(asdict(alarm)) + "\n")

    return EXIT_ALARM if (result.alarms or result.gap) else EXIT_QUIET


if __name__ == "__main__":  # pragma: no cover - exercised via scripts/arb-remote-alarm
    raise SystemExit(main())
