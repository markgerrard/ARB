"""Reconcile a panel's committed audit rows against its verdict — the fail-loud roster guard (spec v2).

reconcile() is the only thing standing between a verdict and the audit log. It refuses (ok=False) unless
the manifest is seq-1, precedes every vote, every rostered seat has exactly one terminal vote, no
unrostered votes exist, and the verdict's per-seat stances match the vote rows. Ground truth is the
committed rows, never the orchestrator's recollection.
"""
import time

from .audit import audit_lag


def _poll_until_stable(count_fn, lag_fn, *, timeout_s, interval_s, sleep, now):
    deadline = now() + timeout_s
    prev = None
    while now() <= deadline:
        count = count_fn()
        lag = lag_fn()
        if count == prev and lag.get("pending", 0) == 0 and lag.get("lag", 0) == 0:
            return True
        prev = count
        sleep(interval_s)
    return False


def _load_rows(conn, run_id):
    rows = conn.execute(
        "SELECT seq, kind, payload FROM audit_events WHERE run_id = %s ORDER BY seq",
        (run_id,),
    ).fetchall()
    return [{"seq": int(s), "kind": k, "payload": p} for (s, k, p) in rows]


def _assert(rows, verdict_payload):
    gaps = []
    manifests = [r for r in rows if r["kind"] == "dispatch"]
    votes = [r for r in rows if r["kind"] == "vote"]

    if len(manifests) != 1:
        gaps.append(f"expected exactly 1 dispatch manifest, found {len(manifests)}; run un-auditable")
        return gaps
    manifest = manifests[0]
    if manifest["seq"] != 1:
        gaps.append(f"manifest seq is {manifest['seq']}, must be 1 (manifest must be committed first)")
    if votes and min(v["seq"] for v in votes) <= manifest["seq"]:
        gaps.append("manifest does not precede all votes; roster not trustworthy")

    roster = set(manifest["payload"].get("roster", []))
    if not roster:
        gaps.append("manifest declares an empty roster")

    vote_by_actor = {}
    for v in votes:
        actor = v["payload"].get("actor")
        if actor not in roster:
            gaps.append(f"unrostered vote from {actor!r}")
            continue
        if actor in vote_by_actor:
            gaps.append(f"duplicate vote for {actor!r}")
        vote_by_actor[actor] = v["payload"].get("stance")

    for seat in roster:
        if seat not in vote_by_actor:
            gaps.append(f"seat {seat!r} declared in roster but never voted")

    verdict_roster = verdict_payload.get("roster")
    if not isinstance(verdict_roster, list) or set(verdict_roster) != roster:
        actual = sorted(verdict_roster) if isinstance(verdict_roster, list) else repr(verdict_roster)
        gaps.append(f"verdict roster {actual} != manifest roster {sorted(roster)}")

    stances = verdict_payload.get("stances", {})
    if set(stances) != roster:
        gaps.append(f"verdict stances keys {sorted(stances)} != roster {sorted(roster)}")
    for seat, claimed in stances.items():
        actual = vote_by_actor.get(seat)
        if actual is not None and claimed != actual:
            gaps.append(f"verdict claims {seat}={claimed!r} but vote row says {actual!r}")
    return gaps


def reconcile(conn, run_id, verdict_payload, *, redis=None, poll_timeout_s=30.0, poll_interval_s=0.25):
    if redis is not None:
        def _count():
            return conn.execute(
                "SELECT count(*) FROM audit_events WHERE run_id = %s", (run_id,)
            ).fetchone()[0]
        stable = _poll_until_stable(
            _count, lambda: audit_lag(redis),
            timeout_s=poll_timeout_s, interval_s=poll_interval_s, sleep=time.sleep, now=time.monotonic,
        )
        if not stable:
            return {"ok": False, "incomplete": True,
                    "gaps": ["audit-consumer-incomplete: rows did not stabilize within poll window"]}
    rows = _load_rows(conn, run_id)
    gaps = _assert(rows, verdict_payload)
    return {"ok": not gaps, "incomplete": False, "gaps": gaps}
