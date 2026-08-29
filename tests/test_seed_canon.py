"""Tests for scripts/check-seed-canon — the seed→canonical pointer lint.

The lint exists because a stale pointer is silent: nothing at read time verifies that a seed's
cited version is the store's current one. A lint that can only pass is the same failure one level
up, so the store-comparison path is exercised here with a stubbed store rather than left to a
machine that happens to have ARB_MEMORY_DSN set.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-seed-canon"


def load_module():
    spec = importlib.util.spec_from_loader(
        "check_seed_canon",
        importlib.machinery.SourceFileLoader("check_seed_canon", str(SCRIPT)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return load_module()


@pytest.fixture
def seeds(tmp_path):
    d = tmp_path / "seeds"
    d.mkdir()
    return d


def write(seeds, name, body):
    (seeds / f"{name}.md").write_text(body, encoding="utf-8")


def run(mod, seeds, store, *args):
    """Run main() with load_store stubbed to `store` (or None for 'unreachable')."""
    mod.load_store = lambda ids: store
    return mod.main(["--seeds", str(seeds), *args])


CLEAN = ({"a-doc": 4}, {("a-doc", 4): "f" * 64})


def test_current_pointer_passes(mod, seeds, capsys):
    write(seeds, "ok", "Canonical: ARB Memory `a-doc` v4, content hash `" + "f" * 64 + "`.\n")
    assert run(mod, seeds, CLEAN) == 0
    assert "OK" in capsys.readouterr().out


def test_stale_version_fails_with_distance(mod, seeds, capsys):
    """The live bug: seed cited v1 while the store was at v3."""
    write(seeds, "stale", "Canonical: ARB Memory `a-doc` v1.\n")
    assert run(mod, seeds, CLEAN) == 1
    err = capsys.readouterr().err
    assert "cites v1 but store is at v4" in err
    assert "3 version(s) stale" in err


def test_hash_mismatch_fails_even_when_version_is_current(mod, seeds, capsys):
    write(seeds, "badhash", "Canonical: ARB Memory `a-doc` v4, content hash `" + "a" * 64 + "`.\n")
    assert run(mod, seeds, CLEAN) == 1
    assert "hash mismatch" in capsys.readouterr().err


def test_unknown_artefact_fails(mod, seeds, capsys):
    write(seeds, "ghost", "Canonical: ARB Memory `not-stored` v1.\n")
    assert run(mod, seeds, CLEAN) == 1
    assert "not in the store at all" in capsys.readouterr().err


def test_floor_pin_warns_but_passes_when_satisfied(mod, seeds, capsys):
    write(seeds, "floor", "Full record: ARB Memory `art-81438f2f5a5c4955` (ADR, v3+).\n")
    store = ({"art-81438f2f5a5c4955": 14}, {})
    assert run(mod, seeds, store) == 0
    assert "can never detect drift" in capsys.readouterr().out


def test_floor_pin_fails_when_store_is_below_the_floor(mod, seeds, capsys):
    write(seeds, "floor", "Full record: ARB Memory `art-81438f2f5a5c4955` (ADR, v13+).\n")
    store = ({"art-81438f2f5a5c4955": 9}, {})
    assert run(mod, seeds, store) == 1
    assert "floor v13+ but store max is v9" in capsys.readouterr().err


def test_unpinned_sibling_is_detected_and_warned(mod, seeds, capsys):
    """`art-a…` (ADR, v3+), `art-b…` (explainer) — the second id is a citation too."""
    write(seeds, "sib",
          "Full record: ARB Memory `art-aaaaaaaaaaaaaaaa` (ADR, v3+), "
          "`art-bbbbbbbbbbbbbbbb` (explainer).\n")
    store = ({"art-aaaaaaaaaaaaaaaa": 5, "art-bbbbbbbbbbbbbbbb": 2}, {})
    assert run(mod, seeds, store) == 0
    out = capsys.readouterr().out
    assert "art-bbbbbbbbbbbbbbbb` cited with NO version" in out


def test_unpinned_sibling_does_not_inherit_the_anchor_version(mod, seeds):
    """A sibling with no version of its own must not borrow the anchor's pin."""
    write(seeds, "sib",
          "Full record: ARB Memory `art-aaaaaaaaaaaaaaaa` v3, `art-bbbbbbbbbbbbbbbb` (explainer).\n")
    cites, _ = mod.parse_file(seeds / "sib.md")
    by_id = {c.artefact_id: c for c in cites}
    assert by_id["art-aaaaaaaaaaaaaaaa"].version == 3
    assert by_id["art-bbbbbbbbbbbbbbbb"].version is None


def test_strict_escalates_warnings_to_failure(mod, seeds):
    write(seeds, "floor", "Full record: ARB Memory `art-81438f2f5a5c4955` (ADR, v3+).\n")
    store = ({"art-81438f2f5a5c4955": 14}, {})
    assert run(mod, seeds, store, "--strict") == 1


def test_intra_file_version_conflict_fails_without_a_store(mod, seeds, capsys):
    """Frontmatter said v3 while the body said v1 — caught with no store access at all."""
    write(seeds, "split",
          '---\ndescription: "canonical detail lives in a-doc v3"\n---\n'
          "Canonical source: ARB Memory `a-doc` v1.\n")
    assert run(mod, seeds, None) == 1
    assert "conflicting versions [1, 3]" in capsys.readouterr().err


def test_store_unreachable_skips_version_checks_but_still_parses(mod, seeds, capsys):
    write(seeds, "stale", "Canonical: ARB Memory `a-doc` v1.\n")
    assert run(mod, seeds, None) == 0
    assert "version checks SKIPPED" in capsys.readouterr().out


def test_require_store_fails_when_unreachable(mod, seeds):
    write(seeds, "stale", "Canonical: ARB Memory `a-doc` v1.\n")
    assert run(mod, seeds, None, "--require-store") == 2


def test_empty_corpus_is_not_a_failure(mod, seeds, capsys):
    write(seeds, "prose", "No citations here.\n")
    assert run(mod, seeds, CLEAN) == 0
    assert "nothing to check" in capsys.readouterr().out


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Point every configured store at a tmp dir so tests never touch the real host stores."""
    codex = tmp_path / "codex"
    pi = tmp_path / "pi" / "global"
    codex.mkdir(parents=True)
    pi.mkdir(parents=True)
    monkeypatch.setenv("CODEX_AUTO_MEMORY_DIR", str(codex))
    monkeypatch.setenv("PI_AUTO_MEMORY_DIR", str(tmp_path / "pi"))
    monkeypatch.delenv("CLAUDE_HARNESS_MEMORY_DIR", raising=False)
    return codex, pi


def test_verbatim_store_divergence_fails(mod, seeds, stores, capsys):
    """The codex case: corpus is correct, the store silently serves something else."""
    codex, _ = stores
    write(seeds, "topic", "Canonical: ARB Memory `a-doc` v4.\n")
    (codex / "topic.md").write_text("Canonical: ARB Memory `a-doc` v1.\n", encoding="utf-8")
    assert run(mod, seeds, CLEAN, "--check-stores") == 1
    err = capsys.readouterr().err
    assert "[codex] `topic` diverges from the corpus" in err


def test_allow_divergent_downgrades_a_known_fork(mod, seeds, stores, capsys):
    """The FABA case: store legitimately holds organic content — visible, not muted."""
    codex, _ = stores
    write(seeds, "topic", "Canonical: ARB Memory `a-doc` v4.\n")
    (codex / "topic.md").write_text("Canonical: ARB Memory `a-doc` v4.\nplus organic\n", encoding="utf-8")
    assert run(mod, seeds, CLEAN, "--check-stores", "--allow-divergent", "topic") == 0
    assert "ALLOWED via --allow-divergent" in capsys.readouterr().out


def test_adapted_store_is_not_byte_compared(mod, seeds, stores, capsys):
    """pi's copies are restructured on purpose — a byte-diff there is noise, not a finding."""
    _, pi = stores
    write(seeds, "topic", "Canonical: ARB Memory `a-doc` v4.\n")
    (pi / "topic.md").write_text(
        "# totally different shape\n\nCanonical: ARB Memory `a-doc` v4.\n", encoding="utf-8")
    assert run(mod, seeds, CLEAN, "--check-stores") == 0
    assert "diverges" not in capsys.readouterr().out


def test_adapted_store_stale_pointer_still_fails(mod, seeds, stores, capsys):
    """Not byte-compared does not mean unchecked: pi's POINTERS are still verified."""
    _, pi = stores
    write(seeds, "topic", "Canonical: ARB Memory `a-doc` v4.\n")
    (pi / "topic.md").write_text(
        "# adapted\n\nCanonical: ARB Memory `a-doc` v1.\n", encoding="utf-8")
    assert run(mod, seeds, CLEAN, "--check-stores") == 1
    err = capsys.readouterr().err
    assert "[pi]" in err and "cites v1 but store is at v4" in err


def test_topic_missing_from_verbatim_store_warns(mod, seeds, stores, capsys):
    write(seeds, "topic", "Canonical: ARB Memory `a-doc` v4.\n")
    assert run(mod, seeds, CLEAN, "--check-stores") == 0
    assert "in the corpus but not the store" in capsys.readouterr().out


def test_unconfigured_store_is_reported_not_silently_skipped(mod, seeds, stores, capsys):
    write(seeds, "topic", "Canonical: ARB Memory `a-doc` v4.\n")
    run(mod, seeds, CLEAN, "--check-stores")
    assert "store not checked: claude-harness" in capsys.readouterr().out


def test_store_citations_are_labelled_by_store(mod, seeds, stores, capsys):
    _, pi = stores
    write(seeds, "topic", "Canonical: ARB Memory `a-doc` v4.\n")
    (pi / "other.md").write_text("Canonical: ARB Memory `a-doc` v1.\n", encoding="utf-8")
    run(mod, seeds, CLEAN, "--check-stores")
    assert "[pi] " in capsys.readouterr().err


def test_stores_are_not_touched_without_the_flag(mod, seeds, stores, capsys):
    """Default run stays corpus-only, so the checkout gate never depends on host state."""
    codex, _ = stores
    write(seeds, "topic", "Canonical: ARB Memory `a-doc` v4.\n")
    (codex / "topic.md").write_text("Canonical: ARB Memory `a-doc` v1.\n", encoding="utf-8")
    assert run(mod, seeds, CLEAN) == 0
    assert "codex" not in capsys.readouterr().out


def test_live_corpus_parses_to_expected_pointers(mod):
    """Guards the regexes against the real corpus, without needing the store."""
    seeds_dir = ROOT / "docs" / "agent-memory-seeds"
    if not seeds_dir.is_dir():
        pytest.skip("seeds corpus not present")
    found = {}
    for path in seeds_dir.rglob("*.md"):
        for c in mod.parse_file(path)[0]:
            found.setdefault(c.artefact_id, c)
    assert "arb-seat-scorecard" in found, "roster citation regressed"
    assert "orch-round-closure-discipline" in found, "closure-doctrine citation regressed"
