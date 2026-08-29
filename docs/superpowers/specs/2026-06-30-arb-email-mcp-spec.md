# ARB Email MCP — spec

> Status: **spec, for panel** (2026-06-30). Implements the panel-reviewed design
> `docs/superpowers/specs/2026-06-30-arb-email-mcp-design.md`. Read it first.

A Postmark-backed `email_send` tool: one tool on the existing ARB Memory **door** (OAuth) plus a
**local stdio MCP** for seats. Sender FIXED `arb@example.com`, stream FIXED `arb`, recipients
allowlisted. Mirrors ARB Files.

## Spec-panel revisions (BINDING — supersede conflicting text below)

Reviewed by codex + cold-Opus + pi-GLM — 3/3 SOUND_WITH_CHANGES, **no P0** (matcher confirmed
bypass-free; see the panel's reports for the full record). Apply:

- **R1 — payload `To` is the NORMALIZED address.** `parse_single_recipient` returns the canonical
  lowercased bare addr; the tool layer passes THAT to `client.send`, and the Postmark payload `"To"`
  is THAT value — **never the raw client string**. (The matcher's safety rests on the sent value ==
  the validated value.) Test: mixed-case/space-padded `to` → payload `To` is canonical.
- **R2 — all pre-send validation + denial audit live in the TOOL layer.** `EmailTools.email_send`
  (and the local tool) run scope → rate → **subject/body validation** → `to` parse+allowlist, emitting
  best-effort `email_send_denied` (never raises) for EVERY reject (control-char/oversize subject,
  missing/oversize body, malformed/non-allowlisted `to`, missing scope, rate trip) **before** any
  network, then re-raise the user error. `client.send` keeps the same checks as a defensive second
  line but is no longer the denial-audit owner. Tests: each reject class emits a denial event.
- **R3 — OAuth scope is NOT the containment (honesty fix).** Keep `email.send` in `valid_scopes`
  (grantable so the tool works), out of `default_scopes`. But the door auto-advertises `valid_scopes`
  and connectors request the advertised set, so any authorized connector effectively gets `email.send`
  — it is NOT a per-connector operator gate. The real containment is **From-fixed + explicit-address
  allowlist + per-min/day caps + the `SEND_ENABLED` kill switch + audit**. Drop any "explicit-grant
  only" security claim; the `not in default_scopes` test is hygiene, not containment.
- **R4 — `SEND_ENABLED` gates BOTH door and local** (one-knob shutdown): the local stdio server also
  refuses to register/serve `email_send` when `ARB_EMAIL_SEND_ENABLED != "1"`. (It's a
  registration/startup gate, not a hot runtime switch — documented.)
- **R5 — control chars:** reject `ord < 32` **OR `ord == 127` (DEL) OR U+0085/U+2028/U+2029** in
  `subject` and `to`.
- **R6 — body cap is BYTES:** `len(body.encode("utf-8")) <= body_max`.
- **R7 — `recipient_allowed` fail-closed:** if `addr` lacks exactly one `@` or has an empty host →
  `return False` (never `IndexError`). `@domain` match is **exact host equality** (so
  `x@evilexample.com` and subdomains are DENIED). Tests pin both.
- **R8 — config load validates:** `default_to` MUST itself pass the allowlist (else `ValueError`);
  allowlist parse drops empty entries (trailing comma) and fails closed (`ValueError`) if the result
  is empty.
- **R9 — rate windows:** both per-minute and per-day are **sliding monotonic windows** (mirror ARB
  Files' `time.monotonic` sliding window); both are per-process (documented caveat).

## Config — `src/arb_email/config.py`

`Settings` (frozen) from `ARB_EMAIL_*` env; `load_settings(env)`:

| field | env | default |
|---|---|---|
| `api_url` | `ARB_EMAIL_API_URL` | `https://api.postmarkapp.com/email` |
| `token` | `ARB_EMAIL_POSTMARK_TOKEN` | **required** |
| `sender` | `ARB_EMAIL_FROM` | `arb@example.com` |
| `stream` | `ARB_EMAIL_STREAM` | `arb` |
| `default_to` | `ARB_EMAIL_DEFAULT_TO` | `arb@example.com` |
| `to_allowlist` | `ARB_EMAIL_TO_ALLOWLIST` | **= `[default_to]`** (explicit-address; domain entries opt-in) |
| `send_enabled` | `ARB_EMAIL_SEND_ENABLED` | `"1"` for local; the **door** requires it `"1"` to register (kill switch) |
| `subject_max` | `ARB_EMAIL_SUBJECT_MAX` | 255 |
| `body_max` | `ARB_EMAIL_BODY_MAX` | 102400 (100 KiB) |
| `rate_per_min` | `ARB_EMAIL_RATE_PER_MIN` | 10 |
| `rate_per_day` | `ARB_EMAIL_RATE_PER_DAY` | 100 |

`to_allowlist` parse: comma-split, trim, lowercase; each entry is either a full address (`a@b.com`) or
a domain entry `@b.com`. Missing required token → `ValueError`. **Empty allowlist is invalid** (would
allow nothing or, if mis-handled, everything) → fail-closed `ValueError`.

## Address validation — `src/arb_email/addresses.py`

`parse_single_recipient(raw: str) -> str` — the load-bearing matcher primitive:

1. Reject if `raw` contains `,` (multi-recipient), any control char (`\r \n \t \0` or `ord < 32`), or
   `<`/`>`/`"` (display-name / angle-addr wrapping). → `ValueError`.
2. Parse with `email.utils.parseaddr`; require the result has **empty display-name** and a non-empty
   addr-spec equal (case-insensitively, after `.strip()`) to the input. Any divergence → `ValueError`.
3. Validate the addr-spec shape: exactly one `@`, non-empty local + host, host has a dot. → else `ValueError`.
4. Return the **lowercased bare addr-spec**.

`recipient_allowed(addr: str, allowlist: list[str]) -> bool` — `addr` is the parsed bare address
(lowercased). Allowed iff: `addr` exactly equals an address entry, **or** `addr`'s host (`addr.split("@")[1]`)
matches a `@domain` entry's domain exactly. Never substring/`in` on raw input.

## Postmark client — `src/arb_email/client.py`

`EmailClient(settings, *, http_post=None, now=None, audit_sink=None)`:

- `send(subject, html_body, text_body, *, to, actor) -> dict`:
  1. **Validate (fail-closed, before any network):** `subject` non-empty, `len ≤ subject_max`, no control
     chars; at least one of `html_body`/`text_body`, each `len ≤ body_max`; `to` (caller passes the
     resolved recipient) re-validated via `parse_single_recipient` + `recipient_allowed` → else
     `ValueError` (and the caller emits a denial audit — see Tools).
  2. **Build payload from an allowlisted key set ONLY:** `{"From": sender, "To": to, "Subject": subject,
     "MessageStream": stream}` + `HtmlBody`/`TextBody` when present. **Never** include `Headers`,
     `ReplyTo`, `Cc`, `Bcc`, or any client-derived key. Pass as a dict to the JSON poster (no string templating).
  3. POST to `api_url` with header `X-Postmark-Server-Token: token`. Non-2xx → `RuntimeError` (retry
     hint). JSON parse failure → `RuntimeError`.
  4. `ErrorCode == 0` → success: return `{sent: True, message_id: <MessageID>, to, stream}`. `ErrorCode
     != 0` → `RuntimeError(f"postmark error {code}: {Message}")` — **never** a silent success.
  5. **Post-send audit is log-only:** emit `{op:"email_send", actor, to, subject, message_id, stream,
     ts}` via `audit_sink`; if the sink raises, **catch + deadletter-log it** — never re-raise (the
     email is already sent; raising would cause a retry duplicate). Return the success dict regardless.
- Injectable `http_post(url, json, headers) -> (status, json_obj)` and `now()` for tests (no real Postmark).

## Tools

### Door — `src/arb_email/mcp/door_tools.py`

`EmailTools(client, settings, *, require_scope=None, actor=None)` — `email_send(subject, html_body=None,
text_body=None, to=None)`:

1. `self._require_scope("email.send")` → fail-closed `PermissionError`. *(scope is a gate, but the
   real containment is the allowlist + caps; see design.)*
2. Rate: per-actor per-minute **and** per-day (two in-memory windowed counters, keyed by `actor()`);
   over either → denial audit + `ValueError("rate limit exceeded")`. (Per-process; documented; a
   shared store is a future hardening.)
3. Resolve `to`: `to or settings.default_to`; `parse_single_recipient` + `recipient_allowed`. On
   reject → **denial audit** `{op:"email_send_denied", actor, to:<raw>, reason, subject, ts}` (best-effort,
   never raises) + `ValueError`.
4. `client.send(...)`.

`_actor()` and `_require_scope` mirror ARB Files (`get_access_token`; `client_id`).

### Local — `src/arb_email/mcp/local_server.py` + `run.py`

stdio MCP `arb-email-local` registering `email_send` (same validation/caps/allowlist; **no scope gate**
— trusted seat host). `actor` = seat id.

## Door wiring + scope — `src/arb_email/mcp/door_wire.py` and an edit to `src/arb_memory/mcp/server.py`

- **`server.py` (build_server):** append `"email.send"` to `valid_scopes` (so a client may *request* it);
  **do NOT add it to `default_scopes`** (explicit grant only). A test asserts `"email.send" in
  valid_scopes and "email.send" not in default_scopes`.
- **`door_wire.register_email_tool(server, env, *, client_factory=None) -> bool`:** returns `False`
  (no-op) unless `ARB_EMAIL_POSTMARK_TOKEN` is set **and** `ARB_EMAIL_SEND_ENABLED == "1"` (kill
  switch). Construction wrapped in try/except → log + `False` on any error (fail-soft; memory door
  unaffected). On success, registers the `email_send` wrapper (docstring: states From-fixed,
  allowlisted recipients, the `arb` stream, and that subject/body are model-supplied). It does **not**
  mutate OAuth scopes (that's the `server.py` edit).
- `server.py` calls `register_email_tool(server, os.environ)` after the memory + files registration.

## Audit

- **Success:** `email_send` (log-only post-send as above).
- **Denial:** `email_send_denied` for allowlist-reject, control-char/format-reject, missing scope, and
  rate-limit trips — best-effort (never raises, never blocks the user-visible error). Includes the raw
  proposed `to`, a reason, actor, subject, ts. `subject` is treated as semi-sensitive audit content.
- Reuse a small `src/arb_email/audit.py` `default_audit_sink` (structured JSON log), like ARB Files.

## Error handling (summary)

| condition | result |
|---|---|
| empty/oversize subject, control chars | `ValueError` before send |
| no body / oversize body | `ValueError` |
| `to` not a clean single allowlisted address | `ValueError` + denial audit |
| missing `email.send` scope (door) | `PermissionError` + denial audit |
| rate (min or day) exceeded | `ValueError` + denial audit |
| Postmark `ErrorCode != 0` / non-2xx / bad JSON | `RuntimeError` (never silent success) |
| post-send audit sink raises | caught + deadletter-logged; send still reported success |

## Testing (`tests/arb_email/`)

Unit (fake `http_post`):
- **address matcher (the security core):** accepts `arb@example.com`; **rejects**
  `"evil@x.com" <arb@example.com>`, `"arb@example.com" <evil@x.com>`, `a@f.com, b@evil.com`,
  `arb@example.com\r\nBcc: v@x.com`, display-name forms, `arb@example.com` → normalises lower.
- **allowlist:** exact-address allow/deny; `@domain` entry allows host, denies other host; empty
  allowlist → config `ValueError`; default allowlist = `[default_to]` (NOT domain).
- **From/stream fixed:** payload always carries `sender`/`arb`; client `to`/subject can't change them.
- **payload key set:** serialized body contains only the allowlisted keys — never `Headers`/`ReplyTo`/
  `Cc`/`Bcc` regardless of input (incl. a subject like `"ok\r\nBcc: v@x"` → rejected before send anyway).
- **caps:** subject/body oversize rejected; rate-per-min and rate-per-day each trip and emit denial audit.
- **scope:** door `email_send` raises `PermissionError` without `email.send` + denial audit.
- **Postmark mapping:** `ErrorCode 0` → MessageID + success audit; `ErrorCode != 0` → RuntimeError; non-2xx → RuntimeError.
- **audit ordering:** a sink that raises post-send → send still returns success (no re-raise), deadletter logged.
- **scope wiring:** `"email.send" in valid_scopes and not in default_scopes` (guards `server.py`).
- **door wiring:** no-op without token or with `ARB_EMAIL_SEND_ENABLED!=1`; fail-soft on construction error; registers `email_send` with a fake client.

E2E (env-guarded `ARB_EMAIL_E2E=1`, real Postmark `arb` stream → `arb@example.com`): one real send,
assert `ErrorCode 0` + a MessageID; assert a deliberately non-allowlisted `to` is rejected *before* any
network call.

## Out of scope (v1)

Multi-recipient/list/CC/BCC, attachments, templates, `ReplyTo`, custom `Headers`, delivery-status
polling, inbound, HTML sanitizer (recipients are allowlisted-trusted; email clients sandbox HTML).
Shared cross-process rate store (in-process documented).
