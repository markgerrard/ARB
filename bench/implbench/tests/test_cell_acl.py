from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from implbench.harness.cell_runtime import ACLIdentity, ACLLifecycle, ACLNotProvisioned


@dataclass
class FakeValkey:
    calls: list[tuple[str, object]] = field(default_factory=list)
    prefixes: dict[str, set[str]] = field(default_factory=dict)
    users: set[str] = field(default_factory=set)
    clients: dict[str, int] = field(default_factory=dict)

    def ping(self) -> bool:
        self.calls.append(("ping", None))
        return True

    def provision(self, identity: ACLIdentity) -> None:
        self.calls.append(("provision", identity))
        self.users.add(identity.user)
        self.prefixes[identity.prefix] = set()
        self.clients[identity.user] = 1

    def namespace_keys(self, prefix: str) -> set[str]:
        self.calls.append(("keys", prefix))
        return set(self.prefixes.get(prefix, set()))

    def cross_prefix_probe(self, user: str, prefix: str, forbidden_prefix: str) -> bool:
        self.calls.append(("cross-prefix", (user, prefix, forbidden_prefix)))
        return False

    def disable_user(self, user: str) -> None:
        self.calls.append(("disable", user))

    def kill_clients(self, user: str) -> None:
        self.calls.append(("kill-clients", user))
        self.clients.pop(user, None)

    def delete_prefix(self, prefix: str) -> None:
        self.calls.append(("delete-prefix", prefix))
        self.prefixes.pop(prefix, None)

    def delete_user(self, user: str) -> None:
        self.calls.append(("delete-user", user))
        self.users.discard(user)

    def authenticate(self, user: str, password: str) -> bool:
        self.calls.append(("authenticate", (user, password)))
        return user in self.users


def test_acl_identity_is_random_and_cell_prefixed() -> None:
    first = ACLIdentity.create("cell-" + "a" * 64, token="one")
    second = ACLIdentity.create("cell-" + "a" * 64, token="two")
    assert first.user != second.user
    assert first.prefix != second.prefix
    assert first.user.startswith("implbench-cell-")
    assert first.prefix.startswith("implbench:")
    assert first.password == "one"


def test_acl_lifecycle_proves_namespace_not_endpoint_and_orders_retirement() -> None:
    valkey = FakeValkey()
    lifecycle = ACLLifecycle(valkey)
    identity = ACLIdentity.create("cell-" + "b" * 64, token="secret")

    lifecycle.provision(identity)
    assert lifecycle.pre_empty(identity) is True
    assert lifecycle.cross_prefix_denied(identity) is True
    assert lifecycle.endpoint_reachable() is True

    lifecycle.close(identity)

    names = [name for name, _ in valkey.calls]
    assert names.index("disable") < names.index("kill-clients") < names.index("delete-prefix") < names.index("delete-user")
    assert names[-2:] == ["authenticate", "keys"]
    assert valkey.authenticate(identity.user, identity.password) is False
    assert valkey.namespace_keys(identity.prefix) == set()


def test_acl_not_provisioned_still_runs_authenticated_cleanup_probes() -> None:
    valkey = FakeValkey()
    lifecycle = ACLLifecycle(valkey)
    identity = ACLIdentity.create("cell-" + "c" * 64, token="secret")

    with pytest.raises(ACLNotProvisioned):
        lifecycle.close(identity)

    assert [name for name, _ in valkey.calls] == [
        "disable", "kill-clients", "delete-prefix", "delete-user", "authenticate", "keys"
    ]
