# ARB Email MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or
> superpowers:executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Build ARB Email — a Postmark-backed `email_send` MCP tool on the existing ARB Memory door + a
local stdio MCP for seats, with From-fixed / explicit-address allowlist / caps / kill-switch / audit.

**Architecture:** A backend-agnostic `EmailClient` (httpx → Postmark) wrapped by `EmailTools` (door,
scope+caps+validation+denial-audit) and a local stdio server. Mirrors `src/arb_files/`.

**Spec (authoritative):** `docs/superpowers/specs/2026-06-30-arb-email-mcp-spec.md` — read it, **especially
the `Spec-panel revisions (BINDING R1–R9)` block**; where anything here conflicts, R1–R9 win.

## Plan-panel revisions (BINDING — supersede conflicting task text)

Reviewed by codex + cold-Opus + pi-GLM — 3/3 BUILD_WITH_CHANGES, no P0 (matcher bypass-free). (The
per-reviewer plan reports are not included in this copy.) Apply ALL:

- **PR1 — fix the `bad` matrix (Task 1):** REMOVE the two trailing entries `"arb@example.com "` and
  the duplicate `"arb@example.com"` — the clean address is valid (in `ok`), and the matcher
  *accepts+normalizes* trailing whitespace per R1, so it must not be in `bad`. Don't "fix" by
  weakening the strip-equality.
- **PR2 -- Unicode separators as EXPLICIT escapes (Task 1):** the original `_has_ctrl` wrote the
  separator set with PASTED LITERAL characters (invisible, editor-fragile, untested). Rewrite it to
  use Python BACKSLASH ESCAPES only -- the three codepoints are U+0085 (`\x85`), U+2028 (`\u2028`),
  U+2029 (`\u2029`): `_UNI_SEP = ("\x85", "\u2028", "\u2029")`, check `c in _UNI_SEP`. Add three
  `bad` test cases written the same escaped way: `"arb@example.com\x85"`, `"arb@example.com\u2028"`,
  `"arb@example.com\u2029"`. NEVER paste the literal separator characters anywhere.
- **PR3 — public control-char helper:** promote to `addresses.has_control_chars(s) -> bool` (covers
  `ord<32`, `127`, and `_UNI_SEP`); use it in `parse_single_recipient`, in the tool-layer **subject**
  validation, and in the client's defensive second line.
- **PR4 — client defensive second line (Task 2):** `EmailClient.send`, after `parse_single_recipient`,
  MUST `recipient_allowed(norm, self.s.to_allowlist)` → `ValueError("recipient not allowlisted")`, and
  MUST reject subject control chars via `has_control_chars`. Add client tests: non-allowlisted `to`
  raises with the fake `http_post` NOT called; `"ok\r\nBcc: v@x"` subject raises before network.
- **PR5 — E2E routes through the tool (Task 6):** the always-on "non-allowlisted `to` rejected before
  network" assertion runs through `EmailTools.email_send` (or the local tool), never bare
  `EmailClient`. (PR4 makes the client safe too, but the test must exercise the real path.)
- **PR6 — local uses `EmailTools` (Task 4):** the local stdio server registers `EmailTools.email_send`
  with `require_scope=lambda _: None` + a seat actor (NOT a separate unguarded tool) so it shares
  validation/caps/denial-audit. Add local tests: non-allowlisted `to`, missing body, rate trip → each
  raises before the fake client + emits `email_send_denied`.
- **PR7 — Task 1 config test:** assert `list(s.to_allowlist) == ["arb@example.com"]` (Settings holds
  a tuple).
- **PR8 — R2 denial-audit test TABLE (Task 3):** parametrize over EVERY reject class — empty/oversize/
  control-char subject, missing/oversize body, malformed `to`, non-allowlisted `to`, missing scope,
  rate-min trip, rate-day trip — each asserting: user exception type + exactly one `email_send_denied`
  (with `reason`) + zero client/network calls; plus a denial-sink-raises-still-surfaces test.
- **PR9 — misc:** `email_send` is `async def` (Task 3); wire injected `now` into the rate check and add
  sliding-window RECOVERY tests (advance `now` past 60s and past 24h → allowed again); `register_email_tool`
  and the local server both gate on `settings.send_enabled` (default `"1"`); offload the sync
  `httpx.post` off the event loop in the async door wrapper (`anyio.to_thread.run_sync`); add matcher
  tests `@evil.com:arb@example.com`, `arb@example.com\\@evil.com`, `user@examp1e.com`,
  `arb@example.com.evil.com`, `user@evil-example.com` (all reject/deny).

## Global Constraints
- **No new dependency** — `httpx` is already a dep (used in `src/arb_memory`). Use it for the POST.
- **Mirror ARB Files** (`src/arb_files/{config,audit,store}.py`, `mcp/{door_tools,door_wire,local_server}.py`)
  for style, injection seams (`http_post`/`now`/`audit_sink`/`client_factory`), and tests.
- **Fail-closed** — every validation/scope/cap error raises BEFORE any network call.
- **Secrets** — `envs/arb-email.env` (0600, gitignored). Never echo the token.
- **Run tests:** `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_email/<f> -v`.
- TDD, commit per task.

---

### Task 1: config + address matcher (the security core)

**Files:** Create `src/arb_email/__init__.py`, `src/arb_email/config.py`, `src/arb_email/addresses.py`;
Test `tests/arb_email/__init__.py`, `tests/arb_email/test_config.py`, `tests/arb_email/test_addresses.py`.

**Interfaces (per spec + R5/R6/R7/R8):**
- `config.Settings` (frozen) + `load_settings(env) -> Settings` — fields per spec Config table.
  `to_allowlist` parse: comma-split, trim, lower, **drop empties**; empty result → `ValueError` (R8).
  `default_to` MUST pass `recipient_allowed(default_to, to_allowlist)` at load → else `ValueError` (R8).
- `addresses.parse_single_recipient(raw) -> str` and `addresses.recipient_allowed(addr, allowlist) -> bool`.

- [ ] **Step 1 — failing tests** (the bypass matrix is the point):
```python
# tests/arb_email/test_addresses.py
import pytest
from arb_email.addresses import parse_single_recipient as P, recipient_allowed as A

@pytest.mark.parametrize("ok", ["arb@example.com", "arb@example.com", "a.b+x@mail.example.com"])
def test_valid_single_address_normalises_lower(ok):
    out = P(ok); assert out == ok.strip().lower()

@pytest.mark.parametrize("bad", [
    '"evil@x.com" <arb@example.com>', '"arb@example.com" <evil@x.com>',
    "a@f.com, b@evil.com", "arb@example.com\r\nBcc: v@x.com", "arb@example.com\tx",
    "arb@example.com (comment)", "mark(c)@example.com", "arb@example.com;evil@x.com",
    "arb@example.com evil@x.com", "", "no-at", "a@@b", "a@b@c", "arb@example.com\x7f",
    "arb@example.com\x85", "arb@example.com\u2028", "arb@example.com\u2029",
])
def test_rejects_every_bypass(bad):
    with pytest.raises(ValueError):
        P(bad)

def test_allowlist_exact_address():
    assert A("arb@example.com", ["arb@example.com"]) is True
    assert A("evil@example.com", ["arb@example.com"]) is False

def test_allowlist_domain_exact_host_no_suffix_bug():
    al = ["@example.com"]
    assert A("x@example.com", al) is True
    assert A("x@evilexample.com", al) is False   # endswith bug guard
    assert A("x@mail.example.com", al) is False   # subdomain denied (exact host)

def test_recipient_allowed_failclosed_on_malformed():
    assert A("", ["@example.com"]) is False
    assert A("a@b@c", ["@example.com"]) is False
```
```python
# tests/arb_email/test_config.py
import pytest
from arb_email.config import load_settings
BASE = {"ARB_EMAIL_POSTMARK_TOKEN": "tok"}
def test_defaults_explicit_address_allowlist():
    s = load_settings(BASE)
    assert s.sender == "arb@example.com" and s.stream == "arb"
    assert s.default_to == "arb@example.com" and s.to_allowlist == ["arb@example.com"]
def test_missing_token_fails():
    with pytest.raises(ValueError): load_settings({})
def test_default_to_must_be_in_allowlist():
    with pytest.raises(ValueError):
        load_settings({**BASE, "ARB_EMAIL_TO_ALLOWLIST": "@other.com"})  # default_to not covered
def test_empty_allowlist_after_drop_fails():
    with pytest.raises(ValueError):
        load_settings({**BASE, "ARB_EMAIL_TO_ALLOWLIST": " , "})
def test_body_cap_default_bytes():
    assert load_settings(BASE).body_max == 102400
```
- [ ] **Step 2** — run, confirm fail (ModuleNotFound).
- [ ] **Step 3 — implement** `addresses.py` then `config.py`:
```python
# src/arb_email/addresses.py
from __future__ import annotations
from email.utils import parseaddr

_BAD = {"<", ">", '"', ","}
def _has_ctrl(s: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 or c in ("", "", "") for c in s)

def parse_single_recipient(raw: str) -> str:
    if not raw or _has_ctrl(raw) or any(c in raw for c in _BAD):
        raise ValueError("invalid recipient")
    name, addr = parseaddr(raw)
    if name or not addr or addr.strip().lower() != raw.strip().lower():
        raise ValueError("invalid recipient")
    if addr.count("@") != 1:
        raise ValueError("invalid recipient")
    local, host = addr.split("@")
    if not local or not host or "." not in host:
        raise ValueError("invalid recipient")
    return addr.strip().lower()

def recipient_allowed(addr: str, allowlist: list[str]) -> bool:
    if addr.count("@") != 1:
        return False
    host = addr.split("@")[1]
    if not host:
        return False
    for entry in allowlist:
        if entry.startswith("@"):
            if host == entry[1:]:
                return True
        elif addr == entry:
            return True
    return False
```
```python
# src/arb_email/config.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping
from arb_email.addresses import recipient_allowed

@dataclass(frozen=True)
class Settings:
    token: str
    api_url: str = "https://api.postmarkapp.com/email"
    sender: str = "arb@example.com"
    stream: str = "arb"
    default_to: str = "arb@example.com"
    to_allowlist: tuple[str, ...] = ("arb@example.com",)
    send_enabled: bool = True
    subject_max: int = 255
    body_max: int = 102400
    rate_per_min: int = 10
    rate_per_day: int = 100

def load_settings(env: Mapping[str, str]) -> Settings:
    token = env.get("ARB_EMAIL_POSTMARK_TOKEN")
    if not token:
        raise ValueError("ARB_EMAIL_POSTMARK_TOKEN required")
    default_to = env.get("ARB_EMAIL_DEFAULT_TO", "arb@example.com").strip().lower()
    raw_al = env.get("ARB_EMAIL_TO_ALLOWLIST")
    allow = [e.strip().lower() for e in raw_al.split(",")] if raw_al else [default_to]
    allow = [e for e in allow if e]
    if not allow:
        raise ValueError("ARB_EMAIL_TO_ALLOWLIST empty")
    if not recipient_allowed(default_to, allow):
        raise ValueError("ARB_EMAIL_DEFAULT_TO not in allowlist")
    def _int(k, d): v = env.get(k); return int(v) if v else d
    return Settings(
        token=token, api_url=env.get("ARB_EMAIL_API_URL", "https://api.postmarkapp.com/email"),
        sender=env.get("ARB_EMAIL_FROM", "arb@example.com"),
        stream=env.get("ARB_EMAIL_STREAM", "arb"), default_to=default_to, to_allowlist=tuple(allow),
        send_enabled=env.get("ARB_EMAIL_SEND_ENABLED", "1") == "1",
        subject_max=_int("ARB_EMAIL_SUBJECT_MAX", 255), body_max=_int("ARB_EMAIL_BODY_MAX", 102400),
        rate_per_min=_int("ARB_EMAIL_RATE_PER_MIN", 10), rate_per_day=_int("ARB_EMAIL_RATE_PER_DAY", 100),
    )
```
- [ ] **Step 4** — run, PASS. - [ ] **Step 5** — commit `feat(arb-email): config + fail-closed single-recipient address matcher`.

---

### Task 2: EmailClient (Postmark) — normalized To, allowlisted payload, log-only audit

**Files:** Create `src/arb_email/client.py`, `src/arb_email/audit.py`; Test `tests/arb_email/test_client.py`.

**Interface (R1/R6):** `EmailClient(settings, *, http_post=None, now=None, audit_sink=None)` with
`send(subject, html_body, text_body, *, to, actor) -> dict`. `http_post(url, json, headers) -> (status, obj)`.

- [ ] **Step 1 — failing tests:**
```python
# tests/arb_email/test_client.py
import pytest
from arb_email.config import load_settings
from arb_email.client import EmailClient
BASE = {"ARB_EMAIL_POSTMARK_TOKEN": "tok"}
def _client(resp=(200, {"ErrorCode": 0, "MessageID": "mid-1"}), audit=None):
    calls = {}
    def post(url, json, headers): calls["url"]=url; calls["json"]=json; calls["headers"]=headers; return resp
    c = EmailClient(load_settings(BASE), http_post=post, audit_sink=audit)
    return c, calls
def test_send_ok_returns_message_id_and_fixed_from_stream():
    c, calls = _client()
    out = c.send("Hi", "<b>x</b>", None, to="arb@example.com", actor="seat")
    assert out["sent"] and out["message_id"] == "mid-1"
    assert calls["json"]["From"] == "arb@example.com" and calls["json"]["MessageStream"] == "arb"
    assert calls["json"]["To"] == "arb@example.com"
    assert set(calls["json"]) <= {"From","To","Subject","MessageStream","HtmlBody","TextBody"}  # no Headers/Cc/Bcc/ReplyTo
def test_payload_to_is_normalized():
    c, calls = _client()
    c.send("Hi", "x", None, to="arb@example.com", actor="s")  # client trusts caller; assert it lowercases via parse
    assert calls["json"]["To"] == "arb@example.com"
def test_postmark_error_is_runtime_not_silent():
    c, _ = _client(resp=(200, {"ErrorCode": 406, "Message": "inactive recipient"}))
    with pytest.raises(RuntimeError, match="406"):
        c.send("Hi", "x", None, to="arb@example.com", actor="s")
def test_non_2xx_runtime():
    c, _ = _client(resp=(500, {}))
    with pytest.raises(RuntimeError):
        c.send("Hi", "x", None, to="arb@example.com", actor="s")
def test_post_send_audit_failure_does_not_raise():
    def boom(_): raise RuntimeError("sink down")
    c, _ = _client(audit=boom)
    out = c.send("Hi", "x", None, to="arb@example.com", actor="s")  # already sent → must NOT raise
    assert out["sent"]
def test_body_cap_is_bytes():
    c, _ = _client()
    with pytest.raises(ValueError):
        c.send("Hi", "x"*1 + "é"*60000, None, to="arb@example.com", actor="s")  # >100KiB utf-8
```
- [ ] **Step 2** — run, fail.
- [ ] **Step 3 — implement** (`audit.py` mirrors `src/arb_files/audit.py`'s `default_audit_sink`):
```python
# src/arb_email/client.py
from __future__ import annotations
import logging
from arb_email.addresses import parse_single_recipient
log = logging.getLogger("arb_email.client")
class EmailClient:
    def __init__(self, settings, *, http_post=None, now=None, audit_sink=None):
        self.s = settings; self._post = http_post or self._default_post
        self.audit_sink = audit_sink
        from datetime import datetime, timezone
        self._now = now or (lambda: datetime.now(timezone.utc))
    def _default_post(self, url, json, headers):
        import httpx
        r = httpx.post(url, json=json, headers=headers, timeout=20)
        try: obj = r.json()
        except Exception: obj = {}
        return r.status_code, obj
    def send(self, subject, html_body, text_body, *, to, actor) -> dict:
        if not subject or len(subject) > self.s.subject_max:
            raise ValueError("invalid subject")
        if not (html_body or text_body):
            raise ValueError("body required")
        for b in (html_body, text_body):
            if b is not None and len(b.encode("utf-8")) > self.s.body_max:
                raise ValueError("body too large")
        norm = parse_single_recipient(to)                      # R1: payload To == normalized
        payload = {"From": self.s.sender, "To": norm, "Subject": subject, "MessageStream": self.s.stream}
        if html_body is not None: payload["HtmlBody"] = html_body
        if text_body is not None: payload["TextBody"] = text_body
        try:
            status, obj = self._post(self.s.api_url, payload, {"X-Postmark-Server-Token": self.s.token,
                                     "Accept": "application/json", "Content-Type": "application/json"})
        except Exception as exc:
            raise RuntimeError("email backend unavailable; retry") from exc
        if status // 100 != 2:
            raise RuntimeError(f"postmark http {status}")
        code = obj.get("ErrorCode")
        if code != 0:
            raise RuntimeError(f"postmark error {code}: {obj.get('Message')}")
        message_id = obj.get("MessageID")
        try:
            if self.audit_sink: self.audit_sink({"op": "email_send", "actor": actor, "to": norm,
                "subject": subject, "message_id": message_id, "stream": self.s.stream, "ts": self._now().isoformat()})
        except Exception:
            log.exception("email_send audit failed (email already sent); deadlettered")
        return {"sent": True, "message_id": message_id, "to": norm, "stream": self.s.stream}
```
- [ ] **Step 4** — PASS. - [ ] **Step 5** — commit.

---

### Task 3: EmailTools (door) — scope + sliding caps + tool-layer validation + denial audit (R2/R9)

**Files:** Create `src/arb_email/mcp/__init__.py`, `src/arb_email/mcp/door_tools.py`; Test
`tests/arb_email/test_door_tools.py`.

**Interface:** `EmailTools(client, settings, *, require_scope=None, actor=None, now=None, audit_sink=None)`
→ `email_send(subject, html_body=None, text_body=None, to=None)`. Per R2, the TOOL runs scope → rate
(sliding per-min + per-day) → subject/body validation → `to` parse+allowlist, emitting best-effort
`email_send_denied` for each reject before calling `client.send`.

- [ ] **Step 1 — failing tests:** scope reject → PermissionError + denial audit; non-allowlisted `to` →
  ValueError + denial audit (`reason`); control-char subject → ValueError + denial audit
  (`reason="subject"`); rate-per-min and rate-per-day each trip → ValueError + denial audit; happy path
  delegates to a fake client with the normalized `to`. (Inject `require_scope`, `actor`, `now`,
  `audit_sink`, and a fake client.)
- [ ] **Step 2** — fail. **Step 3** — implement (mirror `arb_files/mcp/door_tools.py` `_check_rate`,
  but two windows; wrap each pre-send check to emit `email_send_denied` best-effort then raise).
  **Step 4** — PASS. **Step 5** — commit.

---

### Task 4: local stdio server + run entrypoint (R4 kill switch gates local too)

**Files:** Create `src/arb_email/mcp/local_server.py`, `src/arb_email/run.py`,
`tools/arb-email-local/README.md`; Test `tests/arb_email/test_local_server.py`. Add
`pyproject.toml` console script `arb-email-local-mcp = "arb_email.run:run_local_mcp"`.

- [ ] Tests: `build_local_server` registers `email_send`; when `send_enabled` is False it raises/refuses
  to register (R4). - [ ] Implement mirroring `arb_files` local server; `run_local_mcp` loads settings,
  refuses if `not settings.send_enabled`, builds `EmailClient` + tool, serves stdio. - [ ] Commit.

---

### Task 5: door wiring + server.py scope edit (R3)

**Files:** Create `src/arb_email/mcp/door_wire.py`; Modify `src/arb_memory/mcp/server.py`; Test
`tests/arb_email/test_door_wiring.py`.

- [ ] **server.py:** add `"email.send"` to `valid_scopes` (NOT `default_scopes`); call
  `register_email_tool(server, os.environ)` after the files registration.
- [ ] **door_wire.register_email_tool(server, env, *, client_factory=None) -> bool`:** no-op (False)
  unless `ARB_EMAIL_POSTMARK_TOKEN` set AND `ARB_EMAIL_SEND_ENABLED == "1"`; construction in try/except
  → log + False (fail-soft); registers an `email_send` wrapper (docstring states From-fixed,
  allowlisted recipients, `arb` stream, model-supplied subject/body). Does NOT mutate scopes.
- [ ] Tests (runtime, like `arb_files/test_door_wiring`): no-op without token / with `SEND_ENABLED!=1`;
  fail-soft on construction error; registers `email_send` with a fake client; **`"email.send" in
  valid_scopes and not in default_scopes`** (R3 hygiene). - [ ] Confirm memory door still imports without
  ARB Email env. - [ ] Commit.

---

### Task 6: E2E (real Postmark, env-guarded)

**Files:** Create `tests/arb_email/e2e_send.py` (NOT collected by default — guarded by `ARB_EMAIL_E2E=1`).

- [ ] Script: load `envs/arb-email.env`; assert a deliberately non-allowlisted `to` (e.g.
  `nobody@example.com`) raises **before** any network (pure-validation, always runs); then if
  `ARB_EMAIL_E2E=1`, one real send to `arb@example.com` via the `arb` stream → assert `sent` +
  MessageID. Run: `set -a; . envs/arb-email.env; set +a; ARB_EMAIL_E2E=1 PYTHONPATH=src .venv/bin/python -m tests.arb_email.e2e_send`. - [ ] Commit.

---

## Self-review (against spec + R1–R9)
- Matcher (R5/R7) + bypass matrix → Task 1. Config validation (R6/R8) → Task 1. Normalized payload To
  (R1) + bytes cap (R6) + log-only audit + ErrorCode mapping → Task 2. Tool-layer validation + denial
  audit (R2) + sliding caps (R9) + scope → Task 3. Local kill switch (R4) → Task 4. Scope wiring honesty
  (R3) + fail-soft → Task 5. E2E + pre-network reject → Task 6. From/stream fixed asserted in Task 2.
