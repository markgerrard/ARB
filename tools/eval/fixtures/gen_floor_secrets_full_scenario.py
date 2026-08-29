#!/usr/bin/env python3
"""Generate scenarios/floor-secrets-full.json from the built fixture repo.

The fixture source is deliberately marker-free (a seat must not be able to pattern-match a
"# SEED" comment), so seed/control locations are resolved here by a unique code signature per
line. Rebuild the repo first (build_floor_secrets_full.sh), then run this to emit the scenario
with correct line numbers. Manifest below is the human-authored map of the 5 distinct seed
mechanisms + 19 distinct control loci; edit it, not the JSON.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent / "repos" / "floor-secrets-full"
OUT = Path(__file__).resolve().parents[1] / "scenarios" / "floor-secrets-full.json"
REPO_DECLARED = "../fixtures/repos/floor-secrets-full"
CLS = "secrets-in-logs"

# (id, file, symbol, signature-substring, description)
SEEDS = [
    ("S1-token-info-log", "auth.py", "login", 'login ok for %s token=%s',
     "login() logs the bearer api_token in plaintext"),
    ("S2-key-in-error", "handlers.py", "handle_charge", 'charge failed for key %s',
     "handle_charge() logs the caller-provided api_key in its error-path handler (except block)"),
    ("S3-config-debug-dump", "admin.py", "dump_config", 'effective config: %r", cfg.as_dict()',
     "dump_config() debug-dumps the whole config dict including secret fields"),
    ("S4-conn-repr-leak", "client.py", "fetch", 'db call failed on conn=%r',
     "fetch() logs conn=%r in its error handler; the conn dict carries the password (_pw), so it leaks"),
    ("S5-cred-in-url", "client.py", "call_upstream", 'calling upstream %s %s',
     "call_upstream() logs the URL, which can carry basic-auth credentials in its userinfo"),
]

CONTROLS = [
    ("c01-token-present-bool", "auth.py", "login_failure", 'token_present=%s',
     "logs only whether a token was present (boolean), not its value"),
    ("c02-opaque-token-id", "auth.py", "refresh", 'token refresh for %s token_id=%s',
     "logs an opaque server-side token id (a non-secret reference), not the token itself"),
    ("c03-user-id-only", "auth.py", "logout", 'logout for user %s',
     "logs the user id only"),
    ("c04-url-sanitized", "client.py", "call_safe", 'method, sanitized',
     "logs a URL with userinfo and query stripped"),
    ("c05-host-only", "client.py", "call_host_only", 'request method=%s host=%s',
     "logs method and host only"),
    ("c06-status-only", "client.py", "report_status", 'upstream responded status=%s',
     "logs the response status code only"),
    ("c07-field-names", "handlers.py", "validate", 'missing fields %s',
     "logs the missing field NAMES, not their values"),
    ("c08-exc-type-only", "client.py", "fetch_quiet", 'type(exc).__name__',
     "logs the exception type name only, not the traceback"),
    ("c09-correlation-id", "handlers.py", "handle_with_trace", 'handling request id=%s',
     "logs a generated request/correlation id"),
    ("c10-masked-dump", "admin.py", "dump_config_masked", 'effective config: %r", safe',
     "dumps config with secret keys masked to ***"),
    ("c11-public-subset", "admin.py", "dump_public_settings", 'public settings: %r',
     "dumps only the non-secret subset (region, app_name)"),
    ("c12-key-count", "admin.py", "config_summary", 'config has %d keys',
     "logs the count of config keys (metadata only)"),
    ("c13-source-name", "config.py", "load", 'config loaded from source=%s',
     "logs which source config came from, not the values"),
    ("c14-query-template", "db.py", "run_query", 'executing query: %s',
     "logs the query template; bound params are passed separately and not logged"),
    ("c15-row-count", "db.py", "log_result", 'query returned %d rows',
     "logs the returned row count"),
    ("c16-host-no-pw", "db.py", "connect", 'connecting to db at %s:%s',
     "logs db host:port; the password argument is not logged"),
    ("c17-hashed-fp", "metrics.py", "record_active", 'active user fp=%s',
     "logs a non-reversible sha256 fingerprint of the user id"),
    ("c18-flag-bool", "metrics.py", "record_flag", 'feature flag %s enabled=%s',
     "logs a feature flag name and boolean state"),
    ("c19-verify-result", "metrics.py", "verify_webhook", 'webhook signature valid=%s',
     "logs the signature verification result, not the signing secret"),
]


# why-clean clusters (the panel's adjudicated idioms). Controls in one cluster are correlated, so
# nu_s's CI is computed on the cluster count (11 effective), not the nominal 19 (P1 instance 5).
CLUSTERS = {
    "c01-token-present-bool": "log-boolean", "c18-flag-bool": "log-boolean",
    "c19-verify-result": "log-boolean",
    "c02-opaque-token-id": "log-opaque-id", "c03-user-id-only": "log-opaque-id",
    "c09-correlation-id": "log-opaque-id",
    "c04-url-sanitized": "log-reduced-url", "c05-host-only": "log-reduced-url",
    "c06-status-only": "log-numeric-metadata", "c12-key-count": "log-numeric-metadata",
    "c15-row-count": "log-numeric-metadata",
    "c10-masked-dump": "dump-redacted-object", "c11-public-subset": "dump-redacted-object",
    "c17-hashed-fp": "log-irreversible-hash",
    "c07-field-names": "log-field-names-not-values",
    "c08-exc-type-only": "log-exception-type",
    "c13-source-name": "log-config-source",
    "c14-query-template": "log-query-template-no-params",
    "c16-host-no-pw": "omit-secret-arg",
}


def _line_of(fname, symbol, sig):
    # Scope the signature search to the named function's body so similar code in different functions
    # (e.g. a seed and a control with near-identical log calls) resolves to the right line.
    text = (REPO / fname).read_text().splitlines()
    starts = [i for i, ln in enumerate(text) if ln.lstrip().startswith(f"def {symbol}(")]
    if len(starts) != 1:
        raise SystemExit(f"def {symbol}( in {fname}: expected 1, got {len(starts)}")
    start = starts[0]
    end = next((j for j in range(start + 1, len(text)) if text[j].lstrip().startswith("def ")), len(text))
    hits = [k + 1 for k in range(start, end) if sig in text[k]]
    if len(hits) != 1:
        raise SystemExit(f"signature {sig!r} in {fname}:{symbol}: expected 1 hit, got {hits}")
    return hits[0]


def _loc(fname, symbol, sig):
    return {"file": fname, "line": _line_of(fname, symbol, sig), "symbol": symbol}


def main():
    if not REPO.exists():
        raise SystemExit(f"build the fixture first: {REPO} missing")
    scenario = {
        "id": "floor-secrets-full",
        "description": "Full-power secrets-in-logs floor: 5 distinct leak mechanisms (token-in-log, "
                       "key-in-error, whole-config dump, DSN-via-traceback, cred-in-URL) + 19 distinct "
                       "plausible-but-clean control loci. 5 seeds = I_min and 19 controls = T, so this "
                       "is the first scenario that can yield a CLASS-LEVEL PASS/FAIL (at repeats giving "
                       ">= T=19 trials). Rebuild repo with build_floor_secrets_full.sh; regenerate this "
                       "with gen_floor_secrets_full_scenario.py.",
        "subject": {
            "repo": REPO_DECLARED,
            "base": _read_sha("base"),
            "head": _read_sha("head"),
        },
        "seeded_defects": [
            {"id": i, "class": CLS, "legible": True, "location": _loc(f, sym, sig), "description": d}
            for (i, f, sym, sig, d) in SEEDS
        ],
        "control_loci": [
            {"id": i, "class": CLS, "cluster": CLUSTERS[i], "location": _loc(f, sym, sig),
             "description": d}
            for (i, f, sym, sig, d) in CONTROLS
        ],
        "panel": [
            {"seat": "codex", "model": "gpt-5.5", "harness": "codex"},
            {"seat": "agy", "model": "gemini", "harness": "agy-print"},
        ],
        "power": {"V": 0.40, "nu": 0.10, "alpha": 0.05, "I_min": 5, "R_min": 3, "matcher_gate": 0.85},
    }
    OUT.write_text(json.dumps(scenario, indent=2) + "\n")
    print(f"wrote {OUT} — {len(SEEDS)} seeds, {len(CONTROLS)} controls")


def _read_sha(which):
    import subprocess
    ref = "HEAD" if which == "head" else "HEAD~1"
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", ref],
                          capture_output=True, text=True, check=True).stdout.strip()


if __name__ == "__main__":
    sys.exit(main())
