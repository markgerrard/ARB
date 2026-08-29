from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from agent_redis_bridge.redis_io import RedisCli, RedisConfig

from .bus_acl import AclProvisioner, RedisConnector
from .bus_registrar import BusRegistrar, REGISTRAR_ID, REG_PREFIX
from .store import RegistrationStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="bus-registrar")
    parser.add_argument("--legacy-env-file", type=Path, required=True)
    parser.add_argument(
        "--store", type=Path,
        default=Path(os.environ.get(
            "ARB_BUS_REGISTRAR_STORE",
            "/var/lib/arb-bus-registrar/registrar.sqlite3",
        )),
    )
    parser.add_argument("--acl-file", type=Path, default=Path("/etc/valkey/users.acl"))
    parser.add_argument(
        "--credentials-file", type=Path,
        default=Path("/etc/valkey/credentials.json"),
    )
    parser.add_argument("--new-host", default=os.environ.get("ARB_BUS_REGISTRAR_NEW_HOST", "127.0.0.1"))
    parser.add_argument("--new-port", type=int, default=int(os.environ.get("ARB_BUS_REGISTRAR_NEW_PORT", "6379")))
    parser.add_argument("--new-endpoint", default=os.environ.get("ARB_BUS_REGISTRAR_NEW_ENDPOINT", "rediss://arb-bus.example.com:6379/12"))
    parser.add_argument("--new-no-tls", action="store_true")
    parser.add_argument("--new-ca-cert", default=os.environ.get("ARB_BUS_REGISTRAR_NEW_CA_CERT", "/etc/ssl/certs/ca-certificates.crt"))
    parser.add_argument("--agent-id", default=REGISTRAR_ID)
    parser.add_argument("--prefix", default=REG_PREFIX)
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    legacy = RedisConfig.from_env_file(args.legacy_env_file, {})
    if int(legacy.db) != 12:
        raise SystemExit("bus-registrar: legacy transport must use DB12")
    connector = RedisConnector(
        host=args.new_host, port=args.new_port, tls=not args.new_no_tls,
        ca_cert=args.new_ca_cert,
    )
    BusRegistrar(
        store=RegistrationStore(args.store),
        redis_client=RedisCli(legacy).client,
        provisioner=AclProvisioner(
            acl_path=args.acl_file,
            credentials_path=args.credentials_file,
            connector=connector,
        ),
        endpoint=args.new_endpoint,
        prefix=args.prefix,
        agent_id=args.agent_id,
    ).run()


if __name__ == "__main__":
    main()
