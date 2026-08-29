#!/usr/bin/env bash
# Authorbench role runner. Usage:
#   confined-authorbench.sh author|judge <mode> [args...]
set -euo pipefail

ROLE="${1:?role required}"
MODE="${2:-run}"
shift 2 || true

IMAGE="${ARB_EVAL_IMAGE:-arb-eval-seat:latest}"
CODEX_CONFIG="${CODEX_CONFIG_DIR:-$HOME/.codex}"
# Credentials: the container never sees ~/.codex/auth.json. codex authenticates with the API key the
# operator exports (OPENAI_API_KEY); only config.toml (model/settings, no secrets) is mounted read-only.
: "${OPENAI_API_KEY:?confined-authorbench: export OPENAI_API_KEY — codex in the container authenticates by API key only)}"

case "$ROLE" in
  author|judge) ;;
  *) echo "confined-authorbench: unknown role '$ROLE' (author|judge)" >&2; exit 2 ;;
esac

if [ "$MODE" = "path-read" ]; then
  TARGET="${1:?target path required}"
  docker run --rm --network=none --tmpfs /work "$IMAGE" bash -lc '
    if cat "$1" >/dev/null 2>&1; then
      echo "unexpected host path readable: $1" >&2
      exit 1
    fi
    echo "host path absent from jail"
    exit 0
  ' _ "$TARGET"
  exit 0
fi

PROMPT="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MOUNTS=()
CANARY_SURFACE=""
if [ "$ROLE" = "author" ]; then
  WORKSPACE="${AUTHORBENCH_WORKSPACE:?AUTHORBENCH_WORKSPACE required for author role}"
  AUTHOR_VISIBLE="${AUTHORBENCH_AUTHOR_VISIBLE:?AUTHORBENCH_AUTHOR_VISIBLE required for author role}"
  MOUNTS+=(-v "$WORKSPACE":/work:ro -v "$AUTHOR_VISIBLE":/author-visible:ro)
  CANARY_SURFACE="$AUTHOR_VISIBLE"
  CODEX_WORKDIR="/work"
else
  EXPORT="${AUTHORBENCH_EXPORT:?AUTHORBENCH_EXPORT required for judge role}"
  DRAFT="${AUTHORBENCH_DRAFT:?AUTHORBENCH_DRAFT required for judge role}"
  FACT_KEY="${AUTHORBENCH_FACT_KEY:?AUTHORBENCH_FACT_KEY required for judge role}"
  RUBRIC="${AUTHORBENCH_RUBRIC:?AUTHORBENCH_RUBRIC required for judge role}"
  MOUNTS+=(-v "$EXPORT":/export:ro -v "$DRAFT":/draft.md:ro -v "$FACT_KEY":/fact_key.yaml:ro -v "$RUBRIC":/rubric.yaml:ro)
  CANARY_SURFACE="$(dirname "$DRAFT")"
  CODEX_WORKDIR="/export"
fi

"$SCRIPT_DIR/authorbench-canary.sh" "$ROLE" "$CANARY_SURFACE"

docker run --rm \
  -v "$CODEX_CONFIG/config.toml":/config-ro/config.toml:ro \
  -e OPENAI_API_KEY \
  "${MOUNTS[@]}" \
  -e CODEX_WORKDIR="$CODEX_WORKDIR" \
  --tmpfs /home/authorbench \
  "$IMAGE" bash -lc '
    set -euo pipefail
    mkdir -p /home/authorbench/.codex
    cp /config-ro/config.toml /home/authorbench/.codex/ 2>/dev/null || true
    export HOME=/home/authorbench
    export CODEX_HOME=/home/authorbench/.codex
    export ARB_MEMORY_REDIS_URL=
    export ARB_AUDIT_REDIS_URL=
    codex exec --dangerously-bypass-approvals-and-sandbox --cd "$CODEX_WORKDIR" "$0"
  ' "$PROMPT"
