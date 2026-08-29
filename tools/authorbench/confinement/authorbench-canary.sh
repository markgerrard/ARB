#!/usr/bin/env bash
# Authorbench confinement canary. Usage: authorbench-canary.sh author|judge <mount-dir>
set -euo pipefail

ROLE="${1:?role required}"
SURFACE="${2:?mount surface required}"
IMAGE="${ARB_EVAL_IMAGE:-arb-eval-seat:latest}"

case "$ROLE" in
  author)
    TOKENS="${AUTHORBENCH_OUTCOME_TOKENS:-docs/superpowers/reviews/2026-07-03-arb-watch-history-authoring-bakeoff.md}
${AUTHORBENCH_IDENTITY_TOKENS:-AUTHOR_IDENTITY_TOKEN}"
    ;;
  judge)
    TOKENS="${AUTHORBENCH_IDENTITY_TOKENS:-AUTHOR_IDENTITY_TOKEN}"
    ;;
  *) echo "authorbench-canary: unknown role '$ROLE' (author|judge)" >&2; exit 2 ;;
esac

docker run --rm --network=none -v "$SURFACE":/surface:ro -e TOKENS="$TOKENS" "$IMAGE" bash -lc '
  set -euo pipefail
  ls /surface >/dev/null || { echo "CANARY FAIL: surface not readable"; exit 2; }
  printf "%s\n" "$TOKENS" | grep -v "^$" > /work-tok
  hit=$(grep -rlFf /work-tok /surface 2>/dev/null | head -1 || true)
  img_hit=$(find / -xdev -type f ! -path /work-tok 2>/dev/null | xargs grep -lFf /work-tok 2>/dev/null | head -1 || true)
  if [ -n "$hit$img_hit" ]; then
    echo "CANARY FAIL: forbidden token present in read-surface: ${hit}${img_hit}"; exit 1
  fi
  echo "CANARY OK: read-surface clean for role"
'
