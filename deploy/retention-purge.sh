#!/bin/sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
RETENTION_DSN="${ARB_RETENTION_DSN:?set ARB_RETENTION_DSN to the dedicated retention-role DSN}"

docker compose -f "$COMPOSE_FILE" exec -T eval env \
  ARB_MEMORY_DSN="$RETENTION_DSN" \
  ARB_EVAL_RETENTION_DAYS=56 \
  python -m arb_memory eval-purge

docker compose -f "$COMPOSE_FILE" exec -T transcript env \
  ARB_MEMORY_DSN="$RETENTION_DSN" \
  ARB_TRANSCRIPT_RETENTION_DAYS=56 \
  python -m arb_memory transcript-purge
