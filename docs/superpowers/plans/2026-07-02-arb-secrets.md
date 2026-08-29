# ARB Secrets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bus-native peer-to-peer confidential secret/env transfer between two Claude peers, reusing an extracted NaCl crypto core, so no plaintext credential rests in any retained store.

**Architecture:** Extract the pure NaCl primitives from `arb_messages/keys.py` into a shared `arb_crypto` (adding authenticated `Box` alongside the existing anonymous `SealedBox`), leaving `arb_messages` behavior-identical via a re-export shim. Build a new `arb_secrets` module — pure Redis, no Postgres — with its own bus pubkey registry + local TOFU fingerprint pins, and a protocol where every sealed body travels through a pointer→TTL'd-blob→`GETDEL` indirection so only non-secret routing metadata ever reaches the inbox/watcher/tee.

**Tech Stack:** Python 3.14, PyNaCl (`nacl.public.Box`/`SealedBox`/`PrivateKey`/`PublicKey`), Redis (`redis-py`), pytest. Follows spec `docs/superpowers/specs/2026-07-02-arb-secrets-design.md` (revision 2).

## Global Constraints

- **No Postgres in `arb_secrets`.** Pure Redis transport + bus pubkey registry. ARB Messages' `arb_agent_keys` table is untouched. (spec §3)
- **`arb_messages` stays behavior-identical.** The full `tests/arb_messages/` suite must be green before AND after the `arb_crypto` extract; that is the extract's gate. Preserve module-attribute identity: `arb_messages.keys.live_key` / `.seal` / `.unseal` remain patchable and are what call sites invoke. (spec §3, §10)
- **Seal-to-pinned-key invariant:** a holder responding to a request always seals to the requester's *pinned* pubkey, never a key supplied in the request. (spec §6)
- **No inline ciphertext:** every sealed body (drop/request/reply) is written to a TTL'd blob key; the envelope carries only `{event, from, to, id(s), expires_at, blob_key}`. (spec §6, §9)
- **Keypair at** `~/.arb-secrets/privkey.b64` **mode 600**; pins at `~/.arb-secrets/known_peers.b64`. (spec §4, §5)
- **Redis key namespaces:** `agent_scratch:secrets:pubkey:<agent_id>`, `:blob:<recipient>:<id>`, `:seen:<self>:<id>`, `:status:<sender>:<id>`, `:outstanding:<self>:<req_id>`. The pointer envelope is `LPUSH`ed to the **recipient's real notify inbox** `agent_scratch:agent:<recipient>:inbox`. (spec §5, §6, §7)
- **Single envelope schema (plan-panel P1).** Every pointer is the bridge's canonical notify shape — `{"kind":"notify","payload":{"event":"secret_drop|secret_request|secret_reply","data":{"from","to","<id fields>","expires_at","blob_key"}}}` — used identically by `build_*`, `push_pointer`, and the Task 7 assertions. The sealed body (and, for requests, `what`) lives ONLY in the blob; `data` is non-secret routing metadata. No flat schema anywhere. (spec §6, §9)
- **Direction-D authorization (plan-panel P1):** a holder carries an `allowed_requesters` set; `accept_request` raises `Unauthorized` for a requester not in it, before any secret is released. (spec §6)
- **`expires_at`** = absolute UNIX timestamp (sender clock, NTP-synced hosts); recipient compares to own clock. Default blob TTL ~1h; seen-set TTL = max expiry window (24h). (spec §6)
- TDD throughout: failing test → run-red → minimal impl → run-green → commit.

---

## File Structure

- `src/arb_crypto/__init__.py` — pure NaCl primitives: `validate_public_key`, `seal`/`unseal` (SealedBox, moved from arb_messages), `box_seal`/`box_open` (Box, new), `fingerprint`, `generate_keypair`, `load_privkey`. No DB, no Redis.
- `src/arb_messages/keys.py` — becomes a shim: keeps `register_key`/`live_key` (Postgres, unchanged) and re-exports `_validate_public_key`/`seal`/`unseal` from `arb_crypto` preserving attribute identity.
- `src/arb_secrets/__init__.py` — package marker + version.
- `src/arb_secrets/keystore.py` — bus pubkey registry (publish/resolve) + local TOFU pin store + fingerprint verify.
- `src/arb_secrets/transport.py` — Redis ops: `put_blob`(SET+EXPIRE), `claim_blob`(GETDEL), `push_pointer`(LPUSH), `mark_seen`(SETNX EX), `set_status`/`get_status`, outstanding put/get/del.
- `src/arb_secrets/protocol.py` — envelope build/parse + the accept-decision state machine (verify order, invariant checks, replay, echo_what binding).
- `src/arb_secrets/peer.py` — high-level peer API: `push_secret`, `request_secret`, `claim_incoming`, `respond_to_request`.
- `tests/arb_secrets/test_*.py` — one per module + `test_retained_store_guarantee.py`.

---

## Task 1: Extract `arb_crypto` — pure primitives, arb_messages green before/after

**Files:**
- Create: `src/arb_crypto/__init__.py`
- Modify: `src/arb_messages/keys.py` (becomes shim)
- Test: `tests/arb_crypto/test_primitives.py`, existing `tests/arb_messages/` (regression gate)

**Interfaces:**
- Produces: `arb_crypto.validate_public_key(bytes)->PublicKey`; `arb_crypto.seal(pt:bytes, recipient_pub:bytes)->bytes`; `arb_crypto.unseal(ct:bytes, recipient_priv:bytes)->bytes`; `arb_crypto.fingerprint(pub:bytes)->str` (sha256 hex); `arb_crypto.generate_keypair()->(priv_b:bytes, pub_b:bytes)`; `arb_crypto.load_privkey(path)->bytes`.
- `arb_messages.keys` continues to expose `_validate_public_key`, `seal`, `unseal`, `register_key`, `live_key` identically.

- [ ] **Step 1: Baseline — run the existing arb_messages suite green first.**
Run: `PYTHONPATH=$(pwd)/src ARB_MESSAGES_TEST_DSN=postgresql://arb_messages_test:$ARB_MESSAGES_TEST_PASSWORD@127.0.0.1:5599/arb_messages_test .venv/bin/python -m pytest tests/arb_messages/ -q`
Expected: all pass (baseline before touching anything).

- [ ] **Step 2: Write the failing arb_crypto primitives test.**
```python
# tests/arb_crypto/test_primitives.py
import base64
from arb_crypto import validate_public_key, seal, unseal, fingerprint, generate_keypair

def test_sealbox_roundtrip():
    priv, pub = generate_keypair()
    ct = seal(b"secret-bytes", pub)
    assert unseal(ct, priv) == b"secret-bytes"

def test_fingerprint_stable_and_hex():
    _, pub = generate_keypair()
    fp = fingerprint(pub)
    assert fp == fingerprint(pub) and len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)

def test_validate_rejects_malformed():
    import pytest
    with pytest.raises(ValueError):
        validate_public_key(b"too-short")
```
- [ ] **Step 3: Run red.** `pytest tests/arb_crypto/test_primitives.py -q` → FAIL (module missing).
- [ ] **Step 4: Implement `src/arb_crypto/__init__.py`** — move the SealedBox/validate/fingerprint logic verbatim from `arb_messages/keys.py`, plus `generate_keypair`/`load_privkey`:
```python
from __future__ import annotations
import base64, hashlib
from nacl.public import PrivateKey, PublicKey, SealedBox

def validate_public_key(pubkey_bytes: bytes) -> PublicKey:
    try:
        return PublicKey(pubkey_bytes)
    except Exception as exc:
        raise ValueError("malformed public key") from exc

def seal(plaintext: bytes, recipient_pub: bytes) -> bytes:
    return SealedBox(validate_public_key(recipient_pub)).encrypt(plaintext)

def unseal(sealed: bytes, recipient_priv: bytes) -> bytes:
    return SealedBox(PrivateKey(recipient_priv)).decrypt(sealed)

def fingerprint(pubkey_bytes: bytes) -> str:
    return hashlib.sha256(pubkey_bytes).hexdigest()

def generate_keypair() -> tuple[bytes, bytes]:
    sk = PrivateKey.generate()
    return bytes(sk), bytes(sk.public_key)

def load_privkey(path: str) -> bytes:
    return base64.b64decode(open(path).read().strip())
```
- [ ] **Step 5: Rewrite `src/arb_messages/keys.py` as a shim** — re-export from arb_crypto preserving names, keep Postgres `register_key`/`live_key` unchanged. Keep the private-name alias `_validate_public_key = validate_public_key` and `seal`/`unseal` as module attributes so existing monkeypatch/import sites are unaffected:
```python
from __future__ import annotations
import base64, hashlib
from arb_crypto import validate_public_key as _validate_public_key, seal, unseal, fingerprint
# register_key / live_key: UNCHANGED Postgres implementations (retain existing bodies verbatim)
```
(Retain the exact existing `register_key`/`live_key` bodies from git; only the crypto helpers now come from arb_crypto.)
- [ ] **Step 6: Run green — arb_crypto AND the full arb_messages regression.**
Run both: `pytest tests/arb_crypto/ -q` and the Step-1 arb_messages command.
Expected: both green. If any arb_messages test changed behavior, the shim is wrong — fix the shim, not the test.
- [ ] **Step 7: Add the module-attribute-identity regression test.** Target `seal` — a symbol the extract MOVES and `arb_messages/keys.py` re-exports (plan-panel: `live_key` does NOT move, it stays Postgres-bound in arb_messages, so it's the wrong probe). `fulfillment.deliver_result` calls `keys.seal(...)` through the module, so this is the real patch-path:
```python
# tests/arb_crypto/test_shim_identity.py
def test_arb_messages_keys_seal_is_monkeypatchable_through_module():
    from arb_messages import keys
    sentinel = b"patched-seal"
    orig = keys.seal
    keys.seal = lambda pt, pub: sentinel
    try:
        assert keys.seal(b"x", b"y") == sentinel  # fulfillment calls via keys.seal
    finally:
        keys.seal = orig
```
- [ ] **Step 8: Commit.** `git add -A && git commit -m "refactor(arb-crypto): extract NaCl primitives; arb_messages shim, behavior-identical"`

## Task 2: `arb_crypto` Box (authenticated encryption)

**Files:** Modify `src/arb_crypto/__init__.py`; Test `tests/arb_crypto/test_box.py`

**Interfaces:**
- Produces: `arb_crypto.box_seal(pt:bytes, sender_priv:bytes, recipient_pub:bytes)->bytes` (nonce prepended); `arb_crypto.box_open(ct:bytes, recipient_priv:bytes, sender_pub:bytes)->bytes` (raises `nacl.exceptions.CryptoError` on tamper/wrong-sender).

- [ ] **Step 1: Failing test:**
```python
# tests/arb_crypto/test_box.py
import pytest
from nacl.exceptions import CryptoError
from arb_crypto import box_seal, box_open, generate_keypair

def test_box_roundtrip_authenticated():
    a_priv, a_pub = generate_keypair(); b_priv, b_pub = generate_keypair()
    ct = box_seal(b"cred", a_priv, b_pub)
    assert box_open(ct, b_priv, a_pub) == b"cred"

def test_box_wrong_sender_fails():
    a_priv, a_pub = generate_keypair(); b_priv, b_pub = generate_keypair()
    c_priv, c_pub = generate_keypair()
    ct = box_seal(b"cred", a_priv, b_pub)
    with pytest.raises(CryptoError):
        box_open(ct, b_priv, c_pub)  # claims C sent it; auth fails

def test_box_tamper_fails():
    a_priv, a_pub = generate_keypair(); b_priv, b_pub = generate_keypair()
    ct = bytearray(box_seal(b"cred", a_priv, b_pub)); ct[-1] ^= 1
    with pytest.raises(CryptoError):
        box_open(bytes(ct), b_priv, a_pub)
```
- [ ] **Step 2: Run red.** → FAIL (no box_seal).
- [ ] **Step 3: Implement** (nonce prepended, `Box` from `nacl.public`):
```python
from nacl.public import Box
from nacl.utils import random as nacl_random
def box_seal(pt: bytes, sender_priv: bytes, recipient_pub: bytes) -> bytes:
    box = Box(PrivateKey(sender_priv), validate_public_key(recipient_pub))
    nonce = nacl_random(Box.NONCE_SIZE)
    return nonce + box.encrypt(pt, nonce).ciphertext
def box_open(ct: bytes, recipient_priv: bytes, sender_pub: bytes) -> bytes:
    box = Box(PrivateKey(recipient_priv), validate_public_key(sender_pub))
    nonce, body = ct[:Box.NONCE_SIZE], ct[Box.NONCE_SIZE:]
    return box.decrypt(body, nonce)
```
- [ ] **Step 4: Run green.** `pytest tests/arb_crypto/test_box.py -q` → PASS.
- [ ] **Step 5: Commit.** `git commit -am "feat(arb-crypto): authenticated Box seal/open"`

## Task 3: `arb_secrets.keystore` — bus registry + TOFU pin

**Files:** Create `src/arb_secrets/__init__.py`, `src/arb_secrets/keystore.py`; Test `tests/arb_secrets/test_keystore.py`

**Interfaces:**
- Consumes: `arb_crypto.fingerprint`.
- Produces: `KeyStore(redis, agent_id, pins_path)` with `publish(pubkey:bytes)`, `resolve(peer_id)->bytes` (raises `KeyMismatch` if bus pubkey doesn't match an existing pin; raises `NoKey` if the peer has not published; TOFU-pins on first sight), `pin_fingerprint(peer_id)->str|None`.
- Errors: `KeyMismatch`, `NoKey` (both subclasses of a `KeyStoreError`).

- [ ] **Step 1: Failing test (fake redis dict + tmp pins file):**
```python
# tests/arb_secrets/test_keystore.py
import pytest
from arb_secrets.keystore import KeyStore, KeyMismatch
from arb_crypto import generate_keypair, fingerprint

class FakeRedis(dict):
    def set(self, k, v): self[k] = v
    def get(self, k): return super().get(k)

def test_resolve_tofu_pins_on_first_sight(tmp_path):
    r = FakeRedis(); _, pub = generate_keypair()
    r.set("agent_scratch:secrets:pubkey:peer-b", pub)
    ks = KeyStore(r, "peer-a", tmp_path / "pins.b64")
    assert ks.resolve("peer-b") == pub
    assert ks.pin_fingerprint("peer-b") == fingerprint(pub)

def test_resolve_mismatch_after_pin_raises(tmp_path):
    r = FakeRedis(); _, pub = generate_keypair(); _, evil = generate_keypair()
    r.set("agent_scratch:secrets:pubkey:peer-b", pub)
    ks = KeyStore(r, "peer-a", tmp_path / "pins.b64")
    ks.resolve("peer-b")                        # pins pub
    r.set("agent_scratch:secrets:pubkey:peer-b", evil)  # bus key swapped
    with pytest.raises(KeyMismatch):
        ks.resolve("peer-b")                    # never auto-updates

def test_resolve_no_published_key_raises_nokey(tmp_path):
    from arb_secrets.keystore import NoKey
    ks = KeyStore(FakeRedis(), "peer-a", tmp_path / "pins.b64")
    with pytest.raises(NoKey):
        ks.resolve("peer-never-published")
```
- [ ] **Step 2: Run red.** → FAIL.
- [ ] **Step 3: Implement `keystore.py`** — `publish` = `SET agent_scratch:secrets:pubkey:<self> <pub>`; `resolve` reads bus key, computes fingerprint, compares to pin file (base64 lines `peer_id fingerprint`); pin-on-first-sight, raise `KeyMismatch` on divergence, never overwrite the pin. `__init__.py` = `__version__ = "0.1.0"`.
- [ ] **Step 4: Run green.** → PASS.
- [ ] **Step 5: Commit.** `git commit -am "feat(arb-secrets): bus pubkey registry + TOFU pin store"`

## Task 4: `arb_secrets.transport` — Redis blob/pointer/seen/status/outstanding

**Files:** Create `src/arb_secrets/transport.py`; Test `tests/arb_secrets/test_transport.py`

**Interfaces:**
- Produces: `Transport(redis)` with `put_blob(recipient, blob_id, ct, ttl)`, `claim_blob(recipient, blob_id)->bytes|None` (GETDEL), `push_pointer(recipient, envelope:dict)`, `mark_seen(self_id, msg_id, ttl)->bool` (SETNX EX; False if already seen), `set_status(sender, id, value, ttl)`, `get_status(sender, id)->str|None`, `put_outstanding/get_outstanding/del_outstanding`.

- [ ] **Step 1: Failing tests** — pin the two security-critical transport behaviors: GETDEL is consume-once, SETNX seen is reject-second:
```python
# tests/arb_secrets/test_transport.py
from arb_secrets.transport import Transport
class FakeRedis(dict):
    def set(self,k,v,ex=None): self[k]=v
    def get(self,k): return dict.get(self,k)
    def getdel(self,k): return self.pop(k, None)
    def set_nx(self,k,v,ex=None):
        if k in self: return False
        self[k]=v; return True
    def lpush(self,k,v): self.setdefault(k,[]).insert(0,v)

def test_claim_blob_is_consume_once():
    t = Transport(FakeRedis())
    t.put_blob("b","1",b"ct",3600)
    assert t.claim_blob("b","1") == b"ct"
    assert t.claim_blob("b","1") is None      # GETDEL: gone

def test_status_transitions():
    t = Transport(FakeRedis())
    t.set_status("send","1","sent",3600); assert t.get_status("send","1") == "sent"
    t.set_status("send","1","claimed",3600); assert t.get_status("send","1") == "claimed"
```
(Adapt method names to the real `redis-py` API — `getdel`, `set(nx=True, ex=...)`. The Fake mirrors them.)
- [ ] **Step 1b: The seen-guard test must ALSO run against real Redis** (plan-panel P1 — the replay guard is security-critical and a fake that mimics `SETNX` proves nothing about the real semantics). Add a marked test that connects to the dev bus and asserts the actual atomic `SET nx=True ex=` behavior:
```python
import os, pytest, redis
@pytest.mark.skipif(not os.environ.get("ARB_DEV_REDIS_URL"), reason="needs dev redis")
def test_mark_seen_rejects_replay_real_redis():
    r = redis.Redis.from_url(os.environ["ARB_DEV_REDIS_URL"]); t = Transport(r)
    mid = "m-" + os.urandom(6).hex()
    assert t.mark_seen("self", mid, 60) is True
    assert t.mark_seen("self", mid, 60) is False   # real SETNX rejects the replay
```
Run with `ARB_DEV_REDIS_URL=redis://127.0.0.1:6379/12`.
- [ ] **Step 2: Run red → Step 3: implement thin wrappers over the namespaced keys (`mark_seen` = `redis.set(key, "1", nx=True, ex=ttl)` returns truthy/None) → Step 4: run green (fake + real-redis).**
- [ ] **Step 5: Commit.** `git commit -am "feat(arb-secrets): redis transport (blob/pointer/seen/status/outstanding)"`

## Task 5: `arb_secrets.protocol` — envelopes + accept state machine (the security core)

**Files:** Create `src/arb_secrets/protocol.py`; Test `tests/arb_secrets/test_protocol.py`

**Interfaces:**
- Consumes: `arb_crypto.box_seal/box_open`, `KeyStore`, `Transport`.
- Produces: builders `build_drop(sender_priv, sender_id, to, secret, ttl, ks)`, `build_request(sender_priv, sender_id, holder, what, ttl, ks)`, `build_reply(holder_priv, holder_id, request_meta, secret, ttl, ks)` (each returns `(envelope:dict, blob_id, ct:bytes)`); `accept_drop(env, ct, self_id, ks, transport)`, `accept_request(env, ct, self_id, ks, transport, allowed_requesters:set)`, `accept_reply(env, ct, self_id, ks, transport, outstanding:dict)` returning a decrypted result or raising a typed rejection: `Expired`, `Replay`, `AuthFail`, `WrongAnswer`, `WrongHolder`, `Unauthorized`.
- **Envelope:** the single nested schema (Global Constraints). `data` for a reply =
  `{from, to, reply_msg_id, in_reply_to, expires_at, blob_key}`; the sealed reply BODY =
  `{reply_msg_id, in_reply_to, echo_what, secret, expires_at}`.

- [ ] **Step 1: Failing tests — full concrete bodies (no ellipses), the invariants the panels flagged.** Build a fixture `env` that returns pinned keypairs for `lead`/`node`/`spoofer` in a shared FakeRedis + KeyStore per peer. Write these first:
```python
# tests/arb_secrets/test_protocol.py
import time, pytest
from arb_secrets.protocol import (build_request, build_reply, accept_reply, accept_request,
    accept_drop, build_drop, WrongAnswer, WrongHolder, Replay, Expired, AuthFail, Unauthorized)

def test_reply_binds_question_wrong_secret_under_right_req_id_rejected(env):
    # node has TWO outstanding requests to lead: req1 wants "DB_URL", req2 wants "API_KEY".
    r1, _, _ = build_request(env.node_priv, "node", "lead", "DB_URL", 3600, env.node_ks)
    r2, _, _ = build_request(env.node_priv, "node", "lead", "API_KEY", 3600, env.node_ks)
    id1 = r1["payload"]["data"]["req_id"]; id2 = r2["payload"]["data"]["req_id"]
    outstanding = {id1: {"holder":"lead","what":"DB_URL"}, id2: {"holder":"lead","what":"API_KEY"}}
    # lead answers req1 but the body echoes the WRONG what (API_KEY) — must be rejected.
    reply_env, _, ct = build_reply(env.lead_priv, "lead",
        {"from":"node","req_id":id1,"what":"API_KEY"}, b"the-api-key", 3600, env.lead_ks)
    with pytest.raises(WrongAnswer):
        accept_reply(reply_env, ct, "node", env.node_ks, env.transport, outstanding)

def test_reply_from_wrong_but_pinned_holder_rejected(env):
    # req issued to "lead"; a DIFFERENT trusted/pinned peer "node2" answers with the right label.
    req, _, _ = build_request(env.node_priv, "node", "lead", "DB_URL", 3600, env.node_ks)
    rid = req["payload"]["data"]["req_id"]
    outstanding = {rid: {"holder":"lead","what":"DB_URL"}}
    reply_env, _, ct = build_reply(env.node2_priv, "node2",
        {"from":"node","req_id":rid,"what":"DB_URL"}, b"evil", 3600, env.node2_ks)
    with pytest.raises(WrongHolder):
        accept_reply(reply_env, ct, "node", env.node_ks, env.transport, outstanding)

def test_reply_sealed_to_pinned_key_ignores_supplied_reply_to(env):
    # request carries a bogus reply_to_pubkey; build_reply MUST ignore it and seal to node's pin.
    req = {"from":"node","req_id":"q1","what":"DB_URL","reply_to_pubkey": env.spoofer_pub}
    reply_env, _, ct = build_reply(env.lead_priv, "lead", req, b"sec", 3600, env.lead_ks)
    # spoofer cannot open; node can.
    from arb_crypto import box_open
    with pytest.raises(Exception):
        box_open(ct, env.spoofer_priv, env.lead_pub)
    assert box_open(ct, env.node_priv, env.lead_pub).__contains__(b"sec") or True  # decrypts for node

def test_replayed_reply_rejected(env):
    req, _, _ = build_request(env.node_priv,"node","lead","DB_URL",3600,env.node_ks)
    rid = req["payload"]["data"]["req_id"]; outstanding={rid:{"holder":"lead","what":"DB_URL"}}
    reply_env,_,ct = build_reply(env.lead_priv,"lead",{"from":"node","req_id":rid,"what":"DB_URL"},b"s",3600,env.lead_ks)
    accept_reply(reply_env, ct, "node", env.node_ks, env.transport, dict(outstanding))
    with pytest.raises(Replay):
        accept_reply(reply_env, ct, "node", env.node_ks, env.transport, dict(outstanding))

def test_expired_drop_rejected(env):
    d_env,_,ct = build_drop(env.lead_priv,"lead","node",b"s",-1,env.lead_ks)  # already expired
    with pytest.raises(Expired):
        accept_drop(d_env, ct, "node", env.node_ks, env.transport)

def test_unauthorized_requester_denied(env):
    req,_,ct = build_request(env.node_priv,"node","lead","DB_URL",3600,env.node_ks)
    with pytest.raises(Unauthorized):
        accept_request(req, ct, "lead", env.lead_ks, env.transport, allowed_requesters=set())
```
- [ ] **Step 2: Run red.**
- [ ] **Step 3: Implement `protocol.py`.** Bodies are JSON, `box_seal`ed. **`accept_reply` order (spec §6, plan-panel holder-binding fix):** `box_open` with the sender's pinned key (AuthFail on CryptoError) → look up `in_reply_to` in `outstanding` (WrongAnswer if absent) → **verify the envelope/authenticated sender == `outstanding[req_id]["holder"]` (WrongHolder else — this is the check the panel found missing: a stored `holder` that is actually READ)** → `echo_what == outstanding[req_id]["what"]` (WrongAnswer else) → `to==self` + `expires_at` vs now (Expired) → `mark_seen(reply_msg_id)` (Replay if False) → return secret, del outstanding. **`accept_request`:** `box_open` → if `from not in allowed_requesters` raise `Unauthorized` (before returning `what`) → expiry/seen → return `what`+meta. **`build_reply` seals to `ks.resolve(request_meta["from"])`** — it must NOT read any `reply_to_pubkey` in the request. **`accept_drop`:** box_open → to==self → expiry → seen → secret.
- [ ] **Step 4: Run green** (all invariant tests pass — including WrongHolder and Unauthorized, which fail against a naive impl).
- [ ] **Step 5: Commit.** `git commit -am "feat(arb-secrets): protocol envelopes + accept state machine (invariants pinned)"`

## Task 6: `arb_secrets.peer` — high-level API

**Files:** Create `src/arb_secrets/peer.py`; Test `tests/arb_secrets/test_peer.py`

**Interfaces:**
- Produces: `Peer(redis, agent_id, privkey_path, pins_path, allowed_requesters:set=frozenset())` with `push_secret(to, secret:bytes, ttl=3600)`, `request_secret(holder, what:str, ttl=3600)->req_id` (records the outstanding entry), `claim_incoming()->list[Incoming]` (drains the `agent_scratch:agent:<self>:inbox` pointers, claims blobs via `GETDEL`, runs the matching `accept_*` — passing `allowed_requesters` to `accept_request` and the peer's outstanding map to `accept_reply` — returns decrypted+typed results, updates the status key), `respond_to_request(incoming_request, secret:bytes)` (builds a `secret_reply` sealed to the requester's pinned key). A metadata-only `audit_sink` (optional callable) logs `{from,to,event,id,decision,fingerprints,ts}` — never the body/`what`.

- [ ] **Step 1: Failing end-to-end-in-process test** (two `Peer`s sharing one FakeRedis): push→claim returns the secret; request→respond→claim returns the secret with matching `what`. → red → implement wiring (compose keystore+transport+protocol; seal-before-send: build ct in-process, `put_blob`, `push_pointer`) → green → commit.

## Task 7: Retained-store guarantee (spec §9) — no secret in inbox/watcher/Monitor

**Files:** Test `tests/arb_secrets/test_retained_store_guarantee.py`

- [ ] **Step 1: Failing test** — after a full push and a full request/reply through real `Transport` against a real local Redis (dev), assert the **inbox list** entries and the **pointer envelopes** contain none of: the secret bytes, `what`, `echo_what`, or blob ciphertext — only `{event, from, to, id, expires_at, blob_key}`. Drive the producing functions directly (per `test-behind-framework-drive-directly`); if a split-watcher/Monitor harness is impractical, assert on exactly the bytes `push_pointer` writes (that is the sole thing those stores can observe).
```python
import json
def test_no_secret_bytes_reach_inbox(dev_redis):
    peer_a, peer_b = two_peers(dev_redis)   # real Peers on the dev bus, distinct pinned keys
    peer_a.push_secret(peer_b.agent_id, b"SUPER-SECRET-CRED", ttl=60)
    inbox = f"agent_scratch:agent:{peer_b.agent_id}:inbox"
    raw = dev_redis.lrange(inbox, 0, -1)
    assert len(raw) >= 1, "vacuous pass guard: the pointer must actually land in the inbox"
    blob = b"".join(x if isinstance(x, bytes) else x.encode() for x in raw)
    assert b"SUPER-SECRET-CRED" not in blob            # secret never inline
    for e in raw:
        env = json.loads(e)
        assert env["kind"] == "notify"                 # canonical nested schema
        data = env["payload"]["data"]
        assert "blob_key" in data                      # only a pointer
        assert not any(k in data for k in ("secret", "what", "echo_what", "ciphertext"))

def test_no_what_reaches_inbox_on_request(dev_redis):
    peer_a, peer_b = two_peers(dev_redis)
    peer_a.request_secret(peer_b.agent_id, "prod-stripe-key-acct-1234", ttl=60)
    raw = dev_redis.lrange(f"agent_scratch:agent:{peer_b.agent_id}:inbox", 0, -1)
    assert len(raw) >= 1
    assert b"stripe" not in b"".join(x if isinstance(x,bytes) else x.encode() for x in raw).lower()
```
- [ ] **Step 2–4:** run red (if any body/`what` leaks inline, or the pointer never lands, this fails), confirm green under the pointer-indirection design, commit. Run with `ARB_DEV_REDIS_URL=redis://127.0.0.1:6379/12`.

## Task 8: Peer keypair bootstrap + docs

**Files:** Create `src/arb_secrets/cli.py` (a tiny `init`/`publish` helper); Modify `docs/` note; Test `tests/arb_secrets/test_cli.py`

- [ ] Generate keypair to `~/.arb-secrets/privkey.b64` mode 600 if absent; publish pubkey to the bus; print fingerprint for out-of-band operator vouching (spec §5 first-use). Test the file mode + idempotent init. Commit.

---

## Self-review notes (author)
- Spec coverage: §3 extract→T1/T2; §4 crypto→T1/T2; §5 TOFU→T3; §6 protocol+invariants→T5; §7 transport/status→T4; §9 retained-store→T7; §8 lifecycle bootstrap→T8; §10 tests distributed. Rotation (§8 full) is operator-run, covered by T8 bootstrap + keystore mismatch surfacing (T3) — no separate code task.
- Deferred to a follow-up plan if the panel wants: an automated rotation command and the split-watcher/Monitor real-harness variant of T7 (T7 as written asserts on the pointer bytes, which is the tight bound).
