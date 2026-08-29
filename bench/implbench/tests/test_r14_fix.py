from __future__ import annotations

from pathlib import Path

import pytest

from implbench.harness.cell_runtime import CellPaths, CellRuntime, IdentityAllocator
from implbench.harness.controller import Controller
from implbench.harness.runtime import ProductionRuntimeUnavailable, _copy_descriptor_tree


class _Allocator(IdentityAllocator):
    def __init__(self) -> None:
        self.next = 20_000

    def mint(self, role: str) -> int:
        del role
        self.next += 1
        return self.next


class _Provisioner:
    real = True

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def provision_planes(self, paths, identities, *, attempt_id: str) -> None:
        self.events.append(("provision", attempt_id))

    def start_seat_daemon(self, paths, identities, *, attempt_id: str) -> None:
        self.events.append(("start", attempt_id))

    def stop_seat_daemon(self, paths, identities, *, attempt_id: str) -> None:
        self.events.append(("stop", attempt_id))

    def prove_absent(self, paths, identities, *, attempt_id: str) -> bool:
        self.events.append(("absent", attempt_id))
        return True


class _Acl:
    def __init__(self) -> None:
        self.users: set[str] = set()

    def provision(self, identity) -> None:
        self.users.add(identity.user)

    def namespace_keys(self, prefix: str) -> set[str]:
        return set()

    def cross_prefix_probe(self, user: str, prefix: str) -> bool:
        return False

    def disable_user(self, user: str) -> None:
        pass

    def kill_clients(self, user: str) -> None:
        pass

    def delete_prefix(self, prefix: str) -> None:
        pass

    def delete_user(self, user: str) -> None:
        self.users.discard(user)

    def authenticate(self, user: str, password: str) -> bool:
        del password
        return user in self.users


def test_real_cell_provisioning_starts_and_proves_absence(tmp_path: Path) -> None:
    provisioner = _Provisioner()
    paths = CellPaths.for_run("oi-pi-bakeoff-r14", "cell-" + "a" * 64, root=tmp_path)
    runtime = CellRuntime(
        paths,
        allocator=_Allocator(),
        acl=_Acl(),
        processes=type("Processes", (), {"close": lambda self, identities, grace_s: None})(),
        provisioner=provisioner,
        attempt_id="attempt-cell-r14-1",
        require_provisioner=True,
    )
    runtime.allocate()
    runtime.provision()
    runtime.mark_dispatched()
    runtime.close()
    assert [event[0] for event in provisioner.events] == ["provision", "start", "stop", "absent"]
    assert not paths.cell_root.exists()


def test_descriptor_copy_rejects_symlinks_and_hardlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe").write_bytes(b"safe")
    destination = tmp_path / "destination"
    (source / "link").symlink_to(source / "safe")
    with pytest.raises(ProductionRuntimeUnavailable, match="symlink"):
        _copy_descriptor_tree(source, destination)

    (source / "link").unlink()
    (source / "hardlink").hardlink_to(source / "safe")
    destination = tmp_path / "destination-2"
    with pytest.raises(ProductionRuntimeUnavailable, match="unsafe entry"):
        _copy_descriptor_tree(source, destination)


def test_completion_projection_preserves_authoritative_receipts_and_failure(tmp_path: Path) -> None:
    class Runtime:
        def completion_projection(self):
            return {"receipt_oids": (), "infrastructure_failure": None, "dirty": False}

    controller = Controller(
        tmp_path / "close.ndjson",
        runtime=Runtime(),
        close_context={
            "dispatch_status": "ok", "receipts": ("a" * 40,), "imported_oids": (), "dirty": False,
            "seal_complete": True, "receipts_authenticated": True, "imported_graph_attested": False,
            "infrastructure_failure": "trusted-failure",
        },
    )
    controller._sync_lifecycle_context()
    assert controller.close_context["receipts"] == ("a" * 40,)
    assert controller.close_context["infrastructure_failure"] == "trusted-failure"


def test_attempt_recovery_requires_the_same_attempt_journal(tmp_path: Path) -> None:
    events: list[str] = []

    class Runtime:
        def __getattr__(self, name):
            if name in {"stop_tools", "drain_rpc", "kill_planes", "close_acl", "final_status", "kill_git", "census_snapshot", "destroy"}:
                return lambda name=name: events.append(name)
            raise AttributeError(name)

    context = {
        "dispatch_status": "ok", "receipts": (), "imported_oids": (), "dirty": False,
        "seal_complete": True, "receipts_authenticated": True, "imported_graph_attested": False,
        "infrastructure_failure": None,
    }
    journal = tmp_path / ("attempt-" + "a" * 64 + ".close.ndjson")
    controller = Controller(journal, runtime=Runtime(), close_context=context, crash_before={"KILL_PLANES"})
    with pytest.raises(RuntimeError, match="KILL_PLANES"):
        controller.close()
    rows = controller.journal.read()
    assert any(row["phase"] == "KILL_PLANES" and row["status"] == "prepared" for row in rows)
    recovered = Controller(journal, runtime=Runtime(), close_context=context)
    recovered.recover()
    assert "kill_planes" in events
    assert any(row["phase"] == "KILL_PLANES" and row["status"] == "committed" for row in recovered.journal.read())
