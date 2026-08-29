"""The immutable manifest-v2 schedule.

This module deliberately has no dependency on the manifest builder.  A controller can
re-expand the order from the seed and task pins and compare the canonical bytes before
dispatching any cell.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .cell_runtime import cell_id_for


class ScheduleError(ValueError):
    """Raised when a frozen schedule input is invalid."""


_SEED_RE = re.compile(r"^[0-9a-f]{64}$")
_PAIRS = ("GLM", "Kimi")
_PAIR_ARMS = {
    "GLM": ("glm-pi", "glm-zcode"),
    "Kimi": ("kimi-pi", "kimi-cli"),
}
_TAG = b"implbench-schedule-v1\x00"


@dataclass(frozen=True)
class ScheduleCell:
    schedule_index: int
    repetition: int
    pair: str
    arm: str
    task_id: str
    task_index: int
    fixture_sha: str
    cell_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schedule_index": self.schedule_index,
            "repetition": self.repetition,
            "pair": self.pair,
            "arm": self.arm,
            "task_id": self.task_id,
            "task_index": self.task_index,
            "fixture_sha": self.fixture_sha,
            "cell_id": self.cell_id,
        }


def validate_seed(seed: str) -> bytes:
    if not isinstance(seed, str) or not _SEED_RE.fullmatch(seed):
        raise ScheduleError("seed must be exactly 64 lowercase hexadecimal characters")
    return bytes.fromhex(seed)


def random_word(seed: str | bytes, counter: int) -> int:
    seed_bytes = validate_seed(seed) if isinstance(seed, str) else seed
    if len(seed_bytes) != 32:
        raise ScheduleError("seed must contain exactly 32 bytes")
    if not isinstance(counter, int) or counter < 0 or counter >= 2**64:
        raise ScheduleError("counter must be an unsigned 64-bit integer")
    digest = hashlib.sha256(_TAG + seed_bytes + counter.to_bytes(8, "big")).digest()
    return int.from_bytes(digest[:8], "big")


def fisher_yates_task_order(task_ids: Iterable[str], seed: str) -> list[str]:
    validate_seed(seed)
    values = list(task_ids)
    if not all(isinstance(task, str) and task for task in values):
        raise ScheduleError("task IDs must be unique non-empty strings")
    order = sorted(values, key=lambda value: value.encode("utf-8"))
    if len(set(order)) != len(order):
        raise ScheduleError("task IDs must be unique non-empty strings")
    counter = 0
    for i in range(len(order) - 1, 0, -1):
        bound = (2**64 // (i + 1)) * (i + 1)
        while True:
            word = random_word(seed, counter)
            counter += 1
            if word < bound:
                break
        j = word % (i + 1)
        order[i], order[j] = order[j], order[i]
    return order


def cell_suffix(cell_id: str) -> str:
    """Return the fixed lowercase base32 suffix used in seat identities."""
    digest = hashlib.sha256(cell_id.encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def expand_schedule(seed: str, tasks: Sequence[tuple[str, str]] | Sequence[dict[str, str]]) -> tuple[ScheduleCell, ...]:
    """Expand the frozen 4×2×8×2 nesting into immutable cells."""
    validate_seed(seed)
    normalized: list[tuple[str, str]] = []
    for task in tasks:
        if isinstance(task, dict):
            task_id = task.get("task_id")
            fixture_sha = task.get("fixture_sha")
        else:
            task_id, fixture_sha = task
        if not isinstance(task_id, str) or not isinstance(fixture_sha, str):
            raise ScheduleError("tasks must contain task_id and fixture_sha strings")
        normalized.append((task_id, fixture_sha))
    if len(normalized) != 8:
        raise ScheduleError("manifest-v2 schedule requires exactly eight tasks")
    task_ids = [task_id for task_id, _ in normalized]
    seeded_ids = fisher_yates_task_order(task_ids, seed)
    fixture_by_id = dict(normalized)
    original_index = {task_id: index for index, task_id in enumerate(seeded_ids)}

    cells: list[ScheduleCell] = []
    index = 0
    for repetition in range(1, 5):
        pairs = _PAIRS if repetition % 2 else tuple(reversed(_PAIRS))
        ordered_ids = seeded_ids if repetition % 2 else list(reversed(seeded_ids))
        for pair in pairs:
            pi_arm, oi_arm = _PAIR_ARMS[pair]
            for task_id in ordered_ids:
                task_index = original_index[task_id]
                arms = (pi_arm, oi_arm) if (repetition + task_index) % 2 == 0 else (oi_arm, pi_arm)
                for arm in arms:
                    fixture_sha = fixture_by_id[task_id]
                    cells.append(
                        ScheduleCell(
                            schedule_index=index,
                            repetition=repetition,
                            pair=pair,
                            arm=arm,
                            task_id=task_id,
                            task_index=task_index,
                            fixture_sha=fixture_sha,
                            cell_id=cell_id_for(pair, arm, task_id, repetition, index),
                        )
                    )
                    index += 1
    if len({cell.cell_id for cell in cells}) != 128:
        raise ScheduleError("schedule cell IDs must be unique")
    return tuple(cells)


def schedule_dicts(seed: str, tasks: Sequence[tuple[str, str]] | Sequence[dict[str, str]]) -> list[dict[str, object]]:
    return [cell.as_dict() for cell in expand_schedule(seed, tasks)]
