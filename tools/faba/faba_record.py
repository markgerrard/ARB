"""Publish a FABA decision record to ARB Memory over the bus; poll its write receipt.

Contract v2: the HARNESS/driver is the only publisher — round agents never run
this and never hold the bus credential (``faba_launch.publish_and_gate`` is the
gate path; this CLI remains for operator use and diagnostics). The ``receipt``
subcommand polls the consumer's write receipt for a given request id.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))


def _redis_client():
    import redis

    return redis.from_url(os.environ["ARB_MEMORY_REDIS_URL"], decode_responses=True)


def cmd_write(args: argparse.Namespace) -> int:
    from arb_memory import bus

    content = Path(args.file).read_text(encoding="utf-8")
    ulid = bus.memory_write(
        _redis_client(),
        artefact={
            "artefact_id": args.artefact_id,
            "content": content,
            "mime": "text/markdown",
            "source": args.source,
            "author": args.author,
        },
        hints=[],
        request_id=args.request_id,
    )
    print(json.dumps({"ulid": ulid, "request_id": args.request_id, "artefact_id": args.artefact_id}))
    return 0


def poll_receipt(request_id: str, timeout: float, *, client=None, prefix=None) -> dict | None:
    """Non-destructive poll of the consumer's write receipt. None on timeout.

    Transient redis errors retry until the deadline instead of crashing the
    gate (PF9): a dropped connection mid-poll is indistinguishable from slow
    ingestion and deserves the same patience."""
    import redis as redis_lib

    from arb_memory import bus

    client = client if client is not None else _redis_client()
    key = bus.write_result_key(request_id, prefix if prefix is not None else bus.PREFIX)
    deadline = time.monotonic() + timeout
    while True:
        try:
            entries = client.lrange(key, 0, -1)
        except redis_lib.RedisError as exc:
            if time.monotonic() >= deadline:
                print(f"[faba] receipt poll ended on redis error: {exc}", file=sys.stderr)
                return None
            entries = None
        if entries:
            return json.loads(entries[0])
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def cmd_receipt(args: argparse.Namespace) -> int:
    receipt = poll_receipt(args.request_id, args.timeout)
    if receipt is None:
        print(json.dumps({"receipt": None, "request_id": args.request_id}))
        return 2
    print(json.dumps(receipt))
    return 0 if receipt.get("artefact_outcome") in {"stored", "deduped"} else 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    write = sub.add_parser("write", help="publish a decision record over the bus")
    write.add_argument("--file", required=True)
    write.add_argument("--artefact-id", required=True)
    write.add_argument("--request-id", required=True)
    write.add_argument("--author", required=True)
    write.add_argument("--source", default="faba")
    write.set_defaults(func=cmd_write)

    receipt = sub.add_parser("receipt", help="poll the write receipt for a request id")
    receipt.add_argument("--request-id", required=True)
    receipt.add_argument("--timeout", type=float, default=60.0)
    receipt.set_defaults(func=cmd_receipt)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
