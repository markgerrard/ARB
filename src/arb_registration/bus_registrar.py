from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
from typing import Any

from arb_crypto import fingerprint, seal

from .bus import receive, send
from .bus_acl import (
    AclProvisioner, AclProvisionError, validate_declared_roles, validate_host,
)
from .protocol import (
    BUS_DENY_EVENT, BUS_GRANT_EVENT, BUS_REQUEST_EVENT, notify_envelope,
    verify_signed_data,
)
from .store import RegistrationStore, TokenError, iso, utcnow


log = logging.getLogger("arb_registration.bus_registrar")
REG_PREFIX = "reg:"
REGISTRAR_ID = "arb-bus-registrar"
REPLY_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
ALLOWED_PRODUCER_ENVS = {
    "ARB_MEMORY_REDIS_URL", "ARB_EVAL_REDIS_URL", "ARB_TRACE_REDIS_URL",
}
MAX_ATTEMPTS = 5
RETRY_DELAYS = (5, 15, 30, 60, 120)


def validate_self_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"supervisors", "producers"}:
        raise ValueError("self_report must contain supervisors and producers")
    supervisors = value["supervisors"]
    producers = value["producers"]
    if not isinstance(supervisors, list) or not isinstance(producers, list):
        raise ValueError("self_report collections must be lists")
    for item in supervisors:
        if not isinstance(item, dict):
            raise ValueError("supervisor entries must be objects")
        required = ("service", "kind", "env_file")
        if not all(isinstance(item.get(key), str) and item[key] for key in required):
            raise ValueError("supervisor entries require service, kind, and env_file")
        if not item["env_file"].startswith("/"):
            raise ValueError("supervisor env_file must be an absolute resolved path")
    for item in producers:
        if not isinstance(item, dict):
            raise ValueError("producer entries must be objects")
        if not isinstance(item.get("service"), str) or not item["service"]:
            raise ValueError("producer entries require service")
        if item.get("env_var") not in ALLOWED_PRODUCER_ENVS:
            raise ValueError("producer env_var is not a stream-plane URL")
        if item.get("db") not in {5, 6, 7}:
            raise ValueError("producer db must be 5, 6, or 7")
        streams = item.get("streams")
        if not isinstance(streams, list) or not streams or not all(
            isinstance(stream, str) and stream for stream in streams
        ):
            raise ValueError("producer streams must be a non-empty string list")
    # A JSON round trip rejects non-serializable values and gives the store a
    # stable, reviewable inventory representation.
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


def decode_pinned_sealing_key(encoded: str, expected_fingerprint: str) -> bytes:
    try:
        public_key = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("sealing_pubkey must be canonical base64") from exc
    if len(public_key) != 32:
        raise ValueError("sealing_pubkey must decode to 32 bytes")
    actual = fingerprint(public_key)
    if actual != expected_fingerprint:
        raise ValueError("sealing key fingerprint mismatch")
    return public_key


class BusRegistrar:
    def __init__(
        self, *, store: RegistrationStore, redis_client, provisioner: AclProvisioner,
        endpoint: str, prefix: str = REG_PREFIX, agent_id: str = REGISTRAR_ID,
        approval_timeout: timedelta = timedelta(hours=24),
    ) -> None:
        self.store = store
        self.redis = redis_client
        self.provisioner = provisioner
        self.endpoint = endpoint
        self.prefix = prefix
        self.agent_id = agent_id
        self.approval_timeout = approval_timeout

    def run(self) -> None:
        # Deployment invariant: one non-templated registrar instance is the sole provisioner.
        while True:
            envelope = receive(self.redis, self.prefix, self.agent_id, timeout=2)
            if envelope:
                self.handle(envelope)
            self.poll()

    def handle(self, envelope: dict[str, Any]) -> None:
        payload = envelope.get("payload")
        if (
            envelope.get("kind") != "notify"
            or envelope.get("to") != self.agent_id
            or not isinstance(payload, dict)
            or payload.get("event") != BUS_REQUEST_EVENT
        ):
            return
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("event") != BUS_REQUEST_EVENT:
            return
        self._request(data, envelope)

    def _request(self, data: dict[str, Any], envelope: dict[str, Any]) -> None:
        required = (
            "token", "name", "host", "pubkey", "signing_pubkey", "sealing_pubkey",
            "sealing_fingerprint", "reply_agent_id", "client_nonce", "issued_at", "signature",
        )
        if not all(isinstance(data.get(key), str) and data[key] for key in required):
            self._audit("invalid_request_shape", envelope_id=envelope.get("id"))
            return
        if not REPLY_ID_RE.fullmatch(data["reply_agent_id"]):
            self._audit("invalid_reply_agent", envelope_id=envelope.get("id"))
            return
        try:
            host = validate_host(data["host"])
            declared_roles = validate_declared_roles(data.get("roles"))
            self_report = validate_self_report(data.get("self_report"))
            decode_pinned_sealing_key(data["sealing_pubkey"], data["sealing_fingerprint"])
        except (AclProvisionError, ValueError):
            self._audit("invalid_request_shape", envelope_id=envelope.get("id"))
            return
        if not verify_signed_data(data):
            self._audit("invalid_request_signature", envelope_id=envelope.get("id"))
            return
        existing_pin = self.store.host_sealing_pin(host)
        if existing_pin is not None and existing_pin != data["sealing_fingerprint"]:
            self._audit(
                "sealing_key_pin_mismatch", envelope_id=envelope.get("id"), host=host,
            )
            return
        try:
            request_id = self.store.reserve_bus(
                token=data["token"], name=data["name"], host=host,
                request={
                    "pubkey": data["pubkey"],
                    "signing_pubkey": data["signing_pubkey"],
                    "channels_json": "[]",
                    "reply_agent_id": data["reply_agent_id"],
                    "client_nonce": data["client_nonce"],
                    "sealing_pubkey": data["sealing_pubkey"],
                    "sealing_fingerprint": data["sealing_fingerprint"],
                    "self_report_json": json.dumps(
                        self_report, sort_keys=True, separators=(",", ":")
                    ),
                    "declared_roles_json": json.dumps(list(declared_roles)),
                },
            )
        except TokenError:
            self._audit("invalid_registration_token", envelope_id=envelope.get("id"))
            return
        self._audit(
            "registration_pending", request_id=request_id, host=host,
            declared_roles=list(declared_roles),
            inventory_sha256=self._inventory_sha(self_report),
        )

    def poll(self) -> None:
        now = utcnow()
        for request in self.store.bus_pending():
            created = datetime.fromisoformat(request["created_at"].replace("Z", "+00:00"))
            if now - created < self.approval_timeout:
                continue
            self._reply(
                request, BUS_DENY_EVENT,
                {"request_id": request["id"], "reason": "operator approval timed out"},
            )
            self.store.set_bus_status(request["id"], "denied")
            self._audit("registration_timed_out", request_id=request["id"])

        for request in self.store.bus_decided():
            if request["decision"] == "deny":
                self._reply(
                    request, BUS_DENY_EVENT,
                    {"request_id": request["id"], "reason": "operator denied registration"},
                )
                self.store.set_bus_status(request["id"], "denied")
                self._audit("registration_denied", request_id=request["id"])
            else:
                self.store.set_bus_status(request["id"], "approved")

        for request in self.store.bus_approved():
            next_attempt = request.get("next_attempt_at")
            if next_attempt and datetime.fromisoformat(next_attempt.replace("Z", "+00:00")) > now:
                continue
            self._provision(request)

    def _provision(self, request: dict[str, Any]) -> None:
        try:
            public_key = decode_pinned_sealing_key(
                request["sealing_pubkey"], request["sealing_fingerprint"]
            )
            inventory = json.loads(request["self_report_json"])
            declared_roles = validate_declared_roles(
                json.loads(request["declared_roles_json"])
            )
            existing_pin = self.store.host_sealing_pin(request["host"])
            if (
                existing_pin is not None
                and existing_pin != request["sealing_fingerprint"]
            ):
                self._reject_at_provision(
                    request, "sealing_key_pin_mismatch", "sealing key pin mismatch"
                )
                return
            try:
                self.store.assert_request_token_fresh(request["id"])
            except TokenError:
                self._reject_at_provision(
                    request, "registration_token_stale",
                    "registration credential expired or was revoked",
                )
                return
            credentials = self.provisioner.provision(request["host"], declared_roles)
            try:
                self.store.pin_host_sealing_key(
                    host=request["host"],
                    sealing_fingerprint=request["sealing_fingerprint"],
                    request_id=request["id"],
                )
            except TokenError:
                self._reject_at_provision(
                    request, "sealing_key_pin_mismatch", "sealing key pin mismatch"
                )
                return
            bundle = {
                "request_id": request["id"],
                "host": request["host"],
                "endpoint": self.endpoint,
                "roles": credentials,
                "declared_roles": list(declared_roles),
                "inventory_sha256": self._inventory_sha(inventory),
                "issued_at": iso(utcnow()),
            }
            ciphertext = seal(
                json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode(),
                public_key,
            )
            self._reply(
                request, BUS_GRANT_EVENT,
                {
                    "request_id": request["id"],
                    "sealed_bundle_b64": base64.b64encode(ciphertext).decode("ascii"),
                    "recipient_fingerprint": request["sealing_fingerprint"],
                    "inventory_sha256": bundle["inventory_sha256"],
                },
            )
            self.store.set_bus_status(request["id"], "provisioned")
            self._audit(
                "registration_provisioned", request_id=request["id"],
                host=request["host"], declared_roles=list(declared_roles),
                roles=sorted(credentials),
            )
        except Exception as exc:
            log.exception("bus registration provisioning failed request_id=%s", request["id"])
            prior = int(request.get("provision_attempts") or 0)
            delay = RETRY_DELAYS[min(prior, len(RETRY_DELAYS) - 1)]
            attempts, exhausted = self.store.record_attempt_failure(
                request["id"], type(exc).__name__,
                utcnow() + timedelta(seconds=delay), MAX_ATTEMPTS,
            )
            if exhausted:
                self._reply(
                    request, BUS_DENY_EVENT,
                    {"request_id": request["id"], "reason": "credential provisioning failed"},
                )
                self._audit(
                    "registration_provision_failed", request_id=request["id"],
                    attempts=attempts,
                )

    def _reject_at_provision(
        self, request: dict[str, Any], action: str, reason: str
    ) -> None:
        self._reply(
            request, BUS_DENY_EVENT, {"request_id": request["id"], "reason": reason}
        )
        self.store.set_bus_status(request["id"], "denied")
        self._audit(action, request_id=request["id"], host=request["host"])

    def _reply(self, request: dict[str, Any], event: str, data: dict[str, Any]) -> None:
        body = {
            **data, "client_nonce": request["client_nonce"],
            "sealing_fingerprint": request["sealing_fingerprint"],
        }
        send(
            self.redis, self.prefix, request["reply_agent_id"],
            notify_envelope(self.agent_id, request["reply_agent_id"], event, body),
        )

    @staticmethod
    def _inventory_sha(inventory: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _audit(action: str, **fields: Any) -> None:
        log.info("bus_registration %s", json.dumps({"action": action, **fields}, sort_keys=True))
