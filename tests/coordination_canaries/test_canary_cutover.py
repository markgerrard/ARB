"""Cutover canaries: idle flip, active-dispatch flip, and rollback.

Completes prod's required-evidence list. The idle case is deliberately the
happy path — it is the CONTROL. Without it, the stranding canaries could all be
passing because the harness never delivers anything, and a suite where every
test asserts failure cannot tell breakage from evidence.
"""

from __future__ import annotations

from .canary_lib import envelope, meta_ids, run_reliable_watcher

AGENT = "canary-cut"
PEER = "canary-peer"


def test_c1_idle_flip_delivers_cleanly_and_strands_nothing(planes, inbox_dir):
    """CONTROL. With no in-flight work, a flip is clean — and must be seen to be,
    or the stranding assertions elsewhere prove nothing."""
    src, dst = planes
    assert src.depth(AGENT) == 0 and dst.depth(AGENT) == 0

    first = envelope(frm=PEER, to=AGENT, event="post_flip_first_message")
    dst.send(first)

    res = run_reliable_watcher(dst, AGENT, inbox_dir, iterations=1)

    assert meta_ids(res.stdout) == [first["id"]], f"clean flip failed to deliver: {res.stdout}"
    assert dst.depth(AGENT) == 0 and dst.processing_depth(AGENT) == 0
    assert src.depth(AGENT) == 0, "nothing should have been left on the source plane"
    assert src.processing_depth(AGENT) == 0


def test_c2_request_in_flight_at_flip_time_is_stranded_with_its_reply_path(planes, inbox_dir):
    """B3 under load: an unconsumed request plus its reply path, both on the old plane.

    The dispatcher enqueued a request and is waiting. The flip happens before the
    responder consumed it. Neither the request nor any reply follows the identity.
    """
    src, dst = planes

    inflight = envelope(frm=AGENT, to=PEER, kind="request", event="turn_in_flight")
    src.client.lpush(src.inbox_key(PEER), __import__("json").dumps(inflight, separators=(",", ":")))
    assert src.depth(PEER) == 1

    # Identity flips; the responder's inbox is untouched by the flip.
    live = envelope(frm=PEER, to=AGENT, event="unrelated_post_flip_traffic")
    dst.send(live)
    res = run_reliable_watcher(dst, AGENT, inbox_dir, iterations=1)

    assert meta_ids(res.stdout) == [live["id"]]
    assert src.depth(PEER) == 1, "in-flight request should still be stranded on the old plane"
    assert dst.depth(PEER) == 0, "nothing migrated the in-flight request to the new plane"


def test_c9_rollback_leaves_residue_and_presence_cannot_say_which_plane_is_live(planes, inbox_dir):
    """Rollback: the state written while on the target plane does not come back,
    and both planes now claim the identity is alive.

    The second half is the operationally nastier one. Each watcher writes its own
    heartbeat to its own plane, so after a flip-and-rollback the identity appears
    present on BOTH buses. Presence surfaces therefore cannot answer "which plane
    is this identity actually consuming?" — the question rollback most needs
    answered. v8 treats presence as fail-soft; this is why that matters.
    """
    src, dst = planes

    # Flip to dst: consume there, which writes a heartbeat on dst.
    on_dst = envelope(frm=PEER, to=AGENT, event="handled_while_flipped")
    dst.send(on_dst)
    run_reliable_watcher(dst, AGENT, inbox_dir, iterations=1)

    # Something arrives on dst after the decision to roll back.
    orphan = envelope(frm=PEER, to=AGENT, event="arrived_during_rollback")
    dst.send(orphan)

    # Roll back to src: consume there, writing a heartbeat on src too.
    back_home = envelope(frm=PEER, to=AGENT, event="post_rollback")
    src.send(back_home)
    res = run_reliable_watcher(src, AGENT, inbox_dir, iterations=1)

    assert meta_ids(res.stdout) == [back_home["id"]]
    assert dst.depth(AGENT) == 1, "the orphan should be stranded on the abandoned plane"

    src_alive = src.client.ttl(src.status_key(AGENT))
    dst_alive = dst.client.ttl(dst.status_key(AGENT))
    assert src_alive > 0 and dst_alive > 0, (
        f"expected a live heartbeat on BOTH planes after flip+rollback "
        f"(src={src_alive}, dst={dst_alive}); if only one is live, presence has "
        "become authoritative and rollback got easier"
    )
