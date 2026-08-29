"""Shared ACP client helpers (Agent Client Protocol).

Extracted from cursor_acp so grok_acp (and any future ACP engine) reuses the
panel-reviewed allow-option selection instead of forking it. Behavior must stay
byte-identical to the cursor original (GROK-1 design v1.3, D1).

Also hosts ``TurnPolicyPermissionMixin`` — the same authorization rule for ACP
engines that subclass ``GenericAcpEngine`` (whose base cancels every ask).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DENY_MARKERS = (
    "disallow",
    "do not",
    "don't",
    "dont",
    "not allow",
    "never",
    "deny",
    "reject",
    "decline",
    "block",
    "forbid",
    "refuse",
    "cancel",
)


def _select_allow_option(params: dict[str, Any]) -> str | None:
    """Pick the optionId of an "allow" choice from a session/request_permission
    payload. ACP options carry a ``kind`` (allow_once / allow_always /
    reject_once / reject_always) — that is authoritative, so prefer it: single-use
    allow, then always-allow.

    Only if no option declares an allow ``kind`` do we fall back to a substring
    heuristic over the optionId/kind (NOT the free-text name, which is too noisy —
    e.g. "shallow" contains "allow"). The fallback also rejects any option bearing
    a negative marker so "disallow" / "do not allow" can never be selected.
    Returns None if no allow option is offered (caller cancels)."""
    options = params.get("options")
    if not isinstance(options, list):
        return None
    valid = [o for o in options if isinstance(o, dict) and isinstance(o.get("optionId"), str)]
    for preferred in ("allow_once", "allow_always"):
        for o in valid:
            if o.get("kind") == preferred:
                return o["optionId"]
    for o in valid:
        token = f"{o['optionId']} {o.get('kind') or ''}".lower()
        if any(marker in token for marker in _DENY_MARKERS):
            continue
        if "allow" in token:
            return o["optionId"]
    return None


class TurnPolicyPermissionMixin:
    """Answer ``session/request_permission`` from the ACTIVE TURN's policy.

    ``GenericAcpEngine._respond_to_client_request`` cancels every permission ask
    unconditionally. That is correct for agents driven into a non-gating mode
    (kimi's ``yolo``), but omp and opencode ask for approval on ordinary tool
    calls, so on that base **every tool-using turn returns
    ``stopReason=cancelled`` with empty results** — observed live 2026-08-02 on
    an omp-acp dispatch that reached the seat, ran, and came back cancelled.

    Authorization comes ONLY from the policy the active turn threads through,
    which is grok-acp's reviewed fail-closed floor (GROK-1 v1.3 D2) applied to
    the generic-acp subclass family:

    - inside a ``trusted`` turn → select an allow option via the shared,
      panel-reviewed :func:`_select_allow_option`;
    - outside a turn (the handshake ``request()`` paths, where
      ``_active_policy`` is None) → deny, so a stray ask can never be
      authorized by a policy left over from a previous turn;
    - an ask naming a non-current session → deny regardless of policy
      (GROK-1 v1.3 D3b: structurally unauthorizable);
    - a ``trusted`` ask offering no allow option → deny, not "assume yes".

    Mix in BEFORE the engine base so these overrides win the MRO.
    """

    _active_policy: str | None = None

    def run_turn_with_progress(self, task, *, timeout=3600, policy="trusted", on_event=None):
        # Anything already queued belongs to a PREVIOUS turn (or to the
        # handshake), never to this one — cancel stale asks before this turn's
        # policy goes live, or they get authorized by it. See
        # _cancel_stale_permission_asks.
        self._cancel_stale_permission_asks()
        self._active_policy = policy
        try:
            return super().run_turn_with_progress(
                task, timeout=timeout, policy=policy, on_event=on_event
            )
        finally:
            # Clearing is load-bearing: it restores the deny floor for the
            # handshake paths that share this responder.
            self._active_policy = None

    def _cancel_stale_permission_asks(self) -> None:
        """Deny permission asks left over from an earlier turn.

        The policy checks in :meth:`_respond_to_client_request` authorize at
        DEQUEUE time, so an ask that arrived during turn A but was still queued
        when turn A returned would be answered under turn B's policy. A write the
        agent requested under ``plan`` mode could therefore be approved by an
        unrelated later dispatch. The session check does not catch it: this family
        issues ``session/new`` once in ``start()``, so ``session_id`` is identical
        across turns, and the bridge only re-keys via ``reset_context`` when a
        request explicitly asks for fresh context (``fresh_context_default`` is
        False by default).

        grok-acp — whose reviewed fail-closed floor this mixin is derived from —
        is not exposed: it retires after every turn, and a non-retiring grok seat
        rotates ``session/new`` per dispatch precisely so its D3b session gate
        correlates (``grok_acp._rotate_session_if_reused``). The mixin inherited
        D3b's ask-time wording without the cross-turn invariant behind it. Rather
        than add a per-turn session round trip to every engine in this family,
        close it where the assumption actually breaks: a turn owns only the asks
        that arrive while it is running.

        Non-permission messages are put back in order — draining stale progress
        notifications is a separate concern and not this method's business.
        """
        pending: list[dict[str, Any]] = []
        while True:
            try:
                message = self.messages.get_nowait()
            except Exception:  # queue.Empty — the only expected exit
                break
            if (
                isinstance(message, dict)
                and message.get("method") == "session/request_permission"
            ):
                logger.warning(
                    "[acp] permission ask %r was still queued when a new turn started; "
                    "denied fail-closed (an ask belongs to the turn it arrived in)",
                    message.get("id"),
                )
                self._cancel_permission(message.get("id"))
                continue
            pending.append(message)
        for message in pending:
            self.messages.put(message)

    def _cancel_permission(self, request_id: Any) -> None:
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "result": {"outcome": {"outcome": "cancelled"}}}
        )

    def _respond_to_client_request(self, message: dict[str, Any]) -> None:
        if message.get("method") != "session/request_permission":
            super()._respond_to_client_request(message)
            return

        request_id = message.get("id")
        raw_params = message.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

        ask_session = params.get("sessionId")
        if ask_session != self.session_id:
            logger.warning(
                "[acp] permission ask for non-current session %r (current %r); "
                "denied fail-closed",
                ask_session,
                self.session_id,
            )
            self._cancel_permission(request_id)
            return

        if self._active_policy != "trusted":
            self._cancel_permission(request_id)
            return

        option_id = _select_allow_option(params)
        if option_id is None:
            logger.warning(
                "[acp] permission ask offered no allow option (or params malformed); "
                "cancelled despite trusted policy (fail-closed floor)"
            )
            self._cancel_permission(request_id)
            return

        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
            }
        )
