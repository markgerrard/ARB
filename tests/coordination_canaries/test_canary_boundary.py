"""Boundary canaries: what a depth-zero quiesce check does NOT buy you.

Evidences prod review findings P1-2 (no admission fence) and P1-3, and supplies
the "late foreign send after depth-zero", "reply arrival at the boundary" and
"stale target backlog" cases from its required-evidence list.

READ THE ASSERTION DIRECTION. These canaries assert that loss/stranding HAPPENS.
That is not an endorsement of the behaviour — it is the boundary condition being
pinned so it cannot regress silently. If an admission fence, tombstone or
forwarder later lands, these tests SHOULD go red; that red is the signal the
amendment's §4 table may finally be rewritten from "closed" to closed-in-fact.
A canary asserting the desired-but-absent behaviour would just be a failing test
nobody can act on.
"""

from __future__ import annotations

from .canary_lib import (
    envelope, meta_ids, run_reliable_watcher,
)

AGENT = "canary-orch"
PEER = "canary-peer"


def test_c3_late_foreign_send_after_depth_zero_is_stranded_on_the_old_plane(planes, inbox_dir):
    """B2: a depth-zero observation is not a barrier.

    The quiesce check passes (depth 0 on the source plane), the identity flips to
    the target plane, and only THEN does a foreign sender push. Nothing in the
    system refuses that push or forwards it.
    """
    src, dst = planes

    # 1. Quiesce check passes on the source plane.
    assert src.depth(AGENT) == 0, "precondition: source inbox must be drained"

    # 2. Identity has flipped: its consumer now runs against the target plane.
    live = envelope(frm=PEER, to=AGENT, event="post_flip_live_traffic")
    dst.send(live)

    # 3. A sender that never saw the flip pushes to the OLD plane, after the check.
    late = envelope(frm="foreign-cohort-sender", to=AGENT, event="late_send_after_quiesce")
    src.send(late)

    res = run_reliable_watcher(dst, AGENT, inbox_dir, iterations=1)
    surfaced = meta_ids(res.stdout)

    assert live["id"] in surfaced, f"target-plane traffic not delivered: {res.stdout}"
    assert late["id"] not in surfaced, (
        "late send crossed planes — if this fires, a forwarder exists and B2's "
        "closure claim can be re-examined"
    )
    assert src.depth(AGENT) == 1, "late envelope should be sitting stranded on the source plane"
    assert late["id"] not in {p.stem for p in inbox_dir.glob('*.json')}, (
        "stranded envelope must not appear on disk — it was never received"
    )


def test_c3_control_the_identical_envelope_delivers_if_the_consumer_stays_put(planes, inbox_dir):
    """CONTROL for C3/C4. Both assert an envelope did NOT arrive — which is also
    what you would observe if the envelope were malformed, mis-keyed, or the
    harness simply never delivered anything.

    Byte-identical envelope, same inbox key, same consumer: the only difference
    is which plane the consumer reads. It arrives. So the stranding above is
    caused by the flip and nothing else.
    """
    src, _dst = planes
    late = envelope(frm="foreign-cohort-sender", to=AGENT, event="late_send_after_quiesce")
    src.send(late)

    res = run_reliable_watcher(src, AGENT, inbox_dir, iterations=1)

    assert late["id"] in meta_ids(res.stdout), (
        "the 'stranded' envelope is not deliverable even on its own plane — "
        f"C3/C4 would be passing for the wrong reason: {res.stdout} {res.stderr}"
    )
    assert src.depth(AGENT) == 0


def test_c4_reply_to_the_old_plane_never_reaches_a_flipped_waiter(planes, inbox_dir):
    """B3: request/reply affinity is lost across a flip.

    A responder that resolved the sender's inbox BEFORE the flip replies to the
    source plane. The waiter is now consuming the target plane, so the reply is
    not late — it is unreachable.
    """
    src, dst = planes

    request = envelope(frm=AGENT, to=PEER, kind="request", event="dispatch_in_flight")
    reply = envelope(frm=PEER, to=AGENT, kind="reply", event="turn_result")
    reply["in_reply_to"] = request["id"]

    # Responder replies to the plane it knew about.
    src.send(reply)

    # Waiter is on the target plane and sees a full inbox drain there.
    res = run_reliable_watcher(dst, AGENT, inbox_dir, iterations=1, blmove_timeout=1)
    surfaced = meta_ids(res.stdout)

    assert reply["id"] not in surfaced, "reply crossed planes"
    assert src.depth(AGENT) == 1, "reply should be stranded on the source plane"
    assert dst.processing_depth(AGENT) == 0
    # The waiter cannot distinguish "reply stranded" from "peer still working":
    # both look like an empty inbox. That indistinguishability is the finding.
    assert surfaced == [], f"target plane should have been silent, got {surfaced}"


def test_c8_stale_target_backlog_is_consumed_ahead_of_live_traffic(planes, inbox_dir):
    """§5.2's necessity, demonstrated: the Mini's 17 stale orders, in miniature.

    A superseded GO and a superseded HOLD are already sitting on the target plane
    when the identity flips. On first read they surface in queue order, ahead of
    live traffic, carrying no marker that distinguishes them from current orders.
    """
    _src, dst = planes

    stale_go = envelope(frm="orch-old", to=AGENT, event="GO", data={"superseded": True})
    stale_hold = envelope(frm="orch-old", to=AGENT, event="HOLD", data={"superseded": True})
    dst.send(stale_go)
    dst.send(stale_hold)

    live = envelope(frm=PEER, to=AGENT, event="first_live_instruction_after_flip")
    dst.send(live)

    res = run_reliable_watcher(dst, AGENT, inbox_dir, iterations=3)
    surfaced = meta_ids(res.stdout)

    assert surfaced[:2] == [stale_go["id"], stale_hold["id"]], (
        f"expected the stale backlog to surface first, got {surfaced}"
    )
    assert live["id"] in surfaced

    # The receiver has no transport-level way to tell them apart: same shape,
    # same sender field, no epoch or generation marker anywhere on the wire.
    from .canary_lib import read_envelope
    stale_body = read_envelope(inbox_dir, stale_go["id"])
    live_body = read_envelope(inbox_dir, live["id"])
    assert set(stale_body) == set(live_body), (
        "if the shapes differ, a discriminator exists and the drain step could be "
        "replaced by a filter"
    )
    assert "epoch" not in stale_body and "generation" not in stale_body, (
        "a generation/epoch field would let a receiver reject superseded orders; "
        "its absence is why §5.2 has to be a manual archive+diff+drain"
    )
