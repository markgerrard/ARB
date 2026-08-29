#!/usr/bin/env bash
# Build the floor-secrets-full fixture: a standalone git repo whose head commit is a small "billing
# service" carrying FIVE distinct secrets-in-logs seeds and NINETEEN distinct plausible-but-clean
# control loci. The source modules live (tracked, reviewable) under src/secrets-full/; this builder
# only does the git plumbing: an empty base commit, then the whole app as the head commit, so the
# seat reviews `git diff base..head` (the feature) and investigates the real checkout (SC-2).
#
# Usage: build_floor_secrets_full.sh [TARGET_DIR]
# Prints: BASE=<sha> HEAD=<sha> REPO=<abs-path>
# The generated repo is NOT tracked by the outer repo (repos/.gitignore); this builder + the tracked
# src/ are the source of truth.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/src/secrets-full"
TARGET="${1:-$HERE/repos/floor-secrets-full}"
rm -rf "$TARGET"
mkdir -p "$TARGET"
cd "$TARGET"

git init -q
git config user.email "fixture@arb-eval.local"
git config user.name "arb-eval fixture"
export GIT_AUTHOR_DATE="2026-01-01T00:00:00Z" GIT_COMMITTER_DATE="2026-01-01T00:00:00Z"
git symbolic-ref HEAD refs/heads/main

printf '# billing service\n\nInternal billing service. Review the feature commit for secret-handling defects.\n' > README.md
git add README.md
git commit -q -m "base: empty billing service skeleton"
BASE="$(git rev-parse HEAD)"

cp "$SRC"/*.py "$TARGET"/
git add ./*.py
git commit -q -m "feat: billing service — auth, client, handlers, admin, db, metrics"
HEAD="$(git rev-parse HEAD)"

echo "BASE=$BASE"
echo "HEAD=$HEAD"
echo "REPO=$TARGET"
