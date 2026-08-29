"""Dual-plane canaries: the two findings that bite hardest during a flip.

P1-4 — two watchers on two buses have no shared atomic claim, so one logical
message delivered to both planes wakes the agent twice.
P1-3 — Redis ACL identity is not bound to the envelope's ``from``, so a foreign
credential can present as a trusted cohort sender.

Both are exercised against real servers with real ACL users, because both are
claims about what the transport does NOT guarantee, and a shim would simply
inherit whatever the shim's author believed.
"""

from __future__ import annotations

import inspect
import json

import pytest

from .canary_lib import envelope, meta_ids, run_reliable_watcher

AGENT = "canary-dual"
TRUSTED = "trusted-orch"


def test_c6_same_envelope_id_on_both_planes_wakes_the_agent_twice(planes, inbox_dir):
    """One logical order, two planes, one shared spool: two wakes, one file.

    This is the dual-watcher rule's actual failure mode. §6 of the amendment
    offers "the dual-watcher rule" as a mitigation for inter-cohort loss; what it
    buys is a trade of loss for possible DUPLICATE EXECUTION, which for a stale
    GO is the worse direction.
    """
    plane_a, plane_b = planes
    env = envelope(frm="orch", to=AGENT, event="GO")
    eid = env["id"]

    # The same envelope id delivered on both buses — e.g. a sender that retried
    # across the partition, or a relay that republished after a flip.
    plane_a.send(env)
    plane_b.send(dict(env))

    out_a = run_reliable_watcher(plane_a, AGENT, inbox_dir, iterations=1).stdout
    out_b = run_reliable_watcher(plane_b, AGENT, inbox_dir, iterations=1).stdout

    wakes = meta_ids(out_a) + meta_ids(out_b)
    assert wakes.count(eid) == 2, (
        f"expected one wake per plane for the same id, got {wakes}. If this is 1, "
        "a cross-plane claim has landed and P1-4 is closed."
    )
    files = list(inbox_dir.glob(f"{eid}.json"))
    assert len(files) == 1, "id-keyed spool should collapse to one file"

    # The asymmetry stated plainly: the spool deduplicates, the wake does not.
    assert len(files) < wakes.count(eid), (
        "disk dedup and wake dedup have converged; the duplicate-execution "
        "exposure this canary exists for would be gone"
    )


def test_c6b_neither_plane_can_see_the_others_claim(planes):
    """Why the duplicate is structural rather than a watcher bug.

    There is no shared key, lock or claim between the planes — each :processing
    list is local to its own server. Any exactly-once story has to be built
    ABOVE the transport; it cannot be recovered from it.
    """
    plane_a, plane_b = planes
    env = envelope(frm="orch", to=AGENT, event="GO")
    blob = json.dumps(env, separators=(",", ":"))

    plane_a.client.lpush(plane_a.processing_key(AGENT), blob)

    assert plane_a.processing_depth(AGENT) == 1
    assert plane_b.processing_depth(AGENT) == 0, (
        "a claim on one plane is invisible to the other — this is the structural gap"
    )
    assert plane_b.client.keys("*") == [] or all(
        not k.endswith(":processing") for k in plane_b.client.keys("*")
    )


def _make_acl_user(plane, username: str, password: str, rules: list[str]) -> None:
    plane.client.execute_command("ACL", "SETUSER", username, "on", f">{password}", *rules)


def _client_as(plane, username: str, password: str):
    import redis

    return redis.Redis(
        host=plane.host, port=plane.port, db=int(plane.db),
        username=username, password=password, decode_responses=True,
    )


def test_c7_a_foreign_credential_can_present_as_a_trusted_sender(planes, inbox_dir):
    """P1-3: transport identity is not bound to the envelope's ``from``.

    A distinct ACL user — one with no relationship to the trusted orchestrator —
    pushes an envelope claiming to be from it. The consumer surfaces it as
    trusted, because nothing compares the two.
    """
    plane, _ = planes
    inbox = plane.inbox_key(AGENT)

    _make_acl_user(plane, "foreign-cohort", "pw-foreign",
                   ["resetkeys", "-@all", "+select", "+lpush", f"~{inbox}"])
    foreign = _client_as(plane, "foreign-cohort", "pw-foreign")

    forged = envelope(frm=TRUSTED, to=AGENT, kind="notify", event="GO")
    foreign.lpush(inbox, json.dumps(forged, separators=(",", ":")))

    res = run_reliable_watcher(plane, AGENT, inbox_dir, iterations=1)

    assert forged["id"] in meta_ids(res.stdout), f"envelope not delivered: {res.stdout}"
    delivered = json.loads((inbox_dir / f"{forged['id']}.json").read_text())
    assert delivered["from"] == TRUSTED, (
        "the envelope arrives attributed to the trusted sender even though a "
        "foreign credential pushed it"
    )
    assert f"from={TRUSTED}" in res.stdout, (
        "the surfaced meta line attributes it to the trusted sender too, so a "
        "human reading the wake sees no discrepancy either"
    )


def test_c7b_the_foreign_credential_is_genuinely_foreign(planes):
    """Control for the canary above: if the ACL were permissive, C7 would prove
    nothing — a user allowed to do everything is not evidence of unbound identity."""
    plane, _ = planes
    inbox = plane.inbox_key(AGENT)
    _make_acl_user(plane, "foreign-narrow", "pw-narrow",
                   ["resetkeys", "-@all", "+select", "+lpush", f"~{inbox}"])
    foreign = _client_as(plane, "foreign-narrow", "pw-narrow")

    import redis

    # It can push...
    foreign.lpush(inbox, "{}")
    # ...and can do nothing else, so the only capability it exercised is LPUSH.
    with pytest.raises(redis.exceptions.ResponseError):
        foreign.get(plane.status_key(AGENT))
    with pytest.raises(redis.exceptions.ResponseError):
        foreign.lrange(inbox, 0, -1)


def test_c7c_the_bridge_authorises_on_the_body_field_not_the_connection(planes):
    """The same finding at the code level, so it is pinned even where a canary
    cannot reach: sender policy is keyed by the envelope's own ``sender`` field."""
    from agent_redis_bridge import bridge as bridge_mod

    src = inspect.getsource(bridge_mod.Bridge.handle_raw)
    assert "self.sender_policies.get(envelope.sender" in src, (
        "policy lookup no longer keys off the body's sender field — if this "
        "changed, sender binding may have landed and P1-3 should be re-reviewed"
    )
    control_src = inspect.getsource(bridge_mod.Bridge.handle_control)
    assert "self.sender_policies.get(envelope.sender" in control_src
    # steer/cancel bypass the claim gate: a forged control reaches an admitted turn.
    assert 'envelope.kind in {"steer", "cancel"}' in src
