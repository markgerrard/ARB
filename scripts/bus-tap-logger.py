#!/usr/bin/env python3
"""Agent-bus tap logger — passive, token-free chatter archive.

Reads the `agent_scratch:tap` stream (dual-written by every envelope sender
alongside the real inbox RPUSH; see docs/bus-tap-logging.md) and appends each
envelope as one JSONL line to a dated log file. The stream is the short-term
buffer (MAXLEN-capped by senders); the files are the archive.

Design constraints honoured:
  - NEVER touches inbox lists (no BLPOP/LPOP anywhere) — cannot race recipients.
  - Zero LLM involvement — no token cost.
  - Restart-safe: last-read stream id persisted; XREAD resumes from it, and the
    stream's buffer covers logger downtime up to MAXLEN messages.
  - Self-rotating: one file per UTC date; files older than RETENTION_DAYS deleted
    on each date rollover (and at startup).

Env (same names the bridge/v0 scripts use; reads ENV_FILE if set):
  REDIS_HOST/REDIS_PORT/REDIS_USERNAME/REDIS_PASSWORD  (or AGENT_REDIS_* variants)
  AGENT_REDIS_DB      (default 12)
  TAP_STREAM          (default agent_scratch:tap)
  TAP_LOG_DIR         (default /var/log/agent-bus or ~/agent-bus-logs fallback)
  TAP_RETENTION_DAYS  (default 14)
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import redis


def env(*names, default=None):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def load_env_file():
    path = os.environ.get("ENV_FILE")
    if path and Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_env_file()

    host = env("AGENT_REDIS_HOST", "REDIS_HOST")
    port = int(env("AGENT_REDIS_PORT", "REDIS_PORT", default="6379"))
    user = env("AGENT_REDIS_USER", "REDIS_USERNAME", default="default")
    password = env("AGENT_REDIS_PASSWORD", "REDIS_PASSWORD")
    db = int(env("AGENT_REDIS_DB", default="12"))
    use_tls = bool(env("AGENT_REDIS_TLS", default="1"))  # managed buses are TLS; set AGENT_REDIS_TLS= (empty) for plain
    stream = env("TAP_STREAM", default="agent_scratch:tap")
    retention = int(env("TAP_RETENTION_DAYS", default="14"))

    log_dir = Path(env("TAP_LOG_DIR", default="")) if env("TAP_LOG_DIR") else None
    if log_dir is None:
        log_dir = Path("/var/log/agent-bus")
        if not os.access(log_dir.parent, os.W_OK) and not log_dir.exists():
            log_dir = Path.home() / "agent-bus-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Tap archive is plaintext bus traffic (may carry in-band secrets) — lock the
    # dir to owner+group, never world-traversable.
    try:
        os.chmod(log_dir, 0o750)
    except OSError:
        pass
    state_file = log_dir / ".last-stream-id"

    if not host or not password:
        print("FATAL: no Redis host/password in env", file=sys.stderr)
        sys.exit(78)

    r = redis.Redis(
        host=host, port=port, username=user, password=password, db=db,
        ssl=use_tls, decode_responses=True,
        socket_keepalive=True, health_check_interval=30,
    )

    last_id = state_file.read_text().strip() if state_file.exists() else "$"
    current_date = None
    fh = None

    def rotate(now_utc):
        nonlocal current_date, fh
        d = now_utc.strftime("%Y-%m-%d")
        if d != current_date:
            if fh:
                fh.close()
            current_date = d
            log_path = log_dir / f"agent-bus-{d}.log"
            fh = open(log_path, "a", buffering=1)
            # Plaintext bus archive may carry in-band secrets — never world-readable.
            try:
                os.chmod(log_path, 0o640)
            except OSError:
                pass
            cutoff = now_utc - timedelta(days=retention)
            for f in log_dir.glob("agent-bus-*.log"):
                try:
                    fdate = datetime.strptime(f.stem.replace("agent-bus-", ""), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if fdate < cutoff:
                        f.unlink()
                except ValueError:
                    continue

    rotate(datetime.now(timezone.utc))
    print(f"bus-tap-logger: stream={stream} db={db} dir={log_dir} retention={retention}d from_id={last_id}", flush=True)

    backoff = 1
    while True:
        try:
            resp = r.xread({stream: last_id}, count=200, block=30000)
            backoff = 1
            now = datetime.now(timezone.utc)
            rotate(now)
            if not resp:
                continue
            for _, entries in resp:
                for entry_id, fields in entries:
                    record = {"logged_at": now.isoformat(), "stream_id": entry_id}
                    raw = fields.get("envelope")
                    if raw:
                        try:
                            record["envelope"] = json.loads(raw)
                        except json.JSONDecodeError:
                            record["envelope_raw"] = raw
                    else:
                        record["fields"] = fields
                    fh.write(json.dumps(record, separators=(",", ":")) + "\n")
                    last_id = entry_id
            state_file.write_text(last_id)
        except redis.exceptions.ConnectionError as e:
            print(f"redis connection error: {e}; retry in {backoff}s", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except KeyboardInterrupt:
            break

    if fh:
        fh.close()


if __name__ == "__main__":
    main()
