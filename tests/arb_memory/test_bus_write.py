import json
from contextlib import nullcontext

import pytest

pytest.importorskip("redis")
pytest.importorskip("psycopg")

from arb_memory.bus import handle_write_intent, memory_write


class _IntentResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _IntentContractConn:
    def __init__(self):
        self.inserted = False
        self.receipt = None

    def transaction(self):
        return nullcontext()

    def execute(self, query, params):
        if query.startswith("INSERT INTO idempotency_keys"):
            if self.inserted:
                return _IntentResult(None)
            self.inserted = True
            return _IntentResult((params[0],))
        if query.startswith("UPDATE idempotency_keys"):
            self.receipt = params[0].obj
            return _IntentResult(None)
        if query.startswith("SELECT receipt"):
            return _IntentResult((self.receipt,))
        raise AssertionError(query)


def test_handle_write_intent_returns_receipt_and_replay_without_database(monkeypatch):
    import arb_memory.store as store

    receipt = {
        "artefact_outcome": "none",
        "artefact_id": None,
        "version": None,
        "hints_stored": 0,
    }
    monkeypatch.setattr(store, "write_artefact_and_hints", lambda *args, **kwargs: receipt)
    conn = _IntentContractConn()
    intent = {"ulid": "U1", "kind": "hints", "artefact": None, "hints": []}

    assert handle_write_intent(conn, intent, embed=lambda text: []) == (receipt, False)
    assert handle_write_intent(conn, intent, embed=lambda text: []) == (receipt, True)


def test_memory_write_xadds_json_payload(redis_bus):
    ulid = memory_write(
        redis_bus,
        artefact={"artefact_id": "d.md", "content": "c"},
        hints=[{"text": "t"}],
        source="seat",
        author="agy",
        prefix=redis_bus.prefix,
    )

    rows = redis_bus.xrange(f"{redis_bus.prefix}arbmem:writes")
    assert len(rows) == 1
    _, fields = rows[0]
    payload = json.loads(fields["payload"])
    assert fields["ulid"] == ulid
    assert payload == {
        "ulid": ulid,
        "kind": "artefact+hints",
        "artefact": {"artefact_id": "d.md", "content": "c"},
        "hints": [{"text": "t", "source": "seat", "author": "agy"}],
    }


def test_write_intent_is_idempotent(scratch, fake_embed):
    intent = {
        "ulid": "U1",
        "kind": "artefact+hints",
        "artefact": {"artefact_id": "d.md", "content": "c"},
        "hints": [{"text": "t"}],
    }

    receipt, is_replay = handle_write_intent(scratch, intent, embed=fake_embed)
    assert receipt == {
        "artefact_outcome": "stored",
        "artefact_id": "d.md",
        "version": 1,
        "hints_stored": 1,
    }
    assert is_replay is False

    replay_receipt, is_replay = handle_write_intent(scratch, intent, embed=fake_embed)
    assert replay_receipt == receipt
    assert is_replay is True
    assert scratch.execute("SELECT count(*) FROM artefacts WHERE artefact_id='d.md'").fetchone()[0] == 1


def test_replay_returns_its_original_receipt_after_a_newer_version_is_written(scratch, fake_embed):
    first = {
        "ulid": "U-original",
        "kind": "artefact",
        "artefact": {"artefact_id": "d.md", "content": "first"},
        "hints": [],
    }
    newer = {
        "ulid": "U-newer",
        "kind": "artefact",
        "artefact": {"artefact_id": "d.md", "content": "newer"},
        "hints": [],
    }

    original_receipt, _ = handle_write_intent(scratch, first, embed=fake_embed)
    newer_receipt, _ = handle_write_intent(scratch, newer, embed=fake_embed)
    replay_receipt, is_replay = handle_write_intent(scratch, first, embed=fake_embed)

    assert original_receipt["version"] == 1
    assert newer_receipt["version"] == 2
    assert (replay_receipt, is_replay) == (original_receipt, True)


def test_legacy_idempotency_row_returns_unknown_receipt(scratch, fake_embed):
    scratch.execute("INSERT INTO idempotency_keys (key) VALUES ('U-legacy')")

    receipt, is_replay = handle_write_intent(
        scratch,
        {"ulid": "U-legacy", "kind": "hints", "artefact": None, "hints": []},
        embed=fake_embed,
    )

    assert (receipt, is_replay) == ({"artefact_outcome": "unknown"}, True)


def test_receipt_counts_new_hints_when_artefact_is_deduped(scratch, fake_embed):
    first = {
        "ulid": "U-dedup-1",
        "kind": "artefact+hints",
        "artefact": {"artefact_id": "d.md", "content": "c"},
        "hints": [{"text": "first"}],
    }
    second = {
        "ulid": "U-dedup-2",
        "kind": "artefact+hints",
        "artefact": {"artefact_id": "d.md", "content": "c"},
        "hints": [{"text": "second"}],
    }

    handle_write_intent(scratch, first, embed=fake_embed)
    receipt, is_replay = handle_write_intent(scratch, second, embed=fake_embed)

    assert receipt == {
        "artefact_outcome": "deduped",
        "artefact_id": "d.md",
        "version": 1,
        "hints_stored": 1,
    }
    assert is_replay is False


def test_idempotency_and_write_are_atomic(scratch, fake_embed, monkeypatch):
    import arb_memory.store as store

    monkeypatch.setattr(
        store,
        "write_artefact_and_hints",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        handle_write_intent(
            scratch,
            {"ulid": "U2", "kind": "artefact", "artefact": {"artefact_id": "d.md", "content": "c"}},
            embed=fake_embed,
        )
    assert scratch.execute("SELECT count(*) FROM idempotency_keys WHERE key='U2'").fetchone()[0] == 0
    assert scratch.execute("SELECT count(*) FROM artefacts WHERE artefact_id='d.md'").fetchone()[0] == 0
