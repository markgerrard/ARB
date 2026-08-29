from __future__ import annotations

from datetime import datetime, timezone
import logging

from arb_email.addresses import has_control_chars, parse_single_recipient, recipient_allowed


log = logging.getLogger("arb_email.client")


class EmailClient:
    def __init__(self, settings, *, http_post=None, now=None, audit_sink=None):
        self.s = settings
        self._post = http_post or self._default_post
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.audit_sink = audit_sink

    def _default_post(self, url, json, headers):
        import httpx

        response = httpx.post(url, json=json, headers=headers, timeout=20)
        try:
            obj = response.json()
        except Exception as exc:
            raise RuntimeError("email backend returned invalid JSON") from exc
        return response.status_code, obj

    def send(self, subject, html_body, text_body, *, to, actor) -> dict:
        if not subject or len(subject) > self.s.subject_max or has_control_chars(subject):
            raise ValueError("invalid subject")
        if not (html_body or text_body):
            raise ValueError("body required")
        for body in (html_body, text_body):
            if body is not None and len(body.encode("utf-8")) > self.s.body_max:
                raise ValueError("body too large")

        norm = parse_single_recipient(to)
        if not recipient_allowed(norm, self.s.to_allowlist):
            raise ValueError("recipient not allowlisted")

        payload = {
            "From": self.s.sender,
            "To": norm,
            "Subject": subject,
            "MessageStream": self.s.stream,
        }
        if html_body is not None:
            payload["HtmlBody"] = html_body
        if text_body is not None:
            payload["TextBody"] = text_body

        try:
            status, obj = self._post(
                self.s.api_url,
                payload,
                {
                    "X-Postmark-Server-Token": self.s.token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("email backend unavailable; retry") from exc

        if status // 100 != 2:
            raise RuntimeError(f"postmark http {status}")
        code = obj.get("ErrorCode")
        if code != 0:
            raise RuntimeError(f"postmark error {code}: {obj.get('Message')}")
        message_id = obj.get("MessageID")

        try:
            if self.audit_sink:
                self.audit_sink(
                    {
                        "op": "email_send",
                        "actor": actor,
                        "to": norm,
                        "subject": subject,
                        "message_id": message_id,
                        "stream": self.s.stream,
                        "ts": self._now().isoformat(),
                    }
                )
        except Exception:
            log.exception("email_send audit failed (email already sent); deadlettered")

        return {"sent": True, "message_id": message_id, "to": norm, "stream": self.s.stream}
