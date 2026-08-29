"""Pin the import-provenance guard in ``tests/conftest.py``.

The guard exists to stop a suite reporting green for code it never imported.
Nothing else in the suite fails if it is deleted — a refactor could drop it and
every run would stay green, which is precisely the failure mode it guards
against. So the guard needs its own test, and that test has to prove the guard
can FAIL, not merely that it is present.

Each case builds a synthetic checkout that always has a real ``src/`` (as any
genuine checkout does), copies the REAL conftest into its ``tests/``, and then
runs pytest against it as a subprocess. The variable is what, if anything, is
placed EARLIER on ``PYTHONPATH`` to shadow that ``src/`` — which is exactly how
the hazard occurs in the wild.

Two cases here were added because a reviewer proved the earlier version of this
file passed against broken guards:

- ``src-foreign`` pins ancestry rather than substring containment. A guard
  written as ``str(expected) in str(origin)`` accepts ``<checkout>/src-foreign``
  and passed every earlier case.
- the mixed-origin case pins that ALL shipped packages are checked. A guard that
  checks only ``agent_redis_bridge`` accepts a process whose ``arb_memory``
  resolves somewhere else entirely.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# pytest's exit code for a conftest that raises during import. Asserted
# explicitly rather than "nonzero": a bare refusal would also be satisfied by a
# collection error, a plugin crash, or a typo in this test's own scaffolding.
PYTEST_USAGE_ERROR = 4

REAL_CONFTEST = Path(__file__).resolve().parent / "conftest.py"
MARKER = "import-provenance"

# Packages the synthetic checkout ships from its own src/. Two is enough to
# exercise the mixed-origin case; the guard discovers this set from src/ itself.
SHIPPED = ("agent_redis_bridge", "arb_memory")

# Mirrors the interpreter actually running the tests, rather than pinning a
# version string that silently ages.
SITE_PACKAGES_REL = Path(
    ".venv", "lib", f"python{sys.version_info.major}.{sys.version_info.minor}",
    "site-packages",
)


def _write_package(parent: Path, name: str) -> None:
    package = parent / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        f'__version__ = "0.0.0-stub-{name}"\n', encoding="utf-8"
    )


def _build_fake_checkout(
    root: Path,
    shadow_parent: Path | None = None,
    shadow_packages: tuple[str, ...] = SHIPPED,
) -> Path:
    """Lay out a synthetic checkout; return its ``tests/`` directory.

    The checkout always gets a genuine ``src/`` holding every package in
    ``SHIPPED``. ``shadow_parent``, when given, receives copies of
    ``shadow_packages`` and is placed FIRST on PYTHONPATH by the caller.
    """
    src = root / "src"
    for name in SHIPPED:
        _write_package(src, name)

    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(REAL_CONFTEST, tests_dir / "conftest.py")
    (tests_dir / "test_probe.py").write_text(
        "def test_probe():\n    assert True\n", encoding="utf-8"
    )

    if shadow_parent is not None:
        for name in shadow_packages:
            _write_package(shadow_parent, name)
    return tests_dir


def _run_pytest(tests_dir: Path, *path_entries: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tests_dir),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            # Precedes site-packages, so these entries win over the editable
            # install — the same mechanism as the bug being guarded against.
            "PYTHONPATH": os.pathsep.join(str(p) for p in path_entries),
        },
    )


def _assert_refused(result: subprocess.CompletedProcess, why: str) -> None:
    assert result.returncode == PYTEST_USAGE_ERROR, (
        f"{why}\nexpected pytest usage error {PYTEST_USAGE_ERROR}, got "
        f"{result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert MARKER in result.stdout + result.stderr
    assert "1 passed" not in result.stdout


def test_guard_accepts_the_checkouts_own_src(tmp_path):
    """Packages under ``<checkout>/src`` are this tree's code: collection runs."""
    tests_dir = _build_fake_checkout(tmp_path)

    result = _run_pytest(tests_dir, tmp_path / "src")

    assert result.returncode == 0, (
        f"guard wrongly refused this checkout's own src/\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert MARKER not in result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_guard_refuses_a_foreign_checkout(tmp_path):
    """The original hazard: packages resolve outside the checkout entirely."""
    root = tmp_path / "checkout"
    foreign = tmp_path / "another-checkout" / "src"
    tests_dir = _build_fake_checkout(root, shadow_parent=foreign)

    _assert_refused(
        _run_pytest(tests_dir, foreign, root / "src"),
        "a package resolving to a different checkout was accepted",
    )


def test_guard_refuses_a_stale_copy_inside_the_checkouts_own_venv(tmp_path):
    """Inside the checkout is not enough — it has to be the checkout's ``src``.

    A non-editable ``pip install .`` leaves a copy under
    ``<checkout>/.venv/lib/pythonX.Y/site-packages/``. That path IS inside the
    checkout root, so a root-level check waves it through, yet it is a stale
    snapshot rather than the ``src/`` tree the diff edits — and
    ``_link_base_venv`` would mirror it into every worktree.
    """
    site_packages = tmp_path / SITE_PACKAGES_REL
    tests_dir = _build_fake_checkout(tmp_path, shadow_parent=site_packages)

    _assert_refused(
        _run_pytest(tests_dir, site_packages, tmp_path / "src"),
        "a stale copy inside the checkout's own .venv was accepted",
    )


def test_guard_refuses_a_sibling_directory_sharing_the_src_prefix(tmp_path):
    """Ancestry, not substring: ``<checkout>/src-foreign`` is not ``<checkout>/src``.

    Pins the containment test itself. A guard written as
    ``str(_EXPECTED_SRC) not in str(_IMPORTED_FROM)`` passes every other case in
    this file while accepting this one, because ``.../src-foreign/...`` contains
    the text ``.../src``.
    """
    src_foreign = tmp_path / "src-foreign"
    tests_dir = _build_fake_checkout(tmp_path, shadow_parent=src_foreign)

    _assert_refused(
        _run_pytest(tests_dir, src_foreign, tmp_path / "src"),
        "a sibling directory sharing the src name prefix was accepted",
    )


def test_guard_refuses_a_tests_shim_reaching_outside_the_checkout(tmp_path):
    """``tests/`` is an accepted provider — but only for code inside the checkout.

    A namespace-extension shim appends to ``__path__`` when it executes, which is
    AFTER the import-time check has passed. A shim pointing outside the checkout
    therefore yields a package that looks local at the top level while its
    submodules execute foreign code. A reviewer demonstrated this against an
    earlier version of the guard, where it was merely asserted in a comment.

    Pins ``pytest_collection_modifyitems``; fails if that hook is removed.
    """
    outside = tmp_path / "outside-the-checkout" / "agent_redis_bridge"
    _write_package(outside.parent, "agent_redis_bridge")
    (outside / "inner.py").write_text('WHOAMI = "OUTSIDE"\n', encoding="utf-8")

    tests_dir = _build_fake_checkout(tmp_path)
    # A shim under tests/ that reaches out of the tree entirely.
    shim = tests_dir / "agent_redis_bridge"
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "__init__.py").write_text(
        f'__path__.append({str(outside)!r})\n', encoding="utf-8"
    )
    # The shim only runs if something imports the package, so the probe must.
    (tests_dir / "test_probe.py").write_text(
        "import agent_redis_bridge\n\n\ndef test_probe():\n    assert True\n",
        encoding="utf-8",
    )

    result = _run_pytest(tests_dir, tmp_path / "src")

    _assert_refused(result, "a tests/ shim reaching outside the checkout was accepted")
    assert "outside-the-checkout" in result.stdout + result.stderr, (
        "the refusal should name the escaping path"
    )


def test_guard_refuses_a_shim_that_only_executes_once_tests_run(tmp_path):
    """The escape still fails the run when the import happens inside a test body.

    A test that imports its package inside the function has not executed the shim
    at collection time, so ``sys.modules`` is empty and the collection hook sees
    nothing. This is not hypothetical: the reviewer's original reproduction was
    written this way, and an earlier version of this fix — which checked only at
    collection — passed it cleanly while foreign code executed.

    The individual test still passes; the RUN must not. Pins
    ``pytest_sessionfinish``.
    """
    outside = tmp_path / "outside-the-checkout" / "agent_redis_bridge"
    _write_package(outside.parent, "agent_redis_bridge")
    (outside / "inner.py").write_text('WHOAMI = "OUTSIDE"\n', encoding="utf-8")

    tests_dir = _build_fake_checkout(tmp_path)
    shim = tests_dir / "agent_redis_bridge"
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "__init__.py").write_text(
        f'__path__.append({str(outside)!r})\n', encoding="utf-8"
    )
    # Import INSIDE the test body — the shim runs only once tests are executing.
    (tests_dir / "test_probe.py").write_text(
        "def test_probe():\n"
        "    import agent_redis_bridge.inner as i\n"
        "    assert i.WHOAMI\n",
        encoding="utf-8",
    )

    result = _run_pytest(tests_dir, tmp_path / "src")

    assert result.returncode == PYTEST_USAGE_ERROR, (
        "a late-executing shim reaching outside the checkout let the run report "
        f"success\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert MARKER in result.stdout + result.stderr
    assert "outside-the-checkout" in result.stdout + result.stderr


def test_guard_refuses_a_stale_non_shim_package_under_tests(tmp_path):
    """``tests/<pkg>`` is accepted only when it EXTENDS src, never when it replaces it.

    The five real shims append ``<checkout>/src/<pkg>`` to their ``__path__``. A
    stale directory that merely shadows the name does not — yet it sits inside the
    checkout, so a providers-only check accepts it while ``src/<pkg>`` never
    executes at all. A reviewer reproduced this as a live false-green against the
    guard's first two revisions, including the PR as originally opened.

    Pins requirement (2) in ``_provenance_failures``.
    """
    tests_dir = _build_fake_checkout(tmp_path)
    # A package under tests/ that shadows the name WITHOUT extending into src.
    stale = tests_dir / "agent_redis_bridge"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "__init__.py").write_text(
        'WHOAMI = "STALE-COPY-IN-TESTS"\n', encoding="utf-8"
    )
    (tests_dir / "test_probe.py").write_text(
        "import agent_redis_bridge\n\n\ndef test_probe():\n    assert True\n",
        encoding="utf-8",
    )

    result = _run_pytest(tests_dir, tmp_path / "src")

    _assert_refused(result, "a stale non-shim package under tests/ replaced src/")
    assert "does not load from" in result.stdout + result.stderr


def test_guard_refuses_a_shim_that_extends_src_AND_reaches_outside(tmp_path):
    """A shim can satisfy "src is in play" and still smuggle in foreign code.

    This is the case requirement (1) uniquely covers, and the reason it is not
    redundant with (2). The earlier escape tests appended ONLY an outside path, so
    (2) caught them on its own and (1) went unpinned — deleting (1) left all tests
    green. Here the shim appends BOTH ``src/<pkg>`` (satisfying (2)) and a
    directory outside the checkout, so only (1) can refuse it.

    Submodule resolution walks ``__path__`` in order, so the outside entry can
    shadow or supply modules that src does not define.
    """
    outside = tmp_path / "outside-the-checkout" / "agent_redis_bridge"
    _write_package(outside.parent, "agent_redis_bridge")
    (outside / "smuggled.py").write_text('WHOAMI = "OUTSIDE"\n', encoding="utf-8")

    tests_dir = _build_fake_checkout(tmp_path)
    shim = tests_dir / "agent_redis_bridge"
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "__init__.py").write_text(
        "from pathlib import Path\n"
        '__path__.append(str(Path(__file__).resolve().parents[2] / "src" / "agent_redis_bridge"))\n'
        f'__path__.append({str(outside)!r})\n',
        encoding="utf-8",
    )
    (tests_dir / "test_probe.py").write_text(
        "import agent_redis_bridge\n\n\ndef test_probe():\n    assert True\n",
        encoding="utf-8",
    )

    result = _run_pytest(tests_dir, tmp_path / "src")

    _assert_refused(
        result, "a shim extending src AND reaching outside the checkout was accepted"
    )
    assert "outside this checkout" in result.stdout + result.stderr


def test_guard_accepts_a_genuine_namespace_extension_shim(tmp_path):
    """The counterpart: a shim that DOES extend into src must still be accepted.

    Without this, the fix for the stale-replacement case could pass by refusing
    every ``tests/<pkg>`` directory — which would refuse the real suite's own
    arrangement. This is the test that makes that over-correction visible.
    """
    tests_dir = _build_fake_checkout(tmp_path)
    shim = tests_dir / "agent_redis_bridge"
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "__init__.py").write_text(
        "from pathlib import Path\n"
        '__path__.append(str(Path(__file__).resolve().parents[2] / "src" / "agent_redis_bridge"))\n',
        encoding="utf-8",
    )
    (tests_dir / "test_probe.py").write_text(
        "import agent_redis_bridge\n\n\ndef test_probe():\n    assert True\n",
        encoding="utf-8",
    )

    result = _run_pytest(tests_dir, tmp_path / "src")

    assert result.returncode == 0, (
        "guard refused a genuine namespace-extension shim — this would refuse the "
        f"real suite's own layout\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert MARKER not in result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_guard_refuses_a_mixed_origin_process(tmp_path):
    """One package local, a sibling foreign — the whole process is untrustworthy.

    Pins that every shipped package is checked. A guard that validates only
    ``agent_redis_bridge`` accepts this: that package resolves to the checkout's
    own src, while ``arb_memory`` — the package the original incident actually
    involved — resolves somewhere else entirely.
    """
    shadow = tmp_path / "elsewhere"
    tests_dir = _build_fake_checkout(
        tmp_path, shadow_parent=shadow, shadow_packages=("arb_memory",)
    )

    result = _run_pytest(tests_dir, shadow, tmp_path / "src")

    _assert_refused(result, "a mixed-origin process was accepted")
    assert "arb_memory" in result.stdout + result.stderr, (
        "the refusal should name the offending package"
    )


def test_guard_refuses_a_shadowed_pep420_namespace_package(tmp_path):
    """A shipped package with no ``__init__.py`` is still this tree's code.

    Pins the discovery predicate specifically. ``packages.find`` leaves
    ``namespaces`` at its default of true, so a PEP 420 directory under ``src/``
    ships like any other package — but an ``__init__.py``-based owned-set omitted
    it, so a foreign regular package of the same name shadowed it and executed
    with the suite green. No other test in this file creates a namespace package,
    so this one uniquely covers that predicate.
    """
    root = tmp_path / "checkout"
    tests_dir = _build_fake_checkout(root)

    # PEP 420 portion in the checkout's own src: modules, deliberately no __init__.py.
    namespace_dir = root / "src" / "arb_namespace"
    namespace_dir.mkdir(parents=True)
    (namespace_dir / "local.py").write_text("VALUE = 'local'\n", encoding="utf-8")

    # A foreign REGULAR package of the same name. Regular packages beat namespace
    # portions, so this wins the import outright once it precedes src on the path.
    foreign = tmp_path / "foreign"
    _write_package(foreign, "arb_namespace")
    (foreign / "arb_namespace" / "local.py").write_text(
        "VALUE = 'foreign'\n", encoding="utf-8"
    )

    result = _run_pytest(tests_dir, foreign, root / "src")

    _assert_refused(result, "a shadowed PEP 420 namespace package was accepted")
    assert "arb_namespace" in result.stdout + result.stderr, (
        "the refusal should name the offending package"
    )
