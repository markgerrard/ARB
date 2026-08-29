"""Durability canaries: crash with an item in :processing.

From prod's required-evidence list. Establishes what the BLMOVE consumer's
crash-safety claim actually buys — and what it does not, which is where P1-4
lives: the element survives, but the AGENT WAKE is not deduplicated, so recovery
can re-surface a message the agent already acted on.
"""

from __future__ import annotations

import json
import time

from .canary_lib import (
    envelope, kill_hard, meta_ids, run_reliable_watcher, spawn_reliable_watcher,
    wait_until,
)

AGENT = "canary-durable"


def test_c5_sigkill_mid_flight_never_loses_the_envelope(planes, inbox_dir):
    """The no-loss invariant, asserted without depending on kill timing.

    A SIGKILL can land before the BLMOVE, between BLMOVE and the disk write, or
    after the LREM-ack. Rather than racing for one window, assert the invariant
    that must hold in ALL of them: the envelope is on the inbox, in :processing,
    or on disk — never absent from all three. A timing-dependent assertion here
    would be flaky and would prove less.
    """
    plane, _ = planes
    env = envelope(frm="peer", to=AGENT, event="in_flight_at_crash")

    proc = spawn_reliable_watcher(plane, AGENT, inbox_dir, blmove_timeout=5)
    # Wait for the watcher's own heartbeat before sending. Sending into a
    # not-yet-listening watcher would test nothing about crash-safety — the
    # envelope would simply still be on the inbox and the invariant would hold
    # for the wrong reason.
    assert wait_until(lambda: plane.client.exists(plane.status_key(AGENT)) == 1, timeout=15), (
        "watcher never published a heartbeat; it was not listening"
    )
    plane.send(env)
    kill_hard(proc, grace=0.15)  # mid-flight, wherever that lands

    on_inbox = plane.depth(AGENT)
    in_processing = plane.processing_depth(AGENT)
    on_disk = (inbox_dir / f"{env['id']}.json").exists()

    assert on_inbox + in_processing + (1 if on_disk else 0) >= 1, (
        "envelope vanished from inbox, :processing AND disk after SIGKILL — "
        "this is the loss the BLMOVE pattern exists to prevent"
    )


def test_c5b_crashed_predecessor_leaves_a_recoverable_element(planes, inbox_dir):
    """A deliberately deterministic model of the post-crash state.

    Seeding :processing directly is exactly what a dead consumer leaves behind
    (BLMOVE committed, LREM never ran). Deterministic where a real kill is racy,
    so the RECOVERY path is tested rather than the timing.
    """
    plane, _ = planes
    env = envelope(frm="peer", to=AGENT, event="left_by_crashed_predecessor")
    blob = json.dumps(env, separators=(",", ":"))
    plane.client.lpush(plane.processing_key(AGENT), blob)

    res = run_reliable_watcher(plane, AGENT, inbox_dir, iterations=1)

    assert env["id"] in meta_ids(res.stdout), f"startup re-drain did not recover it: {res.stdout}"
    assert (inbox_dir / f"{env['id']}.json").exists(), "recovered element never reached disk"
    assert plane.processing_depth(AGENT) == 0, ":processing not acked after recovery"


def test_c5c_recovery_re_wakes_the_agent_for_a_message_already_on_disk(planes, inbox_dir):
    """P1-4, first half: disk writes dedup by id, agent wakes do not.

    The predecessor got as far as writing the envelope to disk but died before
    the LREM-ack. On restart the file already exists, so the write is correctly
    skipped as a duplicate — and the meta line is emitted ANYWAY. For an envelope
    carrying an instruction, that is a second execution prompt for one order.
    """
    plane, _ = planes
    env = envelope(frm="orch", to=AGENT, event="GO")
    blob = json.dumps(env, separators=(",", ":"))

    # Predecessor's state: written to disk, still unacked in :processing.
    (inbox_dir / f"{env['id']}.json").write_text(blob + "\n")
    plane.client.lpush(plane.processing_key(AGENT), blob)

    res = run_reliable_watcher(plane, AGENT, inbox_dir, iterations=1)

    assert "skipped-duplicate=1" in res.stdout, (
        f"expected the disk write to be deduplicated: {res.stdout}"
    )
    assert env["id"] in meta_ids(res.stdout), (
        "expected the wake to be emitted despite the duplicate — if this goes red, "
        "wake-level dedup has landed and P1-4's first half is closed"
    )


def test_c5d_processing_survives_repeated_crashes_without_growing(planes, inbox_dir):
    """A recovery loop must converge: N crashes must not leave N copies.

    Guards the failure mode where re-drain re-queues rather than acks, which
    would turn every restart into a slow leak of the coordination plane.
    """
    plane, _ = planes
    env = envelope(frm="peer", to=AGENT, event="repeated_recovery")
    blob = json.dumps(env, separators=(",", ":"))
    plane.client.lpush(plane.processing_key(AGENT), blob)

    for _ in range(3):
        run_reliable_watcher(plane, AGENT, inbox_dir, iterations=1)
        time.sleep(0.05)

    assert plane.processing_depth(AGENT) == 0, ":processing grew or never drained"
    assert plane.depth(AGENT) == 0, "recovery re-queued onto the inbox instead of acking"
