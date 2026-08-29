from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
from uuid import uuid4

from agent_redis_bridge.redis_io import RedisCli, RedisConfig

from .bus import receive, send
from .crypto import load_or_create_key, public_identity
from .protocol import (
    DENY_EVENT, GRANT_EVENT, PROFILE_READY_EVENT, PROVISION_EVENT, REQUEST_EVENT,
    notify_envelope, signed_data,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _buzz_command() -> list[str]:
    command = os.environ.get("ARB_SEAT_BUZZ_CLI", "").strip()
    if not command:
        raise ValueError("ARB_SEAT_BUZZ_CLI is required to publish the approved profile")
    argv = shlex.split(command)
    if not argv:
        raise ValueError("ARB_SEAT_BUZZ_CLI must contain a command")
    return argv


def _profile(
    name: str, secret: str, relay_url: str, buzz_command: list[str],
    auth_tag: str | None = None,
) -> None:
    env = dict(os.environ)
    env["BUZZ_PRIVATE_KEY"] = secret
    env["BUZZ_RELAY_URL"] = relay_url
    if auth_tag is not None:
        env["BUZZ_AUTH_TAG"] = auth_tag
    proc = subprocess.run(
        buzz_command + ["users", "set-profile", "--name", name, "--about", "ARB registered seat"],
        text=True, capture_output=True, env=env,
    )
    if proc.returncode:
        raise RuntimeError(f"profile publication failed: {proc.stderr.strip()}")


def read_token(token: str | None, token_file: Path | None, token_stdin: bool = False) -> str:
    if token_file is not None:
        if token_file.stat().st_mode & 0o077:
            raise ValueError("token file must be mode 0600")
        value = token_file.read_text(encoding="utf-8").strip()
    elif token_stdin:
        value = sys.stdin.read().strip()
    else:
        value = (token or "").strip()
    if not value:
        raise ValueError("token is empty")
    return value


def main() -> None:
    ap = argparse.ArgumentParser(prog="seat-register")
    ap.add_argument("--name", required=True)
    token_group = ap.add_mutually_exclusive_group(required=True)
    token_group.add_argument("--token")
    token_group.add_argument(
        "--token-file", type=Path,
        help="read the one-time token from a mode-0600 file instead of process argv",
    )
    token_group.add_argument(
        "--token-stdin", action="store_true",
        help="read the one-time token from standard input",
    )
    ap.add_argument("--key-file", required=True, type=Path)
    ap.add_argument("--host", default=socket.getfqdn())
    ap.add_argument("--channel", action="append", default=[])
    ap.add_argument("--env-file", required=True, type=Path)
    ap.add_argument("--registrar-id", default="arb-seat-registrar")
    ap.add_argument("--reply-agent-id")
    ap.add_argument("--timeout", type=int, default=86400)
    args = ap.parse_args()

    try:
        # Validate every local prerequisite before reading a one-time token from
        # stdin or sending a request that reserves it at the registrar.
        buzz_command = _buzz_command()
        token = read_token(args.token, args.token_file, args.token_stdin)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"seat-register: {exc}") from exc

    secret = load_or_create_key(args.key_file)
    pubkey, compressed = public_identity(secret)
    reply_agent_id = args.reply_agent_id or f"seat-registration-{pubkey[:16]}"
    config = RedisConfig.from_env_file(args.env_file, {})
    redis_client = RedisCli(config).client
    client_nonce = uuid4().hex
    fields = {
        "token": token, "name": args.name, "host": args.host, "pubkey": pubkey,
        "reply_agent_id": reply_agent_id, "channels": args.channel,
        "client_nonce": client_nonce, "issued_at": _iso_now(),
    }
    data = signed_data(REQUEST_EVENT, fields, secret, compressed)
    send(redis_client, config.prefix, args.registrar_id, notify_envelope(reply_agent_id, args.registrar_id, REQUEST_EVENT, data))
    print(json.dumps({"status": "waiting_for_operator", "pubkey": pubkey, "reply_agent_id": reply_agent_id}))

    deadline = time.monotonic() + args.timeout
    request_id: str | None = None
    while time.monotonic() < deadline:
        envelope = receive(redis_client, config.prefix, reply_agent_id, timeout=min(5, max(1, int(deadline - time.monotonic()))))
        if not envelope:
            continue
        payload = envelope.get("payload", {})
        event, reply = payload.get("event"), payload.get("data")
        if (
            envelope.get("from") != args.registrar_id
            or not isinstance(reply, dict)
            or reply.get("pubkey", pubkey) != pubkey
            or reply.get("client_nonce") != client_nonce
        ):
            continue
        if event == DENY_EVENT:
            raise SystemExit(f"seat-register: denied: {reply.get('reason', 'operator denied registration')}")
        if event == PROVISION_EVENT:
            request_id = reply.get("request_id")
            relay_url = reply.get("relay_url")
            if not isinstance(request_id, str) or not isinstance(relay_url, str):
                continue
            auth_tag = reply.get("auth_tag")
            if auth_tag is not None and not isinstance(auth_tag, str):
                raise SystemExit("seat-register: provision auth_tag is not a string")
            _profile(args.name, secret, relay_url, buzz_command, auth_tag)
            ready_fields = {
                "request_id": request_id, "pubkey": pubkey, "profile_name": args.name,
                "client_nonce": client_nonce, "issued_at": _iso_now(),
            }
            ready = signed_data(PROFILE_READY_EVENT, ready_fields, secret, compressed)
            send(redis_client, config.prefix, args.registrar_id, notify_envelope(reply_agent_id, args.registrar_id, PROFILE_READY_EVENT, ready))
            print(json.dumps({"status": "profile_published", "request_id": request_id}))
        elif event == GRANT_EVENT and reply.get("request_id") == request_id:
            print(json.dumps({"status": "granted", **reply}, indent=2))
            return
    raise SystemExit("seat-register: timed out waiting for grant or denial")


if __name__ == "__main__":
    main()
