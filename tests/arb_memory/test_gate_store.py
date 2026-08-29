import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")

from arb_memory import gate_store  # noqa: E402

HARNESS = "harness-runner"
VERIFIER = "claude-b"


def _seed_claim(scratch):
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, status, author_seat, author_family, "
        "author_family_provenance) VALUES ('c-1', 'f-1', 'confirmed', 'codex-a', 'gpt', 'wire')"
    )


def _seed_artefact(scratch, artefact_id, author):
    scratch.execute(
        "INSERT INTO artefacts (artefact_id, version, content, content_hash, source, author) "
        "VALUES (%s, 1, 'rerun log', 'h', 'harness', %s)",
        (artefact_id, author),
    )


def _attest(scratch, artefact_id):
    gate_store.insert_attestation(
        scratch,
        claim_id="c-1",
        verifier_seat=VERIFIER,
        verifier_family="claude",
        family_provenance="wire",
        restatement="the door is unwired",
        mechanism="audit.py:13-20 is never called from run.py",
        falsifier="a passing production-wiring test",
        falsifier_kind="command",
        rerun_artefact_id=artefact_id,
        rerun_artefact_version=1,
        harness_authors=frozenset({HARNESS}),
    )


def test_harness_authored_rerun_is_admitted(scratch):
    _seed_claim(scratch)
    _seed_artefact(scratch, "art-rerun", HARNESS)
    _attest(scratch, "art-rerun")
    assert scratch.execute(
        "SELECT count(*) FROM attestations WHERE claim_id = 'c-1'"
    ).fetchone()[0] == 1


def test_verifier_authored_rerun_is_refused_and_names_the_author(scratch):
    """F4: citing your own transcript is self-report one pointer deeper."""
    _seed_claim(scratch)
    _seed_artefact(scratch, "art-self", VERIFIER)
    with pytest.raises(gate_store.HarnessIdentityRefused) as exc:
        _attest(scratch, "art-self")
    assert exc.value.author == VERIFIER
    assert VERIFIER in str(exc.value)
    assert "art-self" in str(exc.value)
    assert scratch.execute(
        "SELECT count(*) FROM attestations WHERE claim_id = 'c-1'"
    ).fetchone()[0] == 0, "refused attestation was written anyway"


def test_missing_rerun_artefact_is_refused_not_admitted(scratch):
    """Fail-closed: an unresolvable pointer must not pass as 'no evidence of a problem'."""
    _seed_claim(scratch)
    with pytest.raises(gate_store.HarnessIdentityRefused) as exc:
        _attest(scratch, "art-does-not-exist")
    assert "art-does-not-exist" in str(exc.value)
    assert scratch.execute(
        "SELECT count(*) FROM attestations WHERE claim_id = 'c-1'"
    ).fetchone()[0] == 0
