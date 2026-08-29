from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .bus_acl import AclProvisionError, validate_host
from .store import RegistrationStore, TokenError


log = logging.getLogger("arb_registration.bus_approval_cli")


def main() -> None:
    parser = argparse.ArgumentParser(prog="bus-registrar-approve")
    parser.add_argument(
        "--store", type=Path,
        default=Path(os.environ.get(
            "ARB_BUS_REGISTRAR_STORE",
            "/var/lib/arb-bus-registrar/registrar.sqlite3",
        )),
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--approve", metavar="REQUEST_ID")
    action.add_argument("--deny", metavar="REQUEST_ID")
    action.add_argument("--unpin", metavar="HOST")
    action.add_argument("--list", action="store_true")
    args = parser.parse_args()

    store = RegistrationStore(args.store)
    if args.list:
        rows = []
        for request in store.bus_requests():
            inventory = json.loads(request.pop("self_report_json"))
            roles_json = request.pop("declared_roles_json")
            declared_roles = json.loads(roles_json) if roles_json else None
            rows.append({
                **request, "declared_roles": declared_roles,
                "self_report": inventory,
            })
        print(json.dumps(rows, indent=2, sort_keys=True))
        return

    if args.unpin:
        try:
            host = validate_host(args.unpin)
            audit = store.unpin_host_sealing_key(host=host, source="operator-cli")
        except (AclProvisionError, TokenError) as exc:
            raise SystemExit(f"bus-registrar-approve: {exc}") from exc
        log.warning("bus_registration %s", json.dumps(audit, sort_keys=True))
        print(json.dumps(audit, sort_keys=True))
        return

    request_id = args.approve or args.deny
    decision = "approve" if args.approve else "deny"
    if not store.set_bus_decision(request_id, decision, "operator-cli"):
        raise SystemExit("bus-registrar-approve: request is not pending or was already decided")
    print(json.dumps({"request_id": request_id, "decision": decision}, sort_keys=True))


if __name__ == "__main__":
    main()
