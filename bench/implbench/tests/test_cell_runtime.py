from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from implbench.harness.cell_runtime import (
    CellPaths,
    CellRuntime,
    IdentityAllocator,
    cell_id_for,
    delete_tree_descriptor_safe,
)


class FakeAllocator(IdentityAllocator):
    def __init__(self) -> None:
        self.next_uid = 41000

    def mint(self, role: str) -> int:
        self.next_uid += 1
        return self.next_uid


class FakeACL:
    def __init__(self) -> None:
        self.provisioned: list[object] = []
        self.closed = False

    def provision(self, identity) -> None:
        self.provisioned.append(identity)
        return identity

    def close(self, identity) -> None:
        self.closed = True


class FakeProcesses:
    def __init__(self) -> None:
        self.empty = False

    def close(self, identities, *, grace_s: float) -> None:
        self.empty = True


def test_cell_id_and_agent_identity_are_immutable_and_cell_scoped() -> None:
    cell_id = cell_id_for("GLM", "glm-pi", "c1-parser", 2, 17)
    assert cell_id.startswith("cell-")
    assert cell_id == cell_id_for("GLM", "glm-pi", "c1-parser", 2, 17)
    assert cell_id != cell_id_for("GLM", "glm-pi", "c1-parser", 3, 17)

    paths = CellPaths.for_run("oi-pi-bakeoff-test", cell_id, root=Path("/Users/Shared/arb-implbench"))
    agent_id = paths.agent_id("pi-sdk-agentredisbridge-bake-glm52")
    assert agent_id.startswith("pi-sdk-agentredisbridge-bake-glm52-")
    assert agent_id == paths.agent_id("pi-sdk-agentredisbridge-bake-glm52")


def test_runtime_creates_controller_owned_0700_roots_and_fresh_planes(tmp_path: Path) -> None:
    paths = CellPaths.for_run("oi-pi-bakeoff-test", "cell-" + "a" * 64, root=tmp_path)
    runtime = CellRuntime(paths, allocator=FakeAllocator(), acl=FakeACL(), processes=FakeProcesses())

    runtime.allocate()
    runtime.provision()

    assert paths.cell_root.is_dir()
    assert stat.S_IMODE(paths.cell_root.stat().st_mode) == 0o700
    assert paths.cell_root.stat().st_uid == os.getuid()
    assert len({runtime.identities.control, runtime.identities.tool, runtime.identities.git}) == 3
    assert runtime.tool_gid == runtime.identities.tool_gid
    assert runtime.tool_gid > 0
    assert paths.control_home.is_dir()
    assert paths.tool_home.is_dir()
    assert paths.git_home.is_dir()
    assert paths.bus_namespace.is_dir()
    assert all(not path.is_symlink() for path in paths.managed_paths)


def test_write_ahead_recovery_converges_after_each_provisioning_side_effect(tmp_path: Path) -> None:
    for fault_after in range(1, 7):
        paths = CellPaths.for_run("oi-pi-bakeoff-test", "cell-" + f"{fault_after:064x}", root=tmp_path / str(fault_after))
        runtime = CellRuntime(
            paths,
            allocator=FakeAllocator(),
            acl=FakeACL(),
            processes=FakeProcesses(),
            fault_after=fault_after,
        )
        runtime.allocate()
        with pytest.raises(RuntimeError, match="injected provisioning fault"):
            runtime.provision()
        runtime.recover()
        assert not paths.cell_root.exists()
        assert runtime.state == "DESTROYED"


def test_descriptor_safe_delete_does_not_follow_hostile_symlink(tmp_path: Path) -> None:
    root = tmp_path / "cell"
    target = tmp_path / "outside"
    root.mkdir()
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    (root / "link").symlink_to(target, target_is_directory=True)
    (root / "regular.txt").write_text("remove")

    delete_tree_descriptor_safe(root)

    assert not root.exists()
    assert (target / "keep.txt").read_text() == "keep"
