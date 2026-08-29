#!/usr/bin/env bash
# Run ONE reviewing seat CONFINED (decision panel Option C). The seat runs in a container whose
# read-surface is the image + the fixture (RO) + the seat's auth; the scenario/answer-key is absent.
# The canary runs FIRST and the review is refused if the boundary does not hold. The container IS the
# boundary, so the engine runs with its own sandbox bypassed (panel nesting answer).
#
# Seats:
#   codex — config.toml mounted RO into a writable CODEX_HOME; authenticates with the operator-exported
#           OPENAI_API_KEY. The container never sees ~/.codex/auth.json.
#   agy   — the agy-home docker volume (one-time OAuth login persisted; macOS keychain doesn't
#           transplant, so agy is authed once interactively into the volume and reused).
#
# Usage: confined-review.sh <seat> <fixture-dir> <base-sha> <head-sha> <scenario.json>
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${ARB_EVAL_IMAGE:-arb-eval-seat:latest}"
SEAT="$1"; FIXTURE="$(cd "$2" && pwd)"; BASE="$3"; HEAD="$4"; SCENARIO="$5"

# 1) prove THIS fixture's jail excludes the answer-key before any scored read
"$HERE/canary.sh" "$FIXTURE" "$SCENARIO" || exit 43

# 2) the review brief — class list mirrors the schema TAXONOMY so findings are matcher-compatible
CLASSES="cors, tls-transport, secrets-in-logs, auth-on-endpoint, authorization-scoping, input-trust, pii-logging-retention, egress, correctness, perf, logic, test-gap"
PROMPT="You are a code reviewer with shell + file tools. The change under review is \
\`git -C /fixture diff ${BASE}..${HEAD}\`. INVESTIGATE the checkout at /fixture (open files, trace call \
sites, verify against the rest of the repo — do NOT reason from the diff alone). Report each genuine \
defect on its OWN line EXACTLY as: <class> | <file>:<line> | <one-line description>, where <class> is \
one of: ${CLASSES}. If none: no issues. No prose, no markdown."

set +e
case "$SEAT" in
  codex)
    CODEX_CONFIG="${CODEX_CONFIG_DIR:-$HOME/.codex}"
    : "${OPENAI_API_KEY:?confined-review: export OPENAI_API_KEY — codex in the container authenticates by API key only)}"
    docker run --rm \
      -e ARB_EVAL_MODEL="${ARB_EVAL_MODEL:-}" \
      -e ARB_PROV_NONCE="${ARB_PROV_NONCE:-}" \
      -v "$FIXTURE":/fixture:ro \
      -e OPENAI_API_KEY \
      -v "$CODEX_CONFIG/config.toml":/config-ro/config.toml:ro \
      --tmpfs /work \
      "$IMAGE" bash -lc '
        mkdir -p /work/h && cp /config-ro/config.toml /work/h/ 2>/dev/null || true
        export CODEX_HOME=/work/h
        model_args=()
        if [ -n "${ARB_EVAL_MODEL:-}" ]; then model_args=(--model "$ARB_EVAL_MODEL"); fi
        version="$(codex --version 2>/dev/null | head -n 1 || true)"
        [ -n "$version" ] || version="UNKNOWN"
        codex exec "${model_args[@]}" --dangerously-bypass-approvals-and-sandbox --cd /fixture "$0"
        if [ -n "${ARB_PROV_NONCE:-}" ]; then
          version_json="$(printf "%s" "$version" | sed "s/\\\\/\\\\\\\\/g; s/\"/\\\\\"/g")"
          printf "ARB_PROV_%s{\"codex\":\"%s\"}</ARB_PROV_%s>\n" "$ARB_PROV_NONCE" "$version_json" "$ARB_PROV_NONCE"
        fi
      ' "$PROMPT" ;;
  agy)
    AGY_VOL="${ARB_AGY_VOLUME:-agy-home}"
    docker run --rm \
      -e HOME=/seat \
      -e ARB_EVAL_MODEL="${ARB_EVAL_MODEL:-}" \
      -e ARB_PROV_NONCE="${ARB_PROV_NONCE:-}" \
      -v "$FIXTURE":/fixture:ro \
      -v "$AGY_VOL":/seat/.gemini \
      "$IMAGE" bash -lc '
        model_args=()
        if [ -n "${ARB_EVAL_MODEL:-}" ]; then model_args=(--model "$ARB_EVAL_MODEL"); fi
        version="$(agy --version 2>/dev/null | head -n 1 || true)"
        [ -n "$version" ] || version="UNKNOWN"
        cd /fixture && agy "${model_args[@]}" --print --dangerously-skip-permissions "$0"
        if [ -n "${ARB_PROV_NONCE:-}" ]; then
          version_json="$(printf "%s" "$version" | sed "s/\\\\/\\\\\\\\/g; s/\"/\\\\\"/g")"
          printf "ARB_PROV_%s{\"agy\":\"%s\"}</ARB_PROV_%s>\n" "$ARB_PROV_NONCE" "$version_json" "$ARB_PROV_NONCE"
        fi
      ' "$PROMPT" ;;
  *)
    echo "confined-review: unknown seat '$SEAT' (codex|agy)" >&2; exit 2 ;;
esac
rc=$?
set -e
[ "$rc" -eq 43 ] && exit 1
exit "$rc"
