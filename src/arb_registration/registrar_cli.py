from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from agent_redis_bridge.redis_io import RedisCli, RedisConfig

from .buzz import BuzzOps
from .registrar import Registrar
from .store import RegistrationStore


def main() -> None:
    ap = argparse.ArgumentParser(prog="seat-registrar")
    ap.add_argument("--env-file", type=Path, default=Path(os.environ.get("AGENT_ENV_FILE", "")), required=not bool(os.environ.get("AGENT_ENV_FILE")))
    ap.add_argument("--store", type=Path, default=Path(os.environ.get("ARB_REGISTRAR_STORE", "/var/lib/arb-seat-registrar/registrar.sqlite3")))
    ap.add_argument("--agent-id", default=os.environ.get("ARB_REGISTRAR_AGENT_ID", "arb-seat-registrar"))
    ap.add_argument(
        "--buzz-env-file", type=Path,
        default=Path(os.environ["ARB_REGISTRAR_BUZZ_ENV_FILE"])
        if os.environ.get("ARB_REGISTRAR_BUZZ_ENV_FILE") else None,
    )
    args = ap.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = RedisConfig.from_env_file(args.env_file, {})
    Registrar(
        store=RegistrationStore(args.store), redis_client=RedisCli(config).client,
        prefix=config.prefix, agent_id=args.agent_id,
        relay_url=os.environ["ARB_REGISTRAR_RELAY_URL"],
        buzz=BuzzOps(buzz_env_file=args.buzz_env_file),
    ).run()


if __name__ == "__main__":
    main()
