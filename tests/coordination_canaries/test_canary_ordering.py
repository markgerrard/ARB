"""Ordering canary: the two consumers drain a backlog in OPPOSITE directions.

Not on prod's required list — it surfaced while building the stale-backlog canary,
when an ordering assertion failed and the instrument turned out to be at fault.
Checking the instrument exposed a real asymmetry worth pinning, because §5.2 of the
amendment ("archive + diff + drain the target inbox") is an ordering-sensitive
procedure and the operator's mental model of "the stale GO surfaces first" is only
true for one of the two consumers.

Both consumers are driven here for real; neither behaviour is inferred from flags.
"""

from __future__ import annotations

from .canary_lib import (
    drain_then_kill, envelope, meta_ids, run_reliable_watcher, spawn_split_watcher,
)

AGENT = "canary-order"


def _backlog(plane, n=3):
    """Three sends, oldest first — the shape of a stale backlog."""
    return [plane.send(envelope(frm="orch-old", to=AGENT, event=f"order-{i}")) for i in range(n)]


def test_reliable_watcher_drains_a_backlog_oldest_first(planes, inbox_dir):
    """LPUSH (head) + BLMOVE RIGHT (tail) = FIFO. The oldest stale order lands first."""
    plane, _ = planes
    ids = _backlog(plane)

    res = run_reliable_watcher(plane, AGENT, inbox_dir, iterations=3)

    assert meta_ids(res.stdout) == ids, (
        "reliable watcher is expected to drain oldest-first (FIFO); "
        f"sent {ids}, surfaced {meta_ids(res.stdout)}"
    )


def test_operational_blpop_watcher_drains_a_backlog_NEWEST_first(planes, inbox_dir):
    """LPUSH (head) + BLPOP (head) = LIFO — the reverse of its sibling.

    This is the consumer armed on arb-buzz today. Against a stale backlog it
    surfaces the MOST RECENT superseded order first, so a drain procedure that
    assumes chronological replay is reading the queue backwards. If this ever
    goes red, the two consumers have converged and §5.2's ordering caveat can go.
    """
    plane, _ = planes
    ids = _backlog(plane)

    proc = spawn_split_watcher(plane, AGENT, inbox_dir)
    out = drain_then_kill(proc, plane, AGENT)

    surfaced = meta_ids(out)
    assert set(surfaced) == set(ids), f"not all envelopes surfaced: {surfaced} vs {ids}"
    assert surfaced == list(reversed(ids)), (
        f"expected newest-first (LIFO) from the BLPOP consumer; sent {ids}, surfaced {surfaced}"
    )


def test_the_two_consumers_disagree_on_order_for_identical_input(planes, inbox_dir, tmp_path):
    """The asymmetry stated as one assertion, so it cannot be read as a fluke of
    either test above. Same producer, same backlog, opposite delivery order."""
    plane_r, plane_b = planes

    reliable_ids = _backlog(plane_r)
    reliable_out = run_reliable_watcher(plane_r, AGENT, inbox_dir, iterations=3).stdout

    blpop_dir = tmp_path / "blpop-inbox"
    blpop_dir.mkdir()
    blpop_ids = _backlog(plane_b)
    proc = spawn_split_watcher(plane_b, AGENT, blpop_dir)
    blpop_out = drain_then_kill(proc, plane_b, AGENT)

    reliable_order = [reliable_ids.index(i) for i in meta_ids(reliable_out)]
    blpop_order = [blpop_ids.index(i) for i in meta_ids(blpop_out)]

    assert reliable_order == [0, 1, 2], reliable_order
    assert blpop_order == [2, 1, 0], blpop_order
    assert reliable_order != blpop_order, (
        "the two consumers agree on order — if so, this canary has served its purpose "
        "and the ordering caveat in the drain procedure can be dropped"
    )
