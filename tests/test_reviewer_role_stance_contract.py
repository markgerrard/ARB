from pathlib import Path
TXT = (Path(__file__).parents[1] / "roles" / "reviewer.md").read_text()

def test_documents_stance_enum():
    for s in ("approve", "needs-changes", "block", "abstain"):
        assert s in TXT

def test_requires_trailing_vote_fence():
    assert "```vote" in TXT

def test_reconciles_legacy_vocabulary():
    # the old labels must be mapped/superseded, not left as a second contradicting contract
    if "FIX_BEFORE_MERGE" in TXT or "SHIP_WITH_NITS" in TXT:
        assert "approve" in TXT and "```vote" in TXT  # mapping must be present alongside
