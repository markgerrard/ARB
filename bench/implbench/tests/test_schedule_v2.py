from __future__ import annotations

import hashlib

import pytest

from implbench.harness.schedule import (
    ScheduleError,
    expand_schedule,
    fisher_yates_task_order,
    random_word,
    validate_seed,
)


def test_seed_and_fisher_yates_vector() -> None:
    seed = "00" * 32
    assert validate_seed(seed) == bytes(32)
    assert fisher_yates_task_order(
        ["z", "a", "é"], seed
    ) == fisher_yates_task_order(["é", "z", "a"], seed)
    expected = hashlib.sha256(
        b"implbench-schedule-v1\0" + bytes(32) + (0).to_bytes(8, "big")
    ).digest()
    assert random_word(seed, 0) == int.from_bytes(expected[:8], "big")


@pytest.mark.parametrize("seed", ["0" * 63, "0" * 65, "A" * 64, "g" * 64, "00" * 31])
def test_seed_is_exact_lower_hex_32_bytes(seed: str) -> None:
    with pytest.raises(ScheduleError):
        validate_seed(seed)


def test_schedule_has_exact_nesting_and_unique_cells() -> None:
    task_rows = [(chr(ord("a") + index), f"sha-{index}") for index in range(8)]
    cells = expand_schedule("00" * 32, task_rows)
    assert len(cells) == 4 * 2 * 8 * 2
    assert [cell.schedule_index for cell in cells] == list(range(len(cells)))
    assert len({cell.cell_id for cell in cells}) == len(cells)
    assert [(cells[0].repetition, cells[0].pair), (cells[16].repetition, cells[16].pair)] == [(1, "GLM"), (1, "Kimi")]
    assert all(cell.task_index >= 0 for cell in cells)
    assert all(cell.arm in {"glm-pi", "glm-zcode", "kimi-pi", "kimi-cli"} for cell in cells)


def test_schedule_rejection_sampler_counter_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter([2**64 - 1, 0, 0])
    monkeypatch.setattr("implbench.harness.schedule.random_word", lambda seed, counter: next(values))
    assert fisher_yates_task_order(["a", "b", "c"], "00" * 32) == ["b", "c", "a"]
