"""ARB Secrets E2E: two real peers, distinct keypairs, live dev bus (db12), real Box.
Exercises push (A) and request/reply (D) end to end, and asserts no plaintext hit the inbox."""
import os, sys, json, tempfile, shutil, redis

RUN = "e2e-" + os.urandom(4).hex()
r = redis.Redis.from_url("redis://127.0.0.1:6379/12")
from arb_secrets.peer import Peer

tmp = tempfile.mkdtemp(prefix="arb-secrets-e2e-")
A_id, B_id = f"claude-{RUN}-lead", f"claude-{RUN}-node"

def mk(agent_id, allowed):
    d = os.path.join(tmp, agent_id); os.makedirs(d)
    return Peer(r, agent_id, os.path.join(d, "privkey.b64"), os.path.join(d, "pins.b64"),
                allowed_requesters=allowed)

created_keys = []
def note_keys():
    for k in r.scan_iter(match=f"agent_scratch:secrets:*{RUN}*"): created_keys.append(k)
    for k in r.scan_iter(match=f"agent_scratch:agent:*{RUN}*"): created_keys.append(k)

try:
    lead = mk(A_id, allowed={B_id})   # lead will accept requests from node (auto-publishes pubkey)
    node = mk(B_id, allowed={A_id})
    # mutual pin (TOFU first-use)
    lead.keystore.resolve(B_id); node.keystore.resolve(A_id)

    # --- Direction A: lead pushes a secret to node ---
    SECRET_A = b"CF_TOKEN=cfat_live_" + os.urandom(6).hex().encode()
    lead.push_secret(B_id, SECRET_A, ttl=120)
    # assert plaintext never in node's inbox
    inbox = r.lrange(f"agent_scratch:agent:{B_id}:inbox", 0, -1)
    blob = b"".join(x if isinstance(x, bytes) else x.encode() for x in inbox)
    assert len(inbox) >= 1, "push pointer missing from inbox"
    assert SECRET_A not in blob, "SECRET LEAKED INTO INBOX (push)"
    got = node.claim_incoming()
    assert any(getattr(i, "secret", None) == SECRET_A for i in got), f"push not received: {got}"
    print(f"[A push] OK — node received {SECRET_A[:14]!r}..., inbox carried only a pointer")

    # --- Direction D: node requests a secret from lead ---
    WHAT = "prod-stripe-key-acct-1234"
    req_id = node.request_secret(A_id, WHAT, ttl=120)
    # the request's `what` must not be cleartext in lead's inbox
    linbox = r.lrange(f"agent_scratch:agent:{A_id}:inbox", 0, -1)
    lblob = b"".join(x if isinstance(x, bytes) else x.encode() for x in linbox)
    assert b"stripe" not in lblob.lower(), "WHAT LEAKED INTO INBOX (request)"
    incoming = lead.claim_incoming()
    reqs = [i for i in incoming if i.event == "secret_request"]
    assert reqs, f"lead did not receive the request: {incoming}"
    assert reqs[0].what == WHAT, f"decrypted what mismatch: {reqs[0].what!r}"
    SECRET_D = b"STRIPE_KEY=sk_live_" + os.urandom(6).hex().encode()
    lead.respond_to_request(reqs[0], SECRET_D)
    replies = node.claim_incoming()
    assert any(getattr(i, "secret", None) == SECRET_D for i in replies), f"reply not received: {replies}"
    print(f"[D request] OK — node asked {WHAT!r} (sealed), lead answered, node got {SECRET_D[:16]!r}...")

    # --- idempotent re-claim: reply already consumed (GETDEL) ---
    again = node.claim_incoming()
    assert not any(getattr(i, "secret", None) == SECRET_D for i in again), "reply re-delivered after GETDEL"
    print("[consume-once] OK — GETDEL means the reply is not re-delivered")

    print("\nE2E PASS: push + request/reply round-tripped over the live bus; no plaintext in any inbox.")
finally:
    note_keys()
    if created_keys:
        r.delete(*created_keys)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"cleaned up {len(created_keys)} bus keys + tmp keystore")
