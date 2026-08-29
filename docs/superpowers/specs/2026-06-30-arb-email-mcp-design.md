# ARB Email MCP — design

> Status: **design, panel-reviewed → revised** (2026-06-30). Third ARB companion plane after ARB
> Memory (searchable notes) and ARB Files (opaque blobs). ARB Email = **send transactional email**.

## Design-panel outcome (2026-06-30)

Reviewed by codex + cold-Opus + pi-GLM — all **SOUND_WITH_CHANGES** (see the panel's reports for the
full record). Required fixes (folded below + into the spec):

- **Scope wiring** (pi-GLM): door scopes are fixed at `build_server()` (`server.py:324`); `register_*`
  cannot add them. `email.send` is added to **`valid_scopes` only** (grantable on request), **never
  `default_scopes`** — so send is an explicit grant, not inherited by every connector. (The original
  "door_wire adds scopes" claim was impossible; corrected.)
- **To matcher** (Opus P0 + codex + pi-GLM): parse a **single** RFC-5322 addr-spec; reject
  display-name/angle-addr, any comma/multi-recipient, and control chars; match the **bare parsed
  address** by exact-equality / parsed-host suffix — never substring on the raw input.
- **Default allowlist = explicit-address** (`arb@example.com`); domain-wildcard is **opt-in**
  (3/3 unanimous — the operator fork resolves to the tighter default).
- **Audit-after-send = log-only / deadletter, never raise** (email is non-idempotent; raising → retry
  → duplicate). Distinguish "send failed" from "send ok, audit degraded".
- **Daily cap + kill switch**, **denial audit** (record rejected/abusive attempts), **control-char +
  allowlisted-key-set payload** (no `Headers`/`ReplyTo`/`Cc`/`Bcc`), subject = semi-sensitive audit.

Confirmed sound: From/stream-fixed, mirror-ARB-Files, httpx (no new dep), fail-soft wiring, single
`email_send` v1, `ErrorCode!=0 → RuntimeError`. Do **not** accept a `to` list in v1.

## Purpose

Let agents — **seats** and, via the existing ARB Memory door, **claude.ai / ChatGPT** — send
transactional email (notifications, reports, alerts) through **Postmark**. The canonical use is
"email Mark a result/alert," but the tool is general (subject + body + recipient).

Backend: **Postmark** transactional API (`https://api.postmarkapp.com/email`). Sender **fixed** to
`arb@example.com`, message stream **`arb`**. Server token disk-only in `envs/arb-email.env`
(gitignored, 0600). Smoke-tested 2026-06-30 (`ErrorCode 0`, sender + stream valid).

## The threat that shapes everything: an email-send tool is an abuse vector

Unlike ARB Memory/Files (which act on *your own* stores), email **leaves your trust boundary and
reaches third parties** under *your domain's reputation*. Exposed to claude.ai/ChatGPT connectors —
which can be **prompt-injected** — an unconstrained `email_send` is a phishing/spam cannon that would
also torch `example.com`'s sender reputation. So the design's spine is **containment of who can be
emailed and as whom**, not feature richness.

## Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Backend | **Postmark** via `httpx` (already a dep — no new dependency) |
| 2 | **Sender (`From`)** | **FIXED `arb@example.com`** from env — **never client-settable** (anti-spoof; a client can't send as anyone else) |
| 3 | **Recipient (`To`) policy** | **Allowlist.** `to` defaults to `ARB_EMAIL_DEFAULT_TO` (`arb@example.com`); a client-supplied `to` must match `ARB_EMAIL_TO_ALLOWLIST` (default: domain `example.com`). Non-allowlisted → `ValueError` **before** any send. This is the core abuse containment. |
| 4 | Message stream | **FIXED `arb`** from env (not client-settable) |
| 5 | Transports | **Mirror ARB Files**: `email_send` on the existing ARB Memory **door** (OAuth, scope-gated) + a **local stdio MCP** (`arb-email-local`) for seats |
| 6 | Scope | new OAuth scope **`email.send`** (door), enforced like `files.write` |
| 7 | Rate limit | per-token, **conservative** (`email_send_rate_per_min` default **10**) — an email flood is costlier than a file flood |
| 8 | Audit | every send emits an audit event (actor, to, subject, message_id, stream, ts) — like the ARB Files delete audit; emit-failure not swallowed |
| 9 | Tool surface | **one tool: `email_send`** (KISS). No read/status tool (Postmark message search needs an *account* token we deliberately don't hold). |

## Architecture (mirrors ARB Files)

```
 claude.ai ─┐                ARB Memory door  (+ email_send, scope email.send)
 ChatGPT  ──┼─ OAuth ──▶  ───────────────────────────────────────────────▶ Postmark API
            │                                                                (arb@example.com,
 seats ── stdio ──▶  arb-email-local (direct, EmailClient holds token) ────▶  stream "arb")
```

| Unit | File | Responsibility |
|------|------|----------------|
| Config | `src/arb_email/config.py` | load + validate `ARB_EMAIL_*`; defaults; parse the To-allowlist |
| Client | `src/arb_email/client.py` | `EmailClient.send(to, subject, html_body, text_body, *, actor)` → POST to Postmark; validate To-allowlist + From-fixed; map `ErrorCode != 0` → `RuntimeError`; audit hook; injectable `http_post`/`now` for tests |
| Door tool | `src/arb_email/mcp/door_tools.py` | `EmailTools.email_send` — scope (`email.send`) + rate-limit + cap checks, then `client.send` |
| Door wiring | `src/arb_email/mcp/door_wire.py` | `register_email_tool(server, env, *, client_factory=None)` — fail-soft (config/back-end error logs + skips; memory door unaffected); adds `email.send` to door scopes |
| Local | `src/arb_email/mcp/local_server.py` + `run.py` | stdio MCP `arb-email-local` for seats |
| Provisioning | `tools/arb-email-local/` | seat install docs |

## Tool contract — `email_send`

`email_send(subject: str, html_body: str | None = None, text_body: str | None = None, to: str | None = None) -> dict`
returns `{sent: True, message_id, to, stream}`.

- **Validation (fail-closed, before any send):**
  - `subject` required, non-empty, ≤ 255 chars.
  - at least one of `html_body` / `text_body`; each ≤ `email_body_max` (default 100 KiB).
  - `to`: if omitted → `ARB_EMAIL_DEFAULT_TO`; if given → must be a syntactically valid address **and**
    match the allowlist (`ARB_EMAIL_TO_ALLOWLIST`, comma-sep exact addresses and/or `@domain`
    entries); else `ValueError`. (Multiple recipients out of scope v1 — single `to`.)
  - `From` and `MessageStream` are **server-set**, never from the client.
- **Scope:** door requires `email.send` (fail-closed `PermissionError`); local stdio is trusted (no scope).
- **Send:** POST to Postmark; `ErrorCode == 0` → success (return `MessageID`); non-zero → `RuntimeError`
  carrying Postmark's `Message` (never a silent success). Network error → `RuntimeError` retry hint.
- **Audit:** on success, emit `{op:"email_send", actor, to, subject, message_id, stream, ts}`.

## Capability matrix

| Client | Can send |
|---|---|
| **Seat** (local stdio) | ✓ direct, To-allowlisted |
| **claude.ai / ChatGPT** (door) | ✓ with `email.send` scope, To-allowlisted — the model composes subject/body; `From`/stream/recipient-policy are server-enforced |

## Error handling

- Invalid/non-allowlisted `to`, empty subject, oversize body, missing body → `ValueError` before send.
- Missing `email.send` scope → `PermissionError` before send.
- Postmark `ErrorCode != 0` → `RuntimeError(Postmark Message)`; HTTP/network error → `RuntimeError` retry hint.
- Audit-emit failure → propagates (evidence plane; not swallowed).

## Testing

Unit (inject a fake `http_post` — no real Postmark): To-allowlist accept/reject (exact + domain + bad
domain + malformed); From always fixed; stream always fixed; subject/body validation + caps; scope
gate; rate limit trips; Postmark `ErrorCode != 0` → RuntimeError; success returns MessageID + audits.
E2E (env-guarded, real Postmark `arb` stream, to `arb@example.com`): one real send, assert
`ErrorCode 0` + MessageID.

## Out of scope (v1)

Multiple recipients / CC / BCC; attachments; templates; inbound/parse; delivery-status polling (needs
an account token we don't hold); an HTML sanitizer (the To-allowlist makes recipients trusted).

## Open fork for the operator (panel will weigh in)

**The `To` allowlist default.** Proposed: default `@example.com` domain (so the assistant can email
any example.com address) + the explicit `ARB_EMAIL_DEFAULT_TO`. Tighter alternative: allowlist
**only** `arb@example.com` (pure notify-Mark). Looser (NOT recommended): open `to`. This is the
one materially security-relevant choice; the design defaults to domain-allowlist and surfaces it.
