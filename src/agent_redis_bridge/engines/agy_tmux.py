from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from typing import Any, Callable, Iterable

from ._agy_gate import assert_agy_host_allowed
from .base import EngineError, ProgressCallback, TurnResult
from ._stdio import scrubbed_child_env


IDLE_MARKER = "? for shortcuts"
BUSY_MARKER = "esc to cancel"
TOOL_IGNORE_TYPES = {"USER_INPUT", "CONVERSATION_HISTORY", "CHECKPOINT"}
MODEL_TYPE = "PLANNER_RESPONSE"


@dataclass
class _TranscriptCursor:
    offset: int = 0
    pending: str = ""


class AgyTmuxEngine:
    supports_thread_resume = False
    """Interactive agy adapter using a fresh tmux session and transcript.jsonl tail per turn."""

    consumes_role_profile = False

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None = None,
        command: str = "agy",
        tmux_command: str = "tmux",
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        poll_s: float = 0.2,
        startup_timeout_s: float = 20.0,
        brain_root: Path | None = None,
    ) -> None:
        assert_agy_host_allowed()
        self.cwd = cwd
        self.model = model
        self.command = command
        self.tmux_command = tmux_command
        self.run_command = run_command
        self.poll_s = poll_s
        self.startup_timeout_s = startup_timeout_s
        self.brain_root = brain_root or Path.home() / ".gemini" / "antigravity-cli" / "brain"
        # Private socket: a shared default server can retain a polluted global
        # environment from an earlier unscrubbed client. Isolating the server
        # makes the first new-session (with scrubbed env=) the server's root.
        self._tmux_socket = f"bridge-agy-{uuid.uuid4().hex[:12]}"
        self.healthy = True
        self.active_session: str | None = None
        self.active_brain_dir: Path | None = None
        self._cursors: dict[Path, _TranscriptCursor] = {}

    def start(self) -> None:
        return None

    def run_turn_with_progress(
        self,
        task: str,
        *,
        timeout: int = 3600,
        policy: str = "trusted",
        on_event: ProgressCallback | None,
    ) -> TurnResult:
        assert_agy_host_allowed()
        session = self._session_name()
        turn_id = session
        nonce = f"AGY_TMUX_NONCE_{uuid.uuid4().hex}"
        before = self._brain_snapshot()
        transcript_path: Path | None = None
        saw_busy = False
        saw_progress = False
        last_result = ""
        timed_out = False
        self.active_session = session

        if on_event is not None:
            on_event("turn_started", {"turn_id": turn_id})

        try:
            self._launch_session(session, policy=policy)
            self._send_key(session, "Enter")
            if not self._wait_for_marker(session, IDLE_MARKER, timeout=self.startup_timeout_s):
                self.healthy = False
                raise EngineError(f"agy-tmux startup did not reach idle marker {IDLE_MARKER!r}")
            self._send_task(session, self._task_with_nonce(task, nonce))
            self._send_key(session, "Enter")
            transcript_path = self._wait_for_owned_transcript(before, nonce=nonce, timeout=self.startup_timeout_s)
            self.active_brain_dir = self._brain_dir_for_transcript(transcript_path)

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                emitted = self.tail_transcript(transcript_path, turn_id=turn_id)
                if emitted:
                    saw_progress = True
                for name, data in emitted:
                    if name == "model_text":
                        last_result = data.get("delta") or last_result
                    if on_event is not None:
                        on_event(name, data)
                pane = self._capture_pane(session)
                if BUSY_MARKER in pane:
                    saw_busy = True
                if IDLE_MARKER in pane and (saw_busy or saw_progress):
                    break
                if not self._has_session(session):
                    self.healthy = False
                    return TurnResult(ok=False, result=last_result, error="agy-tmux session died")
                time.sleep(self.poll_s)
            else:
                timed_out = True
                self.healthy = False
                self._send_key(session, "Escape")

            # Drain final appends after the pane becomes idle or after cancel.
            drain_deadline = time.monotonic() + 2.0
            while time.monotonic() < drain_deadline:
                for name, data in self.tail_transcript(transcript_path, turn_id=turn_id):
                    if name == "model_text":
                        last_result = data.get("delta") or last_result
                    if on_event is not None:
                        on_event(name, data)
                time.sleep(self.poll_s)

            ok = not timed_out and bool(last_result.strip())
            if on_event is not None:
                on_event("turn_completed", {"turn_id": turn_id, "ok": ok})
            if timed_out:
                return TurnResult(ok=False, result=last_result.strip(), error=f"agy-tmux turn timed out after {timeout}s")
            return TurnResult(
                ok=ok,
                result=last_result.strip() or f"agy-tmux turn {turn_id} produced no result.",
                # A clean (non-timeout) turn with no PLANNER_RESPONSE used to
                # fail with error=None — illegible downstream (audit AGY-3).
                error=None if ok else f"agy completed without a final response (turn {turn_id})",
            )
        finally:
            self._cleanup(session, transcript_path)
            self.active_session = None
            self.active_brain_dir = None

    def events_from_transcript(self, path: Path, *, turn_id: str) -> Iterable[tuple[str, dict[str, Any]]]:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            yield from self._map_line(raw, turn_id=turn_id)

    def tail_transcript(self, path: Path, *, turn_id: str) -> list[tuple[str, dict[str, Any]]]:
        cursor = self._cursors.setdefault(path, _TranscriptCursor())
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as f:
            f.seek(cursor.offset)
            chunk = f.read()
            cursor.offset = f.tell()
        if not chunk:
            return []
        text = cursor.pending + chunk
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            cursor.pending = lines.pop()
        else:
            cursor.pending = ""
        events: list[tuple[str, dict[str, Any]]] = []
        for line in lines:
            events.extend(self._map_line(line, turn_id=turn_id))
        return events

    def _map_line(self, raw: str, *, turn_id: str) -> list[tuple[str, dict[str, Any]]]:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return []
        event_type = event.get("type")
        if event_type in TOOL_IGNORE_TYPES:
            return []
        step = self._step(event)
        content = event.get("content")
        if not isinstance(content, str):
            content = ""
        if event_type == MODEL_TYPE:
            if not content:
                return []
            return [
                (
                    "model_text",
                    {
                        "delta": content,
                        "content": content,
                        "turn_id": turn_id,
                        "item_id": f"{turn_id}:planner:{step}",
                        "kind": "model_text",
                        "seq": step,
                        "ts": event.get("created_at"),
                    },
                )
            ]
        if not isinstance(event_type, str) or not event_type:
            return []
        args, output = self._split_tool_content(content)
        base_id = f"{turn_id}:tool:{step}"
        return [
            (
                "command_started",
                {
                    "command": args or event_type,
                    "content": args or event_type,
                    "status": "in_progress",
                    "exit_code": None,
                    "tool_name": event_type,
                    "kind": "command_started",
                    "turn_id": turn_id,
                    "item_id": base_id,
                    "seq": step,
                    "ts": event.get("created_at"),
                },
            ),
            (
                "command_output",
                {
                    "delta": output,
                    "content": output,
                    "command": args or event_type,
                    "tool_name": event_type,
                    "kind": "command_output",
                    "turn_id": turn_id,
                    "item_id": f"{base_id}:output",
                    "seq": step,
                    "ts": event.get("created_at"),
                },
            ),
            (
                "command_finished",
                {
                    "command": args or event_type,
                    "content": str(event.get("status") or ""),
                    "status": event.get("status") or "",
                    "exit_code": 0 if event.get("status") == "DONE" else 1,
                    "tool_name": event_type,
                    "kind": "command_finished",
                    "turn_id": turn_id,
                    "item_id": base_id,
                    "seq": step,
                    "ts": event.get("created_at"),
                },
            ),
        ]

    @staticmethod
    def _step(event: dict[str, Any]) -> int:
        try:
            return int(event.get("step_index"))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _split_tool_content(content: str) -> tuple[str, str]:
        if not content:
            return "", ""
        match = re.search(r"(?is)^(?P<args>.*?)(?:\n[ \t]*)Output:\n(?P<output>.*)$", content)
        if match:
            return match.group("args").strip(), match.group("output").strip()
        return "", content.strip()

    def _launch_session(self, session: str, *, policy: str) -> None:
        args = [
            self.tmux_command,
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            "200",
            "-y",
            "50",
            "-c",
            self.cwd,
            self.command,
            "--add-dir",
            self.cwd,
        ]
        if self.model:
            args.extend(["--model", self.model])
        if policy == "trusted":
            args.append("--dangerously-skip-permissions")
        self._run(args, check=True)

    @staticmethod
    def _task_with_nonce(task: str, nonce: str) -> str:
        return f"[bridge agy-tmux tracking nonce: {nonce}]\n{task}"

    def _send_task(self, session: str, text: str) -> None:
        buffer_name = f"{session}-task"
        self._run(
            [self.tmux_command, "load-buffer", "-b", buffer_name, "-"],
            check=True,
            input_text=text,
        )
        self._run([self.tmux_command, "paste-buffer", "-d", "-b", buffer_name, "-t", session], check=True)

    def _send_key(self, session: str, key: str) -> None:
        self._run([self.tmux_command, "send-keys", "-t", session, key], check=True)

    def _capture_pane(self, session: str) -> str:
        result = self._run([self.tmux_command, "capture-pane", "-p", "-t", session], check=False)
        return result.stdout or ""

    def _has_session(self, session: str) -> bool:
        result = self._run([self.tmux_command, "has-session", "-t", session], check=False)
        return result.returncode == 0

    def _wait_for_marker(self, session: str, marker: str, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self._capture_pane(session):
                return True
            if not self._has_session(session):
                return False
            time.sleep(self.poll_s)
        return False

    def _brain_snapshot(self) -> set[Path]:
        if not self.brain_root.exists():
            return set()
        return {p for p in self.brain_root.iterdir() if p.is_dir()}

    def _wait_for_owned_transcript(self, before: set[Path], *, nonce: str, timeout: float) -> Path:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches: list[Path] = []
            for brain in self._brain_snapshot() - before:
                transcript = brain / ".system_generated" / "logs" / "transcript.jsonl"
                if transcript.exists() and self._transcript_contains_user_nonce(transcript, nonce):
                    matches.append(transcript)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                self.healthy = False
                raise EngineError("agy-tmux nonce matched multiple brain transcripts")
            time.sleep(self.poll_s)
        self.healthy = False
        raise EngineError("agy-tmux owned brain transcript did not appear")

    @staticmethod
    def _transcript_contains_user_nonce(transcript: Path, nonce: str) -> bool:
        try:
            with transcript.open("r", encoding="utf-8") as f:
                for raw in f:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "USER_INPUT" and nonce in str(event.get("content") or ""):
                        return True
        except OSError:
            return False
        return False

    @staticmethod
    def _brain_dir_for_transcript(transcript: Path) -> Path:
        return transcript.parents[2]

    def _cleanup(self, session: str, transcript_path: Path | None) -> None:
        self._run([self.tmux_command, "kill-session", "-t", session], check=False)
        brain_dir = self.active_brain_dir
        if brain_dir is None and transcript_path is not None:
            brain_dir = self._brain_dir_for_transcript(transcript_path)
        if brain_dir is not None:
            shutil.rmtree(brain_dir, ignore_errors=True)

    def _tmux_args(self, args: list[str]) -> list[str]:
        """Prefix tmux client invocations with a private -L socket."""
        if not args or args[0] != self.tmux_command:
            return args
        return [args[0], "-L", self._tmux_socket, *args[1:]]

    def _run(self, args: list[str], *, check: bool, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        # Always pass a scrubbed env: tmux new-session inherits the client env into
        # a fresh server, so omitting env= ships ARB_GATE_LANE_WRITER_* / bus creds
        # into the agy child (Slice 1d-i P1). Private -L socket ensures that server
        # is not a shared polluted default. Scrub is the daemon→engine choke point.
        result = self.run_command(
            self._tmux_args(args),
            capture_output=True,
            text=True,
            check=False,
            input=input_text,
            env=scrubbed_child_env(),
        )
        if check and result.returncode != 0:
            raise EngineError(result.stderr or f"command failed: {' '.join(args)}")
        return result

    @staticmethod
    def _session_name() -> str:
        return f"agy-tmux-{uuid.uuid4().hex[:12]}"

    def steer(self, message: str) -> str:
        raise EngineError("agy-tmux does not support mid-turn steer")

    def interrupt(self) -> str:
        session = self.active_session
        if session is None:
            raise EngineError("no active agy-tmux turn to interrupt")
        self._send_key(session, "Escape")
        return session

    def stop(self) -> None:
        session = self.active_session
        if session is not None:
            self._cleanup(session, None)
