"""A live ACL proof must probe keys inside the identity's OWN granted namespace.

`0f47706a` fixed `orchestrator_rules` rebuilding the identity as f"{engine}-{host}" and
dropping the `-orch-` segment. The same reconstruction survived in the VERIFIER at two
sites, because only the generator was swept — the second instance of "fixed what was in
front of me, missed the siblings" in as many days.

The two sites fail differently, and the quiet one is why this test enumerates rather than
asserting a single case:

* own-secret — HARD FAIL, and `provision()` raises only AFTER writing the aclfile and
  ACL LOADing, so a failed proof leaves the ACL applied with no rollback.
* inbox — SILENT PASS on the broad `agent:*:inbox` send grant, proving the wrong property.

Found by codex-arbmem-prod reading the code before calling the provisioner, 2026-08-10.
"""

from __future__ import annotations

import fnmatch

import pytest

from arb_registration.bus_acl import (
    ENGINES, ROLE_NAMES, proof_keys, role_rules, role_username,
)

HOST = "mini-dev"


def _granted(rules: str, prefix: str) -> list[str]:
    return [t.lstrip("~").rstrip(")") for t in rules.split() if t.startswith(prefix)]


@pytest.mark.parametrize("engine", ENGINES)
def test_own_secret_proof_is_inside_the_identitys_granted_pubkey_pattern(engine):
    username = role_username(engine, HOST)
    rules = role_rules(HOST, ROLE_NAMES)[username]
    _, own_secret = proof_keys(username, HOST)
    patterns = _granted(rules, "~agent_scratch:secrets:pubkey:")

    assert patterns, f"{username} has no pubkey grant to prove against"
    assert any(fnmatch.fnmatch(own_secret, p) for p in patterns), (
        f"{own_secret} is outside {patterns} — provision() would deny this proof "
        "AFTER already ACL LOADing"
    )


@pytest.mark.parametrize("engine", ENGINES)
def test_the_dropped_orch_segment_form_is_rejected(engine):
    """The regression itself: the old reconstruction must NOT satisfy the grant."""
    username = role_username(engine, HOST)
    rules = role_rules(HOST, ROLE_NAMES)[username]
    patterns = _granted(rules, "~agent_scratch:secrets:pubkey:")
    old_form = f"agent_scratch:secrets:pubkey:{engine}-{HOST}-acl-proof"

    assert not any(fnmatch.fnmatch(old_form, p) for p in patterns), (
        f"{old_form} unexpectedly matches {patterns}; this test can no longer fail"
    )


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_inbox_proof_exercises_the_identitys_own_namespace_not_the_broad_send_grant(role):
    """`agent:*:inbox` would pass any name, so assert against the OWN-namespace pattern."""
    username = role_username(role, HOST)
    rules = role_rules(HOST, ROLE_NAMES)[username]
    own_inbox, _ = proof_keys(username, HOST)

    own_patterns = [
        p for p in _granted(rules, "~agent_scratch:agent:")
        if not p.startswith("agent_scratch:agent:*")
    ]
    assert own_patterns, f"{username} has no own-namespace agent grant"
    assert any(fnmatch.fnmatch(own_inbox, p) for p in own_patterns), (
        f"{own_inbox} matches no own-namespace pattern in {own_patterns} — it would "
        "pass only via the broad send grant, proving nothing about this identity"
    )


def test_worker_proof_keys_stay_on_the_worker_prefix():
    username = role_username("worker", HOST)
    own_inbox, own_secret = proof_keys(username, HOST)

    assert own_inbox == f"agent_scratch:agent:worker-{HOST}-acl-proof:inbox"
    # Workers deliberately hold no pubkey grant; the key is still derived from the
    # username so it can never collide with an orchestrator's namespace.
    assert own_secret == f"agent_scratch:secrets:pubkey:{username}-acl-proof"
