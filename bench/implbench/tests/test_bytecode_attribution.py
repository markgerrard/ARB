from __future__ import annotations

from pathlib import Path

import pytest

from implbench.harness.scorer_sandbox import (
    BytecodeEvent,
    BytecodeOutcome,
    attribute_bytecode,
)


def test_whole_sandbox_scan_catches_pyc_and_pycache_anywhere(tmp_path: Path) -> None:
    imported = tmp_path / "imported"
    imported.mkdir()
    scratch = tmp_path / "scratch" / "nested" / "__pycache__"
    scratch.mkdir(parents=True)
    (scratch / "x.cpython-312.pyc").write_bytes(b"bytecode")

    result = attribute_bytecode(
        tmp_path,
        imported_tree=imported,
        events=(BytecodeEvent("submitted-program", scratch / "x.cpython-312.pyc", "created"),),
    )
    assert result == BytecodeOutcome.MODEL_G5


def test_infrastructure_role_bytecode_is_unknown() -> None:
    root = Path("/sandbox")
    result = attribute_bytecode(
        root,
        imported_tree=root / "imported",
        events=(BytecodeEvent("keyed-runner", root / "tmp.pyc", "created"),),
    )
    assert result == BytecodeOutcome.INFRASTRUCTURE_UNKNOWN


def test_unprovable_bytecode_attribution_is_unknown() -> None:
    root = Path("/sandbox")
    result = attribute_bytecode(
        root,
        imported_tree=root / "imported",
        events=(BytecodeEvent("unknown", root / "tmp.pyc", "detected"),),
    )
    assert result == BytecodeOutcome.INFRASTRUCTURE_UNKNOWN


def test_imported_tree_bytecode_is_model_g5_even_without_role_event(tmp_path: Path) -> None:
    imported = tmp_path / "imported"
    imported.mkdir()
    (imported / "module.pyc").write_bytes(b"bytecode")
    assert attribute_bytecode(tmp_path, imported_tree=imported, events=()) == BytecodeOutcome.MODEL_G5
