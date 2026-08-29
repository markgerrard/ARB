```bash
# Slice 1d-iv: ordinary dispatch is store-before-send via dispatch_authority.
# 1) Short-lived FABA driver publishes the brief (holds ARB_MEMORY_REDIS_URL):
/<bridge-clone>/scripts/arb-memory-harness-publish \
  --target-agent-id codex-<project>-<workspace> \
  --brief <brief-path> \
  > /tmp/<task>.receipt.json
# 2) Non-FABA enqueue (no publish credential) through the single authority:
FROM_AGENT_ID=claude-<project>-<workspace> \
BRANCH=<your-current-branch> \
AGENT_ENV_FILE=<path-to-the-app-worktree>/.env \
env -u ARB_MEMORY_REDIS_URL \
/<bridge-clone>/scripts/dispatch-dev \
  --engine codex \
  --target-id codex-<project>-<workspace> \
  --timeout 5400 \
  --run-id "$RID" \
  --artefact-id "$(jq -r .artefact_id /tmp/<task>.receipt.json)" \
  --version "$(jq -r .version /tmp/<task>.receipt.json)" \
  --receipt /tmp/<task>.receipt.json \
  --brief <brief-path> \
  > /tmp/<task>.out 2> /tmp/<task>.err
```

`dispatch-dev` wraps the Go client edge (`tools/go-client`, auto-built on first use;
`USE_BASH_DISPATCH=1` falls back to the raw Python `scripts/agent-dispatch`) and
AUTO-DEFAULTS a meaningful `--run-id` (from the `--brief` path slug, or
`<target>-<branch>-<HHMMSS>`) when one isn't given — so it never hits the
`--run-id`/`--adhoc` hard-refuse the raw `agent-dispatch`/`go-client` binaries enforce as of
2026-07-01. Ordinary request/worktree_run **must** pass the pre-minted
`--artefact-id`/`--version`/`--receipt`/`--brief` quartet; free-form positional task
strings were removed in Slice 1d-iv (enqueue only via `dispatch_authority.publish_and_enqueue`).
Still mint one yourself for a panel/multi-round workflow —
`RID=panel-<slug>-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 3)`, reused verbatim on
every seat in that round — because the auto-default is per-call (different target/timestamp
per seat unless they share one brief path), so it won't group a multi-seat
panel under one label on its own. See "Auditing a review/design panel" below.
