"""Runtime-neutral turn events — the seam both runtimes stream through.

Deliberately imports nothing from either runtime. The cross-runtime property
this codebase has held so far is that the vendor-specific surface is exactly
one module (`runner.py` for Claude, `codex_runner.py` for codex) while
`dispatch.py` and `gates.py` move between them unchanged. If these event types
lived in `runner.py`, the codex runner would have to import the Claude runner
in order to speak its own events, and that property would be gone.

The vocabulary is chosen to be expressible by both runtimes and by the ACP
wire, without being a copy of any of the three.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

# ACP's `kind` vocabulary for a tool call. Clients use it to pick an icon and
# to reason about what a call is doing; unmapped tools fall back to "other"
# rather than guessing.
_TOOL_KINDS = {
    "Bash": "execute",
    "Read": "read",
    "Write": "edit",
    "Edit": "edit",
    "Glob": "search",
    "Grep": "search",
}


def tool_kind(tool_name: str) -> str:
    return _TOOL_KINDS.get(tool_name, "other")


# Capture-time caps for the tool preview fields below. The whole transcript
# artifact is capped downstream at 18 KB (buzz-acp `turn_transcript.rs`
# MAX_ARTIFACT_BYTES), so a single `sqlcmd` dump or `git diff` would evict
# every other entry if carried whole. Trimming HERE rather than at the far end
# keeps the wire small too, and the dropped-line count travels with the text so
# the UI can say "N more lines" instead of silently cutting.
MAX_COMMAND_BYTES = 512
MAX_OUTPUT_BYTES = 2048
MAX_OUTPUT_LINES = 40


def clip_output(text: str) -> tuple[str, int]:
    """Trim tool output to the preview budget. Returns (text, dropped_lines).

    Line budget first, then bytes: a wide single-line dump must not defeat the
    line cap, and a many-line dump must not defeat the byte cap.
    """
    if not text:
        return "", 0
    lines = text.splitlines()
    kept_lines = lines[:MAX_OUTPUT_LINES]
    kept = "\n".join(kept_lines)
    encoded = kept.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        kept = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", "ignore")
        kept_lines = kept.splitlines()
    return kept, max(0, len(lines) - len(kept_lines))


@dataclass(frozen=True)
class TextDelta:
    """A chunk of assistant-visible text."""

    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    """A chunk of the model's reasoning stream (codex: reasoning summaries).

    Kept distinct from TextDelta on purpose: `turn()` joins only TextDelta into
    the reply, so reasoning can never leak into what the seat posts. The ACP
    wire maps this to `agent_thought_chunk`, which is what buzz's observer
    pipeline projects into the web-channel "Agent activity" panel — without
    this event the panel's thought view is structurally empty for codex seats.
    """

    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """A tool call has begun.

    Emitting this is load-bearing rather than decorative: buzz's idle deadline
    defaults to 900s and is reset by exactly this update, while a seat dispatch
    is allowed 1800s. See (internal orchestration log).
    """

    tool_call_id: str
    title: str
    kind: str
    # The invocation itself (a shell command, a file path, compact JSON args),
    # already clipped to MAX_COMMAND_BYTES. Optional: engines whose tool events
    # carry no arguments leave it None and the UI shows the title alone.
    command: str | None = None


@dataclass(frozen=True)
class ToolCallCompleted:
    """A tool call has finished. `status` distinguishes success from failure —
    collapsing both into "completed" would make a failing tool invisible."""

    tool_call_id: str
    status: str
    # Preview of what the tool produced, clipped via clip_output. Optional for
    # the same reason as `command`.
    output: str | None = None
    # How many lines clip_output dropped. Carried separately so the UI can say
    # "N more lines" — a truncation the reader can see beats a silent cut.
    output_dropped_lines: int = 0


TurnEvent = Union[TextDelta, ReasoningDelta, ToolCallStarted, ToolCallCompleted]
