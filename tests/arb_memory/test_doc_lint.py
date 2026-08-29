"""Tests for the markdown artefact lint.

The first two tests are the lint's DENY-PROOF: each reconstructs one of the two real
2026-07-26 defects that motivated the module. A lint that cannot fail on the defect it
was written for is vacuously green — see
`docs/defect-classes/deny-proofs-need-adversarial-verification.md`.
"""

from pathlib import Path

from arb_memory.doc_lint import (
    LINT_OK,
    LINT_REFUSED,
    check_structure,
    check_view_count_claims,
    lint,
)


SPEC = Path(__file__).resolve().parents[2] / (
    "docs/superpowers/specs/2026-07-26-bus-side-gate-design.md"
)


def test_founding_defect_heading_consumed_by_edit_anchor():
    """§6's heading was eaten by an Edit that used it as an anchor; §5.3 ran into §6's body."""
    doc = "## 1. A\n\ntext\n\n## 2. B\n\ntext\n\n## 4. D\n\ntext\n"
    gaps = check_structure(doc)
    assert gaps, "lint must fail on a missing top-level section"
    assert "missing top-level sections: [3]" in gaps[0]


def test_founding_defect_stale_view_count():
    """Prose said "two views" after a third view was added to the schema."""
    doc = (
        "CREATE VIEW claim_admissibility_v AS SELECT 1;\n"
        "CREATE VIEW seat_posture_v AS SELECT 1;\n"
        "CREATE VIEW lease_lane_v AS SELECT 1;\n"
        "\nThe bridge holds a credential over two views, SELECT only.\n"
    )
    gaps = check_view_count_claims(doc)
    assert gaps, "lint must fail when a prose count disagrees with the definitions"
    assert '"two views"' in gaps[0] and "3 are defined" in gaps[0]


def test_hyphenated_compound_is_not_a_count_claim():
    """The naive grep flagged "one view-rewrite"; the relational check must not."""
    doc = (
        "CREATE VIEW only_v AS SELECT 1;\n"
        "\nRequiring it in the view leaves the requirement one view-rewrite from disappearing.\n"
    )
    assert check_view_count_claims(doc) == []


def test_duplicate_section_is_caught():
    doc = "## 1. A\n\n## 2. B\n\n## 2. B again\n"
    gaps = check_structure(doc)
    assert any("duplicate" in g for g in gaps)


def test_out_of_order_sections_are_caught():
    doc = "## 1. A\n\n## 3. C\n\n## 2. B\n"
    gaps = check_structure(doc)
    assert any("out of order" in g for g in gaps)


def test_documents_without_numbered_sections_are_not_asserted_on():
    """Most artefacts are prose; the lint must not invent a requirement they never had."""
    assert check_structure("# Title\n\nSome prose.\n") == []


def test_count_claim_without_definitions_is_not_adjudicated():
    """No CREATE VIEW in the document => no ground truth => no claim to make."""
    assert check_view_count_claims("The bridge holds two views.\n") == []


def test_lint_returns_house_style_result():
    bad = lint("## 1. A\n\n## 3. C\n")
    assert bad["outcome"] == LINT_REFUSED
    assert bad["exit_code"] == 5
    assert bad["gaps"], "a refusal must NAME the gap, not just fail"

    good = lint("## 1. A\n\n## 2. B\n")
    assert good == {"outcome": LINT_OK, "exit_code": 0, "gaps": []}


def test_the_spec_that_motivated_this_lint_passes_it():
    """Regression: the real artefact, post-fix, must be clean."""
    result = lint(SPEC.read_text())
    assert result["outcome"] == LINT_OK, result["gaps"]
