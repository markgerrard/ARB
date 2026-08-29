from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import time
from typing import Any

from .bus import receive, send
from .buzz import BuzzOps
from .protocol import (
    DENY_EVENT, GRANT_EVENT, PROFILE_READY_EVENT, PROVISION_EVENT, REQUEST_EVENT,
    notify_envelope, parse_approval_reply, verify_signed_data,
)
from .store import RegistrationStore, TokenError


log = logging.getLogger("arb_registration.audit")
APPROVE_REACTIONS = {"✅", "☑", "☑️"}
DENY_REACTIONS = {"❌", "✖", "✖️"}
MAX_ATTEMPTS = 5
RETRY_DELAYS = (5, 15, 30, 60, 120)


class Registrar:
    def __init__(self, *, store: RegistrationStore, redis_client, prefix: str, agent_id: str,
                 relay_url: str, buzz: BuzzOps, approval_timeout: timedelta = timedelta(hours=24)) -> None:
        self.store = store
        self.redis = redis_client
        self.prefix = prefix
        self.agent_id = agent_id
        self.relay_url = relay_url
        self.buzz = buzz
        self.approval_timeout = approval_timeout
        self._last_contradiction_poll = 0.0

    def run(self) -> None:
        while True:
            envelope = receive(self.redis, self.prefix, self.agent_id, timeout=5)
            if envelope:
                self.handle(envelope)
            self.poll_approvals()

    def handle(self, envelope: dict[str, Any]) -> None:
        payload = envelope.get("payload")
        if envelope.get("kind") != "notify" or not isinstance(payload, dict):
            return
        event, data = payload.get("event"), payload.get("data")
        if not isinstance(data, dict) or data.get("event") != event:
            return
        if event == REQUEST_EVENT:
            self._registration_request(data, envelope)
        elif event == PROFILE_READY_EVENT:
            self._profile_ready(data)

    def _registration_request(self, data: dict[str, Any], envelope: dict[str, Any]) -> None:
        required = (
            "token", "name", "host", "pubkey", "signing_pubkey", "reply_agent_id",
            "client_nonce", "issued_at", "signature",
        )
        if not all(isinstance(data.get(key), str) and data[key] for key in required):
            self._audit("invalid_request_shape", envelope_id=envelope.get("id"))
            return
        channels = data.get("channels")
        if not isinstance(channels, list) or not all(isinstance(item, str) and item for item in channels):
            self._audit("invalid_request_shape", envelope_id=envelope.get("id"))
            return
        if not verify_signed_data(data):
            self._audit("invalid_request_signature", envelope_id=envelope.get("id"))
            return
        try:
            request_id = self.store.reserve(
                token=data["token"], name=data["name"], host=data["host"],
                request={
                    "pubkey": data["pubkey"], "signing_pubkey": data["signing_pubkey"],
                    "channels_json": json.dumps(channels), "reply_agent_id": data["reply_agent_id"],
                    "client_nonce": data["client_nonce"],
                },
            )
        except TokenError:
            self._audit("invalid_registration_credential", envelope_id=envelope.get("id"))
            return
        try:
            standing_warnings = self.buzz.registrar_standing_warnings(channels)
            event_id = self.buzz.post_approval(
                request_id, data["name"], data["host"], data["pubkey"], channels,
                standing_warnings,
            )
            self.store.set_request(request_id, "pending", event_id)
            self._audit(
                "approval_requested", request_id=request_id, event_id=event_id,
                standing_warnings=standing_warnings,
            )
        except Exception:
            log.exception("seat_registration approval_post_failed request_id=%s", request_id)

    def poll_approvals(self) -> None:
        now = datetime.now(timezone.utc)
        for request in self.store.pending():
            if request["status"] == "provisioned":
                continue
            if request["status"] == "profile_ready":
                if not self._attempt_due(request, now):
                    continue
                self._finish_profile(request)
                continue
            created = datetime.fromisoformat(request["created_at"].replace("Z", "+00:00"))
            if request.get("decision") is None and now - created >= self.approval_timeout:
                self._deny(request, "approval timed out", "timed_out")
                continue
            event_id = request.get("approval_event_id")
            if not event_id:
                continue
            if request.get("decision") == "approve":
                if self._attempt_due(request, now):
                    self._approve(request)
                continue
            signals = self._decision_signals(request)
            if not signals:
                continue
            action, source, target_id = signals[0]
            if not self.store.set_decision(request["id"], action, source):
                continue
            self._react_seen(target_id or event_id, request["id"])
            if any(item[0] != action for item in signals[1:]):
                self._thread_reply(
                    request,
                    f"Ignored a contradictory signal for `{request['id']}`; the first valid "
                    f"Mark-authored decision (`{action}`) already won.",
                )
                self.store.mark_contradiction_noted(request["id"])
            if action == "deny":
                self._deny(request, "operator denied registration", "denied")
            else:
                self._approve(self.store.request(request["id"]) or request)
        if time.monotonic() - self._last_contradiction_poll >= 30:
            self._last_contradiction_poll = time.monotonic()
            self._poll_late_contradictions()

    def _decision_signals(self, request: dict[str, str]) -> list[tuple[str, str, str]]:
        event_id = request.get("approval_event_id", "")
        signals: list[tuple[str, str, str]] = []
        try:
            messages = sorted(
                self.buzz.thread(event_id), key=lambda item: str(item.get("created_at", ""))
            )
            for message in messages:
                if str(message.get("pubkey", "")).lower() != self.buzz.mark_pubkey:
                    continue
                parsed = parse_approval_reply(str(message.get("content", "")))
                if parsed and parsed.request_id == request["id"].lower():
                    message_id = str(message.get("id", ""))
                    signals.append((parsed.action, f"reply:{message_id}", message_id))
        except Exception:
            log.exception("seat_registration thread_poll_failed request_id=%s", request["id"])
        try:
            reactions = self.buzz.reactions(event_id)
            for emoji in APPROVE_REACTIONS:
                if self.buzz.mark_pubkey in reactions.get(emoji, set()):
                    signals.append(("approve", f"reaction:{emoji}", event_id))
                    break
            for emoji in DENY_REACTIONS:
                if self.buzz.mark_pubkey in reactions.get(emoji, set()):
                    signals.append(("deny", f"reaction:{emoji}", event_id))
                    break
        except Exception:
            log.exception("seat_registration reaction_poll_failed request_id=%s", request["id"])
        return signals

    @staticmethod
    def _attempt_due(request: dict[str, str], now: datetime) -> bool:
        next_attempt = request.get("next_attempt_at")
        if not next_attempt:
            return True
        return datetime.fromisoformat(next_attempt.replace("Z", "+00:00")) <= now

    def _approve(self, request: dict[str, str]) -> None:
        channels = json.loads(request["channels_json"])
        try:
            auth_tag = self.buzz.owner_auth_tag(request["pubkey"])
            self.buzz.provision(request["pubkey"], channels)
            self.store.set_request(request["id"], "provisioned")
            provision = {
                "request_id": request["id"], "pubkey": request["pubkey"],
                "relay_url": self.relay_url, "channels": channels,
                "client_nonce": request["client_nonce"],
            }
            if auth_tag is not None:
                provision["auth_tag"] = auth_tag
            self._reply(request, PROVISION_EVENT, provision)
            self._audit("registration_provisioned", request_id=request["id"], pubkey=request["pubkey"])
        except Exception as exc:
            log.exception("seat_registration provision_failed request_id=%s", request["id"])
            self._record_failure(
                request, "provisioning", channel=getattr(exc, "channel", None)
            )

    def _profile_ready(self, data: dict[str, Any]) -> None:
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not verify_signed_data(data):
            self._audit("invalid_profile_ready")
            return
        request = self.store.request(request_id)
        if (
            not request
            or request["status"] not in {"provisioned", "profile_ready"}
            or data.get("pubkey") != request["pubkey"]
            or data.get("client_nonce") != request["client_nonce"]
        ):
            self._audit("unexpected_profile_ready", request_id=request_id)
            return
        self.store.set_request(request_id, "profile_ready")
        self._finish_profile(request)

    def _finish_profile(self, request: dict[str, str]) -> None:
        request_id = request["id"]
        try:
            self.buzz.verify_profile(request["pubkey"], request["name"])
            self.buzz.bind_owner(request["pubkey"])
            self.store.set_request(request_id, "approved")
            self._reply(request, GRANT_EVENT, {
                "request_id": request_id, "pubkey": request["pubkey"], "relay_url": self.relay_url,
                "channels": json.loads(request["channels_json"]), "profile_verified": True,
                "client_nonce": request["client_nonce"],
            })
            self._audit("registration_granted", request_id=request_id, pubkey=request["pubkey"])
            channels = json.loads(request["channels_json"])
            auth_note = (
                "NIP-OA kind-0 owner auth tag supplied for profile publication"
                if self.buzz.owner_auth_enabled
                else "NIP-OA owner key is not configured; the auth-tag ceremony remains manual"
            )
            self._thread_reply(
                request,
                f"Approved — relay membership granted, added to "
                f"{', '.join(channels) if channels else '(no requested channels)'}, "
                f"owner bound, profile verified for `{request['pubkey']}`, and {auth_note}.",
            )
        except Exception:
            log.exception("seat_registration profile_completion_failed request_id=%s", request_id)
            self._record_failure(request, "profile verification and owner binding")

    def _deny(self, request: dict[str, str], reason: str, status: str) -> None:
        self.store.set_request(request["id"], status)
        self._reply(request, DENY_EVENT, {
            "request_id": request["id"], "reason": reason,
            "client_nonce": request["client_nonce"],
        })
        self._audit("registration_denied", request_id=request["id"], reason=reason)
        self._thread_reply(
            request,
            f"Denied — request `{request['id']}` closed and its one-time token is burned.",
        )

    def _record_failure(
        self, request: dict[str, str], phase: str, channel: str | None = None,
    ) -> None:
        phase_detail = f"{phase} for channel `{channel}`" if channel else phase
        stored_error = f"{phase} channel={channel}" if channel else phase
        prior = int(request.get("provision_attempts") or 0)
        delay = RETRY_DELAYS[min(prior, len(RETRY_DELAYS) - 1)]
        attempts, exhausted = self.store.record_attempt_failure(
            request["id"], stored_error,
            datetime.now(timezone.utc) + timedelta(seconds=delay),
            MAX_ATTEMPTS,
        )
        if exhausted:
            self._thread_reply(
                request,
                f"Failed — {phase_detail} for request `{request['id']}` exhausted "
                f"{MAX_ATTEMPTS} attempts. The request is closed; see registrar audit logs.",
            )
            self._reply(request, DENY_EVENT, {
                "request_id": request["id"], "reason": f"{phase_detail} failed",
                "client_nonce": request["client_nonce"],
            })
            self._audit(
                "registration_failed", request_id=request["id"], phase=phase,
                channel=channel, attempts=attempts,
            )
        else:
            self._thread_reply(
                request,
                f"{phase_detail.capitalize()} attempt {attempts}/{MAX_ATTEMPTS} failed; "
                f"retrying with backoff. See registrar audit logs.",
            )

    def _react_seen(self, event_id: str, request_id: str) -> None:
        try:
            self.buzz.react(event_id, "👀")
        except Exception:
            log.exception("seat_registration receipt_reaction_failed request_id=%s", request_id)

    def _thread_reply(self, request: dict[str, str], content: str) -> None:
        event_id = request.get("approval_event_id")
        if not event_id:
            return
        try:
            self.buzz.reply(event_id, content)
        except Exception:
            log.exception("seat_registration thread_reply_failed request_id=%s", request["id"])

    def _poll_late_contradictions(self) -> None:
        for request in self.store.decided_for_contradiction_check():
            opposite = "deny" if request["decision"] == "approve" else "approve"
            if any(signal[0] == opposite for signal in self._decision_signals(request)):
                self._thread_reply(
                    request,
                    f"Ignored a later `{opposite}` signal for `{request['id']}`; the first "
                    f"valid decision (`{request['decision']}`) is final.",
                )
                self.store.mark_contradiction_noted(request["id"])

    def _reply(self, request: dict[str, str], event: str, data: dict[str, Any]) -> None:
        send(self.redis, self.prefix, request["reply_agent_id"], notify_envelope(self.agent_id, request["reply_agent_id"], event, data))

    @staticmethod
    def _audit(action: str, **fields: Any) -> None:
        log.info("seat_registration %s", json.dumps({"action": action, **fields}, sort_keys=True))
