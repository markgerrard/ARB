"""DEPRECATED — Python/Textual arb-watch.

Superseded by the Go/Bubble Tea watcher at ``tools/arb-watch-go/`` (chosen 2026-06-26 as
the maintained implementation). This module is **retained for reference, not maintained** —
it is a pure client of the same visibility gateway, so it still runs, but new work (and the
features the Go version has beyond parity: full-width header, ``e`` expand, status/agent
filters, Ctrl+C copy-range, pane focus) lands in Go. Kept so the original design/work isn't
lost. See ``tools/arb-watch-go/README.md``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

BULLET = "⏺"
BRANCH = "⎿"

# Seat age (since last_event_ts) escalation thresholds for a running seat. Past
# STALE_GRACE_S (120s in the reducer) the state itself flips to "stale"; these give
# earlier colour warning that a running seat has gone quiet.
AGE_AMBER_S = 30
AGE_RED_S = 90

# The seat timeline shows the rich transcript (model text + tool calls/output) only.
# Eval/audit lifecycle events (task/turn/command_* metadata) are suppressed — they
# duplicate the transcript or add no detail, and they sort on a different ts clock so
# the bookends clustered at the top. Transcript-only matches Claude Code's view.

# Codex wraps shell commands as `/bin/zsh -lc '<cmd>'`; show the inner command.
_SHELL_WRAPPER = re.compile(r"^\S*sh\s+-l?c\s+(.*)$", re.DOTALL)

from arb_memory.watch import sse_client
from arb_memory.watch.reducer import reduce_seat


class WatchApp(App):
    CSS = """
    Horizontal {
        height: 1fr;
    }

    #seats {
        width: 34%;
        min-width: 28;
    }

    #detail {
        width: 66%;
    }

    #seat-header {
        height: auto;
        background: $accent;
        color: $text;
        text-style: bold;
        padding: 0 2;
    }

    #timeline {
        height: 1fr;
        padding: 1 2;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("c", "copy_transcript", "Copy transcript"),
        ("t", "toggle_timestamps", "Timestamps"),
        ("l", "toggle_labels", "Labels"),
        ("f", "toggle_fullscreen", "Full width"),
        ("m", "menu", "Fleet menu"),
    ]

    def __init__(self, *, base_url: str, token: str, orchestrator: str | None = None):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.orchestrator = orchestrator
        self.seats: dict[str, dict] = {}
        self.timeline_events: list[dict] = []
        self.timeline_lines: list[str] = []
        self.show_timestamps = False
        self.show_labels = False
        self.fullscreen = False
        self.view_mode = "seats"  # "orchestrators" (root menu) | "seats"
        self._selected_task: str | None = None
        self._header_plain: str = ""
        self._known_rows: set[str] = set()
        self._seat_worker = None
        self._orch_worker = None
        self._menu_worker = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="seats")
            with Vertical(id="detail"):
                yield Static("", id="seat-header")
                yield RichLog(id="timeline", wrap=True, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "arb-watch"
        self.query_one("#seats", DataTable).cursor_type = "row"
        self.set_interval(2.0, self._tick_ages)  # wall-clock tick so quiet seats surface
        if self.orchestrator:
            self.enter_seats_view(self.orchestrator)
        else:
            self.enter_orchestrators_view()

    def _tick_ages(self) -> None:
        if self.view_mode != "seats":
            return
        now = datetime.now(timezone.utc)
        table = self.query_one("#seats", DataTable)
        for task_id, seat in self.seats.items():
            if task_id in self._known_rows:
                try:
                    table.update_cell(task_id, "age", _age_text(seat, now=now))
                except Exception:
                    pass
        self._update_seat_header()  # keep the header's live age/state fresh

    def _reset_table(self, columns: tuple[tuple[str, str], ...]) -> None:
        table = self.query_one("#seats", DataTable)
        table.clear(columns=True)
        for label, key in columns:
            table.add_column(label, key=key)

    def action_menu(self) -> None:
        """Go to root: list every orchestrator session in the fleet."""
        self.enter_orchestrators_view()

    def enter_orchestrators_view(self) -> None:
        for worker in (self._seat_worker, self._orch_worker):
            if worker is not None:
                worker.cancel()
        self.view_mode = "orchestrators"
        self.orchestrator = None
        self.seats.clear()
        self._known_rows.clear()
        self._selected_task = None
        self.timeline_events.clear()
        self.timeline_lines.clear()
        self.query_one("#timeline", RichLog).clear()
        self._update_seat_header()
        self.sub_title = "fleet · select an orchestrator session"
        self._reset_table((("Orchestrator session", "orchestrator"),))
        self._menu_worker = self.load_orchestrators()

    @work(name="menu", group="nav", exclusive=True)
    async def load_orchestrators(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{self.base_url}/orchestrators",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
        orchestrators = response.json().get("orchestrators", [])
        table = self.query_one("#seats", DataTable)
        if not orchestrators:
            self.query_one("#timeline", RichLog).write("No orchestrator sessions found in the fleet.")
            return
        for name in orchestrators:
            table.add_row(name, key=name)

    def enter_seats_view(self, orchestrator: str) -> None:
        if self._menu_worker is not None:
            self._menu_worker.cancel()
        if self._orch_worker is not None:
            self._orch_worker.cancel()
        self.view_mode = "seats"
        self.orchestrator = orchestrator
        self.seats.clear()
        self._known_rows.clear()
        self._selected_task = None
        self.timeline_events.clear()
        self.timeline_lines.clear()
        self.query_one("#timeline", RichLog).clear()
        self._update_seat_header()
        self.sub_title = f"{orchestrator} · select a seat  (m: fleet menu)"
        self._reset_table(
            (("Seat", "seat_id"), ("State", "state"), ("Age", "age"), ("Run", "run_id"))
        )
        self._orch_worker = self.watch_orchestrator(orchestrator)

    @work(name="orchestrator", group="orchestrator", exclusive=True)
    async def watch_orchestrator(self, orchestrator: str) -> None:
        url = f"{self.base_url}/sse/orchestrator/{quote(orchestrator, safe='')}"
        async for frame in sse_client.stream(url, self.token):
            self.upsert_seat(frame)

    def upsert_seat(self, frame: dict) -> None:
        data = frame.get("data") or {}
        task_id = data.get("task_id")
        if not task_id:
            return

        previous = self.seats.get(task_id, {})
        seat = dict(data) if "state" in data else reduce_seat(previous, data)
        self.seats[task_id] = seat

        table = self.query_one("#seats", DataTable)
        values = (
            seat.get("seat_id") or "",
            seat.get("state") or "",
            _age_text(seat, now=datetime.now(timezone.utc)),
            seat.get("run_id") or "",
        )
        if task_id not in self._known_rows:
            table.add_row(*values, key=task_id)
            self._known_rows.add(task_id)
        else:
            for column, value in zip(("seat_id", "state", "age", "run_id"), values, strict=True):
                table.update_cell(task_id, column, value)
        if task_id == self._selected_task:
            self._update_seat_header()  # responsive state/run refresh for the open seat

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        if self.view_mode == "orchestrators":
            self.enter_seats_view(key)  # drill from fleet menu into this orchestrator's seats
        else:
            self.select_seat(key)

    def select_seat(self, task_id: str) -> None:
        if self._seat_worker is not None:
            self._seat_worker.cancel()
        self._selected_task = task_id
        self.timeline_events.clear()
        self.timeline_lines.clear()
        self.query_one("#timeline", RichLog).clear()
        self._update_seat_header()
        self._seat_worker = self.watch_seat(task_id)

    def _update_seat_header(self) -> None:
        header = self.query_one("#seat-header", Static)
        seat = self.seats.get(self._selected_task) if self._selected_task else None
        text = _seat_header_text(seat, now=datetime.now(timezone.utc)) if seat else Text("")
        self._header_plain = text.plain  # exposed for tests (Static has no public content getter)
        header.update(text)

    @work(name="seat", group="seat", exclusive=True)
    async def watch_seat(self, task_id: str) -> None:
        url = f"{self.base_url}/sse/seat/{quote(task_id, safe='')}"
        async for frame in sse_client.stream(url, self.token):
            self.append_timeline(frame)

    def append_timeline(self, frame: dict) -> None:
        if _is_lifecycle_noise(frame):
            return
        self.timeline_events.append(frame)
        text = self._render_event(frame)
        self.timeline_lines.append(text.plain)
        log = self.query_one("#timeline", RichLog)
        log.write(text)
        log.write("")  # blank line between entries (Claude-Code spacing)

    def _render_event(self, frame: dict) -> Text:
        """Render one frame Claude-Code style, honoring the timestamp/label toggles.

        Returns a Rich Text (styled spans) — `.plain` is the clean copy string, so the
        clipboard never gets markup tags. ⏺ marks a model action/tool call, ⎿ its output.
        """
        data = frame.get("data") or {}
        text = Text()
        if self.show_timestamps:
            ts = data.get("ts") or data.get("sent_at") or ""
            if ts:
                text.append(ts + " ", style="dim")
        if self.show_labels:
            source = data.get("source") or frame.get("event") or "event"
            kind = data.get("kind") or data.get("event_type") or ""
            label = " ".join(part for part in (source, kind) if part)
            if label:
                text.append(label + " ", style="dim cyan")
        if data.get("source") == "transcript":
            self._append_transcript_body(text, data)
        else:
            event_type = data.get("event_type") or frame.get("event") or ""
            text.append("· ", style="dim")
            text.append(event_type, style="dim")
        return text

    def _append_transcript_body(self, text: Text, data: dict) -> None:
        kind = data.get("kind") or ""
        content = (data.get("content") or "").strip()
        tool_name = (data.get("tool_name") or "").strip()
        meta = data.get("meta") or {}
        if kind == "model_text":
            text.append(BULLET + " ", style="bold green")
            text.append(content)
        elif kind == "model_thinking":
            text.append(BULLET + " ", style="dim")
            text.append(content or "thinking", style="dim italic")
        elif tool_name == "apply_patch" and meta.get("file"):
            text.append(BULLET + " ", style="bold magenta")
            text.append(f"Update({meta.get('file')}) ", style="magenta")
            text.append(f"+{_to_int(meta.get('added'))}/-{_to_int(meta.get('removed'))}", style="dim")
            if content:
                text.append("\n  ")
                text.append(BRANCH + " ", style="dim")
                text.append(content, style="dim")
        elif kind in ("command_started", "tool_call"):
            text.append(BULLET + " ", style="bold cyan")
            text.append(_format_command(tool_name or content), style="cyan")
        elif kind in ("command_output", "command_finished", "tool_output"):
            text.append("  ")
            text.append(BRANCH + " ", style="dim")
            body = (content or tool_name).rstrip("\n")
            text.append(body.replace("\n", "\n    "), style="dim")  # indent continuation lines
        else:
            text.append(BULLET + " ")
            text.append(content)

    def _rerender_timeline(self) -> None:
        log = self.query_one("#timeline", RichLog)
        log.clear()
        self.timeline_lines = []
        for frame in self.timeline_events:
            text = self._render_event(frame)
            self.timeline_lines.append(text.plain)
            log.write(text)
            log.write("")  # blank line between entries (Claude-Code spacing)

    def action_toggle_timestamps(self) -> None:
        """Toggle the leading timestamp on every transcript line and re-render."""
        self.show_timestamps = not self.show_timestamps
        self._rerender_timeline()
        self.notify(f"Timestamps {'on' if self.show_timestamps else 'off'}.")

    def action_toggle_labels(self) -> None:
        """Toggle the source/kind labels (the ⏺/⎿ markers stay) and re-render."""
        self.show_labels = not self.show_labels
        self._rerender_timeline()
        self.notify(f"Labels {'on' if self.show_labels else 'off'}.")

    def action_toggle_fullscreen(self) -> None:
        """Hide the seat list so the transcript fills the width; toggle back."""
        self.fullscreen = not self.fullscreen
        seats = self.query_one("#seats", DataTable)
        detail = self.query_one("#detail", Vertical)
        seats.display = not self.fullscreen
        detail.styles.width = "100%" if self.fullscreen else "66%"
        self.notify(f"Full-width transcript {'on' if self.fullscreen else 'off'}.")

    def action_copy_transcript(self) -> None:
        """Copy the selected seat's full transcript to the clipboard via OSC-52.

        Uses the clean per-line buffer the app already keeps (not a screen scrape),
        so the copy is the whole transcript, pane-scoped, with no repaint/whitespace
        artifacts. OSC-52 reaches the local clipboard over a transparent transport
        (ssh); it is truncated over mosh — see `--dump` for a transport-agnostic path.
        """
        if not self.timeline_lines:
            self.notify("No transcript selected — pick a seat first.", severity="warning")
            return
        text = "\n\n".join(self.timeline_lines)
        self.copy_to_clipboard(text)
        self.notify(f"Copied transcript ({len(self.timeline_lines)} lines) to clipboard.")


def _is_lifecycle_noise(frame: dict) -> bool:
    """True for any non-transcript (eval/audit lifecycle) frame — kept out of the
    transcript timeline so it reads as pure model+tool work, Claude-Code style."""
    data = frame.get("data") or {}
    return data.get("source") != "transcript"


_AGENT_LABELS = {"codex": "codex", "agy": "agy", "pi": "pi", "gemini": "gemini",
                 "cursor": "cursor", "grok": "grok", "kimi": "kimi", "claude": "claude"}


def _agent_of(seat_id: str) -> str:
    """Derive the agent/engine family from the seat-id convention (codex-…, agy-…, pi-…)."""
    head = (seat_id or "").split("-", 1)[0].lower()
    return _AGENT_LABELS.get(head, head or "?")


def _seat_header_text(seat: dict, *, now: datetime) -> Text:
    """One-line seat metadata for the detail-pane header: name · agent · model? · run · state · age."""
    text = Text()
    seat_id = seat.get("seat_id") or "?"
    text.append(seat_id, style="bold")
    text.append(f"  ·  {_agent_of(seat_id)}")
    model = seat.get("model") or seat.get("engine_model")  # shown only if captured upstream
    if model:
        text.append(f"  ·  {model}")
    text.append(f"  ·  run {seat.get('run_id') or '—'}")
    state = seat.get("state")
    if state:
        text.append("  ·  ")
        text.append(state, style="dim")
    age = _age_text(seat, now=now)
    if age.plain not in ("", "—"):
        text.append("  ·  ")
        text.append_text(age)
    return text


def _parse_ts(value):
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _fmt_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _age_text(seat: dict, *, now: datetime) -> Text:
    """Styled 'time since last_event_ts'. Escalates dim→amber→red for a *running*
    seat that's gone quiet; terminal/stale seats show their age dimmed (informational)."""
    ts = _parse_ts(seat.get("last_event_ts"))
    if ts is None:
        return Text("—", style="dim")
    age = (now - ts).total_seconds()
    label = _fmt_age(age)
    if seat.get("state") != "running":
        return Text(label, style="dim")
    if age >= AGE_RED_S:
        return Text(label, style="bold red")
    if age >= AGE_AMBER_S:
        return Text(label, style="yellow")
    return Text(label, style="dim")


def _format_command(raw: str) -> str:
    """Render a tool/command label Claude-Code style: strip the `/bin/zsh -lc '…'`
    shell wrapper and show `Bash(<inner command>)`; leave non-shell tools as-is."""
    raw = (raw or "").strip()
    match = _SHELL_WRAPPER.match(raw)
    if not match:
        return raw
    inner = match.group(1).strip()
    if len(inner) >= 2 and inner[0] in "'\"" and inner[-1] == inner[0]:
        inner = inner[1:-1]
    return f"Bash({inner})"


def _to_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="arb-watch")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.environ.get("ARB_VISIBILITY_TOKEN"))
    parser.add_argument("--orchestrator")
    parser.add_argument(
        "--no-mouse",
        action="store_true",
        help="Start without mouse capture so the terminal's native click-drag text "
        "selection works in every pane (use the arrow keys to move the seat cursor). "
        "Helpful for copy/paste over mosh/iTerm2.",
    )
    args = parser.parse_args(argv)
    if not args.token:
        parser.error("--token or ARB_VISIBILITY_TOKEN is required")
    _print_deprecation_notice()
    WatchApp(base_url=args.base_url, token=args.token, orchestrator=args.orchestrator).run(
        mouse=not args.no_mouse
    )


def _print_deprecation_notice() -> None:
    """Warn that this Python TUI is deprecated in favour of the Go watcher.

    Printed to stderr with a short pause so it's read before Textual's alt-screen takes over
    and clears it. Non-blocking (Ctrl-C during the pause exits); the tool still runs.
    """
    notice = (
        "\n  ⚠  arb-watch (Python/Textual) is DEPRECATED.\n"
        "     Maintained watcher: the Go/Bubble Tea version at tools/arb-watch-go/\n"
        "        cd tools/arb-watch-go && go build -o arb-watch-go . && ./arb-watch-go --help\n"
        "     This Python TUI is kept for reference only and is no longer maintained.\n"
    )
    print(notice, file=sys.stderr, flush=True)
    try:
        time.sleep(3)
    except KeyboardInterrupt:
        raise SystemExit(0)
