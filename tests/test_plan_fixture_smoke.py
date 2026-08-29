"""plan-fixture-smoke extractor tests (fixture-lifecycle coverage hole, 2 specimens)."""
import importlib.machinery
import importlib.util
import pathlib

_path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "plan-fixture-smoke"
_spec = importlib.util.spec_from_loader(
    "plan_fixture_smoke",
    importlib.machinery.SourceFileLoader("plan_fixture_smoke", str(_path)),
)
pfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pfs)


def test_extracts_only_marked_blocks():
    md = (
        "prose\n"
        "```python\nprint('plain python fence — a plan test body, NOT smoke')\n```\n"
        "more\n"
        "```python fixture-smoke\nx = 1\nassert x == 1\n```\n"
        "```python fixture-smoke\ny = x + 1\n```\n"
    )
    blocks = pfs.extract(md)
    assert len(blocks) == 2
    assert "x = 1" in blocks[0] and "y = x + 1" in blocks[1]
    assert "plain python fence" not in "".join(blocks)


def test_no_blocks_is_distinguishable():
    assert pfs.extract("# a plan with no smoke\n```python\npass\n```\n") == []


def test_blocks_share_namespace_and_assertion_failure_surfaces():
    ok, err = pfs.run_blocks(["a = 41", "a += 1\nassert a == 42"])
    assert ok
    ok, err = pfs.run_blocks(["assert False, 'fixture cannot satisfy predicate'"])
    assert not ok
    assert "fixture cannot satisfy predicate" in err


def test_task_tagged_extraction():
    md = (
        "```python fixture-smoke\nuntagged = True\n```\n"
        "```python fixture-smoke task=2\ntagged = 2\n```\n"
        "```python fixture-smoke task=3\ntagged = 3\n```\n"
    )
    blocks = pfs.extract_tagged(md)
    assert [t for t, _ in blocks] == [None, 2, 3]
    assert pfs.select_blocks(blocks, task=2) == [blocks[0][1], blocks[1][1]]  # untagged + task-2
    assert pfs.select_blocks(blocks, task=None) == [b for _, b in blocks]     # no filter: all


def test_red_claim_flags_inert_pin():
    # Sub-species B (specimen 3): a test the plan claims fails pre-edit but that
    # actually passes is an INERT PIN — red_claim must refuse it.
    src = "def test_supposedly_red():\n    assert True\n"
    ok, err = pfs.run_blocks(
        ["red_claim(SRC, expect_fail=['test_supposedly_red'])"],
        extra_namespace={"SRC": src},
    )
    assert not ok
    assert "RED-CLAIM VIOLATION" in err and "test_supposedly_red" in err


def test_red_claim_accepts_genuinely_red():
    src = "def test_genuinely_red():\n    assert False\n"
    ok, err = pfs.run_blocks(
        ["red_claim(SRC, expect_fail=['test_genuinely_red'])"],
        extra_namespace={"SRC": src},
    )
    assert ok, err


def test_tree_exposed_to_blocks(tmp_path):
    marker = tmp_path / "provisioned.sentinel"
    marker.write_text("x")
    ok, err = pfs.run_blocks(
        ["assert (TREE / 'provisioned.sentinel').exists(), 'world-claim failed'"],
        tree=tmp_path,
    )
    assert ok, err
    ok, err = pfs.run_blocks(
        ["assert (TREE / 'provisioned.sentinel').exists(), 'world-claim failed'"],
        tree=tmp_path / "does-not-exist",
    )
    assert not ok and "world-claim failed" in err
