"""Shared containment fixtures for the unit-test process."""

import importlib.util
import sys
from pathlib import Path

import pytest

# Import provenance — fail closed at collection, before any test can report green.
#
# A worktree without its own untracked ``.venv`` resolves ``agent_redis_bridge``
# through the PARENT checkout's editable install (``__editable__`` .pth). The
# suite then exercises the parent tree while reporting green for this one: a
# dispatched worker commits real changes, runs the suite, and reports a pass for
# code the run never imported. The completion gate cannot catch that — the commit
# is real and the tree is clean — so the refusal has to happen here.
#
# ``scripts/coverage_mutation_sweep.py`` already forces PYTHONPATH to
# ``<repo>/src`` for exactly this reason ("an ambient parent-checkout PYTHONPATH
# cannot mask mutations"). That protection was local to one script; this makes it
# hold for every invocation, including a bare ``pytest`` in a venv-less worktree.
#
# The assertion is against ``<checkout>/src``, NOT the checkout root. "Somewhere
# inside this checkout" is too weak: a non-editable ``pip install .`` leaves a
# COPY under ``<checkout>/.venv/lib/pythonX.Y/site-packages/agent_redis_bridge/``,
# which is inside the root and would pass a root-level check while being a
# different (and silently stale) tree from the ``src/`` the diff edits. Because
# ``_link_base_venv`` mirrors the base ``.venv`` into every bridge-created
# worktree, one such install would propagate that stale copy to every worktree at
# once — the same false-green this guard exists to refuse, one directory deeper.
#
# Check EVERY package this checkout ships, not just one. Python resolves each
# top-level package independently through sys.path, so a PYTHONPATH-shadowed or
# separately-installed sibling yields a MIXED-ORIGIN process — agent_redis_bridge
# local, arb_memory foreign — which a one-package check waves through. That the
# editable install currently exposes all of them through a single .pth is
# environmental evidence, not an enforced invariant. arb_memory is also the
# package the incident in ENVIRONMENT-TRAPS 6a actually involved.
#
# The owned set is DISCOVERED from src/ rather than hardcoded, so a package
# added later is covered without anyone remembering to update this list.
#
# Two directories in this checkout may legitimately provide a shipped package:
#
#   src/<pkg>    — the source tree itself
#   tests/<pkg>  — a namespace-extension shim. Five of these exist
#                  (arb_email, arb_files, arb_memory, arb_messages, arb_secrets);
#                  each appends `<checkout>/src/<pkg>` to its own __path__, so
#                  `import arb_memory` resolves to the SHIM while
#                  `arb_memory.mcp.grants` resolves into src/. Refusing tests/
#                  would refuse the suite's own normal arrangement.
#
# Accepting tests/ opens a second question this check CANNOT answer: a shim
# appends to __path__ when its __init__ executes, and nothing stops it appending
# a directory outside the checkout — submodules would then execute foreign code
# while the top-level package still looks local. A reviewer demonstrated exactly
# that against a version of this file where it was only asserted in a comment, so
# it is now ENFORCED by two hooks below, each load-bearing for a different case:
#
#   pytest_collection_modifyitems — the shim ran during collection (a test module
#       imports at module level). Refuses BEFORE any test executes.
#   pytest_sessionfinish          — the shim only ran once tests were executing (a
#       test imports inside its function body). Too late to prevent the run, but
#       it still denies the green, which is the property that matters.
#
# Anything else — notably <checkout>/.venv/.../site-packages — is refused.
#
# ---------------------------------------------------------------------------
# WHAT THIS GUARD DOES NOT DO — a deliberate boundary, not an oversight.
# ---------------------------------------------------------------------------
# It defends against ENVIRONMENTAL misconfiguration: a venv-less worktree, an
# ambient PYTHONPATH, a stale install — cases where the operator believes they are
# testing this tree and are not. That is the incident it was written for.
#
# It does NOT defend against in-tree code that deliberately misdirects imports.
# Three such false-greens were reproduced by reviewers against this file and are
# knowingly left open:
#
#   1. Transient __path__: a shim appends a foreign directory, imports from it,
#      and removes the entry before either hook runs. Both hooks inspect a
#      SNAPSHOT, so they see a clean state. (pytest exits 0.)
#   2. Post-sessionfinish: a pytest_unconfigure hook imports foreign code after
#      the last check. pytest calls sessionfinish and THEN _ensure_unconfigure
#      (_pytest/main.py), so sessionfinish is not the final executable phase.
#   3. Subpackage/submodule escape: the walk covers top-level owned packages, so
#      a subpackage's __path__ or a submodule's __file__ can point outside.
#
# Closing these properly needs a persistent import-resolution guard (a meta_path
# finder) live for the whole process. That was considered and declined: it would
# run in the SAME process as the code it guards against, so the shim that appends
# a path can equally call sys.meta_path.remove(). The class is not closable
# in-process — each layer moves the escape one level down rather than removing it.
#
# So the line is drawn here on purpose: accident is refused, deliberate
# misdirection by committed code is not defended. If you are tempted to "finish"
# this guard, read that paragraph again first — and note that a check which only
# LOOKS complete is worse than one with a stated boundary, because the whole point
# of this file is refusing to report on code it did not exercise.
_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
_EXPECTED_SRC = (_CHECKOUT_ROOT / "src").resolve()
_TESTS_DIR = Path(__file__).resolve().parent
_PROVIDERS = (_EXPECTED_SRC, _TESTS_DIR)

if not _EXPECTED_SRC.is_dir():
    raise RuntimeError(
        f"import-provenance: {_EXPECTED_SRC} is not a directory, so this is not "
        "a usable checkout of this repo. Refusing rather than testing whatever "
        "happens to be on sys.path."
    )

def _is_shipped_package_dir(entry: Path) -> bool:
    """Would setuptools ship this directory as a top-level package?

    ``[tool.setuptools.packages.find] where = ["src"]`` leaves ``namespaces`` at
    its default of true, so PEP 420 directories — no ``__init__.py``, but
    carrying modules — ship exactly like regular packages and must be guarded
    exactly like them. Discovering by ``__init__.py`` alone omitted them from the
    owned set, so a shadowed namespace package executed foreign code with the
    suite still green.

    The identifier test is what keeps ``*.egg-info`` and other non-importable
    directories out; they were previously excluded only as a side effect of
    having no ``__init__.py``.
    """
    if not entry.is_dir() or not entry.name.isidentifier():
        return False
    if (entry / "__init__.py").is_file():
        return True
    return any(entry.rglob("*.py"))


_OWNED_PACKAGES = sorted(
    entry.name for entry in _EXPECTED_SRC.iterdir() if _is_shipped_package_dir(entry)
)


def _resolved_origin(name: str) -> Path | None:
    """Where would ``import <name>`` land? None if it would not resolve."""
    module = sys.modules.get(name)
    origin = getattr(module, "__file__", None) if module is not None else None
    if origin is None:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            return None
        origin = spec.origin if spec is not None else None
    return Path(origin).resolve() if origin else None


_foreign = []
for _name in _OWNED_PACKAGES:
    _origin = _resolved_origin(_name)
    # A package that does not resolve at all is an absence, not a provenance
    # fault — anything importing it fails loudly on its own.
    if _origin is None:
        continue
    if not any(provider in _origin.parents for provider in _PROVIDERS):
        _foreign.append(f"{_name} -> {_origin}")

if _foreign:
    raise RuntimeError(
        "import-provenance: "
        + "; ".join(_foreign)
        + f" — not provided by this checkout ({_EXPECTED_SRC} or {_TESTS_DIR}). "
        "Green here would describe a different tree. Create a .venv for this "
        f"worktree, or run with PYTHONPATH={_EXPECTED_SRC}."
    )


def _escape_message(problems: list[str]) -> str:
    # The "import-provenance:" prefix belongs HERE, not at the call sites: it is
    # the string operators and tests grep for, and having only one caller add it
    # meant the other refused with an unlabelled message.
    return (
        "import-provenance: "
        + "; ".join(problems)
        + " — the suite would report on code this checkout does not provide."
    )


def _inside_checkout(path: Path) -> bool:
    return path in _PROVIDERS or any(
        provider in path.parents for provider in _PROVIDERS
    )


def _provenance_failures() -> list[str]:
    """Two things must hold for every owned package that actually got imported.

    1. Every ``__path__`` entry is inside this checkout. A shim is free to append
       a directory anywhere, which would execute foreign submodules while the
       package still looks local.

    2. This checkout's own ``src/<pkg>`` is genuinely in play — either as the
       package's origin, or as one of its ``__path__`` entries. Requirement (1)
       alone is not enough: a stale, non-shim ``tests/<pkg>/__init__.py`` sits
       INSIDE the checkout and satisfies (1) while wholly REPLACING ``src/<pkg>``.
       That was a live false-green in this guard's first two revisions.

    Expressing (2) as "src/<pkg> must be in play" rather than as a hardcoded list
    of the five known shim names keeps the check self-maintaining: a new shim is
    accepted automatically because it extends into src, and a stale replacement is
    refused automatically because it does not.
    """
    problems = []
    for name in _OWNED_PACKAGES:
        module = sys.modules.get(name)
        if module is None:
            continue  # never imported; nothing executed from it

        search = [Path(p).resolve() for p in (getattr(module, "__path__", None) or [])]
        origin_raw = getattr(module, "__file__", None)
        origin = Path(origin_raw).resolve() if origin_raw else None

        for entry in search:
            if not _inside_checkout(entry):
                problems.append(f"{name}.__path__ -> {entry} (outside this checkout)")

        expected = (_EXPECTED_SRC / name).resolve()
        in_play = expected in search or (
            origin is not None and expected in origin.parents
        )
        if not in_play:
            problems.append(
                f"{name} does not load from {expected} "
                f"(origin={origin}, __path__={[str(s) for s in search]})"
            )
    return problems


def pytest_collection_modifyitems(session, config, items):
    """Second-stage provenance: where submodules will actually load from.

    The import-time check at the top of this file validates where each package's
    ``__init__`` lives. That is not sufficient on its own, because a
    namespace-extension shim under ``tests/`` extends ``__path__`` when it
    executes — after that check has already passed. A shim pointing outside the
    checkout yields a package that looks local at the top level while its
    submodules execute foreign code.

    This hook catches the common case, where a collected test module imports the
    package at module level so the shim runs during collection. It is NOT
    sufficient alone: a test that imports inside its function body has not run
    the shim yet at this point, and ``sys.modules`` is still empty. That gap is
    covered by ``pytest_sessionfinish`` below, which fails the run afterwards.
    """
    problems = _provenance_failures()
    if problems:
        raise pytest.UsageError(_escape_message(problems))


def pytest_sessionfinish(session, exitstatus):
    """Catch shims that only executed once tests were running.

    A test importing a package inside its function body extends ``__path__``
    after collection, so the hook above cannot see it. Re-checking here cannot
    prevent those tests from running, but it CAN stop the run reporting success —
    which is the property that matters. A green result is the thing this guard
    exists to withhold.
    """
    problems = _provenance_failures()
    if problems:
        session.exitstatus = 4
        print("\n" + _escape_message(problems))


@pytest.fixture(autouse=True)
def isolate_bridge_unit_tests_from_operator_audit_bus(monkeypatch, request):
    """Never let Bridge unit tests inherit an operator's production audit URL.

    Bridge construction resolves ``ARB_MEMORY_REDIS_URL`` from the process
    environment.  Developer machines legitimately export that URL so panel
    votes can reach the production audit plane, but ordinary Bridge tests must
    remain hermetic.  Apply this to the whole Bridge-test family so new sibling
    modules are contained automatically instead of relying on per-class setup.
    """

    module_file = Path(request.module.__file__).name
    scrub_bridge = module_file.startswith("test_bridge") or module_file in {
        "test_agent_sdk_bridge_integration.py",
        "test_transcript_flusher.py",
    }
    # Slice 1d-iii consumers of BRIDGE_EXEMPT_* must not inherit ambient operator
    # shell values either (P2-9).
    scrub_exempt = module_file in {
        "test_exempt_git.py",
        "test_seat_preflight.py",
    }
    if scrub_bridge:
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "")
        # Claim-gate credentials are process-env secrets. Scrub ambient values so
        # an operator canary shell cannot turn every bridge unit fixture into a
        # Postgres client or make security tests depend on ambient DSNs.
        for key in (
            "BRIDGE_CLAIM_GATE",
            "ARB_GATE_READER_DSN",
            "ARB_GATE_READER_ROLE",
            "ARB_GATE_LANE_WRITER_DSN",
            "ARB_GATE_LANE_WRITER_ROLE",
            "BRIDGE_WORKTREE_LANE",
            "BRIDGE_EXEMPT_GIT_SSH_COMMAND",
            "BRIDGE_EXEMPT_GIT_KEY_FINGERPRINT",
            "BRIDGE_EXEMPT_PROVISIONING_LEDGER",
            "BRIDGE_EXEMPT_GIT_REMOTE_URL",
            "ARB_MEMORY_DSN",
            "ARB_MEMORY_MCP_DSN",
            # Seat-shaped knobs, not secrets, but they change bridge BEHAVIOUR
            # the bridge fixtures assert on: BRIDGE_ROLE_PROFILE_FILE makes the
            # bridge wrap first-turn prompts in <system_guidance>, and
            # BRIDGE_MAX_PARALLEL changes pool sizing. Running the suite from a
            # shell that has a live seat's environment sourced therefore
            # false-failed 22 bridge tests, and the failures read as product
            # defects rather than as ambient leakage. Observed for real: the
            # 2026-08-03 review panel exported both to launch its seats, and two
            # independent reviewers had to isolate the same phantom before their
            # suite runs went green. Run
            # panel-omp-opencode-arc-20260803T125825Z-570c21.
            "BRIDGE_ROLE_PROFILE_FILE",
            "BRIDGE_MAX_PARALLEL",
        ):
            monkeypatch.delenv(key, raising=False)
    elif scrub_exempt:
        for key in (
            "BRIDGE_WORKTREE_LANE",
            "BRIDGE_EXEMPT_GIT_SSH_COMMAND",
            "BRIDGE_EXEMPT_GIT_KEY_FINGERPRINT",
            "BRIDGE_EXEMPT_PROVISIONING_LEDGER",
            "BRIDGE_EXEMPT_GIT_REMOTE_URL",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "GIT_CONFIG_COUNT",
        ):
            monkeypatch.delenv(key, raising=False)
        # Drop any ambient GIT_CONFIG_KEY_n / VALUE_n overrides.
        for key in list(__import__("os").environ):
            if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
                monkeypatch.delenv(key, raising=False)
