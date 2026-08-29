#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${ARB_EVAL_REAL_BASE_SHA:?set ARB_EVAL_REAL_BASE_SHA before building the real fixture}"
BASE_URL="${ARB_EVAL_REAL_BASE_URL:-https://github.com/nsidnev/fastapi-realworld-example-app.git}"
OUT="$HERE/repos/floor-real-correctness"
SRC="$HERE/src/real-correctness"
rm -rf "$OUT"
git clone -q "$BASE_URL" "$OUT"
git -C "$OUT" checkout -q "$ARB_EVAL_REAL_BASE_SHA"
rm -rf "$OUT/.git"
git -C "$OUT" init -q
git -C "$OUT" config user.email arb-eval@example.invalid
git -C "$OUT" config user.name "ARB Eval"
git -C "$OUT" add .
GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z git -C "$OUT" commit -q -m "real fixture base"
for patch in "$SRC"/*.patch; do
  [ -e "$patch" ] || continue
  git -C "$OUT" apply "$patch"
done
git -C "$OUT" add .
GIT_AUTHOR_DATE=2026-01-01T00:00:01Z GIT_COMMITTER_DATE=2026-01-01T00:00:01Z git -C "$OUT" commit -q -m "real correctness seeds"
