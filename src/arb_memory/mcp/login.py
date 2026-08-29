from __future__ import annotations

from datetime import datetime, timezone
import hmac
import html
import secrets
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from arb_memory.mcp import oauth_store
from arb_memory.mcp.config import load_settings, mcp_connect
from arb_memory.mcp.login_factors import verify_two_factor
from arb_memory.mcp.redirect_policy import OOB_REDIRECT_URI


# Client-independent global login-failure key. The per-client throttle is bypassable by
# registering fresh DCR clients; this sentinel bucket locks regardless of client_id.
_GLOBAL_KEY = "__global__"


def _default_conn_factory():
    return mcp_connect()


# Self-contained styled login page in the Mark Gerrard design system (warm paper, one clay accent,
# Newsreader serif + IBM Plex Mono, restraint). Fields stay empty (security); only the session + csrf
# tokens are substituted (HTML-escaped). Failures are returned separately as plain responses by login_post.
_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>ARB · Authorize</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --paper:#FAF8F4; --paper-sunk:#F3EFE8; --card:#FFFFFF;
  --ink-900:#1F1D1A; --ink-700:#45413B; --ink-500:#6E6960; --ink-400:#918B80;
  --line-200:#E7E2D8; --line-300:#D9D3C7;
  --clay-600:#9E4A2E; --clay-700:#823A22; --clay-100:#EFE0D6;
  --serif:'Newsreader',Georgia,'Times New Roman',serif;
  --mono:'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,monospace;
  --ease:cubic-bezier(.4,0,.2,1);
}
*{box-sizing:border-box}
html,body{margin:0}
body{
  min-height:100vh; display:flex; align-items:center; justify-content:center;
  background:var(--paper); color:var(--ink-700); font-family:var(--serif);
  -webkit-font-smoothing:antialiased; padding:24px;
}
::selection{background:var(--clay-100)}
.card{
  width:100%; max-width:25rem; background:var(--card);
  border:1px solid var(--line-200); border-radius:8px;
  box-shadow:0 1px 3px rgba(31,29,26,.07), 0 6px 16px -10px rgba(31,29,26,.18);
  padding:48px 40px;
}
.eyebrow{
  font-family:var(--mono); font-size:.6875rem; text-transform:uppercase;
  letter-spacing:.14em; color:var(--ink-500); text-align:center; margin:0 0 14px;
}
.brand{
  font-family:var(--serif); font-weight:600; font-size:3.25rem; line-height:1.04;
  letter-spacing:-.012em; color:var(--ink-900); text-align:center; margin:0;
}
.sub{
  font-family:var(--serif); font-size:1.0625rem; color:var(--ink-500);
  text-align:center; margin:8px 0 34px;
}
form{display:flex; flex-direction:column; gap:18px}
.field{display:flex; flex-direction:column; gap:7px}
label{
  font-family:var(--mono); font-size:.6875rem; text-transform:uppercase;
  letter-spacing:.1em; color:var(--ink-500);
}
input.t{
  font-family:var(--mono); font-size:.95rem; color:var(--ink-900);
  background:var(--paper-sunk); border:1px solid var(--line-300);
  border-radius:5px; padding:11px 13px; width:100%;
  transition:border-color 120ms var(--ease), background-color 120ms var(--ease), box-shadow 120ms var(--ease);
}
input.t::placeholder{color:var(--ink-400); letter-spacing:.3em}
input.t:focus{
  outline:none; border-color:var(--clay-600); background:var(--card);
  box-shadow:0 0 0 3px var(--clay-100);
}
button{
  margin-top:8px; font-family:var(--mono); font-size:.8125rem;
  text-transform:uppercase; letter-spacing:.1em; font-weight:500;
  color:var(--paper); background:var(--ink-900); border:1px solid var(--ink-900);
  border-radius:5px; padding:12px 16px; width:100%; cursor:pointer;
  transition:background-color 120ms var(--ease), border-color 120ms var(--ease);
}
button:hover{background:var(--ink-700); border-color:var(--ink-700)}
button:active{transform:translateY(.5px)}
button:focus-visible{outline:2px solid var(--clay-600); outline-offset:2px}
.foot{
  font-family:var(--mono); font-size:.625rem; color:var(--ink-400);
  text-align:center; letter-spacing:.06em; margin:28px 0 0;
}
</style>
</head>
<body>
  <main class="card">
    <p class="eyebrow">Agent Redis Bridge · Memory</p>
    <h1 class="brand">ARB</h1>
    <p class="sub">Authorize this connection</p>
    <form method="post" action="/login" autocomplete="off">
      <input type="hidden" name="session" value="__SESSION__">
      <input type="hidden" name="csrf_token" value="__CSRF__">
      <div class="field">
        <label for="passphrase">Passphrase</label>
        <input class="t" id="passphrase" name="passphrase" type="password"
               autocomplete="off" spellcheck="false" autofocus>
      </div>
      <div class="field">
        <label for="totp">Authenticator code</label>
        <input class="t" id="totp" name="totp" inputmode="numeric" maxlength="6"
               autocomplete="off" spellcheck="false" placeholder="000000">
      </div>
      <button type="submit">Continue</button>
    </form>
    <p class="foot">Encrypted · two-factor required</p>
  </main>
</body>
</html>"""


def _login_form(session_id: str, csrf_token: str) -> HTMLResponse:
    page = _LOGIN_PAGE.replace("__SESSION__", html.escape(session_id, quote=True)).replace(
        "__CSRF__", html.escape(csrf_token, quote=True)
    )
    # The page embeds a one-time CSRF token in markup — never let an intermediary cache it.
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


# Out-of-band completion page: same design system as the login page, shown instead of a redirect
# for a client that registered OOB_REDIRECT_URI (no loopback listener). The code is the only
# sensitive value here — same no-store/no-cache discipline as the login page's CSRF token.
_OOB_CODE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>ARB · Authorized</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --paper:#FAF8F4; --paper-sunk:#F3EFE8; --card:#FFFFFF;
  --ink-900:#1F1D1A; --ink-700:#45413B; --ink-500:#6E6960; --ink-400:#918B80;
  --line-200:#E7E2D8; --line-300:#D9D3C7;
  --clay-600:#9E4A2E; --clay-100:#EFE0D6;
  --serif:'Newsreader',Georgia,'Times New Roman',serif;
  --mono:'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,monospace;
}
*{box-sizing:border-box}
html,body{margin:0}
body{
  min-height:100vh; display:flex; align-items:center; justify-content:center;
  background:var(--paper); color:var(--ink-700); font-family:var(--serif);
  -webkit-font-smoothing:antialiased; padding:24px;
}
.card{
  width:100%; max-width:25rem; background:var(--card);
  border:1px solid var(--line-200); border-radius:8px;
  box-shadow:0 1px 3px rgba(31,29,26,.07), 0 6px 16px -10px rgba(31,29,26,.18);
  padding:48px 40px;
}
.eyebrow{
  font-family:var(--mono); font-size:.6875rem; text-transform:uppercase;
  letter-spacing:.14em; color:var(--ink-500); text-align:center; margin:0 0 14px;
}
h1{
  font-family:var(--serif); font-weight:600; font-size:1.75rem; line-height:1.15;
  letter-spacing:-.012em; color:var(--ink-900); text-align:center; margin:0 0 8px;
}
.sub{
  font-family:var(--serif); font-size:1.0625rem; color:var(--ink-500);
  text-align:center; margin:0 0 28px;
}
.code{
  font-family:var(--mono); font-size:1.25rem; color:var(--ink-900);
  background:var(--paper-sunk); border:1px solid var(--line-300);
  border-radius:5px 5px 0 0; border-bottom:none; padding:16px; text-align:center;
  word-break:break-all; user-select:all;
}
.copy-btn{
  display:block; width:100%; margin:0;
  font-family:var(--mono); font-size:.75rem; text-transform:uppercase;
  letter-spacing:.1em; font-weight:500;
  color:var(--paper); background:var(--ink-900); border:1px solid var(--ink-900);
  border-radius:0 0 5px 5px; padding:10px 16px; cursor:pointer;
  transition:background-color 120ms ease, border-color 120ms ease;
}
.copy-btn:hover{background:var(--ink-700); border-color:var(--ink-700)}
.copy-btn:active{transform:translateY(.5px)}
.copy-btn:focus-visible{outline:2px solid var(--clay-600); outline-offset:2px}
.foot{
  font-family:var(--mono); font-size:.625rem; color:var(--ink-400);
  text-align:center; letter-spacing:.06em; margin:28px 0 0;
}
</style>
</head>
<body>
  <main class="card">
    <p class="eyebrow">Agent Redis Bridge · Memory</p>
    <h1>Authorized</h1>
    <p class="sub">Copy this code into the app that requested it.</p>
    <p class="code" id="code">__CODE__</p>
    <button class="copy-btn" id="copy-btn" type="button">Copy</button>
    <p class="foot">This code is single-use and expires shortly</p>
  </main>
  <script>
    document.getElementById('copy-btn').addEventListener('click', function () {
      var btn = this;
      var text = document.getElementById('code').textContent;
      navigator.clipboard.writeText(text).then(function () {
        btn.textContent = 'Copied';
        setTimeout(function () { btn.textContent = 'Copy'; }, 1500);
      });
    });
  </script>
</body>
</html>"""


def _oob_code_page(code: str) -> HTMLResponse:
    page = _OOB_CODE_PAGE.replace("__CODE__", html.escape(code, quote=True))
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _session_cookie(response, value: str) -> None:
    response.set_cookie(
        "arbmem_login",
        value,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def login_routes(provider, *, settings=None, conn_factory=None) -> list[Route]:
    settings = settings or load_settings()
    conn_factory = conn_factory or _default_conn_factory

    async def login_get(request: Request):
        session_id = request.query_params.get("session") or request.cookies.get("arbmem_login")
        if not session_id:
            return PlainTextResponse("missing session", status_code=400)
        conn = conn_factory()
        session = oauth_store.get_login_session(conn, session_id)
        if session is None:
            return PlainTextResponse("invalid session", status_code=404)
        response = _login_form(session_id, session["csrf_token"])
        _session_cookie(response, session_id)
        return response

    async def login_post(request: Request):
        form = await request.form()
        session_id = str(form.get("session") or request.cookies.get("arbmem_login") or "")
        if not session_id:
            return PlainTextResponse("missing session", status_code=400)
        conn = conn_factory()
        session = oauth_store.get_login_session(conn, session_id)
        if session is None:
            return PlainTextResponse("invalid session", status_code=404)
        if session["fail_count"] >= 5:
            return PlainTextResponse("locked", status_code=429)
        client_id = str(session["authorize_state"].get("client_id") or "")
        if client_id and oauth_store.get_global_login_fail_count(
            conn,
            client_id,
            window_seconds=settings.login_ttl,
        ) >= 5:
            return PlainTextResponse("locked", status_code=429)
        if oauth_store.get_global_login_fail_count(
            conn,
            _GLOBAL_KEY,
            window_seconds=settings.login_ttl,
        ) >= settings.login_global_fail_cap:
            return PlainTextResponse("locked", status_code=429)
        supplied_csrf = str(form.get("csrf_token") or "")
        if not hmac.compare_digest(supplied_csrf, session["csrf_token"]):
            return PlainTextResponse("forbidden", status_code=403)
        if not verify_two_factor(
            str(form.get("passphrase") or ""),
            str(form.get("totp") or ""),
            settings=settings,
        ):
            fail_count = oauth_store.bump_fail(conn, session_id) or 0
            global_fail_count = (
                oauth_store.bump_global_login_fail(
                    conn,
                    client_id,
                    window_seconds=settings.login_ttl,
                )
                if client_id
                else 0
            )
            all_fail_count = oauth_store.bump_global_login_fail(
                conn,
                _GLOBAL_KEY,
                window_seconds=settings.login_ttl,
            )
            locked = (
                fail_count >= 5
                or global_fail_count >= 5
                or all_fail_count >= settings.login_global_fail_cap
            )
            return PlainTextResponse("locked" if locked else "forbidden", status_code=429 if locked else 403)

        conn.execute(
            "UPDATE mcp_auth.login_sessions SET verified_at = %s WHERE session_id = %s",
            (datetime.now(timezone.utc), session_id),
        )
        authorize_state = session["authorize_state"]
        if client_id:
            oauth_store.reset_global_login_fail(conn, client_id)
        oauth_store.reset_global_login_fail(conn, _GLOBAL_KEY)
        if hasattr(provider, "issue_authorization_code"):
            code = provider.issue_authorization_code(authorize_state)
        else:
            code = secrets.token_urlsafe(32)
        if authorize_state["redirect_uri"] == OOB_REDIRECT_URI:
            # No listener to redirect to -- display the code for the human to copy into the
            # client, instead of a Location redirect. Same session-cookie rotation either way.
            response = _oob_code_page(code)
        else:
            params = {"code": code}
            if authorize_state.get("state"):
                params["state"] = authorize_state["state"]
            separator = "&" if "?" in authorize_state["redirect_uri"] else "?"
            response = RedirectResponse(
                f"{authorize_state['redirect_uri']}{separator}{urlencode(params)}",
                status_code=302,
            )
        _session_cookie(response, secrets.token_urlsafe(32))
        return response

    return [
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
    ]
