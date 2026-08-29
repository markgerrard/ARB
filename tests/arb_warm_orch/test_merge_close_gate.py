"""Warm-orch test-drive slice: merge/close PreToolUse gate.

Behavioural instruction made mechanical: the orchestrator's merge/close
actions REFUSE unless the injected evidence resolver reports resolvable
evidence artefacts. The gate is a refusal layer, not an approver — when it has
no objection it returns no opinion ({}), never an explicit allow, so every
other permission layer keeps its authority. Refusals carry SPECIFIC codes
(refusal-is-ambient-assert-the-code); a bare deny would be indistinguishable
from any other layer refusing. Fail closed: a resolver crash denies too, under
its own code. One test per named behavior, red-first.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from arb_warm_orch.gates import (
    EvidenceCheck,
    build_merge_close_gate,
    evaluate_merge_close,
)


@dataclass
class StubEvidenceResolver:
    check: EvidenceCheck = field(default_factory=lambda: EvidenceCheck(resolvable=True))
    calls: list[tuple[str, dict]] = field(default_factory=list)

    def resolve(self, tool_name: str, tool_input: dict) -> EvidenceCheck:
        self.calls.append((tool_name, tool_input))
        return self.check


def _hook_input(command: str, tool_name: str = "Bash") -> dict:
    payload = {"command": command} if tool_name == "Bash" else {}
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": payload,
        "tool_use_id": "toolu_test",
    }


def _run(gate, hook_input):
    return asyncio.run(gate(hook_input, hook_input["tool_use_id"], None))


def _decision(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


def _reason(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


# ------------------------------------------------------------ no opinion


def test_non_merge_bash_command_gets_no_opinion():
    gate = build_merge_close_gate(StubEvidenceResolver())
    out = _run(gate, _hook_input("ls -la"))
    assert out == {}


def test_non_bash_tool_gets_no_opinion():
    gate = build_merge_close_gate(StubEvidenceResolver())
    out = _run(gate, _hook_input("", tool_name="Read"))
    assert out == {}


def test_resolvable_evidence_yields_no_opinion_not_explicit_allow():
    gate = build_merge_close_gate(StubEvidenceResolver(EvidenceCheck(resolvable=True)))
    out = _run(gate, _hook_input("git merge feature-x"))
    assert out == {}


# ------------------------------------------------------------- refusals


def test_git_merge_without_evidence_denies_with_specific_code():
    gate = build_merge_close_gate(
        StubEvidenceResolver(EvidenceCheck(resolvable=False, detail="no close record"))
    )
    out = _run(gate, _hook_input("git merge feature-x"))
    assert _decision(out) == "deny"
    assert "merge-close-evidence-unresolved" in _reason(out)
    assert "no close record" in _reason(out)


def test_audit_close_request_is_gated_too():
    gate = build_merge_close_gate(
        StubEvidenceResolver(EvidenceCheck(resolvable=False, detail="d"))
    )
    out = _run(gate, _hook_input("scripts/arb-audit-close-request --arc b17"))
    assert _decision(out) == "deny"
    assert "merge-close-evidence-unresolved" in _reason(out)


def test_deny_output_names_the_pre_tool_use_event():
    gate = build_merge_close_gate(
        StubEvidenceResolver(EvidenceCheck(resolvable=False))
    )
    out = _run(gate, _hook_input("git merge x"))
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_resolver_crash_fails_closed_with_its_own_code():
    class ExplodingResolver:
        def resolve(self, tool_name, tool_input):
            raise RuntimeError("store unreachable")

    gate = build_merge_close_gate(ExplodingResolver())
    out = _run(gate, _hook_input("git merge feature-x"))
    assert _decision(out) == "deny"
    assert "merge-close-evidence-check-failed" in _reason(out)
    assert "store unreachable" in _reason(out)


# ----------------------------------------------------- resolver contact


def test_resolver_not_consulted_for_unrelated_commands():
    stub = StubEvidenceResolver()
    gate = build_merge_close_gate(stub)
    _run(gate, _hook_input("pytest -q"))
    assert stub.calls == []


def test_resolver_receives_tool_name_and_input():
    stub = StubEvidenceResolver()
    gate = build_merge_close_gate(stub)
    _run(gate, _hook_input("git merge topic"))
    assert stub.calls == [("Bash", {"command": "git merge topic"})]


# ------------------------------------------------- runtime-agnostic core
#
# The Claude PreToolUse hook is one WIRE FORMAT over a decision that is not
# Claude-specific. A second runtime (codex app-server, whose approval requests
# BLOCK the harness) needs the same decision in its own wire format, so the
# decision is extracted and the hook becomes an adapter over it. These tests
# pin the core directly; the hook tests above are the regression net proving
# the extraction changed no Claude-path behaviour.


def test_core_returns_no_opinion_for_unrelated_command():
    decision = evaluate_merge_close(StubEvidenceResolver(), "Bash", {"command": "ls"})
    assert decision.denied is False
    assert decision.code == ""


def test_core_denies_unresolved_merge_with_specific_code():
    decision = evaluate_merge_close(
        StubEvidenceResolver(EvidenceCheck(resolvable=False, detail="no close record")),
        "Bash",
        {"command": "git merge topic"},
    )
    assert decision.denied is True
    assert decision.code == "merge-close-evidence-unresolved"
    assert decision.detail == "no close record"


def test_core_denies_with_its_own_code_when_resolver_crashes():
    class ExplodingResolver:
        def resolve(self, tool_name, tool_input):
            raise RuntimeError("store unreachable")

    decision = evaluate_merge_close(
        ExplodingResolver(), "Bash", {"command": "git merge topic"}
    )
    assert decision.denied is True
    assert decision.code == "merge-close-evidence-check-failed"
    assert "store unreachable" in decision.detail


def test_core_reason_joins_code_and_detail():
    decision = evaluate_merge_close(
        StubEvidenceResolver(EvidenceCheck(resolvable=False, detail="why")),
        "Bash",
        {"command": "git merge topic"},
    )
    assert decision.reason == "merge-close-evidence-unresolved: why"


def test_core_reason_is_bare_code_when_detail_is_empty():
    decision = evaluate_merge_close(
        StubEvidenceResolver(EvidenceCheck(resolvable=False)),
        "Bash",
        {"command": "git merge topic"},
    )
    assert decision.reason == "merge-close-evidence-unresolved"


# --------------------------------------- panel remediation: real merge shapes
#
# Panel finding (grok F1 / cold-Opus P1-2, reproduced by the orchestrator before
# remediation): `\bgit\s+merge\b` only matches the CANONICAL spelling. `git -C
# <path> merge` — the ordinary orchestrator idiom — returned NO OPINION, so the
# gate never fired on a real merge. The gate-tempting tests all used the bare
# form, so the suite stayed green either side of a hole. A check that exercises
# only the canonical spelling of the thing it guards is a check that cannot fail.


def _decide(command: str, resolvable: bool = False):
    return evaluate_merge_close(
        StubEvidenceResolver(EvidenceCheck(resolvable=resolvable, detail="d")),
        "Bash",
        {"command": command},
    )


def test_merge_behind_git_dash_c_path_is_gated():
    assert _decide("git -C /repo merge topic").denied is True


def test_merge_behind_git_config_override_is_gated():
    assert _decide("git -c user.name=x merge topic").denied is True


def test_merge_behind_several_global_flags_is_gated():
    assert _decide("git --no-pager -C /repo -c a=b merge topic").denied is True


def test_git_pull_is_gated_because_it_merges():
    assert _decide("git pull origin main").denied is True


def test_merge_base_is_not_a_merge():
    # False positive from the old \b boundary: `merge-base` reads nothing and
    # writes nothing, so gating it blocks legitimate inspection.
    assert _decide("git merge-base main topic").denied is False


def test_merge_in_a_chained_command_is_gated():
    assert _decide("cd /repo && git -C . merge topic").denied is True


def test_merge_after_a_semicolon_is_gated():
    assert _decide("echo hi; git merge topic").denied is True


def test_a_word_containing_merge_is_not_a_merge():
    assert _decide("git log --grep=merge").denied is False


def test_unparseable_command_still_gates_a_visible_merge():
    # shlex raises on an unbalanced quote; the gate must not fail OPEN on it.
    assert _decide('git -C "/repo merge topic').denied is True


# ---- panel panel-warmorch-4slices-20260802T100544Z-222b63 findings ----


def test_a_separator_inside_a_quoted_path_does_not_wave_the_merge_through():
    """The gate split on `;` `|` `&&` BEFORE tokenising, so a quoted path
    containing one was torn in half: `git -C "/we;rd" merge topic` became
    `git -C "/we` (git, no merge) and `rd" merge topic` (merge, no git), and
    the fail-closed fallback — which needs both words in ONE segment — never
    fired. The docstring claimed refusing to parse is not a reason to wave a
    merge through; for this shape it did exactly that.
    """
    for command in (
        'git -C "/we;rd" merge topic',
        'git -C "/a|b" merge topic',
        "git -C '/x&&y' merge topic",
    ):
        decision = evaluate_merge_close(_NeverResolvable(), "Bash", {"command": command})
        assert decision.denied, f"gate waved through: {command}"
        assert decision.code == "merge-close-evidence-unresolved"


def test_gh_pr_merge_is_merge_shaped():
    """A merge/close gate that misses the PR-merge idiom is not guarding the
    action it names — closing a PR by merging it is precisely the gated act."""
    decision = evaluate_merge_close(
        _NeverResolvable(), "Bash", {"command": "gh pr merge 42 --squash"}
    )
    assert decision.denied
    assert decision.code == "merge-close-evidence-unresolved"


def test_ordinary_commands_still_pass_after_the_tokeniser_change():
    """Guard against over-denial: the fix must not make the gate opine on
    everything, or the orchestrator cannot work."""
    for command in ("ls -la", "git status", "git merge-base a b", "echo 'merge'"):
        decision = evaluate_merge_close(_NeverResolvable(), "Bash", {"command": command})
        assert not decision.denied, f"over-denied: {command}"


class _NeverResolvable:
    def resolve(self, tool_name, tool_input):
        return EvidenceCheck(resolvable=False, detail="nothing on record")


# ---- panel panel-warmorch-remediation-20260802T121813Z-06f457 ----------
#
# The previous fix closed a quoted-separator hole and OPENED a newline one:
# `git status\ngit merge topic` was DENIED at 0fcb55cc and ALLOWED at d83ac874.
# Two seats found it independently. Root causes were stacked — shlex treats
# newline as whitespace so it never became a token (making the "\n" entry in
# _SEPARATOR_TOKENS dead code), and _segment_runs_a_merge returned on the FIRST
# git's subcommand instead of scanning on.
#
# These are written as a CORPUS rather than as the handful of spellings the
# author thought of, because "I tested the forms I imagined" is the failure
# that has now recurred three times on this gate.

MERGE_SHAPED = [
    "git merge topic",
    "git -C /repo merge topic",
    'git -C "/we;rd" merge topic',
    "git -C '/x&&y' merge topic",
    "git pull origin main",
    # compound: the merge is not the first command
    "git status; git merge topic",
    "git status && git merge topic",
    "git status | git merge topic",
    "git status\ngit merge topic",          # the regression
    "git fetch origin\ngit merge origin/main",
    "echo hi\r\ngit merge topic",           # CRLF, same class
    "git status\n\n  git merge topic",      # blank lines + indent
    "ls && git status && git merge topic",  # merge is third
    # gh, with and without global flags
    "gh pr merge 42",
    "gh pr close 42",
    "gh --repo owner/repo pr merge 42",
    "gh -R owner/repo pr close 42",
    # nested shell: an ordinary form, so it is covered rather than disclaimed
    'sh -c "git merge topic"',
    'bash -c "git merge topic"',
    '/bin/sh -c "gh pr merge 42"',
    # a git alias whose expansion merges
    "git -c alias.ship=merge ship topic",
]

NOT_MERGE_SHAPED = [
    "ls -la",
    "git status",
    "git merge-base a b",
    "git log --merges",
    "echo 'merge'",
    "git diff origin/main",
    "gh pr list",
    "gh pr view 42",
    "cat merge.txt",
    'sh -c "ls -la"',
    # a newline INSIDE quotes is data, not a command separator
    'git commit -m "line1\nline2"',
]


@pytest.mark.parametrize("command", MERGE_SHAPED)
def test_every_merge_shaped_command_is_denied(command):
    decision = evaluate_merge_close(_NeverResolvable(), "Bash", {"command": command})
    assert decision.denied, f"gate waved through: {command!r}"
    assert decision.code == "merge-close-evidence-unresolved"


@pytest.mark.parametrize("command", NOT_MERGE_SHAPED)
def test_ordinary_commands_are_not_over_denied(command):
    decision = evaluate_merge_close(_NeverResolvable(), "Bash", {"command": command})
    assert not decision.denied, f"over-denied: {command!r}"


# ---- DOCUMENTED MISSES — live exposure, pinned ------------------------------
#
# READ THIS BEFORE EDITING. Every command below REACHES A MERGE and the gate
# returns NO OPINION. These tests assert that the gate is BROKEN for these
# spellings. They are not a specification of desired behaviour and nothing here
# should be read as endorsing the outcome it asserts.
#
# Why pin a defect. The `gates.py` docstring has carried these as prose since
# 2026-08-02 while nothing in CI held them, so the module's coverage could move
# in either direction between commits without a red test. That is exactly how
# two of these arrived: the remediation that closed the previous round's set
# OPENED `gh --verbose pr merge` and `bash -lc` (panel
# panel-warmorch-rem2-20260802T131437Z-6c5276, unanimous block). Pinning the
# misses makes any change to them VISIBLE; it does not make them acceptable.
#
# Confirmed independently by all four seats of panel
# panel-gate-redesign-20260802T192602Z-3cd426 (8/8 reproduced, control denies):
# `docs/superpowers/reviews/2026-08-02-gate-redesign-panel-verdict.md`.
#
# WHEN ONE OF THESE TESTS FAILS, THAT IS PROBABLY GOOD NEWS. It means the gate
# now catches the spelling. The fix is to move that entry into MERGE_SHAPED and
# re-derive the docstring's CAUGHT / NOT-CAUGHT lists by running this corpus —
# never to relax the assertion back to green.

DOCUMENTED_MISSES = [
    # A backslash line continuation. shlex preserves the backslash rather than
    # removing it as a shell does, then glues a literal newline to the next token.
    "git \\\nmerge topic",
    # shlex treats `#` as a comment character and discards the rest of the LINE,
    # so a real merge on the far side of the `;` is eaten.
    "echo a#b; git merge topic",
    # COMBINED shell flags. Only a bare `-c` token is recognised, so any cluster
    # containing it slips past — including zsh, which _STRING_COMMAND_SHELLS
    # already lists (gates.py:140), making this a flag-parsing gap and not an
    # unsupported shell.
    'bash -lc "git merge topic"',
    'sh -ic "git merge topic"',
    'zsh -lc "git merge topic"',
    # gh BOOLEAN global flags. Flag consumption assumes every flag takes a
    # value, so the boolean swallows `pr` and the subcommand is never seen.
    "gh --verbose pr merge 12",
    "gh -v pr merge 12",
    "gh --help pr merge 12",
    # Indirection a static tokeniser cannot resolve without being a shell.
    'eval "git merge topic"',
    "$(echo git) merge topic",
]


@pytest.mark.parametrize("command", DOCUMENTED_MISSES)
def test_documented_miss_still_slips_through(command):
    """Pins TODAY's behaviour on a known hole. A failure here means the hole
    moved — see the block comment above before touching anything."""
    decision = evaluate_merge_close(_NeverResolvable(), "Bash", {"command": command})
    assert not decision.denied, (
        f"DOCUMENTED MISS IS NOW CAUGHT: {command!r}. This is likely an "
        f"improvement — move it to MERGE_SHAPED and re-derive the gates.py "
        f"docstring by running this corpus. Do not relax this assertion."
    )
    assert decision.code == "", f"unexpected code on a no-opinion path: {decision.code!r}"


def test_the_documented_miss_corpus_can_actually_fail():
    """The corpus above asserts NEGATIVES, and a negative assertion passes just
    as happily against a gate that has been deleted entirely.

    So assert the positive control in the same breath: the canonical spelling
    MUST deny, with its specific code rather than a bare refusal. Without this,
    `test_documented_miss_still_slips_through` would stay green if
    `evaluate_merge_close` were stubbed out to return NO_OPINION — the corpus
    would then be measuring nothing while reporting eleven passes.
    """
    decision = evaluate_merge_close(_NeverResolvable(), "Bash", {"command": "git merge topic"})
    assert decision.denied
    assert decision.code == "merge-close-evidence-unresolved"


# ---- OUT-OF-SCOPE INTEGRATIONS — the boundary, pinned -----------------------
#
# READ THIS BEFORE EDITING, AND DO NOT MERGE THIS CORPUS INTO DOCUMENTED_MISSES
# ABOVE. The two record different things and conflating them misreports both.
#
#   DOCUMENTED_MISSES  the gate INTENDS to catch these and fails to. A defect.
#   this corpus        the gate was never asked to recognise these at all.
#                      `gates.MERGE_SUBCOMMANDS` is `{"merge", "pull"}`, and
#                      the gates.py module docstring states the set is
#                      module-level ON PURPOSE — "widening the set is a
#                      doctrine decision, not a refactor." A BOUNDARY, not
#                      a bug. (Cited by symbol: line-pinned citations on this
#                      module have rotted twice, once while writing this.)
#
# Why pin a boundary. Every command below moves a ref or integrates history,
# and every one returns NO OPINION — measured 2026-08-02. That is defensible,
# but it was recorded nowhere: the gates.py NOT-CAUGHT list carried only the
# PARSING misses, so a reader following its instruction to "treat the NOT-CAUGHT
# list as live exposure" would have understated the exposure by a whole class.
# Pinning the boundary makes any movement in it VISIBLE — in either direction.
#
# The distinction matters most on the OTHER side. If someone widens
# MERGE_SUBCOMMANDS, these tests go red, and that is a DOCTRINE CHANGE landing,
# not a defect being fixed — it needs the owner's sign-off, not a green diff.
# The failure message says so.
#
# Provenance: cold-opus F9 of panel panel-gate-redesign-20260802T192602Z-3cd426
# ("§1's diagnosis accounts for half the exposure"), folded into
# `docs/superpowers/specs/2026-08-02-merge-close-gate-redesign-design-v2.md` §1
# as RC7. The design note is banked; this boundary record is not contingent on
# it, which is why it lands as tests rather than as a proposal.

OUT_OF_SCOPE_INTEGRATIONS = [
    # Replays commits onto a new base — integrates history without a merge.
    "git rebase origin/main",
    "git rebase --onto main topic",
    # Applies someone else's commits onto this branch.
    "git cherry-pick abc1234",
    "git am /tmp/patch.mbox",
    # Moves a branch ref outright, discarding what was there.
    "git reset --hard origin/main",
    "git branch -f main origin/main",
    "git update-ref refs/heads/main abc1234",
    # Publishes to the authoritative remote. Not an integration INTO this
    # checkout, but the act that makes one visible to everyone else.
    "git push origin main",
    "git push --force origin main",
]


@pytest.mark.parametrize("command", OUT_OF_SCOPE_INTEGRATIONS)
def test_out_of_scope_integration_gets_no_opinion(command):
    """Pins the SCOPE boundary, not a defect. See the block comment above."""
    decision = evaluate_merge_close(_NeverResolvable(), "Bash", {"command": command})
    assert not decision.denied, (
        f"SCOPE BOUNDARY MOVED: {command!r} is now gated. If MERGE_SUBCOMMANDS "
        f"was widened, that is a DOCTRINE CHANGE (gates.py:11-13) and needs the "
        f"owner's sign-off — move this entry to MERGE_SHAPED and re-derive the "
        f"gates.py docstring. Do not relax this assertion to make the diff green."
    )
    assert decision.code == "", f"unexpected code on a no-opinion path: {decision.code!r}"


def test_the_out_of_scope_corpus_can_actually_fail():
    """Same argument as the DOCUMENTED_MISSES control, sharpened.

    Nine negative assertions stay green against a deleted gate, so the corpus
    needs a positive control. `git pull` is the sharpest one available: it is
    the IN-SCOPE sibling of `git push` — near-identical shape, same tokeniser
    path, differing only in whether the subcommand is in MERGE_SUBCOMMANDS. If
    `pull` denies while `push` does not, the corpus above is measuring the scope
    set specifically, and not merely that the tokeniser failed on all of them.
    """
    denied = evaluate_merge_close(_NeverResolvable(), "Bash", {"command": "git pull origin main"})
    assert denied.denied
    assert denied.code == "merge-close-evidence-unresolved"

    waved = evaluate_merge_close(_NeverResolvable(), "Bash", {"command": "git push origin main"})
    assert not waved.denied


def test_a_merge_after_a_non_merge_git_is_still_seen():
    """The second root cause, isolated.

    `_segment_runs_a_merge` returned on the FIRST git token's subcommand, so
    once a newline collapsed `git status` and `git merge` into one segment it
    saw `status`, decided "not a merge", and never looked at the second git.
    """
    decision = evaluate_merge_close(
        _NeverResolvable(), "Bash", {"command": "git status git merge topic"}
    )
    assert decision.denied


def test_a_newline_is_a_command_BOUNDARY_not_just_whitespace():
    """Splitting on unquoted newlines is not redundant with scanning on.

    Collapsing a newline to whitespace lets a match span two DIFFERENT
    commands: `echo gh pr` followed by `merge x` is not `gh pr merge`, but a
    single flat token list cannot tell. The mutation sweep initially showed
    the split as removable — because scan-on independently covers detection —
    so this pins the behaviour the split alone provides: no false positive
    across a line boundary.
    """
    decision = evaluate_merge_close(
        _NeverResolvable(), "Bash", {"command": "echo gh pr\nmerge x"}
    )
    assert not decision.denied, "matched across a newline boundary"
