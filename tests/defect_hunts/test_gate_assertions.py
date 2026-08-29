"""Gate tests must assert the refusal CODE, never a bare refusal.

WHY (2026-07-26, observed)
--------------------------
In a default-deny architecture refusal is the *ambient* outcome. Remove any single
mechanism and control usually falls through to another mechanism's refusal — so
"the gate said no" carries almost no information about whether the mechanism under
test still exists. Defence-in-depth makes each layer individually untestable by
outcome: the layers mask each other's absence.

Demonstrated: disabling `if declared_lane == "exempt":` in `claim_gate.check` left a
deny-proof green, because the envelope fell through to claim resolution and was
refused `missing_claim_ref` instead of `lane_not_armed_exempt`. Real refusal, wrong
mechanism, green suite.

Red is not the assertion. **Red-for-the-stated-reason is.**

This guard is only writable because spec §5.1 gives each mechanism a DISTINCT refusal
code. Those codes were justified on dispatcher-routing grounds; they pay for themselves
a second time here — without them, deny-proofs on this system are structurally
unwritable.

See `docs/defect-classes/refusal-is-ambient-assert-the-code.md`.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]

# Test modules that exercise a gate decision function. Add new ones here; the guard is
# opt-in by design, because "asserts is not None" is only suspicious on a gate path.
GATE_TEST_FILES = (
    "tests/test_claim_gate.py",
    "tests/test_claim_gate_deny_proof.py",
    "tests/test_bridge_claim_gate.py",
    "tests/test_mutation_sweep_expect_failed.py",
    "tests/test_changed_test_mutation_gate.py",
    "tests/test_b6_refusal_reply.py",
)

# An assertion that a refusal happened, without pinning WHICH refusal.
_BARE_REFUSAL_MARKERS = (
    "is not None",
    'ok"] is False',
    "ok'] is False",
    '["ok"] is False',
    "['ok'] is False",
    'payload"]["ok"] is False',
    "returncode == 1",
    "returncode != 0",
    "exit_code == 1",
    # B6 five-guard negatives: empty reply list is ambient refusal under layered deny.
    "replies == []",
    "replies, []",
    "len(fake.replies), 0",
)
# Any of these count as pinning the mechanism (outcome.code or error prefix).
_CODE_MARKERS = (
    ".code",
    "outcome.code",
    "actual ==",
    '.split(":", 1)[0]',
    ".split(':', 1)[0]",
    ".startswith(",
    "FAIL[",
    "DECL-",
    "SURVIVOR",
    "WRONG-MECHANISM",
    "SWEEP-REFUSED",
    "DIRTY-AFTER-SWEEP",
    "UNTRACKED-RESIDUE",
    # B6 refusal-reply pins (error_code / soak log / allowlist reason tokens).
    "error_code",
    "envelope-invalid",
    "invalid-payload-task-ref",
    "missing-id",
    "invalid-run_id",
    "drop-self-message",
    "REFUSAL_REPLY_REASONS",
    "missing-payload-message",
    "self.agent_id",
    "sender_policies",
)


def _body_has_bare_refusal(body: str) -> bool:
    return any(m in body for m in _BARE_REFUSAL_MARKERS)


def _body_pins_code(body: str) -> bool:
    return any(m in body for m in _CODE_MARKERS)


def _offending_functions(source: str) -> list[str]:
    """Return test functions asserting a bare refusal with no accompanying code check."""
    tree = ast.parse(source)
    lines = source.splitlines()
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        if _body_has_bare_refusal(body) and not _body_pins_code(body):
            offenders.append(node.name)
    return offenders


def test_gate_tests_pin_the_mechanism_not_merely_the_refusal():
    findings = {}
    for rel in GATE_TEST_FILES:
        path = REPO / rel
        assert path.exists(), f"{rel} is listed in GATE_TEST_FILES but does not exist"
        offenders = _offending_functions(path.read_text(encoding="utf-8"))
        if offenders:
            findings[rel] = offenders

    assert not findings, (
        "these gate tests assert a bare refusal without pinning the refusal code, so they "
        "would stay green if the mechanism under test were deleted and another layer "
        "refused instead: " + repr(findings)
    )


def test_the_guard_itself_is_not_vacuous():
    """Inject-revert, inline: a synthetic offender MUST be detected.

    Without this, a bug in _offending_functions (a wrong marker, a bad ast walk) would make
    the guard above vacuously green — the exact failure the guard exists to prevent, one
    level up.
    """
    offending = (
        "def test_refusal_happens():\n"
        "    outcome = gate.check(envelope)\n"
        "    assert outcome is not None\n"
    )
    assert _offending_functions(offending) == ["test_refusal_happens"]

    bridge_offender = (
        "def test_bridge_refusal():\n"
        '    assert reply["payload"]["ok"] is False\n'
    )
    assert _offending_functions(bridge_offender) == ["test_bridge_refusal"]

    compliant = (
        "def test_refusal_happens():\n"
        "    outcome = gate.check(envelope)\n"
        "    assert outcome is not None\n"
        "    assert outcome.code == gate.MISSING_CLAIM_REF\n"
    )
    assert _offending_functions(compliant) == []

    bridge_compliant = (
        "def test_bridge_refusal():\n"
        '    assert reply["payload"]["ok"] is False\n'
        '    assert error.split(":", 1)[0] == "missing_claim_ref"\n'
    )
    assert _offending_functions(bridge_compliant) == []

    gate_offender = (
        "def test_gate_refusal():\n"
        "    result = run_gate()\n"
        "    assert result.returncode == 1\n"
    )
    assert _offending_functions(gate_offender) == ["test_gate_refusal"]
    gate_compliant = gate_offender + '    assert "FAIL[scope]" in result.stdout\n'
    assert _offending_functions(gate_compliant) == []
