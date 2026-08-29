"""Merge/close PreToolUse gate — refusal layer with injected evidence resolver.

The gate returns no opinion ({}) for anything it does not gate and for gated
actions whose evidence resolves; it never emits an explicit allow, so the
ambient permission layers keep their authority. Refusals are denies with
specific codes:

    merge-close-evidence-unresolved   evidence resolver says not resolvable
    merge-close-evidence-check-failed resolver crashed (fail closed)

What counts as merge/close-shaped is a module-level pattern set on purpose:
the test-drive gates Bash `git merge` and arb-audit-close-request invocations;
widening the set is a doctrine decision, not a refactor.

WHAT THIS GATE ACTUALLY CATCHES — measured, 2026-08-02, after three review
panels. Read this as the enforced contract; the paragraphs above describe
intent, and intent is not enforcement.

CAUGHT (each verified by a test in the corpus in test_merge_close_gate.py):

  * `git merge` / `git pull`, in any arrangement of global flags the tokeniser
    can read: `-C <path>`, `--git-dir=…`, an absolute path to the binary.
  * A separator INSIDE quotes: `git -C "/we;rd" merge x`.
  * Compound commands, with the merge in any position, separated by
    `;` `&&` `||` `|` or an unquoted newline.
  * `gh pr merge` / `gh pr close`, including with value-taking global flags
    (`--repo o/r`, `-R o/r`).
  * `sh -c "git merge x"` / `bash -c …` — but ONLY with a bare `-c` token.
  * A git alias whose definition merges: `-c alias.ship=merge`.
  * Env-prefixed and backgrounded forms: `GIT_DIR=… git merge`, `git merge &`,
    `( git merge )`.

NOT CAUGHT — these reach a merge and the gate returns NO OPINION. Every one is
a real specimen found by a panel and reproduced, not a hypothetical:

  * A BACKSLASH LINE CONTINUATION: `git \\<newline>merge x`. The continuation
    is preserved rather than removed as a shell removes it, and shlex then
    glues a literal newline onto the next token.
  * An unquoted `#`: shlex treats it as a comment character and discards the
    rest of the line, so `echo a#b; git merge x` tokenises to `[echo, a]` and
    a real merge on the far side of the `;` is eaten.
  * COMBINED shell flags: `bash -lc "git merge x"`, `sh -ic …`. Only an exact
    `-c` token is recognised.
  * gh BOOLEAN global flags: `gh --verbose pr merge`, `gh -v pr merge`. Flag
    consumption assumes every flag takes a value, so the boolean swallows `pr`.
  * `eval "git merge x"`, command substitution (`$(echo git) merge x`,
    backticks), a merge inside a script file, and an aliased or shadowed
    binary. Resolving these needs a real shell.

NOT CAUGHT, SECOND CLASS — BY SCOPE, NOT BY PARSE FAILURE. Everything above is
the PARSING half of the exposure. The other half is that MERGE_SUBCOMMANDS is
{"merge", "pull"}: the commands below tokenise perfectly and are then simply
not recognised as integration acts at all. Measured 2026-08-02, same run that
re-derived the lists above:

  * `git rebase`, `git cherry-pick`, `git am` — integrate history into this
    branch without producing a merge commit.
  * `git reset --hard`, `git branch -f`, `git update-ref` — move a branch ref
    outright, discarding what it pointed at.
  * `git push` (and `--force`) — publishes to the authoritative remote; the
    act that makes an integration visible to everyone else.

This class is a BOUNDARY, not a bug — the pattern set is module-level on
purpose (see above) and widening it is a doctrine decision. It is recorded
here because the instruction below, to treat the NOT-CAUGHT list as live
exposure, understated that exposure by a whole class for as long as this went
unwritten. Pinned as measured fact by OUT_OF_SCOPE_INTEGRATIONS in
test_merge_close_gate.py, so movement in EITHER direction is now visible; a
widening turns those tests red, and that is a doctrine change landing rather
than a defect being fixed.

Three panels have now each found more spellings than the last, and two of the
misses above were INTRODUCED by the fix that closed the previous set. That is
a property of the approach: this module re-implements shell parsing to predict
whether a merge will happen, and the input space is a shell grammar. Treat the
CAUGHT list as the guarantee and the NOT-CAUGHT list as live exposure, not as
a to-do — closing them one specimen at a time is what produced this list.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Protocol

# Non-git gated actions still match by name; there is no subcommand to parse.
MERGE_CLOSE_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\barb-audit-close-request\b"),
)

# git subcommands that integrate one history into another. `pull` is here
# because it merges; excluding it would gate the careful spelling and wave
# through the convenient one.
MERGE_SUBCOMMANDS: frozenset[str] = frozenset({"merge", "pull"})

# git's global options, i.e. the ones that may appear BEFORE the subcommand.
# The ones taking a separate value must consume it, or the value gets mistaken
# for the subcommand. This list is why `git -C /repo merge` is now caught: the
# old `\bgit\s+merge\b` pattern only ever matched the canonical spelling, so a
# real merge one flag away from it was never gate-shaped at all (panel finding
# grok F1 / cold-Opus P1-2, reproduced before remediation).
_GIT_GLOBAL_FLAGS_WITH_VALUE: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)

# Fail-closed fallback for a command we cannot tokenise (unbalanced quote, say).
# Refusing to parse is not a reason to wave a merge through.
_FALLBACK_GIT = re.compile(r"\bgit\b")
_FALLBACK_MERGE = re.compile(r"\b(merge|pull)(?![-\w])")


def _is_git_token(token: str) -> bool:
    return token == "git" or token.endswith("/git")


def _segment_runs_a_merge(tokens: list[str]) -> bool:
    """True when this token run invokes a merge-shaped git subcommand.

    Matches the SUBCOMMAND exactly, so `git merge-base` — which reads and writes
    nothing — is no longer swept up by a word-boundary that treats `merge` as a
    complete word inside `merge-base`.
    """
    index = 0
    while index < len(tokens):
        if not _is_git_token(tokens[index]):
            index += 1
            continue
        cursor = index + 1
        while cursor < len(tokens):
            token = tokens[cursor]
            if not token.startswith("-"):
                if token in MERGE_SUBCOMMANDS:
                    return True
                # NOT a merge — keep scanning the rest of the segment for a
                # LATER git. Returning here meant `git status git merge topic`
                # (which is what a collapsed newline looks like) was judged on
                # `status` alone and the second git was never seen.
                break
            # `-c alias.ship=merge` defines an alias whose expansion merges;
            # the invocation `git ... ship` then looks like an ordinary
            # subcommand. Treat the definition itself as merge-shaped.
            if token.startswith("-c") or token == "--config-env":
                value = token[2:] if len(token) > 2 else (
                    tokens[cursor + 1] if cursor + 1 < len(tokens) else ""
                )
                if value.startswith("alias.") and any(
                    sub in value.split("=", 1)[-1].split() for sub in MERGE_SUBCOMMANDS
                ):
                    return True
            if token in _GIT_GLOBAL_FLAGS_WITH_VALUE:
                cursor += 2  # the flag and its separate value
            else:
                cursor += 1  # --flag=value, or a valueless flag
        index += 1
    return False


_SEPARATOR_TOKENS = frozenset({";", "|", "||", "&", "&&"})

# Shells that take a command as a STRING argument. `sh -c "git merge x"` is an
# ordinary form, not an exotic evasion, so the gate recurses into the string.
_STRING_COMMAND_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def _split_unquoted_newlines(command: str) -> list[str]:
    """Split on newlines that are OUTSIDE quotes, honouring backslash escapes.

    A newline separates commands exactly as `;` does, but `shlex` classes it as
    whitespace, so it never becomes a token — which is how
    `git status\\ngit merge topic` slipped the gate after the previous fix
    (the `"\\n"` entry in `_SEPARATOR_TOKENS` was dead code).

    Splitting the RAW string on newlines is not the answer either: that is the
    same mistake as splitting on `;` before tokenising, and would tear a quoted
    path containing a newline in half. So quoting state is tracked here, and
    only unquoted newlines split. An escaped newline is a line CONTINUATION and
    deliberately does not split.
    """
    parts: list[str] = []
    current: list[str] = []
    in_single = in_double = escaped = False
    for ch in command:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\" and not in_single:
            current.append(ch)
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch in "\n\r" and not in_single and not in_double:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return parts


def _tokenise(command: str) -> list[str]:
    """Tokenise a whole command, keeping shell operators as their own tokens.

    `punctuation_chars` is what makes a separator INSIDE quotes stay part of
    its token while an unquoted one becomes a token of its own.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _command_runs_a_merge(command: str) -> bool:
    """Detect a merge-shaped command anywhere in a compound command line.

    TOKENISE FIRST, then split on separator TOKENS. Splitting the raw string
    first — as this did until the 2026-08-02 panel — tore a quoted path
    containing `;`, `|` or `&&` in half, so `git -C "/we;rd" merge topic`
    became `git -C "/we` (git, no merge) and `rd" merge topic` (merge, no git).
    Neither half looked merge-shaped and the fail-closed fallback, which needs
    both words in ONE segment, never fired: the gate waved through a real merge
    on the very idiom the previous panel remediated it for.
    """
    for line in _split_unquoted_newlines(command):
        if not line.strip():
            continue
        try:
            tokens = _tokenise(line)
        except ValueError:
            # Genuinely unparseable (unbalanced quote). Fail CLOSED on the raw
            # line rather than segmenting it — segmenting an unparseable string
            # is what produced the quoted-separator hole.
            if _FALLBACK_GIT.search(line) and _FALLBACK_MERGE.search(line):
                return True
            continue

        segment: list[str] = []
        for token in tokens + [";"]:  # sentinel flushes the final segment
            if token in _SEPARATOR_TOKENS:
                if _segment_is_merge_shaped(segment):
                    return True
                segment = []
            else:
                segment.append(token)
    return False


def _segment_is_merge_shaped(tokens: list[str]) -> bool:
    return (
        _segment_runs_a_merge(tokens)
        or _segment_closes_a_pr(tokens)
        or _segment_runs_a_nested_shell_merge(tokens)
    )


def _segment_runs_a_nested_shell_merge(tokens: list[str]) -> bool:
    """`sh -c "git merge x"` — recurse into the command STRING.

    An ordinary form, not an exotic evasion, so it is covered rather than
    disclaimed. What is still NOT covered, and is not claimed to be: `eval`,
    command substitution (`$(...)`), a merge inside a script file, and an
    aliased or shadowed binary. Those need a real shell to resolve; see the
    honest-limits note in runner.py.
    """
    for index, token in enumerate(tokens):
        name = token.rsplit("/", 1)[-1]
        if name not in _STRING_COMMAND_SHELLS:
            continue
        cursor = index + 1
        while cursor < len(tokens):
            if tokens[cursor] == "-c" and cursor + 1 < len(tokens):
                if _command_runs_a_merge(tokens[cursor + 1]):
                    return True
                break
            if not tokens[cursor].startswith("-"):
                break
            cursor += 1
    return False


_GH_SUBCOMMANDS_CLOSING = frozenset({"merge", "close"})


def _segment_closes_a_pr(tokens: list[str]) -> bool:
    """`gh pr merge` / `gh pr close` — merging a PR IS the gated act.

    A merge/close gate that only knows `git merge` is not guarding the action
    it names; the orchestrator can close a PR without ever invoking git.

    Global flags between `gh` and `pr` are CONSUMED, the same way the git
    parser consumes `-C`. Requiring adjacency let `gh --repo o/r pr merge` and
    `gh -R o/r pr merge` through — the identical shape the first panel found in
    `git -C`, reintroduced one command later.
    """
    for index, token in enumerate(tokens):
        if not (token == "gh" or token.endswith("/gh")):
            continue
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor].startswith("-"):
            # `--repo o/r` takes a separate value; `--repo=o/r` does not.
            if "=" in tokens[cursor]:
                cursor += 1
            else:
                cursor += 2
        if cursor + 1 < len(tokens) and tokens[cursor] == "pr":
            if tokens[cursor + 1] in _GH_SUBCOMMANDS_CLOSING:
                return True
    return False


@dataclass(frozen=True)
class EvidenceCheck:
    """Outcome of one evidence resolution for a gated action."""

    resolvable: bool
    detail: str = ""


class EvidenceResolver(Protocol):
    def resolve(self, tool_name: str, tool_input: dict[str, Any]) -> EvidenceCheck: ...


@dataclass(frozen=True)
class GateDecision:
    """One gate outcome, in no runtime's wire format.

    `denied=False` with an empty code is NO OPINION, not an allow — the
    distinction is load-bearing on the Claude path (other permission layers
    keep their authority) and has to be re-decided by any runtime whose
    approval channel has no other layer behind it.
    """

    denied: bool
    code: str = ""
    detail: str = ""

    @property
    def reason(self) -> str:
        if not self.code:
            return ""
        return f"{self.code}: {self.detail}" if self.detail else self.code


NO_OPINION = GateDecision(denied=False)


def evaluate_merge_close(
    evidence: EvidenceResolver, tool_name: str, tool_input: dict[str, Any]
) -> GateDecision:
    """Runtime-agnostic merge/close decision — the gate's actual judgement."""
    if not _is_merge_close_shaped(tool_name, tool_input):
        return NO_OPINION
    try:
        check = evidence.resolve(tool_name, tool_input)
    except Exception as exc:
        return GateDecision(
            denied=True, code="merge-close-evidence-check-failed", detail=str(exc)
        )
    if check.resolvable:
        return NO_OPINION
    return GateDecision(
        denied=True, code="merge-close-evidence-unresolved", detail=check.detail
    )


def _is_merge_close_shaped(tool_name: str, tool_input: dict[str, Any]) -> bool:
    if tool_name != "Bash":
        return False
    command = str(tool_input.get("command", ""))
    if any(pattern.search(command) for pattern in MERGE_CLOSE_COMMAND_PATTERNS):
        return True
    return _command_runs_a_merge(command)


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def build_merge_close_gate(evidence: EvidenceResolver):
    """Adapt the runtime-agnostic decision to Claude's PreToolUse wire format."""

    async def merge_close_gate(hook_input, tool_use_id, context) -> dict[str, Any]:
        decision = evaluate_merge_close(
            evidence, hook_input["tool_name"], hook_input["tool_input"]
        )
        if not decision.denied:
            return {}
        return _deny(decision.reason)

    return merge_close_gate
