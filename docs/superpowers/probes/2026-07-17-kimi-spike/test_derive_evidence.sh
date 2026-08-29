#!/bin/zsh
# Adversarial self-test for derive_evidence.py --against (r3/sol P1: the checker
# was vacuous — it printed a correct block but never compared it). A green run of
# THIS script is the proof the checker is non-vacuous. Run before trusting --against.
set -e
HERE="${0:A:h}"
PY=/Users/<user>/<workspace>/.venv/bin/python
SPEC=/Users/<user>/<workspace>/docs/superpowers/specs/2026-07-17-kimi-runtime-surface-probe-design-v3.md
SHA=da4c2d2
tmp=$(mktemp -d)

# 1. clean spec PASSES
$PY "$HERE/derive_evidence.py" $SHA --against "$SPEC" >/dev/null 2>&1 \
  && echo "PASS: clean spec accepted" || { echo "FAIL: clean spec rejected"; exit 1; }

# 2. corrupted block FAILS (sol's exact mutation)
sed 's/| ask-log lines | 71 |/| ask-log lines | 999 |/' "$SPEC" > "$tmp/mut.md"
if $PY "$HERE/derive_evidence.py" $SHA --against "$tmp/mut.md" >/dev/null 2>&1; then
  echo "FAIL: corrupted block accepted — checker is VACUOUS"; exit 1
else
  echo "PASS: corrupted block rejected"
fi

# 3. missing block FAILS
grep -v 'ask-log lines' "$SPEC" > "$tmp/noblock.md"
$PY "$HERE/derive_evidence.py" $SHA --against "$tmp/noblock.md" >/dev/null 2>&1 \
  && { echo "FAIL: doc with damaged block accepted"; exit 1; } \
  || echo "PASS: damaged block rejected"

rm -rf "$tmp"
echo "ALL ADVERSARIAL TESTS PASSED — checker is non-vacuous"
