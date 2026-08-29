from __future__ import annotations

import fnmatch
import re

import pytest

from arb_registration.bus_acl import (
    AclProvisionError, ENGINES, ROLE_NAMES, orchestrator_rules, role_rules,
    role_username, validate_declared_roles, validate_host, worker_rules,
)


@pytest.mark.parametrize(
    "host",
    [
        "bad.host", "-bad", "bad_underscore", "", "a ~* +@all", "a:*", "a\n+@all", "*",
    ],
)
def test_host_is_a_lowercase_dns_label(host):
    with pytest.raises(AclProvisionError, match="lower-case DNS label"):
        validate_host(host)


def test_host_is_normalized_before_acl_identity_construction():
    assert validate_host("host-b") == "host-b"


def test_per_engine_role_names_and_exact_minimum_grants():
    rules = role_rules("host-b", ROLE_NAMES)
    assert set(rules) == {
        "claude-orch-host-b", "codex-orch-host-b", "pi-orch-host-b",
        "arb-worker-host-b",
    }
    for engine in ENGINES:
        value = rules[f"{engine}-orch-host-b"]
        assert value == orchestrator_rules(engine, "host-b")
        assert "+select|12" in value
        # Was `~agent_scratch:agent:{engine}-host-b-*:inbox` — which asserted the DEFECT back at
        # the code: it omitted the `-orch-` segment, so the pattern matched no key of the real
        # identity, and the test passed anyway because it only checked the string's shape.
        assert f"~agent_scratch:agent:{role_username(engine, 'host-b')}*:*" in value
        assert "(+lpush ~agent_scratch:agent:*:inbox)" in value
        assert "(+incr +incrby +expire ~arbmem:audit:run:*:seq)" in value
        assert "(+xadd ~arbmem:audit)" in value
        assert "+keys" not in value and "+scan" not in value and "+config" not in value
        assert "~arbmem:writes" not in value

    worker = rules["arb-worker-host-b"]
    assert worker == worker_rules("host-b")
    assert "~agent_scratch:agent:worker-host-b-*:inbox" in worker
    assert "(+lpush ~agent_scratch:agent:*:inbox)" in worker
    assert "+keys" not in worker and "+scan" not in worker and "+config" not in worker
    assert "~arbmem:writes" not in worker


def test_worker_grants_cover_every_command_a_seat_issues_at_startup():
    """The previous shape was written from the spec's prose and asserted against
    itself, so it omitted the whole startup path and NO seat could run under it
    (found 2026-08-09 before first use). These assertions are derived from the code
    paths in redis_io.py / bridge.py, so they fail if a grant regresses.
    """
    worker = worker_rules("host-b")
    own = "agent_scratch:agent:worker-host-b-*"

    # Identity lease is Lua over the daemon's own status/consumer/registry keys.
    assert "+eval" in worker
    for key in (f"~{own}:status", f"~{own}:consumer", "~agent_scratch:registry:worker-host-b-*"):
        assert key in worker, key
    # Registry keys are `registry:<id>`, not `agent:<id>:registry`.
    assert "~agent_scratch:registry:worker-host-b-*" in worker

    # Reliable consume path + control channel.
    for token in ("+blmove", "+lmove", f"~{own}:processing", f"~{own}:processing_claim:*", f"~{own}:control"):
        assert token in worker, token

    # Notify split writes to the CALLER's :notify_inbox, which agent:*:inbox cannot match.
    assert "(+lpush +ltrim ~agent_scratch:agent:*:notify_inbox)" in worker

    # Audit rides a separate connection (DB 5, audit-emitter credential), so DB-12
    # audit grants on this identity are dead weight and must NOT reappear.
    assert "~arbmem:audit" not in worker


def test_agent_inbox_send_pattern_does_not_match_notify_inbox():
    """The glob that made the notify grant necessary: `:inbox` never matches
    `:notify_inbox`. If this ever passes, the separate notify selector can go."""
    assert not fnmatch.fnmatch(
        "agent_scratch:agent:someone:notify_inbox", "agent_scratch:agent:*:inbox"
    )
    assert fnmatch.fnmatch("agent_scratch:agent:someone:inbox", "agent_scratch:agent:*:inbox")


def test_declared_role_subset_maps_only_requested_identities():
    assert set(role_rules("host-b", ("codex", "worker"))) == {
        "codex-orch-host-b", "arb-worker-host-b",
    }


@pytest.mark.parametrize(
    "roles", [[], ["root"], ["codex", "root"], ["codex", "codex"]]
)
def test_unknown_empty_or_duplicate_declared_roles_are_rejected(roles):
    with pytest.raises(AclProvisionError):
        validate_declared_roles(roles)


def _selectors(rules: str) -> list[tuple[set[str], list[str]]]:
    """(commands, key-patterns) per independent selector — the unit Valkey authorizes on."""
    out = []
    for body in re.findall(r"\(([^()]*)\)", rules):
        toks = body.split()
        out.append((
            {t for t in toks if t.startswith(("+", "-"))},
            [t.lstrip("~") for t in toks if t.startswith("~")],
        ))
    return out


def _authorizes(rules: str, command: str, key: str) -> bool:
    """A command is allowed if ANY ONE selector grants both it and the key."""
    return any(
        command in cmds and any(fnmatch.fnmatch(key, p) for p in pats)
        for cmds, pats in _selectors(rules)
    )


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("destructive", ["+del", "+getdel", "+expire"])
def test_no_selector_can_destroy_an_identitys_own_published_trust_root(engine, destructive):
    """A self-publication credential must not be able to UNPUBLISH its own pubkey.

    The pubkey and the ARB Secrets transport keys once shared one selector, so the trust
    root inherited transport verbs by accident of grouping — `+getdel` on a pubkey being
    the tell, a read that destroys what it read. On 2026-08-10 claude-orch-mini-dev deleted
    its own freshly-published key from a capability probe that treated DEL as just another
    thing to enumerate. Checked per-selector because independent selectors are the unit
    Valkey authorizes on: a union over all of them would hide exactly this defect.
    """
    host = "mini-dev"
    rules = orchestrator_rules(engine, host)
    pubkey = f"agent_scratch:secrets:pubkey:{role_username(engine, host)}-cli"

    assert not _authorizes(rules, destructive, pubkey), (
        f"{destructive} is authorized on {pubkey}; rotation is a SET, so no destructive "
        "verb belongs on a trust root"
    )


@pytest.mark.parametrize("engine", ENGINES)
def test_identity_can_still_publish_and_read_its_own_trust_root(engine):
    host = "mini-dev"
    rules = orchestrator_rules(engine, host)
    pubkey = f"agent_scratch:secrets:pubkey:{role_username(engine, host)}-cli"

    for command in ("+set", "+get", "+exists"):
        assert _authorizes(rules, command, pubkey), f"{command} missing on own pubkey"


@pytest.mark.parametrize("engine", ENGINES)
def test_secrets_transport_keeps_the_destructive_verbs_it_needs(engine):
    """The split must not disarm the transport path: a consumed blob should be deletable."""
    host = "mini-dev"
    rules = orchestrator_rules(engine, host)
    blob = f"agent_scratch:secrets:blob:{role_username(engine, host)}-cli:abc123"

    for command in ("+del", "+getdel", "+expire", "+set", "+get"):
        assert _authorizes(rules, command, blob), f"{command} missing on own transport keys"
