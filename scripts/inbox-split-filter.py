#!/usr/bin/env python3
"""
Stdin filter for scripts/agent-inbox-watcher output.

Reads each line from stdin. For lines matching `[inbox] <json envelope>`:
  - emits a short `[inbox-meta] id=... from=... kind=... event=... sent_at=...`
    line on stdout that ALWAYS fits inside Claude Code's Monitor
    task-notification preview (which truncates the surfaced <event> string
    at a fixed cap that long envelopes exceed);
  - writes the full envelope JSON to a per-id file at
    $AGENT_BRIDGE_INBOX_DIR/<id>.json (default /tmp/agent-bridge-inbox/)
    so the Claude session can Read it on demand without truncation.

All other lines (watcher heartbeats, errors, unparseable [inbox] lines)
pass through unchanged so nothing is dropped.

Existing Codex/Gemini bridges are unaffected — they consume Redis via
scripts/agent-redis-bridge-systemd over the engine's native protocol
and never see this stdout stream.
"""

import hashlib
import json
import os
import re
import sys

INBOX_RE = re.compile(r"^\[inbox\] (.*)$")


def fallback_id(body: str) -> str:
    return "noid-" + hashlib.sha256(body.encode()).hexdigest()[:12]


def emit_meta(env: dict) -> str:
    mid = env.get("id", "?")
    frm = env.get("from", "?")
    kind = env.get("kind", "?")
    sent = env.get("sent_at", "?")
    in_reply_to = env.get("in_reply_to", "")
    payload = env.get("payload")
    event = payload.get("event", "?") if isinstance(payload, dict) else "?"
    parts = [
        f"id={mid}",
        f"from={frm}",
        f"kind={kind}",
        f"event={event}",
        f"sent_at={sent}",
    ]
    if in_reply_to:
        parts.append(f"in_reply_to={in_reply_to}")
    return "[inbox-meta] " + " ".join(parts)


def emit(line: str) -> None:
    """Write a stdout line as a SINGLE write+flush.

    print(s) issues two writes (the string, then the newline). The
    downstream consumer's reader can chunk the pipe between those two
    writes, which surfaces as half-lines / shifted line-boundaries in
    task-notification output and per-byte file readers. One write+flush
    keeps the newline together with its line and avoids the shift.
    """
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main() -> None:
    inbox_dir = os.environ.get("AGENT_BRIDGE_INBOX_DIR", "/tmp/agent-bridge-inbox")
    os.makedirs(inbox_dir, exist_ok=True)

    for raw in sys.stdin:
        line = raw.rstrip("\n")
        m = INBOX_RE.match(line)
        if not m:
            emit(line)
            continue
        body = m.group(1)
        if not body.strip():
            # An empty body is never a valid envelope. The watcher no longer
            # produces one (a single-line redis-cli response used to become
            # an empty body via `tail -n +2`), but spooling it here would
            # still fabricate a phantom message: every empty body hashes to
            # the same id, so repeated transport faults all collapse onto one
            # file that looks like a received-but-corrupt peer message.
            # Report it as the artefact it is and spool nothing.
            emit("[inbox-meta] kind=EMPTY-BODY spooled=no note=transport-artifact-not-a-message")
            continue
        try:
            env = json.loads(body)
        except json.JSONDecodeError:
            # Unparseable envelope: spool the raw body under a content-hash id
            # and emit a meta line. Passing the raw [inbox] line through is not
            # enough — the recommended downstream grep keeps only
            # ^\[inbox-meta\] lines, so a passthrough is silently dropped.
            mid = fallback_id(body)
            try:
                with open(os.path.join(inbox_dir, f"{mid}.json"), "w") as f:
                    f.write(body + "\n")
                emit(f"[inbox-meta] id={mid} kind=UNPARSEABLE spooled=yes")
            except OSError as exc:
                emit(f"[inbox-meta] id={mid} kind=UNPARSEABLE spooled=no err={exc}")
                emit(line)
            continue
        # Malformed-but-parseable envelope without an id (seen live
        # 2026-08-05: a work_complete missing id/from/kind lost its payload):
        # spool under a content-hash id so the body always lands on disk.
        mid = env.get("id") or fallback_id(body)
        try:
            with open(os.path.join(inbox_dir, f"{mid}.json"), "w") as f:
                f.write(body + "\n")
        except OSError as exc:
            emit(f"[inbox-split] WARN: write failed for {mid}: {exc}")
        if not env.get("id"):
            env = dict(env, id=mid)
        emit(emit_meta(env))


if __name__ == "__main__":
    main()
