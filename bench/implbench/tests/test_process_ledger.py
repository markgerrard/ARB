from __future__ import annotations

import signal

import pytest

from implbench.harness.cell_runtime import ProcessLedger, ProcessLifecycleError, ProcessRecord


class FakeProcessTable:
    def __init__(self) -> None:
        self.by_uid = {41001: {11, 12}, 41002: {21}, 41003: {31, 32}}
        self.signals: list[tuple[int, int]] = []

    def census_uid(self, uid: int) -> set[int]:
        return set(self.by_uid.get(uid, set()))

    def signal(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        if sig == signal.SIGKILL:
            for pids in self.by_uid.values():
                pids.discard(pid)


def test_ledger_tracks_double_fork_setsid_descendants_and_independent_uids() -> None:
    table = FakeProcessTable()
    ledger = ProcessLedger(table)
    ledger.register(ProcessRecord(pid=11, uid=41001, pgid=11, session_id=11, role="control"))
    ledger.register(ProcessRecord(pid=21, uid=41002, pgid=21, session_id=21, role="tool"))
    ledger.register(ProcessRecord(pid=31, uid=41003, pgid=31, session_id=31, role="git"))

    ledger.close((41001, 41002, 41003), grace_s=0)

    assert all(sig == signal.SIGTERM for _, sig in table.signals[:3])
    assert any(sig == signal.SIGKILL for _, sig in table.signals)
    assert ledger.absence_proof == {41001: True, 41002: True, 41003: True}


def test_ledger_fails_if_independent_uid_census_retains_a_descendant() -> None:
    table = FakeProcessTable()
    table.by_uid[41001] = {11, 99}
    original_signal = table.signal

    def stubborn_signal(pid: int, sig: int) -> None:
        if pid != 99:
            original_signal(pid, sig)
        else:
            table.signals.append((pid, sig))

    table.signal = stubborn_signal
    ledger = ProcessLedger(table)
    ledger.register(ProcessRecord(pid=11, uid=41001, pgid=11, session_id=11, role="control"))

    with pytest.raises(ProcessLifecycleError, match="process census not empty"):
        ledger.close((41001,), grace_s=0)
