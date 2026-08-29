from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
import logging
import time

import anyio

from arb_email.addresses import has_control_chars, parse_single_recipient, recipient_allowed


log = logging.getLogger("arb_email.mcp.door_tools")


class EmailTools:
    def __init__(self, client, settings, *, require_scope=None, actor=None, now=None,
                 wall_now=None, audit_sink=None):
        self.client = client
        self.s = settings
        self._require_scope = require_scope or self._default_require_scope
        self._actor = actor or self._default_actor
        self._now = now or time.monotonic  # MONOTONIC — rate windows only
        # Audit timestamps are WALL-CLOCK ISO (comparable with the client's success-audit ts);
        # never the monotonic rate clock.
        self._wall_now = wall_now or (lambda: datetime.now(timezone.utc))
        self.audit_sink = audit_sink
        self._minute_hits: dict[str, list[float]] = {}
        self._day_hits: dict[str, list[float]] = {}

    def _default_require_scope(self, scope: str) -> None:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None or scope not in (getattr(token, "scopes", None) or []):
            raise PermissionError(f"{scope} scope required")

    def _default_actor(self) -> str:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        return getattr(token, "client_id", None) or "mcp"

    def _deny(self, actor: str, raw_to, subject, reason: str) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink(
                {
                    "op": "email_send_denied",
                    "actor": actor,
                    "to": raw_to,
                    "reason": reason,
                    "subject": subject,
                    "ts": self._wall_now().isoformat(),
                }
            )
        except Exception:
            log.exception("email_send_denied audit failed")

    def _audit_failed(self, actor: str, to, subject, reason: str) -> None:
        # Best-effort forensic record for a Postmark-rejected/failed send (the send produced no mail,
        # but the attempt is an abuse/ops signal worth keeping). Never raises.
        if self.audit_sink is None:
            return
        try:
            self.audit_sink({"op": "email_send_failed", "actor": actor, "to": to, "reason": reason,
                             "subject": subject, "ts": self._wall_now().isoformat()})
        except Exception:
            log.exception("email_send_failed audit failed")

    def _check_window(
        self,
        hits_by_actor: dict[str, list[float]],
        actor: str,
        *,
        limit: int,
        window_seconds: float,
    ) -> bool:
        now = self._now()
        window_start = now - window_seconds
        hits = [stamp for stamp in hits_by_actor.get(actor, []) if stamp >= window_start]
        if len(hits) >= limit:
            hits_by_actor[actor] = hits
            return False
        hits.append(now)
        hits_by_actor[actor] = hits
        return True

    def _check_rates(self, actor: str) -> bool:
        return self._check_window(
            self._minute_hits,
            actor,
            limit=self.s.rate_per_min,
            window_seconds=60.0,
        ) and self._check_window(
            self._day_hits,
            actor,
            limit=self.s.rate_per_day,
            window_seconds=86400.0,
        )

    def _validate(self, subject, html_body, text_body, raw_to) -> str:
        if not subject or len(subject) > self.s.subject_max or has_control_chars(subject):
            raise ValueError("invalid subject")
        if not (html_body or text_body):
            raise ValueError("body required")
        for body in (html_body, text_body):
            if body is not None and len(body.encode("utf-8")) > self.s.body_max:
                raise ValueError("body too large")
        norm = parse_single_recipient(raw_to)
        if not recipient_allowed(norm, self.s.to_allowlist):
            raise ValueError("recipient not allowlisted")
        return norm

    async def email_send(self, subject: str, html_body=None, text_body=None, to=None) -> dict:
        actor = self._actor()
        raw_to = to or self.s.default_to
        try:
            self._require_scope("email.send")
        except PermissionError:
            self._deny(actor, raw_to, subject, "missing scope")
            raise

        if not self._check_rates(actor):
            self._deny(actor, raw_to, subject, "rate limit exceeded")
            raise ValueError("rate limit exceeded")

        try:
            norm = self._validate(subject, html_body, text_body, raw_to)
        except ValueError as exc:
            self._deny(actor, raw_to, subject, str(exc))
            raise

        send = partial(
            self.client.send,
            subject,
            html_body,
            text_body,
            to=norm,
            actor=actor,
        )
        try:
            return await anyio.to_thread.run_sync(send)
        except RuntimeError as exc:
            self._audit_failed(actor, norm, subject, str(exc))
            raise
