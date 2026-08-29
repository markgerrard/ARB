#!/usr/bin/env bash
# Self-setup from the repo: build the confined eval-seat image and prove the absence boundary.
# Portable — plain `docker` (macOS+OrbStack or Linux/CI). Idempotent. Run from anywhere.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EVAL="$(cd "$HERE/.." && pwd)"
IMAGE="${ARB_EVAL_IMAGE:-arb-eval-seat:latest}"

echo "[1/3] build image $IMAGE"
docker build -t "$IMAGE" "$HERE"

echo "[2/3] build the secrets fixture (deterministic)"
bash "$EVAL/fixtures/build_floor_secrets_full.sh" >/dev/null

echo "[3/3] canary — prove the jail excludes the answer-key by construction"
"$HERE/canary.sh" "$EVAL/fixtures/repos/floor-secrets-full" "$EVAL/scenarios/floor-secrets-full.json"

echo "[ok] confined eval-seat image ready + boundary proven. A confined dispatch sets"
echo "     ARB_SEAT_CONFINED_ROOT so the pipeline's fail-closed guard passes meaningfully."
