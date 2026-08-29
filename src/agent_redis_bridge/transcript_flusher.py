from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import queue
from typing import Any, Callable

from .redact import redact


TRUNCATED = "…‹truncated›"
CONTENT_CAP = 256 * 1024
TEXT_REQUIRED_KINDS = {"model_text", "model_thinking", "command_output"}
logger = logging.getLogger("agent_redis_bridge.transcript_flusher")


def build_trace_fields(
    *,
    run_id: str,
    task_id: str,
    turn_index: int,
    seat_id: str,
    orchestrator: str,
    item_id: str,
    seq: int,
    kind: str,
    tool_name: str,
    content: str,
    redactor: Callable[[str], str] = redact,
    content_cap: int = CONTENT_CAP,
) -> dict[str, str] | None:
    kind = str(kind or "")
    item_id = str(item_id or "")
    if not kind or not item_id:
        return None
    if not content and kind in TEXT_REQUIRED_KINDS:
        return None
    if len(content) > content_cap:
        content = content[:content_cap] + TRUNCATED
    content = redactor(content)
    tool_name = redactor(str(tool_name or ""))
    return {
        "run_id": str(run_id or ""),
        "task_id": str(task_id or ""),
        "seat_id": str(seat_id or ""),
        "orchestrator": str(orchestrator or ""),
        "turn_index": str(turn_index),
        "item_id": item_id,
        "seq": str(seq),
        "kind": kind,
        "tool_name": tool_name,
        "content": content,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@dataclass
class _PendingItem:
    run_id: str
    task_id: str
    turn_epoch: int
    seat_id: str
    orchestrator: str
    item_id: str
    seq: int
    kind: str
    tool_name: str
    chunks: list[str]


class TranscriptFlusher:
    content_cap = CONTENT_CAP

    def __init__(
        self,
        q: queue.Queue[dict[str, Any]],
        trace_redis: Any,
        prefix: str,
        *,
        redactor: Callable[[str], str] = redact,
        maxlen: int = 10000,
        poll_s: float = 0.25,
    ) -> None:
        self.q = q
        self.trace_redis = trace_redis
        self.prefix = prefix
        self.redactor = redactor
        self.maxlen = maxlen
        self.poll_s = poll_s
        self.stream = f"{prefix}arbmem:trace"
        self._pending: dict[tuple[str, int, str], _PendingItem] = {}
        self._turn_epoch: dict[str, int] = {}
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        logger.warning("transcript flusher started: stream=%s maxlen=%s", self.stream, self.maxlen)
        try:
            while not self._stop:
                try:
                    item = self.q.get(timeout=self.poll_s)
                except queue.Empty:
                    continue
                self._process_failsoft(item)
        except BaseException:  # daemon thread dying silently is exactly what hid this bug
            logger.exception("transcript flusher run loop crashed (capture will silently stop)")
            raise

    def flush_pending(self) -> None:
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            self._process_failsoft(item)
        self._flush_all()

    def _process_failsoft(self, item: dict[str, Any]) -> None:
        try:
            self._process(item)
        except Exception:
            logger.warning("transcript flusher dropped an item during process", exc_info=True)

    def _process(self, item: dict[str, Any]) -> None:
        task_id = str(item.get("task_id") or "")
        if not task_id:
            return
        if item.get("event") == "turn_end":
            self._flush_task(task_id)
            self._turn_epoch[task_id] = self._turn_epoch.get(task_id, 0) + 1
            return

        data = item.get("data")
        if not isinstance(data, dict):
            data = {}
        kind = str(item.get("kind") or data.get("kind") or item.get("event") or "")
        item_id = str(item.get("item_id") or data.get("item_id") or "")
        if not kind or not item_id:
            return
        delta = data.get("delta")
        content = data.get("content")
        text = delta if isinstance(delta, str) else content if isinstance(content, str) else ""
        if not text and kind in {"model_text", "model_thinking", "command_output"}:
            return
        seq_raw = item.get("seq") if isinstance(item.get("seq"), int) else data.get("seq")
        seq = seq_raw if isinstance(seq_raw, int) else 0
        key = (task_id, self._turn_epoch.get(task_id, 0), item_id)
        # Incremental flush: a new item_id for this task+turn means the previous
        # item(s) are complete — flush them NOW so the live view streams per item
        # instead of dumping the whole turn at turn_end. (Deltas for the SAME item_id
        # still coalesce, since they don't change the item_id.)
        #
        # ASSUMPTION: engines emit items SEQUENTIALLY within a turn (item A's deltas,
        # then item B's) — verified for codex (item/started→completed per item), agent_sdk,
        # and the ACP engines. If an item_id ever INTERLEAVED (A → B → A), A would flush at
        # B and the resumed A would write a SECOND row: content is preserved and ordered by
        # seq (no loss — it's a telemetry stream), but it'd be split. Pinned by
        # test_flusher_interleaved_item_id_splits_into_ordered_rows so a future interleaving
        # engine is a visible behaviour change, not a silent one.
        for stale in [k for k in self._pending if k[0] == task_id and k[1] == key[1] and k[2] != item_id]:
            self._write_failsoft(self._pending.pop(stale))
        pending = self._pending.get(key)
        if pending is None:
            pending = _PendingItem(
                run_id=str(item.get("run_id") or ""),
                task_id=task_id,
                turn_epoch=key[1],
                seat_id=str(item.get("seat_id") or ""),
                orchestrator=str(item.get("orchestrator") or ""),
                item_id=item_id,
                seq=seq,
                kind=kind,
                tool_name=str(data.get("tool_name") or data.get("command") or ""),
                chunks=[],
            )
            self._pending[key] = pending
        else:
            latest_tool_name = str(data.get("tool_name") or data.get("command") or "")
            if latest_tool_name:
                pending.tool_name = latest_tool_name
        pending.chunks.append(text)

    def _flush_task(self, task_id: str) -> None:
        keys = [key for key in self._pending if key[0] == task_id]
        for key in keys:
            pending = self._pending.pop(key)
            self._write_failsoft(pending)

    def _flush_all(self) -> None:
        keys = list(self._pending)
        for key in keys:
            pending = self._pending.pop(key)
            self._write_failsoft(pending)

    def _write_failsoft(self, pending: _PendingItem) -> None:
        try:
            self._write(pending)
        except Exception:
            # Evidence-store no-silent-drop: a swallowed write was permanently lost
            # (item already popped from _pending). Make it loud so it's diagnosable.
            logger.warning(
                "transcript flusher XADD failed (item lost): stream=%s kind=%s item_id=%s",
                self.stream, pending.kind, pending.item_id, exc_info=True,
            )

    def _write(self, pending: _PendingItem) -> None:
        fields = build_trace_fields(
            run_id=pending.run_id,
            task_id=pending.task_id,
            turn_index=pending.turn_epoch,
            seat_id=pending.seat_id,
            orchestrator=pending.orchestrator,
            item_id=pending.item_id,
            seq=pending.seq,
            kind=pending.kind,
            tool_name=pending.tool_name,
            content="".join(pending.chunks),
            redactor=self.redactor,
            content_cap=self.content_cap,
        )
        if fields is None:
            return
        self.trace_redis.xadd(self.stream, fields, maxlen=self.maxlen, approximate=True)
