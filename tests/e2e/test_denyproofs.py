from types import SimpleNamespace
import unittest.mock as mock

import pytest

from tests.e2e import h2_harness
from tests.e2e.runner import run_suite
from tests.e2e.spine import E2EStatus


pytestmark = pytest.mark.e2e


def test_mocked_boundary_maps_to_block_unrun(monkeypatch, tmp_path):
    original_load = h2_harness._load
    real_coll = original_load("h2_collector", "skills/bridge-protocol/gate/h2_collector.py")

    def fake_load(modname, relpath):
        if relpath.endswith("h2_collector.py"):
            return SimpleNamespace(
                append_record=mock.Mock(side_effect=real_coll.append_record),
                shadow_log_path=mock.Mock(side_effect=real_coll.shadow_log_path),
            )
        return original_load(modname, relpath)

    monkeypatch.setattr(h2_harness, "_load", fake_load)
    result = run_suite(case_ids=["enumerated/redis-from-url"], tmp=tmp_path, status_path=tmp_path / "status.json")
    assert result.status is E2EStatus.BLOCK_UNRUN
    assert result.block_unrun_count == 1
    assert result.block_fail_count == 0


def test_zero_case_maps_to_block_unrun(tmp_path):
    result = run_suite(case_ids=[], tmp=tmp_path, status_path=tmp_path / "status.json")
    assert result.status is E2EStatus.BLOCK_UNRUN
    assert result.case_count == 0


def test_expected_surface_breakage_maps_to_block_fail(monkeypatch, tmp_path):
    original_run_case = h2_harness.run_case

    def broken_run_case(case, tmp):
        out = original_run_case(case, tmp)
        out["record_payload"] = dict(out["record_payload"])
        out["record_payload"]["complete"] = True
        out["log_records"][-1] = out["record_payload"]
        return out

    monkeypatch.setattr(h2_harness, "run_case", broken_run_case)
    result = run_suite(case_ids=["discovered/duplicate-id"], tmp=tmp_path, status_path=tmp_path / "status.json")
    assert result.status is E2EStatus.BLOCK_FAIL
    assert result.block_fail_count == 1
    assert result.block_unrun_count == 0
