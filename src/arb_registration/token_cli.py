from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import timedelta
import json
import os
from pathlib import Path
import re

from .store import RegistrationStore, TokenError


def parse_ttl(value: str) -> timedelta:
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", value)
    if not match:
        raise argparse.ArgumentTypeError("ttl must look like 30m, 24h, or 7d")
    seconds = {"m": 60, "h": 3600, "d": 86400}[match.group(2)] * int(match.group(1))
    return timedelta(seconds=seconds)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="seat-token")
    ap.add_argument("--store", type=Path, default=Path(os.environ.get("ARB_REGISTRAR_STORE", "/var/lib/arb-seat-registrar/registrar.sqlite3")))
    sub = ap.add_subparsers(dest="command", required=True)
    mint = sub.add_parser("mint")
    mint.add_argument("--name", required=True)
    mint.add_argument("--host", required=True)
    mint.add_argument("--ttl", type=parse_ttl, default=parse_ttl("24h"))
    sub.add_parser("list")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("token_id")
    return ap


def main() -> None:
    args = parser().parse_args()
    store = RegistrationStore(args.store)
    try:
        if args.command == "mint":
            token, record = store.mint(args.name, args.host, args.ttl)
            print(json.dumps({**asdict(record), "token": token}, indent=2))
        elif args.command == "list":
            print(json.dumps([asdict(item) for item in store.list_tokens()], indent=2))
        else:
            store.revoke(args.token_id)
            print(json.dumps({"token_id": args.token_id, "status": "revoked"}))
    except TokenError as exc:
        raise SystemExit(f"seat-token: {exc}") from exc


if __name__ == "__main__":
    main()
