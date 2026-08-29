from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import uuid
from typing import Any, Callable

from ._agy_gate import assert_agy_host_allowed
from ._stdio import scrubbed_child_env
from .base import EngineError, ProgressCallback, TurnResult


logger = logging.getLogger(__name__)

USER_INPUT_STEP = 14
MODEL_TEXT_STEP = 15
TOOL_STEPS = {5, 7, 8, 9, 17, 21, 33, 101, 138}
USER_INPUT_FIELD = 19
MODEL_TEXT_FIELD = 20
MODEL_TEXT_INNER_FIELD = 1
MODEL_THINKING_INNER_FIELD = 3  # agy packs the model's reasoning here, alongside the answer in .1
TOOL_CALL_FIELD = 5
TOOL_CALL_INNER_FIELD = 4
TOOL_NAME_FIELD = 2
TOOL_NAME_FALLBACK_FIELD = 9
TOOL_ARGS_FIELD = 3
TOOL_RESULT_FIELDS = {
    "view_file": 14,
    "list_dir": 15,
    "grep_search": 13,
    "run_command": 28,
}


def read_varint(buf: bytes, i: int) -> tuple[int | None, int]:
    res = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        res |= (b & 0x7F) << shift
        shift += 7
        i += 1
        if not b & 0x80:
            return res, i
    return None, i


def get_field(blob: bytes | None, target: int) -> bytes | None:
    if blob is None:
        return None
    i = 0
    while i < len(blob):
        tag, i = read_varint(blob, i)
        if tag is None:
            return None
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = read_varint(blob, i)
            if ln is None:
                return None
            end = i + ln
            if end > len(blob):
                return None
            if fn == target:
                return blob[i:end]
            i = end
        elif wt == 0:
            _, i = read_varint(blob, i)
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            return None
    return None


def _length_delimited_fields(blob: bytes | None) -> list[tuple[int, bytes]]:
    if blob is None:
        return []
    fields: list[tuple[int, bytes]] = []
    i = 0
    while i < len(blob):
        tag, i = read_varint(blob, i)
        if tag is None:
            return []
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = read_varint(blob, i)
            if ln is None:
                return []
            end = i + ln
            if end > len(blob):
                return []
            fields.append((fn, blob[i:end]))
            i = end
        elif wt == 0:
            _, i = read_varint(blob, i)
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            return []
    return fields


def _decode_text(blob: bytes | None) -> str:
    if not blob:
        return ""
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _best_result_text(blob: bytes | None) -> str:
    if not blob:
        return ""
    run_output = get_field(get_field(blob, 21), 1)
    if run_output:
        return _decode_text(run_output).strip()
    file_content = get_field(blob, 4)
    if file_content:
        return _decode_text(file_content).strip()
    direct_result = get_field(blob, 3)
    if direct_result and len(direct_result) > 100:
        return _decode_text(direct_result).strip()
    entries = []
    for fn, val in _length_delimited_fields(blob):
        if fn == 3:
            name = _decode_text(get_field(val, 1)).strip()
            if name:
                entries.append(name)
    if entries:
        return "\n".join(entries)
    texts = _collect_text(blob)
    if not texts:
        fallback = _decode_text(blob).strip()
        return fallback if _printable_ratio(fallback) > 0.85 else ""
    return max(texts, key=len).strip()


def _collect_text(blob: bytes | None, *, depth: int = 0) -> list[str]:
    if not blob or depth > 4:
        return []
    texts: list[str] = []
    direct = _decode_text(blob)
    if direct and _printable_ratio(direct) > 0.85:
        texts.append(direct)
    for _, val in _length_delimited_fields(blob):
        texts.extend(_collect_text(val, depth=depth + 1))
    return texts


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    good = 0
    for ch in text:
        if ch.isprintable() or ch in "\n\r\t":
            good += 1
    return good / len(text)


def _is_schema_drift_error(exc: BaseException) -> bool:
    """True for permanent schema-drift SQL errors (missing column/table) vs transient
    conditions like "database is locked" / "disk I/O error" that share OperationalError."""
    msg = str(exc).lower()
    return "no such column" in msg or "no such table" in msg


@dataclass(frozen=True)
class _AgyStep:
    idx: int
    step_type: int
    status: int
    payload: bytes


class AgySqliteTranscript:
    def __init__(self, *, conversations_root: Path, before: set[Path], nonce: str, turn_id: str) -> None:
        self.conversations_root = conversations_root
        self.before = before
        self.nonce = nonce
        self.turn_id = turn_id
        self.bound_db: Path | None = None
        self.last_idx = -1
        self.disabled = False
        self.emitted_model_text = False
        self._dark_announced = False

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        """Poll the bound DB; announce darkness ONCE when the tracker disables.

        The announcement lives at this single choke point (observing the
        `disabled` flag) rather than at each disable site, so every current and
        future disable path re-blinds the bridge by construction (AGY-2 v2.1 —
        an un-emitting disable path would resurrect the false stall_detected).
        """
        events = self._poll_inner()
        if self.disabled and not self._dark_announced:
            self._dark_announced = True
            events = [*events, ("progress_channel", {"state": "dark", "reason": "tracker-disabled"})]
        return events

    def _poll_inner(self) -> list[tuple[str, dict[str, Any]]]:
        if self.disabled:
            return []
        if self.bound_db is None:
            try:
                self.bound_db = self._find_owned_db()
            except EngineError as exc:
                logger.warning("%s; disabling agy-print granular transcript", exc)
                self.disabled = True
                return []
            if self.bound_db is None:
                return []
        try:
            schema_ok = self._schema_ok(self.bound_db)
            if schema_ok is None:
                return []
            if not schema_ok:
                self.disabled = True
                return []
            events: list[tuple[str, dict[str, Any]]] = []
            for step in self._read_steps(self.bound_db, after=self.last_idx):
                self.last_idx = max(self.last_idx, step.idx)
                mapped = self._map_step(step)
                if any(name == "model_text" for name, _ in mapped):
                    self.emitted_model_text = True
                events.extend(mapped)
            return events
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, sqlite3.Error) and _is_schema_drift_error(exc):
                logger.warning("agy-print granular transcript disabled: schema drift on read: %s", exc)
                self.disabled = True
                return []
            logger.warning("agy-print granular transcript transient read failed; will retry: %s", exc)
            return []

    def drain(self) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        while True:
            batch = self.poll()
            if not batch:
                return events
            events.extend(batch)

    def _find_owned_db(self) -> Path | None:
        matches = []
        for db_path in self._candidate_dbs():
            if self._db_has_nonce(db_path):
                matches.append(db_path)
        if len(matches) > 1:
            raise EngineError("agy-print nonce matched multiple conversation databases")
        return matches[0] if matches else None

    def _candidate_dbs(self) -> list[Path]:
        if not self.conversations_root.exists():
            return []
        return sorted(
            p for p in self.conversations_root.glob("*.db") if p.is_file() and p not in self.before
        )

    def _db_has_nonce(self, db_path: Path) -> bool:
        try:
            for step in self._read_steps(db_path, after=-1):
                if step.step_type != USER_INPUT_STEP:
                    continue
                user_input = get_field(step.payload, USER_INPUT_FIELD)
                if user_input and self.nonce.encode("utf-8") in user_input:
                    return True
                if self.nonce.encode("utf-8") in step.payload:
                    return True
        except (OSError, sqlite3.Error):
            return False
        return False

    def _schema_ok(self, db_path: Path) -> bool | None:
        try:
            with contextlib.closing(self._connect(db_path)) as conn:
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='steps'"
                ).fetchone()
                if table is None:
                    logger.warning("agy-print granular transcript disabled: missing steps table in %s", db_path)
                    return False
                rows = conn.execute(
                    "SELECT idx, step_type, status, step_payload FROM steps ORDER BY idx LIMIT 20"
                ).fetchall()
        except sqlite3.Error as exc:
            if _is_schema_drift_error(exc):
                logger.warning("agy-print granular transcript disabled: schema drift in %s: %s", db_path, exc)
                return False
            logger.warning("agy-print granular transcript transient schema read failed for %s; will retry: %s", db_path, exc)
            return None
        for idx, step_type, status, payload in rows:
            step = _AgyStep(int(idx), int(step_type), int(status), bytes(payload or b""))
            if step.step_type == MODEL_TEXT_STEP and get_field(step.payload, MODEL_TEXT_FIELD) is None:
                logger.warning("agy-print granular transcript disabled: model text field missing at idx=%s", idx)
                return False
            if step.step_type in TOOL_STEPS and self._tool_call(step.payload) is None:
                logger.warning("agy-print granular transcript disabled: tool call decode failed at idx=%s", idx)
                return False
            if step.step_type == USER_INPUT_STEP and get_field(step.payload, USER_INPUT_FIELD) is None:
                logger.warning("agy-print granular transcript disabled: user input decode failed at idx=%s", idx)
                return False
        return True

    def _read_steps(self, db_path: Path, *, after: int) -> list[_AgyStep]:
        with contextlib.closing(self._connect(db_path)) as conn:
            rows = conn.execute(
                "SELECT idx, step_type, status, step_payload FROM steps WHERE idx > ? ORDER BY idx",
                (after,),
            ).fetchall()
        return [
            _AgyStep(int(idx), int(step_type), int(status), bytes(payload or b""))
            for idx, step_type, status, payload in rows
        ]

    @staticmethod
    def _connect(db_path: Path) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def _map_step(self, step: _AgyStep) -> list[tuple[str, dict[str, Any]]]:
        if step.step_type == MODEL_TEXT_STEP:
            # agy's model step multiplexes field 20: .3 = reasoning, .1 = answer text. Surface both
            # (thinking first, as it precedes the answer); either may be absent for a given step.
            events: list[tuple[str, dict[str, Any]]] = []
            thinking = self._model_thinking(step.payload)
            if thinking:
                events.append(
                    (
                        "model_thinking",
                        {
                            "delta": thinking,
                            "content": thinking,
                            "turn_id": self.turn_id,
                            "item_id": f"{self.turn_id}:thinking:{step.idx}",
                            "kind": "model_thinking",
                            "seq": step.idx,
                        },
                    )
                )
            text = self._model_text(step.payload)
            if text:
                events.append(
                    (
                        "model_text",
                        {
                            "delta": text,
                            "content": text,
                            "turn_id": self.turn_id,
                            "item_id": f"{self.turn_id}:planner:{step.idx}",
                            "kind": "model_text",
                            "seq": step.idx,
                        },
                    )
                )
            return events
        if step.step_type in TOOL_STEPS:
            call = self._tool_call(step.payload)
            if call is None:
                return []
            name, args, result = call
            base_id = f"{self.turn_id}:tool:{step.idx}"
            command = f"{name} {args}".strip()
            status, exit_code = self._command_status(step.status)
            events = [
                (
                    "command_started",
                    {
                        "command": command,
                        "content": args,
                        "status": "in_progress",
                        "exit_code": None,
                        "tool_name": name,
                        "kind": "command_started",
                        "turn_id": self.turn_id,
                        "item_id": base_id,
                        "seq": step.idx,
                    },
                ),
                (
                    "command_finished",
                    {
                        "command": command,
                        "content": status,
                        "status": status,
                        "exit_code": exit_code,
                        "tool_name": name,
                        "kind": "command_finished",
                        "turn_id": self.turn_id,
                        "item_id": base_id,
                        "seq": step.idx,
                    },
                ),
            ]
            if result:
                events.insert(
                    1,
                    (
                        "command_output",
                        {
                            "delta": result,
                            "content": result,
                            "command": command,
                            "tool_name": name,
                            "kind": "command_output",
                            "turn_id": self.turn_id,
                            "item_id": f"{base_id}:output",
                            "seq": step.idx,
                        },
                    ),
                )
            return events
        return []

    @staticmethod
    def _model_text(payload: bytes) -> str:
        return _decode_text(get_field(get_field(payload, MODEL_TEXT_FIELD), MODEL_TEXT_INNER_FIELD)).strip()

    @staticmethod
    def _model_thinking(payload: bytes) -> str:
        return _decode_text(get_field(get_field(payload, MODEL_TEXT_FIELD), MODEL_THINKING_INNER_FIELD)).strip()

    @staticmethod
    def _tool_call(payload: bytes) -> tuple[str, str, str] | None:
        inner = get_field(get_field(payload, TOOL_CALL_FIELD), TOOL_CALL_INNER_FIELD)
        if inner is None:
            return None
        name = _decode_text(get_field(inner, TOOL_NAME_FIELD) or get_field(inner, TOOL_NAME_FALLBACK_FIELD)).strip()
        args = _decode_text(get_field(inner, TOOL_ARGS_FIELD)).strip()
        if not name:
            return None
        result_field = TOOL_RESULT_FIELDS.get(name)
        result_blob = get_field(payload, result_field) if result_field is not None else None
        used_fallback = result_blob is None
        if result_blob is None:
            result_blob = _largest_non_call_field(payload)
        result = _best_result_text(result_blob)
        # Unknown/missing-field tools fall back to a best-effort guess from arbitrary
        # protobuf; gate that guess through the printable filter so low-printable content
        # reaching _best_result_text's earlier branches (e.g. field-3 direct_result) is
        # suppressed. Known tools with a mapped result field stay verbatim (low-printable
        # bytes there — binary file content, control-byte stdout — are legitimate).
        if used_fallback and result and _printable_ratio(result) <= 0.85:
            result = ""
        return name, args, result

    @staticmethod
    def _command_status(step_status: int) -> tuple[str, int]:
        if step_status == 3:
            return "DONE", 0
        return "FAILED", 1


def _largest_non_call_field(payload: bytes) -> bytes | None:
    candidates = [(fn, val) for fn, val in _length_delimited_fields(payload) if fn != TOOL_CALL_FIELD]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item[1]))[1]


class AgyPrintEngine:
    supports_thread_resume = False
    """Agy CLI adapter using ``agy --print`` one-shot mode.

    agy 1.0.0 ships no headless/ACP/app-server protocol — only ``--print``
    for non-interactive single-prompt runs. That's a downgrade vs gemini-acp
    (no streaming token deltas, no mid-turn steer, no per-tool events), but
    it is sufficient for review-style dispatches where the bridge surface
    is one prompt in, one report out.

    Model selection is optional: agy 1.0.13 accepts ``--model`` in print mode,
    so the engine forwards it only when a model is explicitly configured.
    """

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None = None,
        command: str = "agy",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        conversations_root: Path | None = None,
        poll_s: float = 0.2,
    ) -> None:
        assert_agy_host_allowed()
        self.cwd = cwd
        self.model = model
        self.command = command
        self.popen_factory = popen_factory
        self.conversations_root = conversations_root or Path.home() / ".gemini" / "antigravity-cli" / "conversations"
        self.poll_s = poll_s
        self.process_lock = threading.Lock()
        self.active_process: subprocess.Popen[str] | None = None
        self._progress_seq = 0

    def _next_progress_seq(self) -> int:
        self._progress_seq += 1
        return self._progress_seq

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
        nonce = f"AGY_PRINT_NONCE_{uuid.uuid4().hex}"
        task_with_nonce = self._task_with_nonce(task, nonce)
        args = [self.command, "--print", task_with_nonce, "--print-timeout", f"{timeout}s"]
        if self.cwd:
            # agy ignores the process cwd and falls back to its own
            # ~/.gemini/antigravity-cli/scratch workspace, so the project
            # has to be exposed explicitly via --add-dir or the agent has
            # no files to look at.
            args.extend(["--add-dir", self.cwd])
        if self.model:
            args.extend(["--model", self.model])
        if policy == "trusted":
            args.append("--dangerously-skip-permissions")

        before = self._conversation_snapshot()
        process = self.popen_factory(
            args,
            cwd=self.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=scrubbed_child_env(),
        )
        with self.process_lock:
            self.active_process = process

        if on_event is not None:
            on_event("turn_started", {"turn_id": str(process.pid)})
            # Known-dark turn states refine the bridge's default "unproven"
            # blind reason (AGY-2 v2.1). Closed enum only — no paths/exception
            # text on the bus; detail goes to the local log.
            if not self._transcript_capture_enabled():
                on_event("progress_channel", {"state": "dark", "reason": "capture-off"})
            elif not self.conversations_root.exists():
                logger.warning(
                    "agy-print conversations root missing: %s — transcript channel dark for this turn",
                    self.conversations_root,
                )
                on_event("progress_channel", {"state": "dark", "reason": "root-missing"})

        tracker = None
        stop_polling = threading.Event()
        poll_thread = None
        if on_event is not None and self._transcript_capture_enabled():
            tracker = AgySqliteTranscript(
                conversations_root=self.conversations_root,
                before=before,
                nonce=nonce,
                turn_id=str(process.pid),
            )

            def poll_transcript() -> None:
                while not stop_polling.is_set():
                    for name, data in tracker.poll():
                        on_event(name, data)
                    stop_polling.wait(self.poll_s)

            poll_thread = threading.Thread(target=poll_transcript, daemon=True)
            poll_thread.start()

        try:
            stdout, stderr = process.communicate(timeout=timeout + 30)
        except subprocess.TimeoutExpired:
            stop_polling.set()
            process.kill()
            stdout, stderr = process.communicate()
            if on_event is not None:
                on_event("turn_timeout", {"timeout": timeout})
            return TurnResult(
                ok=False,
                result=(stdout or "").strip(),
                error=f"agy --print exceeded timeout {timeout}s",
            )
        finally:
            stop_polling.set()
            if poll_thread is not None:
                poll_thread.join()
            with self.process_lock:
                self.active_process = None

        if tracker is not None and on_event is not None:
            for name, data in tracker.drain():
                on_event(name, data)

        output = (stdout or "").strip()
        # agy --print exits 0 with empty stdout when unauthenticated in a
        # non-TTY environment (it degrades silently instead of erroring),
        # so an empty turn is a failure, never a success.
        ok = process.returncode == 0 and bool(output)
        if ok and on_event is not None and not (tracker and tracker.emitted_model_text):
            turn_id = str(process.pid)
            on_event(
                "model_text",
                {
                    "delta": output,
                    "turn_id": turn_id,
                    "item_id": f"{turn_id}:text",
                    "kind": "model_text",
                    "seq": self._next_progress_seq(),
                },
            )
        if on_event is not None:
            on_event(
                "turn_completed",
                {"turn_id": str(process.pid), "ok": ok, "exit_code": process.returncode},
            )

        if ok:
            error = None
        elif process.returncode != 0:
            error = (stderr or "").strip() or f"agy --print exit code {process.returncode}"
        else:
            error = (
                "agy --print exited 0 with empty stdout — likely unauthenticated "
                "(run `agy` interactively to log in) or a silent provider failure."
            )
            stderr_text = (stderr or "").strip()
            if stderr_text:
                error = f"{error} stderr: {stderr_text}"

        return TurnResult(
            ok=ok,
            result=output or f"agy --print pid {process.pid} produced no output.",
            error=error,
        )

    @staticmethod
    def _task_with_nonce(task: str, nonce: str) -> str:
        return f"[bridge agy-print tracking nonce: {nonce}]\n{task}"

    def _conversation_snapshot(self) -> set[Path]:
        if not self.conversations_root.exists():
            return set()
        return {p for p in self.conversations_root.glob("*.db") if p.is_file()}

    @staticmethod
    def _transcript_capture_enabled() -> bool:
        return os.environ.get("ARB_TRANSCRIPT_CAPTURE", "on").lower() not in {"0", "off", "false", "no"}

    def steer(self, message: str) -> str:
        raise EngineError("agy --print does not support mid-turn steer")

    def interrupt(self) -> str:
        with self.process_lock:
            process = self.active_process
        if process is None:
            raise EngineError("no active agy --print turn to interrupt")
        process.terminate()
        return str(process.pid)

    def stop(self) -> None:
        with self.process_lock:
            process = self.active_process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
