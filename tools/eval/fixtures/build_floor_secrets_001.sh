#!/usr/bin/env bash
# Build the floor-secrets-001 fixture: a standalone git repo with a clean base commit and a head
# commit that introduces ONE legible secrets-in-logs defect (the seed) plus ONE plausible-but-clean
# control locus (a security-adjacent log line that does NOT leak a secret — a flag there is a false
# positive). The seat reviews `git diff base..head` and investigates the checkout (SC-2).
#
# Usage: build_floor_secrets_001.sh [TARGET_DIR]
# Prints: BASE=<sha> HEAD=<sha> REPO=<abs-path>  (consumed by the scenario JSON)
#
# Idempotent: wipes and rebuilds TARGET_DIR. The fixture repo is NOT tracked by the outer repo
# (see tools/eval/fixtures/repos/.gitignore); this builder is the source of truth.
set -euo pipefail

TARGET="${1:-$(cd "$(dirname "$0")" && pwd)/repos/floor-secrets-001}"
rm -rf "$TARGET"
mkdir -p "$TARGET"
cd "$TARGET"

git init -q
git config user.email "fixture@arb-eval.local"
git config user.name "arb-eval fixture"
git symbolic-ref HEAD refs/heads/main

# --- base commit: clean auth module -------------------------------------------------
cat > auth.py <<'PY'
import logging

logger = logging.getLogger("auth")


def login(user_id, api_token):
    if not _verify(user_id, api_token):
        return False
    logger.info("login attempt for user %s", user_id)
    return True


def logout(user_id):
    logger.info("logout for user %s", user_id)
    return True


def _verify(user_id, api_token):
    return bool(user_id) and bool(api_token)


def _session_id(user_id):
    # a non-secret, rotating per-login identifier (safe to log)
    return f"sess-{abs(hash(user_id)) % 100000:05d}"
PY
git add auth.py
git commit -q -m "base: auth module (clean logging)"
BASE="$(git rev-parse HEAD)"

# --- head commit: introduce ONE seed + ONE clean control ----------------------------
cat > auth.py <<'PY'
import logging

logger = logging.getLogger("auth")


def login(user_id, api_token):
    if not _verify(user_id, api_token):
        return False
    # logs the bearer token in plaintext to the application log
    logger.info("login ok for user %s with token=%s", user_id, api_token)
    return True


def logout(user_id):
    # security-adjacent but clean: session id is a non-secret rotating identifier
    logger.info("logout for user %s session=%s", user_id, _session_id(user_id))
    return True


def _verify(user_id, api_token):
    return bool(user_id) and bool(api_token)


def _session_id(user_id):
    # a non-secret, rotating per-login identifier (safe to log)
    return f"sess-{abs(hash(user_id)) % 100000:05d}"
PY
git add auth.py
git commit -q -m "head: log token on login; add session id to logout log"
HEAD="$(git rev-parse HEAD)"

echo "BASE=$BASE"
echo "HEAD=$HEAD"
echo "REPO=$TARGET"
