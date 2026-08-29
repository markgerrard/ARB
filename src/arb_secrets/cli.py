from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from nacl.public import PrivateKey

from arb_crypto import fingerprint, generate_keypair, load_privkey
from arb_secrets.keystore import KeyStore

DEFAULT_PRIVKEY = Path.home() / ".arb-secrets" / "privkey.b64"


def init_keypair(path: str | Path = DEFAULT_PRIVKEY) -> tuple[bytes, str]:
    path = Path(path)
    if path.exists():
        privkey = load_privkey(path)
    else:
        privkey, _ = generate_keypair()
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(base64.b64encode(privkey).decode("ascii"))
    pubkey = bytes(PrivateKey(privkey).public_key)
    return pubkey, fingerprint(pubkey)


def publish_key(redis, agent_id: str, privkey_path: str | Path = DEFAULT_PRIVKEY) -> str:
    pubkey, fp = init_keypair(privkey_path)
    KeyStore(redis, agent_id, Path(privkey_path).expanduser().parent / "known_peers.b64").publish(pubkey)
    return fp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arb-secrets")
    sub = parser.add_subparsers(dest="command", required=True)
    init_cmd = sub.add_parser("init")
    init_cmd.add_argument("--privkey", default=str(DEFAULT_PRIVKEY))
    publish_cmd = sub.add_parser("publish")
    publish_cmd.add_argument("agent_id")
    publish_cmd.add_argument("--redis-url", required=True)
    publish_cmd.add_argument("--privkey", default=str(DEFAULT_PRIVKEY))
    args = parser.parse_args(argv)

    if args.command == "init":
        _, fp = init_keypair(args.privkey)
        print(fp)
        return 0

    import redis

    r = redis.Redis.from_url(args.redis_url)
    print(publish_key(r, args.agent_id, args.privkey))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
