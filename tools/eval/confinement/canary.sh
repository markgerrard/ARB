#!/usr/bin/env bash
# Confinement canary — proves the jail is ABSENCE-based, not deny-based (decision panel Option C).
# Runs the SAME image a seat runs in, mounts ONLY the fixture (read-only), and asserts:
#   (1) the fixture is fully readable (the investigation we measure), and
#   (2) the scenario/answer-key is ABSENT from the ENTIRE jail read-surface (image layers + the
#       fixture mount incl its .git) — denied-because-absent, at no path, complete mount manifest.
# This is the per-run boundary proof: don't trust the manifest, test it. Park the run on any failure.
# Portable: plain `docker` (works on Linux/CI and macOS+OrbStack). Usage: canary.sh <fixture-dir> <scenario.json>
set -euo pipefail
IMAGE="${ARB_EVAL_IMAGE:-arb-eval-seat:latest}"
FIXTURE="$(cd "$1" && pwd)"
SCENARIO="$2"
# The unique answer-key labels that must NOT appear anywhere the seat can read (IDs, not descriptive
# cluster tags — tags legitimately coincide with code, e.g. a 'clamp' cluster vs a clamp() function).
TOKENS="$(python3 - "$SCENARIO" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("\n".join([s["id"] for s in d["seeded_defects"]] + [c["id"] for c in d["control_loci"]]))
PY
)"
docker run --rm --network=none -v "$FIXTURE":/fixture:ro -e TOKENS="$TOKENS" "$IMAGE" bash -c '
  set -e
  ls /fixture >/dev/null || { echo "CANARY FAIL: fixture not readable"; exit 2; }
  printf "%s\n" "$TOKENS" | grep -v "^$" > /work-tok
  # image layers (root fs; -xdev does NOT cross into the /fixture bind mount) + the fixture separately.
  # Exclude the canary OWN token file /work-tok which contains the tokens by construction.
  img_hit=$(find / -xdev -type f ! -path /work-tok 2>/dev/null | xargs grep -lFf /work-tok 2>/dev/null | head -1 || true)
  fix_hit=$(grep -rlFf /work-tok /fixture 2>/dev/null | head -1 || true)
  # git OBJECTS: covers all objects (loose, packed, reflog-only, stash, dangling)
  hist_hit=$(git -C /fixture cat-file --batch-all-objects --batch 2>/dev/null | grep -a -Ff /work-tok | head -1 || true)
  if [ -n "$img_hit$fix_hit$hist_hit" ]; then
    echo "CANARY FAIL: answer-key token present in the jail read-surface: ${img_hit}${fix_hit}${hist_hit:+ <git-objects>}"; exit 1
  fi
  echo "CANARY OK: fixture readable; answer-key ABSENT from the entire jail read-surface (image + working tree + ALL git objects: loose/packed/unreachable/reflog/stash)"
'
