"""Unit tests for faba_schema (the content half of the FABA gate) and for
publish_and_gate (the harness-publish flow that closes PF1/PF2)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
for p in (str(FABA), str(FABA.parents[1] / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from faba_schema import (  # noqa: E402
    basis_ref,
    must_carry_ids,
    open_finding_ids,
    parse_findings,
    reopened_finding_ids,
    validate_decision_record,
)

GOOD = """# FABA decision record — round 3
Subject: art-x | Prior record: art-prior | Status: ok

**What the subject IS:** art-x is the toy subject artefact these tests bind to

## Round task
the task

## Findings
| id | severity | status | evidence (command, exit code, ref) | reopen-if |
|----|----------|--------|-------------------------------------|-----------|
| F1 | info | closed | pytest, exit 0, abc123 | * |
| PF2 | P1 | open (confirmed) | grep, exit 0, bus.py:18 |  |

## Recommendation
fix PF2

## Open items
PF2 carries
"""

# A pre-reopen-column record: closed finding, 4-column table. Kept to prove the
# parser stays tolerant (open_finding_ids over an old prior record must not
# regress — panel-faba-v2-r2 cold-opus nit 2). NEW records must carry reopen-if.
LEGACY_4COL = """# FABA decision record — round 2
Subject: art-x | Prior record: none | Status: ok

**What the subject IS:** the toy subject

## Round task
the task

## Findings
| id | severity | status | evidence (command, exit code, ref) |
|----|----------|--------|-------------------------------------|
| F1 | info | closed | pytest, exit 0, abc123 |
| PF2 | P1 | open (confirmed) | grep, exit 0, bus.py:18 |

## Recommendation
fix PF2

## Open items
PF2 carries
"""


class TestValidate:
    def test_good_record_passes(self):
        check = validate_decision_record(GOOD)
        assert check.ok and check.status == "ok" and check.subject == "art-x"

    def test_failed_status_is_schema_valid(self):
        check = validate_decision_record(GOOD.replace("Status: ok", "Status: failed"))
        assert check.ok and check.status == "failed"

    def test_missing_header_fails(self):
        check = validate_decision_record(GOOD.replace("# FABA decision record — round 3", "# notes"))
        assert not check.ok and any("header" in p for p in check.problems)

    def test_missing_recommendation_fails(self):
        check = validate_decision_record(GOOD.split("## Recommendation")[0])
        assert not check.ok

    def test_garbage_fails_with_multiple_problems(self):
        check = validate_decision_record("hello world")
        assert not check.ok and len(check.problems) >= 3

    def test_empty_findings_table_fails(self):
        gutted = GOOD.replace("| F1 | info | closed | pytest, exit 0, abc123 | * |\n", "").replace(
            "| PF2 | P1 | open (confirmed) | grep, exit 0, bus.py:18 |  |\n", ""
        )
        check = validate_decision_record(gutted)
        assert not check.ok and any("no parseable finding rows" in p for p in check.problems)

    def test_missing_subject_summary_fails(self):
        """F10: the 'What the subject IS' line is schema, not convention — a
        record naming its subject by id alone strands a zero-context successor."""
        stripped = "\n".join(
            line for line in GOOD.splitlines() if not line.startswith("**What the subject IS")
        )
        check = validate_decision_record(stripped)
        assert not check.ok and any("What the subject IS" in p for p in check.problems)

    def test_parenthetical_subject_summary_passes(self):
        varied = GOOD.replace(
            "**What the subject IS:**",
            "**What the subject IS (for a zero-context successor):**",
        )
        assert validate_decision_record(varied).ok

    def test_prior_open_coverage_enforced(self):
        check = validate_decision_record(GOOD, prior_open_ids=["PF2", "PF7"])
        assert not check.ok
        assert any("PF7" in p for p in check.problems)
        assert not any("'PF2'" in p for p in check.problems)


class TestReopenPredicate:
    """The reopen predicate is a per-finding SCHEMA field, not instructional
    prose (ADR art-81438f2f5a5c4955 § decision-record schema): every CLOSED
    finding must state what tree change reopens it. Owner decision 2026-07-18:
    conservative-broad default ('*' = the subject subtree, the r28 precedent),
    narrowable to a pathspec only with evidence the finding is isolated."""

    def test_closed_finding_without_reopen_scope_fails(self):
        holed = GOOD.replace(
            "| F1 | info | closed | pytest, exit 0, abc123 | * |",
            "| F1 | info | closed | pytest, exit 0, abc123 |  |",
        )
        check = validate_decision_record(holed)
        assert not check.ok and any("reopen" in p for p in check.problems)

    def test_closed_finding_with_broad_default_passes(self):
        assert validate_decision_record(GOOD).ok  # F1 closes with the '*' subtree default

    def test_closed_finding_with_narrowed_pathspec_passes(self):
        narrowed = GOOD.replace(
            "| F1 | info | closed | pytest, exit 0, abc123 | * |",
            "| F1 | info | closed | pytest, exit 0, abc123 | tools/faba/faba_schema.py |",
        )
        assert validate_decision_record(narrowed).ok

    def test_open_finding_needs_no_reopen_scope(self):
        # PF2 is open with an empty reopen cell — an open finding cannot be
        # reopened, so it must not be flagged.
        check = validate_decision_record(GOOD)
        assert not any("PF2" in p and "reopen" in p for p in check.problems)

    def test_reopen_scope_is_parsed(self):
        rows = {r["id"]: r for r in parse_findings(GOOD)}
        assert rows["F1"]["reopen"] == "*"
        assert rows["PF2"]["reopen"] == ""

    def test_legacy_4col_record_still_yields_open_ids(self):
        # Parser tolerance: an old 4-column prior record still surfaces its open
        # ids for the coverage check, even though it predates the reopen column.
        assert open_finding_ids(LEGACY_4COL) == ["PF2"]

    def test_contract_schema_documents_reopen_column(self):
        # Drift guard: the human-readable schema (round-contract.md) and the
        # machine validator must agree the column exists. If the validator
        # enforces reopen-if but the contract stops documenting it, this fails.
        contract = (FABA / "round-contract.md").read_text(encoding="utf-8")
        assert "reopen-if" in contract


class TestReopenConsumer:
    """The consumer side (ADR art-81438f2f5a5c4955 open item #12): given a prior
    record and the paths that changed since it, reopen the CLOSED findings whose
    reopen-if scope matches the delta. Pure — the caller supplies changed_paths
    (git diff belongs in the launch/driver layer, not here)."""

    def test_broad_default_reopens_on_any_change(self):
        assert reopened_finding_ids(GOOD, ["some/unrelated/file.py"]) == ["F1"]

    def test_no_change_reopens_nothing(self):
        assert reopened_finding_ids(GOOD, []) == []

    def test_open_findings_are_never_reopened(self):
        # PF2 is open; it is carried by open_finding_ids, never surfaced here.
        assert "PF2" not in reopened_finding_ids(GOOD, ["bus.py"])

    def test_narrowed_pathspec_reopens_only_on_matching_change(self):
        narrowed = GOOD.replace(
            "| F1 | info | closed | pytest, exit 0, abc123 | * |",
            "| F1 | info | closed | pytest, exit 0, abc123 | tools/faba/faba_schema.py |",
        )
        assert reopened_finding_ids(narrowed, ["tools/faba/faba_schema.py"]) == ["F1"]
        assert reopened_finding_ids(narrowed, ["tools/faba/other.py"]) == []

    def test_directory_scope_matches_paths_beneath_it(self):
        scoped = GOOD.replace(
            "| F1 | info | closed | pytest, exit 0, abc123 | * |",
            "| F1 | info | closed | pytest, exit 0, abc123 | tools/faba/ |",
        )
        assert reopened_finding_ids(scoped, ["tools/faba/subagent/x.py"]) == ["F1"]
        assert reopened_finding_ids(scoped, ["tools/other/x.py"]) == []

    def test_glob_scope_matches(self):
        globbed = GOOD.replace(
            "| F1 | info | closed | pytest, exit 0, abc123 | * |",
            "| F1 | info | closed | pytest, exit 0, abc123 | tools/faba/*.py |",
        )
        assert reopened_finding_ids(globbed, ["tools/faba/faba_schema.py"]) == ["F1"]

    def test_multiple_scope_tokens_any_match(self):
        multi = GOOD.replace(
            "| F1 | info | closed | pytest, exit 0, abc123 | * |",
            "| F1 | info | closed | pytest, exit 0, abc123 | src/a.py, tools/faba/b.py |",
        )
        assert reopened_finding_ids(multi, ["tools/faba/b.py"]) == ["F1"]
        assert reopened_finding_ids(multi, ["src/a.py"]) == ["F1"]
        assert reopened_finding_ids(multi, ["docs/c.md"]) == []

    def test_must_carry_is_open_set_when_nothing_changed(self):
        # Backward-compatible: no delta -> exactly the open findings.
        assert must_carry_ids(GOOD, []) == open_finding_ids(GOOD)

    def test_must_carry_unions_open_and_reopened(self):
        # F1 (closed, reopen '*') reopens on any change and joins open PF2.
        assert must_carry_ids(GOOD, ["anything.py"]) == ["F1", "PF2"]


class TestBasisRef:
    """Auto-basis (ADR art-81438f2f5a5c4955 open item #13): a record may record
    the commit it verified against, so a successor's reopen consumer can default
    its --prior-basis to it. Optional — absent or 'none' means no recorded basis
    (older records stay valid)."""

    def test_basis_ref_parsed(self):
        r = GOOD.replace(
            "Subject: art-x | Prior record: art-prior | Status: ok",
            "Subject: art-x | Prior record: art-prior | Status: ok\nBasis: abc123",
        )
        assert basis_ref(r) == "abc123"
        assert validate_decision_record(r).ok  # additive: a basis line keeps a record valid

    def test_basis_none_is_none(self):
        r = GOOD.replace(
            "Subject: art-x | Prior record: art-prior | Status: ok",
            "Subject: art-x | Prior record: art-prior | Status: ok\nBasis: none",
        )
        assert basis_ref(r) is None

    def test_absent_basis_is_none(self):
        assert basis_ref(GOOD) is None


class TestParse:
    def test_parse_findings_skips_separator_rows(self):
        rows = parse_findings(GOOD)
        assert [r["id"] for r in rows] == ["F1", "PF2"]

    def test_open_finding_ids(self):
        assert open_finding_ids(GOOD) == ["PF2"]


class TestPublishAndGate:
    def _setup(self, tmp_path, record=GOOD):
        ws = tmp_path / "ws"
        ws.mkdir()
        if record is not None:
            (ws / "decision-record.md").write_text(record, encoding="utf-8")
        return ws

    def test_missing_record_fails_without_publishing(self, tmp_path, monkeypatch):
        from faba_launch import publish_and_gate

        called = []
        monkeypatch.setattr("redis.from_url", lambda *a, **k: called.append("client") or None)
        result = publish_and_gate(
            "redis://x",
            workspace=self._setup(tmp_path, record=None),
            record_artefact_id="art-1",
            request_id="rq-1",
            author="t",
            prior_open_ids=[],
            receipt_timeout=1,
        )
        passed, reason, receipt, check = result
        assert not passed and "no decision-record.md" in reason
        assert result.phase == "not_enqueued"
        assert called == []  # never touched the bus

    def test_invalid_record_fails_without_publishing(self, tmp_path, monkeypatch):
        from faba_launch import publish_and_gate

        called = []
        monkeypatch.setattr("redis.from_url", lambda *a, **k: called.append("client") or None)
        result = publish_and_gate(
            "redis://x",
            workspace=self._setup(tmp_path, record="garbage"),
            record_artefact_id="art-1",
            request_id="rq-1",
            author="t",
            prior_open_ids=[],
            receipt_timeout=1,
        )
        passed, reason, receipt, check = result
        assert not passed and "schema/binding/coverage" in reason
        assert check is not None and not check.ok
        assert called == []

    def test_valid_record_publishes_as_harness_and_gates_on_receipt(self, tmp_path, monkeypatch):
        import faba_record
        from arb_memory import bus
        from faba_launch import publish_and_gate

        events = []

        class StubClient:
            def delete(self, key):
                events.append(("delete", key))

        monkeypatch.setattr("redis.from_url", lambda *a, **k: StubClient())
        monkeypatch.setattr(
            bus, "memory_write", lambda client, **kw: events.append(("write", kw["request_id"])) or "01ULID"
        )
        monkeypatch.setattr(
            faba_record,
            "poll_receipt",
            lambda rid, timeout, client=None: {"artefact_outcome": "stored", "artefact_id": "art-1", "version": 1},
        )
        result = publish_and_gate(
            "redis://x",
            workspace=self._setup(tmp_path),
            record_artefact_id="art-1",
            request_id="rq-1",
            author="t",
            prior_open_ids=["PF2"],
            receipt_timeout=1,
        )
        passed, reason, receipt, check = result
        assert passed and "v1 stored" in reason
        assert result.phase == "receipt_confirmed"
        # clean-slate DEL happens BEFORE the write, on the deterministic key
        assert events[0] == ("delete", bus.write_result_key("rq-1"))
        assert events[1] == ("write", "rq-1")

    def test_write_raise_is_not_enqueued(self, tmp_path, monkeypatch):
        from arb_memory import bus
        from faba_launch import publish_and_gate

        class StubClient:
            def delete(self, key):
                return 1

        monkeypatch.setattr("redis.from_url", lambda *a, **k: StubClient())
        monkeypatch.setattr(
            bus,
            "memory_write",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write down")),
        )
        result = publish_and_gate(
            "redis://x",
            workspace=self._setup(tmp_path),
            record_artefact_id="art-1",
            request_id="rq-1",
            author="t",
            prior_open_ids=["PF2"],
            receipt_timeout=1,
        )

        assert not result.passed and result.phase == "not_enqueued"
        assert "publish refused before enqueue" in result.reason


class TestBindingAndStatus:
    def test_binding_match_passes(self):
        check = validate_decision_record(GOOD, expected_round=3, expected_subject="art-x")
        assert check.ok and check.round_number == 3

    def test_wrong_round_fails(self):
        check = validate_decision_record(GOOD, expected_round=9, expected_subject="art-x")
        assert not check.ok and any("round" in p for p in check.problems)

    def test_wrong_subject_fails(self):
        check = validate_decision_record(GOOD, expected_round=3, expected_subject="art-elsewhere")
        assert not check.ok and any("subject" in p for p in check.problems)

    def test_empty_evidence_cell_fails(self):
        holed = GOOD.replace(
            "| F1 | info | closed | pytest, exit 0, abc123 | * |", "| F1 | info | closed |  | * |"
        )
        check = validate_decision_record(holed)
        assert not check.ok and any("empty evidence" in p for p in check.problems)

    def test_failed_status_record_publishes_but_round_fails(self, tmp_path, monkeypatch):
        """panel-faba-v2 codex F1: a Status: failed record is published for the
        audit trail yet the round outcome is FAILURE, never exit 0."""
        import faba_record
        from arb_memory import bus
        from faba_launch import publish_and_gate

        events = []

        class StubClient:
            def delete(self, key):
                events.append("delete")

        monkeypatch.setattr("redis.from_url", lambda *a, **k: StubClient())
        monkeypatch.setattr(bus, "memory_write", lambda client, **kw: events.append("write") or "01ULID")
        monkeypatch.setattr(
            faba_record,
            "poll_receipt",
            lambda rid, timeout, client=None: {"artefact_outcome": "stored", "artefact_id": "art-1", "version": 1},
        )
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "decision-record.md").write_text(GOOD.replace("Status: ok", "Status: failed"), encoding="utf-8")
        passed, reason, receipt, check = publish_and_gate(
            "redis://x",
            workspace=ws,
            record_artefact_id="art-1",
            request_id="rq-1",
            author="t",
            prior_open_ids=[],
            receipt_timeout=1,
        )
        assert "write" in events  # published for audit
        assert not passed and "Status: failed" in reason


class TestPollReceiptRetry:
    def test_transient_redis_error_retries_to_success(self, monkeypatch):
        """PF9: a dropped connection mid-poll retries until the deadline."""
        import json as _json

        from faba_record import poll_receipt

        import redis as redis_lib

        calls = {"n": 0}

        class FlakyClient:
            def lrange(self, key, a, b):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise redis_lib.ConnectionError("transient drop")
                return [_json.dumps({"artefact_outcome": "stored", "artefact_id": "art-1", "version": 1})]

        monkeypatch.setattr("time.sleep", lambda s: None)
        receipt = poll_receipt("rq-1", timeout=5, client=FlakyClient())
        assert receipt and receipt["artefact_outcome"] == "stored"
        assert calls["n"] == 2


class TestValidateAuthoredArtefact:
    def _valid(self):
        return (
            "# Design — frobnicator\n\n**Change summary:** first draft.\n\n"
            + "body paragraph with enough substance to clear the stub floor. " * 8
        )

    def test_valid_artefact_passes(self):
        from faba_schema import validate_authored_artefact

        check = validate_authored_artefact(self._valid())
        assert check.ok, check.problems

    def test_stub_blocks(self):
        from faba_schema import validate_authored_artefact

        check = validate_authored_artefact("# t\n\n**Change summary:** x\n\nshort")
        assert not check.ok
        assert any("stub" in p for p in check.problems)

    def test_missing_title_blocks(self):
        from faba_schema import validate_authored_artefact

        check = validate_authored_artefact(self._valid().replace("# Design — frobnicator", "Design"))
        assert not check.ok
        assert any("title" in p for p in check.problems)

    def test_missing_change_summary_blocks(self):
        from faba_schema import validate_authored_artefact

        check = validate_authored_artefact(self._valid().replace("**Change summary:**", "**Notes:**"))
        assert not check.ok
        assert any("Change summary" in p for p in check.problems)

    def test_trailing_markup_blocks(self):
        """v17/v19 publish blemish: the author child stochastically closes a
        full-body Write with a wrapper tag from nowhere; the tail check makes
        the stop-gate bounce it back to the child instead of publishing it."""
        from faba_schema import validate_authored_artefact

        check = validate_authored_artefact(self._valid() + "\n</content>\n")
        assert not check.ok
        assert any("trailing markup" in p for p in check.problems)

    def test_trailing_markup_variant_tags_block(self):
        from faba_schema import validate_authored_artefact

        for tag in ("</document>", "</artefact>", "</content >"):
            check = validate_authored_artefact(self._valid() + f"\n{tag}\n")
            assert not check.ok, f"{tag} should be rejected as wrapper residue"

    def test_closing_tag_inside_body_is_not_flagged(self):
        """Only the LAST non-blank line is wrapper residue; markup discussed
        inside the artefact prose (e.g. this very incident's writeup) is fine."""
        from faba_schema import validate_authored_artefact

        check = validate_authored_artefact(
            self._valid() + "\nthe blemish was a stray tag line\n</content>\nfollowed by prose.\n"
        )
        assert check.ok, check.problems

    def test_trailing_markup_allowed_for_staged_prior(self):
        """The staged-prior role reads the store verbatim: a historical tail
        blemish must not block the revision that exists to remove it."""
        from faba_schema import validate_authored_artefact

        check = validate_authored_artefact(
            self._valid() + "\n</content>\n", allow_trailing_markup=True
        )
        assert check.ok, check.problems


class TestValidateDispatchBriefExported:
    """Slice 1d-iv Task 4: dispatch-brief validator is a peer of the authored
    artefact validator. Full assumptions matrix lives in test_dispatch_brief.py."""

    def test_validate_dispatch_brief_is_importable_and_shape_gated(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief("no assumptions here", target_vantage="host-a")
        assert not check.ok
        assert check.problems


class TestValidateRevisionFold:
    """F14(c) hygiene tier: the fold check is deliberately ONLY byte-inequality —
    the panel (panel-f14c-design-20260720T033218Z-5ec74f) rejected title/length
    heuristics as false-positive-prone and padding-incentivizing."""

    def test_identical_body_blocks(self):
        from faba_schema import validate_revision_fold

        body = "# t\n\n**Change summary:** x\n\nbody\n"
        check = validate_revision_fold(body, body)
        assert not check.ok
        assert any("byte-identical" in p for p in check.problems)

    def test_any_difference_passes(self):
        from faba_schema import validate_revision_fold

        prior = "# t\n\n**Change summary:** x\n\nbody\n"
        check = validate_revision_fold(prior + "\nnew changelog entry\n", prior)
        assert check.ok, check.problems

    def test_condensing_rewrite_passes(self):
        """A sanctioned condense (much shorter than prior) is legitimate — the
        rejected 0.5x length heuristic must NOT be reintroduced here."""
        from faba_schema import validate_revision_fold

        prior = "# t\n\n**Change summary:** x\n\n" + "long body " * 200
        check = validate_revision_fold("# renamed\n\n**Change summary:** condensed.\n\nshort\n", prior)
        assert check.ok, check.problems
