"""Assumptions-schema + dispatch-brief validator tests (Slice 1d-iv Task 4).

The validator proves presence and shape of the assumptions section, not
completeness of real-world preconditions. A missing real precondition is a
review residual, not something this gate claims to detect.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
REPO = FABA.parents[1]
for p in (str(FABA), str(REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)


TARGET_VANTAGE = "mac-host-dev"


def _brief(*, assumptions_obj, title="# Dispatch brief — probe", body="## Instructions\n\nDo the work.\n"):
    block = json.dumps(assumptions_obj, indent=2)
    return f"{title}\n\n## Assumptions\n```json\n{block}\n```\n\n{body}"


def _valid_empty():
    return _brief(assumptions_obj={"items": []})


def _valid_assumed():
    return _brief(
        assumptions_obj={
            "items": [
                {
                    "statement": "DNS resolves the bus host",
                    "status": "assumed",
                    "vantage": TARGET_VANTAGE,
                }
            ]
        }
    )


def _valid_demonstrated():
    return _brief(
        assumptions_obj={
            "items": [
                {
                    "statement": "Redis is reachable",
                    "status": "demonstrated",
                    "vantage": TARGET_VANTAGE,
                    "artefact_id": "art-demo-1",
                    "version": 2,
                }
            ]
        }
    )


def _valid_mixed():
    return _brief(
        assumptions_obj={
            "items": [
                {
                    "statement": "Redis is reachable",
                    "status": "demonstrated",
                    "vantage": TARGET_VANTAGE,
                    "artefact_id": "art-demo-1",
                    "version": 1,
                },
                {
                    "statement": "Operator is awake",
                    "status": "assumed",
                    "vantage": TARGET_VANTAGE,
                },
            ]
        }
    )


class TestValidateDispatchBriefShape:
    def test_empty_assumptions_passes(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(_valid_empty(), target_vantage=TARGET_VANTAGE)
        assert check.ok, check.problems

    def test_assumed_item_passes(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(_valid_assumed(), target_vantage=TARGET_VANTAGE)
        assert check.ok, check.problems

    def test_demonstrated_item_passes(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(_valid_demonstrated(), target_vantage=TARGET_VANTAGE)
        assert check.ok, check.problems

    def test_mixed_items_pass(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(_valid_mixed(), target_vantage=TARGET_VANTAGE)
        assert check.ok, check.problems

    def test_missing_assumptions_section_fails(self):
        from faba_schema import validate_dispatch_brief

        text = "# Title\n\n## Instructions\n\nbody only\n"
        check = validate_dispatch_brief(text, target_vantage=TARGET_VANTAGE)
        assert not check.ok
        assert any("assumptions" in p.lower() for p in check.problems)

    def test_malformed_json_fails(self):
        from faba_schema import validate_dispatch_brief

        text = "# Title\n\n## Assumptions\n```json\n{not json\n```\n\n## Instructions\n\nbody\n"
        check = validate_dispatch_brief(text, target_vantage=TARGET_VANTAGE)
        assert not check.ok
        assert any("json" in p.lower() or "malformed" in p.lower() for p in check.problems)

    def test_unknown_top_level_key_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(assumptions_obj={"items": [], "extra": True}),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("unknown" in p.lower() for p in check.problems)

    def test_unknown_item_key_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "x",
                            "status": "assumed",
                            "vantage": TARGET_VANTAGE,
                            "note": "nope",
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("unknown" in p.lower() for p in check.problems)

    def test_unknown_status_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "x",
                            "status": "maybe",
                            "vantage": TARGET_VANTAGE,
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("status" in p.lower() for p in check.problems)

    def test_blank_statement_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "  ",
                            "status": "assumed",
                            "vantage": TARGET_VANTAGE,
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("statement" in p.lower() for p in check.problems)

    def test_blank_vantage_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "x",
                            "status": "assumed",
                            "vantage": "",
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("vantage" in p.lower() for p in check.problems)

    def test_boolean_version_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "x",
                            "status": "demonstrated",
                            "vantage": TARGET_VANTAGE,
                            "artefact_id": "art-1",
                            "version": True,
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("version" in p.lower() for p in check.problems)

    def test_nonpositive_version_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "x",
                            "status": "demonstrated",
                            "vantage": TARGET_VANTAGE,
                            "artefact_id": "art-1",
                            "version": 0,
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("version" in p.lower() for p in check.problems)

    def test_demonstrated_without_ref_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "x",
                            "status": "demonstrated",
                            "vantage": TARGET_VANTAGE,
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("artefact" in p.lower() or "ref" in p.lower() or "version" in p.lower() for p in check.problems)

    def test_assumed_with_ref_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "x",
                            "status": "assumed",
                            "vantage": TARGET_VANTAGE,
                            "artefact_id": "art-1",
                            "version": 1,
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("assumed" in p.lower() or "ref" in p.lower() or "artefact" in p.lower() for p in check.problems)

    def test_duplicate_json_keys_fail(self):
        from faba_schema import validate_dispatch_brief

        # Raw text with duplicate keys at the assumptions object level.
        text = (
            "# Title\n\n## Assumptions\n```json\n"
            '{"items": [], "items": []}\n'
            "```\n\n## Instructions\n\nbody\n"
        )
        check = validate_dispatch_brief(text, target_vantage=TARGET_VANTAGE)
        assert not check.ok
        assert any("duplicate" in p.lower() for p in check.problems)

    def test_demonstrated_wrong_vantage_fails(self):
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "x",
                            "status": "demonstrated",
                            "vantage": "other-host",
                            "artefact_id": "art-1",
                            "version": 1,
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert not check.ok
        assert any("vantage" in p.lower() for p in check.problems)

    def test_assumed_wrong_vantage_still_ok_for_item_vantage_mismatch(self):
        """Assumed items name a vantage but are not demonstrated-for-target;
        mismatch of item vantage against target is allowed only when assumed? 

        Plan: "a demonstration whose vantage does not equal the selected
        registry target's advertised vantage unless it is assumed."
        Assumed with a different vantage is therefore ok.
        """
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(
            _brief(
                assumptions_obj={
                    "items": [
                        {
                            "statement": "x",
                            "status": "assumed",
                            "vantage": "other-host",
                        }
                    ]
                }
            ),
            target_vantage=TARGET_VANTAGE,
        )
        assert check.ok, check.problems

    def test_missing_title_fails(self):
        from faba_schema import validate_dispatch_brief

        text = _valid_empty().replace("# Dispatch brief — probe", "No hash title")
        check = validate_dispatch_brief(text, target_vantage=TARGET_VANTAGE)
        assert not check.ok
        assert any("title" in p.lower() for p in check.problems)

    def test_missing_body_after_assumptions_fails(self):
        from faba_schema import validate_dispatch_brief

        text = _brief(assumptions_obj={"items": []}, body="   \n")
        check = validate_dispatch_brief(text, target_vantage=TARGET_VANTAGE)
        assert not check.ok
        assert any("body" in p.lower() or "instructions" in p.lower() for p in check.problems)

    def test_does_not_claim_to_detect_omitted_preconditions(self):
        """Contract: the validator proves shape, not completeness.

        An empty items list is an *explicit* no-precondition claim and must
        pass — the gate does not invent missing real-world preconditions.
        """
        from faba_schema import validate_dispatch_brief

        check = validate_dispatch_brief(_valid_empty(), target_vantage=TARGET_VANTAGE)
        assert check.ok
        joined = " ".join(check.problems).lower()
        assert "omitted" not in joined
        assert "completeness" not in joined


class TestPublishGateAcceptsSelectedValidator:
    def test_publish_artefact_and_gate_uses_injected_validator(self, tmp_path, monkeypatch):
        """Parameterization seam: selected validator, not hardcoded authored."""
        from faba_launch import publish_artefact_and_gate
        from faba_schema import RecordCheck

        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "artefact.md").write_text("# t\n\nbody\n", encoding="utf-8")
        seen = []

        def reject_all(text):
            seen.append(text)
            return RecordCheck(ok=False, problems=["injected-reject"])

        result = publish_artefact_and_gate(
            "redis://unused/0",
            workspace=workspace,
            artefact_id="art-x",
            request_id="rq-x",
            author="test",
            receipt_timeout=0.1,
            validate=reject_all,
        )
        assert not result.passed
        assert "injected-reject" in result.reason
        assert seen, "injected validator must run before any bus access"
        assert result.phase == "not_enqueued"
