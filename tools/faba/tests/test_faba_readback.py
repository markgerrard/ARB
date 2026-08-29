import json
import sys
from pathlib import Path

import pytest
import redis

FABA = Path(__file__).resolve().parent.parent
for path in (str(FABA), str(FABA.parents[1] / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from faba_launch import publish_artefact_and_gate


PRIOR = (
    "# Design — frobnicator\n\n**Change summary:** first draft.\n\n"
    + "prior body paragraph with enough substance to clear the stub floor. " * 8
    + "\n"
)
CURRENT = PRIOR.replace("first draft", "folded revision").replace("prior body", "revised body", 1)


class DriverRedis:
    def __init__(self, *, fetch_raw=None, xadd_error=None, blpop_error=None):
        self.fetch_raw = fetch_raw
        self.xadd_error = xadd_error
        self.blpop_error = blpop_error
        self.xadds = []
        self.deleted = []

    def xadd(self, stream, fields, maxlen=None, approximate=None):
        if self.xadd_error:
            raise self.xadd_error
        self.xadds.append((stream, fields))
        return "1-0"

    def blpop(self, key, timeout):
        if self.blpop_error:
            raise self.blpop_error
        if self.fetch_raw is None:
            return None
        return key, self.fetch_raw

    def delete(self, key):
        self.deleted.append(key)

    def lrange(self, key, start, end):
        published = json.loads(self.xadds[-1][1]["payload"])["artefact"]
        return [
            json.dumps(
                {
                    "artefact_outcome": "stored",
                    "artefact_id": published["artefact_id"],
                    "version": 2,
                }
            )
        ]


def _workspace(tmp_path, *, revision):
    (tmp_path / "artefact.md").write_text(CURRENT if revision else PRIOR, encoding="utf-8")
    if revision:
        (tmp_path / "prior-record.md").write_text(PRIOR, encoding="utf-8")
    return tmp_path


def _run(monkeypatch, tmp_path, outcome, *, revision):
    client = DriverRedis()
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: client)
    calls = []

    def fetch_by_id(seen_client, artefact_id):
        calls.append((seen_client, artefact_id))
        return outcome

    result = publish_artefact_and_gate(
        "redis://memory/0",
        workspace=_workspace(tmp_path, revision=revision),
        artefact_id="art-faba-au-readback",
        request_id="req-readback",
        author="faba-au-test",
        receipt_timeout=0,
        revision=revision,
        fetch_by_id=fetch_by_id,
    )
    return result, client, calls


@pytest.mark.parametrize(
    ("outcome", "failure_class"),
    [
        (None, "timeout"),
        ({}, "malformed"),
        ({"outcome": "malformed"}, "malformed"),
        ({"outcome": "infra_exhausted"}, "infra_exhausted"),
        ({"outcome": "request_unsent"}, "request_unsent"),
        ({"outcome": "result_unreadable"}, "result_unreadable"),
        ({"outcome": "binary_unsupported"}, "binary_unsupported"),
    ],
)
def test_unverifiable_fetch_classes_refuse_without_publish(
    monkeypatch, tmp_path, outcome, failure_class
):
    (passed, reason, receipt, check), client, calls = _run(
        monkeypatch, tmp_path, outcome, revision=False
    )

    assert not passed and failure_class in reason
    assert receipt is None and check.ok
    assert calls == [(client, "art-faba-au-readback")]
    assert client.xadds == []


def test_not_found_revision_refuses(monkeypatch, tmp_path):
    result, client, _ = _run(
        monkeypatch, tmp_path, {"outcome": "not_found"}, revision=True
    )
    assert not result[0] and "not_found" in result[1]
    assert client.xadds == []


def test_not_found_fresh_is_only_publish_on_miss_branch(monkeypatch, tmp_path):
    result, client, _ = _run(
        monkeypatch, tmp_path, {"outcome": "not_found"}, revision=False
    )
    assert result[0]
    assert len(client.xadds) == 1
    payload = json.loads(client.xadds[0][1]["payload"])
    assert payload["artefact"]["content"] == PRIOR


def test_existing_id_fresh_refuses_blind_overwrite(monkeypatch, tmp_path):
    result, client, _ = _run(
        monkeypatch,
        tmp_path,
        {"outcome": "ok", "version": 1, "content": PRIOR},
        revision=False,
    )
    assert not result[0] and "already exists" in result[1]
    assert result.phase == "not_enqueued"
    assert result.refusal_cause == "fresh_id_already_exists"
    assert client.xadds == []


def test_publish_write_raise_is_not_enqueued(monkeypatch, tmp_path):
    from arb_memory import bus

    client = DriverRedis()
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: client)
    monkeypatch.setattr(
        bus, "memory_write", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write down"))
    )

    result = publish_artefact_and_gate(
        "redis://memory/0",
        workspace=_workspace(tmp_path, revision=False),
        artefact_id="art-faba-au-write-fail",
        request_id="req-write-fail",
        author="faba-au-test",
        receipt_timeout=0,
        fetch_by_id=lambda *args, **kwargs: {"outcome": "not_found"},
    )

    assert not result.passed and result.phase == "not_enqueued"
    assert "publish refused before enqueue" in result.reason


def test_stale_prior_refuses_with_version_and_both_body_hashes(monkeypatch, tmp_path):
    store_body = PRIOR + "out-of-band v2\n"
    result, client, _ = _run(
        monkeypatch,
        tmp_path,
        {"outcome": "ok", "version": 2, "content": store_body},
        revision=True,
    )
    assert not result[0]
    assert "store_version=2" in result[1]
    assert "store_sha256=" in result[1] and "prior_sha256=" in result[1]
    assert client.xadds == []


def test_matching_revision_publishes_real_payload(monkeypatch, tmp_path):
    result, client, calls = _run(
        monkeypatch,
        tmp_path,
        {"outcome": "ok", "version": 1, "content": PRIOR},
        revision=True,
    )
    assert result[0]
    assert calls == [(client, "art-faba-au-readback")]
    assert len(client.xadds) == 1
    payload = json.loads(client.xadds[0][1]["payload"])
    assert payload["artefact"] == {
        "artefact_id": "art-faba-au-readback",
        "content": CURRENT,
        "mime": "text/markdown",
        "source": "faba-author",
        "author": "faba-au-test",
    }


def test_crlf_revision_prior_matches_raw_store_content(monkeypatch, tmp_path):
    prior = PRIOR.replace("\n", "\r\n")
    current = CURRENT.replace("\n", "\r\n")
    (tmp_path / "prior-record.md").write_bytes(prior.encode("utf-8"))
    (tmp_path / "artefact.md").write_bytes(current.encode("utf-8"))
    client = DriverRedis()
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: client)

    passed, reason, _, _ = publish_artefact_and_gate(
        "redis://memory/0",
        workspace=tmp_path,
        artefact_id="art-faba-au-crlf",
        request_id="req-crlf",
        author="faba-au-test",
        receipt_timeout=0,
        revision=True,
        fetch_by_id=lambda seen_client, artefact_id: {
            "outcome": "ok",
            "artefact_id": artefact_id,
            "version": 1,
            "content": prior,
        },
    )

    assert passed, reason
    assert len(client.xadds) == 1
    payload = json.loads(client.xadds[0][1]["payload"])
    assert payload["artefact"]["content"].encode("utf-8") == current.encode("utf-8")


def test_byte_identical_crlf_revision_refuses_at_publish_fold_check(monkeypatch, tmp_path):
    prior = PRIOR.replace("\n", "\r\n")
    (tmp_path / "prior-record.md").write_bytes(prior.encode("utf-8"))
    (tmp_path / "artefact.md").write_bytes(prior.encode("utf-8"))
    client = DriverRedis()
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: client)

    passed, reason, receipt, check = publish_artefact_and_gate(
        "redis://memory/0",
        workspace=tmp_path,
        artefact_id="art-faba-au-crlf-noop",
        request_id="req-crlf-noop",
        author="faba-au-test",
        receipt_timeout=0,
        revision=True,
        fetch_by_id=lambda seen_client, artefact_id: {
            "outcome": "ok",
            "artefact_id": artefact_id,
            "version": 1,
            "content": prior,
        },
    )

    assert not passed
    assert "revision-fold check" in reason
    assert "byte-identical" in reason
    assert receipt is None and not check.ok
    assert client.xadds == []


@pytest.mark.parametrize(
    ("client", "failure_class"),
    [
        # The two transport legs are deliberately DISTINCT: a failed XADD never asked the
        # store anything, a failed BLPOP asked and could not hear back. Neither is
        # infra_exhausted, which is the store's own statement that it tried and gave up.
        (DriverRedis(xadd_error=redis.ConnectionError("xadd down")), "request_unsent"),
        (DriverRedis(blpop_error=redis.ConnectionError("blpop down")), "result_unreadable"),
        (DriverRedis(fetch_raw="not-json"), "malformed"),
    ],
)
def test_real_fetch_transport_and_decode_failures_refuse_at_driver(
    monkeypatch, tmp_path, client, failure_class
):
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: client)

    passed, reason, receipt, check = publish_artefact_and_gate(
        "redis://memory/0",
        workspace=_workspace(tmp_path, revision=False),
        artefact_id="art-faba-au-transport",
        request_id="req-transport",
        author="faba-au-test",
        receipt_timeout=0,
    )

    assert not passed and failure_class in reason
    assert receipt is None and check.ok
    assert client.deleted == []
