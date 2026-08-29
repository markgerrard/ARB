from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import time
from uuid import uuid4

from arb_crypto import load_privkey, unseal
from arb_secrets.cli import init_keypair
from agent_redis_bridge.redis_io import RedisCli, RedisConfig

from .bus import receive, send
from .bus_acl import (
    AclProvisionError, ROLE_NAMES, role_rules, validate_declared_roles,
)
from .bus_registrar import REGISTRAR_ID, REG_PREFIX, validate_self_report
from .crypto import load_or_create_key, public_identity
from .protocol import (
    BUS_DENY_EVENT, BUS_GRANT_EVENT, BUS_REQUEST_EVENT, notify_envelope, signed_data,
)
from .register_cli import read_token


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_mode & 0o077:
        raise ValueError("existing credentials file must be mode 0600")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _validate_bundle_role_union(
    host: str, declared_roles: tuple[str, ...], bundle_roles: object
) -> bool:
    if not isinstance(bundle_roles, dict):
        return False
    actual = set(bundle_roles)
    requested = set(role_rules(host, declared_roles))
    allowed = set(role_rules(host, ROLE_NAMES))
    return requested <= actual <= allowed


def main() -> None:
    parser = argparse.ArgumentParser(prog="bus-register")
    parser.add_argument("--name")
    parser.add_argument("--host", default=socket.gethostname().split(".", 1)[0].lower())
    parser.add_argument(
        "--roles", required=True,
        help="comma-separated subset of claude,codex,pi,worker",
    )
    token = parser.add_mutually_exclusive_group(required=True)
    token.add_argument("--token")
    token.add_argument("--token-file", type=Path)
    token.add_argument("--token-stdin", action="store_true")
    parser.add_argument("--signing-key-file", type=Path, required=True)
    parser.add_argument("--sealing-key-file", type=Path, required=True)
    parser.add_argument("--self-report", type=Path, required=True)
    parser.add_argument("--legacy-env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registrar-id", default=REGISTRAR_ID)
    parser.add_argument("--reply-agent-id")
    parser.add_argument("--prefix", default=REG_PREFIX)
    parser.add_argument("--timeout", type=int, default=86400)
    args = parser.parse_args()

    try:
        report = validate_self_report(json.loads(args.self_report.read_text()))
        declared_roles = validate_declared_roles(
            [role.strip() for role in args.roles.split(",") if role.strip()]
        )
        registration_token = read_token(args.token, args.token_file, args.token_stdin)
    except (AclProvisionError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"bus-register: {exc}") from exc

    if args.sealing_key_file.exists() and args.sealing_key_file.stat().st_mode & 0o077:
        raise SystemExit("bus-register: sealing key file must be mode 0600")
    sealing_public, sealing_fingerprint = init_keypair(args.sealing_key_file)
    signing_secret = load_or_create_key(args.signing_key_file)
    pubkey, signing_public = public_identity(signing_secret)
    host = args.host.strip().lower()
    name = (args.name or host).strip()
    reply_agent_id = args.reply_agent_id or f"bus-registration-{sealing_fingerprint[:16]}"
    nonce = uuid4().hex
    fields = {
        "token": registration_token,
        "name": name,
        "host": host,
        "pubkey": pubkey,
        "signing_pubkey": signing_public,
        "sealing_pubkey": base64.b64encode(sealing_public).decode("ascii"),
        "sealing_fingerprint": sealing_fingerprint,
        "reply_agent_id": reply_agent_id,
        "client_nonce": nonce,
        "issued_at": _now(),
        "roles": list(declared_roles),
        "self_report": report,
    }
    data = signed_data(BUS_REQUEST_EVENT, fields, signing_secret, signing_public)
    config = RedisConfig.from_env_file(args.legacy_env_file, {})
    if int(config.db) != 12:
        raise SystemExit("bus-register: legacy transport must use DB12")
    redis_client = RedisCli(config).client
    send(
        redis_client, args.prefix, args.registrar_id,
        notify_envelope(reply_agent_id, args.registrar_id, BUS_REQUEST_EVENT, data),
    )
    print(json.dumps({
        "status": "pending_operator_approval",
        "reply_agent_id": reply_agent_id,
        "declared_roles": list(declared_roles),
        "sealing_fingerprint": sealing_fingerprint,
    }, sort_keys=True))

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        envelope = receive(
            redis_client, args.prefix, reply_agent_id,
            timeout=min(5, max(1, int(deadline - time.monotonic()))),
        )
        if not envelope or envelope.get("from") != args.registrar_id:
            continue
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            continue
        event = payload.get("event")
        reply = payload["data"]
        if (
            reply.get("client_nonce") != nonce
            or reply.get("sealing_fingerprint") != sealing_fingerprint
        ):
            continue
        if event == BUS_DENY_EVENT:
            raise SystemExit(
                f"bus-register: denied: {reply.get('reason', 'operator denied registration')}"
            )
        if event != BUS_GRANT_EVENT or not isinstance(reply.get("sealed_bundle_b64"), str):
            continue
        try:
            ciphertext = base64.b64decode(reply["sealed_bundle_b64"], validate=True)
            bundle = json.loads(unseal(ciphertext, load_privkey(args.sealing_key_file)))
        except Exception as exc:
            raise SystemExit("bus-register: sealed credential reply was invalid") from exc
        if (
            bundle.get("host") != host
            or bundle.get("request_id") != reply.get("request_id")
            or bundle.get("inventory_sha256") != reply.get("inventory_sha256")
            or bundle.get("declared_roles") != list(declared_roles)
            or not _validate_bundle_role_union(
                host, declared_roles, bundle.get("roles")
            )
        ):
            raise SystemExit("bus-register: sealed credential reply did not match request")
        _write_private_json(args.output, bundle)
        print(json.dumps({
            "status": "provisioned", "request_id": bundle["request_id"],
            "credentials_file": str(args.output),
        }, sort_keys=True))
        return
    raise SystemExit("bus-register: timed out waiting for operator approval")


if __name__ == "__main__":
    main()
