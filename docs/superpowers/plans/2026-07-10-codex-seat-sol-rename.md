# Codex Seat `-sol` Rename Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **NOTE for this plan specifically:** Tasks 3–7 mutate live host state (launchd, Redis buses). They must run in the orchestrating session on this host (inline execution), not in an isolated worktree or subagent — a subagent can do Task 1 (repo edits) only.

**Goal:** Rename all 7 default codex seats from `codex-<project>-dev` to `codex-<project>-dev-sol` so the model tier (gpt-5.6-sol) is visible in the agent-id, matching the terra/luna convention.

**Architecture:** A seat's agent-id is derived at launch (`derive_agent_id`: `codex-<project>-<workspace>[-<role>]`), so the rename is a launchd migration — new plists that add the `sol` role, bootout of old labels, bootstrap of new — plus a repo commit that updates every live dispatch target and trusted-sender reference. Nothing about the model pin changes (`AGENT_MODEL=gpt-5.6-sol` already set per plist).

**Tech Stack:** launchd plists (PlistBuddy/plutil), bash, the bridge's own `scripts/dispatch-dev` for live verification, pytest for the touched modules.

## Global Constraints

- **Model pins unchanged:** every renamed seat keeps `AGENT_MODEL=gpt-5.6-sol` (verbatim).
- **Effort hazard (memory `codex-effort-mechanics`):** codex seats must only ever restart on bridge code ≥ `1ade57c`. Both clones are at `295b97a` (verified 2026-07-10), so this is satisfied — do NOT interleave a `git checkout` of older code with any seat restart.
- **Fleet-restart discipline:** before every `bootout`/`kickstart` of a shared seat, confirm the seat's last log event is `[reply-sent]` (or the log is idle) — never kill a seat mid-turn.
- **Test gate = targeted, never the full suite** (Mark, 2026-07-10 handoff): `pytest tests/test_wiki_refresh.py tests/test_learn_intake.py -q` is this migration's Python gate (~seconds). The full suite (~19 min) is explicitly NOT a gate here.
- **Protected-file gate:** `envs/*.env` are tracked config; every edit is a merge (single-line replace), never a rewrite. Run `git diff -- <file>` before committing and confirm shape: deleted no / moved no / added-or-changed yes (the one sender line only).
- **Do not rewrite history docs:** `docs/superpowers/{plans,specs,notes,reviews}/*` and old handoffs keep the old ids — they are records of what happened. Only living docs (listed in Task 1) change.
- **zsh gotcha:** any Redis key with a `:` after a variable must be written `"${VAR}:suffix"` (memory `zsh-colon-modifier-mangles-redis-keys`).

## The seat map (single source of truth for every task)

| # | Old agent-id | New agent-id | Old launchd label | New launchd label | Shape | Clone | Autostart |
|---|---|---|---|---|---|---|---|
| 1 | codex-bridge-dev | codex-bridge-dev-example | com.example.codex-bridge.bridge-dev | com.example.codex-bridge.bridge-dev-sol | launcher (`codex-dev`) | <workspace> | YES (RunAtLoad+KeepAlive) |
| 2 | codex-project-g-consult-dev | codex-project-g-consult-dev-sol | com.example.codex-bridge.project-g-dev | com.example.codex-bridge.project-g-dev-sol | launcher (`codex-dev`) | AgentRedisBridge | YES |
| 3 | codex-registry-x-dev | codex-registry-x-dev-sol | com.example.arbseat.codex-registry-x-dev | com.example.arbseat.codex-registry-x-dev-sol | direct module | AgentRedisBridge | no |
| 4 | codex-project-d-dev | codex-project-d-dev-sol | com.example.arbseat.codex-project-d-dev | com.example.arbseat.codex-project-d-dev-sol | direct module | AgentRedisBridge | no |
| 5 | codex-project-h-browser-dev | codex-project-h-browser-dev-sol | com.example.arbseat.codex-project-h-browser-dev | com.example.arbseat.codex-project-h-browser-dev-sol | direct module | AgentRedisBridge | no |
| 6 | codex-project-g-dev | codex-project-g-dev-sol | com.example.arbseat.codex-project-g-dev | com.example.arbseat.codex-project-g-dev-sol | direct module | AgentRedisBridge | no |
| 7 | codex-project-e-dev | codex-project-e-dev-sol | com.example.arbseat.codex-project-e-dev | com.example.arbseat.codex-project-e-dev-sol | direct module | **<workspace>** (venv) | no |

Label convention decision: seats keep their existing label *prefix* (`com.example.codex-bridge.*` for the two autostart seats, `com.example.arbseat.*` for the five no-autostart ones) with `-sol` appended — the prefix encodes the autostart contract, and unifying prefixes would erase that signal. terra/luna are untouched (already suffixed, already arbseat).

**How the id changes mechanically:**
- Launcher shape (#1–2): `scripts/agent-redis-bridge-systemd` parses instance `codex-dev-sol` → workspace=`dev`, role=`sol` → passes `--role sol`. Change is `ProgramArguments:1` from `codex-dev` to `codex-dev-sol`.
- Direct shape (#3–7): plists exec `python3 -m agent_redis_bridge --env-file … --engine codex …` with project/workspace coming from the env file. Change is appending two ProgramArguments entries: `--role`, `sol`.
- Both shapes then hit `derive_agent_id` (`src/agent_redis_bridge/bridge.py:2779`) → `codex-<project>-dev-sol`.

**Also renamed in each plist:** `Label`, `StandardOutPath`, `StandardErrorPath` (old id string → new id string). `AGENT_TRUSTED_SENDERS` values inside the 7 codex plists reference only `claude-*`/`human-*` senders — unchanged.

---

### Task 1: Repo reference cutover (code + tests + envs + living docs)

**Files:**
- Modify: `src/agent_redis_bridge/wiki_refresh.py:140-141,610`
- Modify: `src/agent_redis_bridge/learn_intake.py:31`
- Modify: `scripts/arb-memory-seat-e2e:19`
- Modify: `scripts/dispatch-dev:24,27,34` (comments — copy-paste examples)
- Test: `tests/test_wiki_refresh.py:547,549`, `tests/test_learn_intake.py:344`
- Modify: `envs/agent-sdk-opus-sub-dev.env:20`, `envs/agent-sdk-sonnet-sub-dev.env:20`, `envs/agent-sdk-haiku-sub-dev.env:20`, `envs/agent-sdk-sonnet-reviewer-dev.env:20`
- Modify: `skills/using-agent-bridge/SKILL.md:676,705`, `docs/orchestrator-patterns.md:419`, `docs/BACKLOG.md:269`, `docs/runbooks/arb-memory-seat-e2e.md:17,35,74`

**Interfaces:**
- Produces: repo state where every live dispatch target and trusted-sender for the bridge-dev codex seat is `codex-bridge-dev-example`. Tasks 4–6 rely on this commit being deployed to BOTH clones before any seat restarts.
- Leave alone: `tests/test_bridge_emit_vote.py`, `tests/arb_memory/test_visibility_seat.py` (fixture ids, arbitrary strings, not coupled to a live seat); `tests/test_learn_intake.py:293` (explicit-arg test, not a default); `envs/dev-project-g-consultant.env` + `envs/agent-sdk-*-dev.env` header comments (cosmetic prose); all historical specs/plans.

- [ ] **Step 1: Update the four default-assertion test lines to expect `-sol` (TDD red)**

`tests/test_wiki_refresh.py` lines 547 and 549:
```python
    assert resolve_reviewer("agy-print", {})["target_id"] == "codex-bridge-dev-example"
    assert resolve_reviewer("agent-sdk", {})["target_id"] == "codex-bridge-dev-example"
```
`tests/test_learn_intake.py` line 344:
```python
    assert [d[1] for d in dispatches] == ["codex-bridge-dev-example", "agy-bridge-dev", "pi-sdk-bridge-dev-glm"]
```

- [ ] **Step 2: Run the two test files — expect the three updated assertions to FAIL**

Run: `cd /Users/<user>/<workspace> && .venv/bin/python -m pytest tests/test_wiki_refresh.py tests/test_learn_intake.py -q`
Expected: 3 failures (`resolve_reviewer` ×2, seats-order ×1), everything else green.

- [ ] **Step 3: Update the three source files**

`src/agent_redis_bridge/wiki_refresh.py` — the `_REVIEWER_MAP` entries (lines 140–141):
```python
    "agent-sdk": {"engine": "codex", "target_id": "codex-bridge-dev-example", "timeout": 3600},
    "agy-print": {"engine": "codex", "target_id": "codex-bridge-dev-example", "timeout": 3600},
```
and the fallback (line 610):
```python
        return {"engine": "codex", "target_id": "codex-bridge-dev-example", "timeout": 3600}
```

`src/agent_redis_bridge/learn_intake.py` — the SEATS quorum (line 31):
```python
SEATS = [
    ("codex", "codex-bridge-dev-example"),
    ("agy-print", "agy-bridge-dev"),
    ("pi-sdk", "pi-sdk-bridge-dev-glm"),
]
```

`scripts/arb-memory-seat-e2e` (line 19):
```python
DEFAULT_WRITER_TARGET = "codex-bridge-dev-example"
```

- [ ] **Step 4: Run the two test files — expect all green**

Run: `cd /Users/<user>/<workspace> && .venv/bin/python -m pytest tests/test_wiki_refresh.py tests/test_learn_intake.py -q`
Expected: PASS (0 failures).

- [ ] **Step 5: Update the four asdk env files' trusted-sender line**

In each of `envs/agent-sdk-opus-sub-dev.env`, `envs/agent-sdk-sonnet-sub-dev.env`, `envs/agent-sdk-haiku-sub-dev.env`, `envs/agent-sdk-sonnet-reviewer-dev.env`, line 20 becomes:
```
AGENT_TRUSTED_SENDERS=codex-bridge-dev-example=trusted,claude-bridge-dev=trusted
```

- [ ] **Step 6: Update living docs + dispatch-dev example comments**

Exact substitution `codex-bridge-dev` → `codex-bridge-dev-example` at ONLY these locations:
- `scripts/dispatch-dev` lines 24, 27, 34 (comments)
- `skills/using-agent-bridge/SKILL.md` lines 676 and 705 (audit-roster examples: `seat:codex-bridge-dev` → `seat:codex-bridge-dev-example`)
- `docs/orchestrator-patterns.md` line 419 (`seat:codex-bridge-dev` example)
- `docs/BACKLOG.md` line 269
- `docs/runbooks/arb-memory-seat-e2e.md` lines 17, 35, 74

Then verify no live-surface stragglers:
```bash
grep -rn 'codex-bridge-dev\b' src/ scripts/ envs/ skills/ tests/ --include='*' | grep -v -- '-sol' | grep -v __pycache__
```
Expected output: only `tests/test_bridge_emit_vote.py`, `tests/arb_memory/test_visibility_seat.py`, `tests/test_learn_intake.py:293`, `tests/arb_memory/test_local_memory_mcp_env_file.py` (comment), and env-file header comments — the deliberate leave-alones above. Anything else = missed reference, fix before committing.

- [ ] **Step 7: Protected-file check + commit**

```bash
cd /Users/<user>/<workspace>
git diff --stat
git diff -- envs/   # confirm: each env diff is the single sender line (deleted no / moved no / changed yes)
git add src/agent_redis_bridge/wiki_refresh.py src/agent_redis_bridge/learn_intake.py \
        scripts/arb-memory-seat-e2e scripts/dispatch-dev \
        tests/test_wiki_refresh.py tests/test_learn_intake.py \
        envs/agent-sdk-opus-sub-dev.env envs/agent-sdk-sonnet-sub-dev.env \
        envs/agent-sdk-haiku-sub-dev.env envs/agent-sdk-sonnet-reviewer-dev.env \
        skills/using-agent-bridge/SKILL.md docs/orchestrator-patterns.md \
        docs/BACKLOG.md docs/runbooks/arb-memory-seat-e2e.md
git commit -m "refactor(seats): retarget live references codex-bridge-dev -> codex-bridge-dev-example

Part 1 of the 7-seat -sol rename migration (model tier in the agent-id,
matching terra/luna). Covers wiki-refresh reviewer map + fallback,
learn-intake quorum, e2e writer default, asdk trusted-sender envs, and
living docs. Seat plists cut over host-side in part 2."
```

---

### Task 2: Deploy the commit to both clones

**Interfaces:**
- Consumes: Task 1's commit on `dev`.
- Produces: both `/Users/<user>/<workspace>` and `/Users/<user>/AgentRedisBridge` at the new SHA. Tasks 3–6 must not start until this is done (seats load code at start; the four fleet-clone seats restart in Task 5).

- [ ] **Step 1: Push dev**

Run: `cd /Users/<user>/<workspace> && git push origin dev`
Expected: fast-forward push. CI is disabled by design — no run appears; do not wait for one.

- [ ] **Step 2: Pull the fleet clone and confirm SHA parity**

Run: `git -C /Users/<user>/AgentRedisBridge pull --ff-only && git -C /Users/<user>/AgentRedisBridge rev-parse --short HEAD && git -C /Users/<user>/<workspace> rev-parse --short HEAD`
Expected: both print the same SHA (Task 1's commit).

---

### Task 3: Generate the 7 new plists + rollback backups

**Files:**
- Create: `~/Library/LaunchAgents/com.example.codex-bridge.bridge-dev-sol.plist`, `com.example.codex-bridge.project-g-dev-sol.plist`, `com.example.arbseat.codex-{registry-x,project-d,project-h-browser,project-g,project-e}-dev-sol.plist`
- Create: `/Users/<user>/<workspace>/.claude/plist-backups/2026-07-10-sol-rename/` (rollback set — `.claude/` is gitignored)

**Interfaces:**
- Produces: 7 new plist files on disk (NOT yet bootstrapped) + a backup of the 7 old plists and the 3 asdk plists Task 6 edits. Task 4/5 bootstrap from these paths; the Rollback section restores from the backup dir.

- [ ] **Step 1: Back up all plists this migration touches**

```bash
BK=/Users/<user>/<workspace>/.claude/plist-backups/2026-07-10-sol-rename
mkdir -p "$BK"
cd ~/Library/LaunchAgents
cp com.example.codex-bridge.bridge-dev.plist com.example.codex-bridge.project-g-dev.plist \
   com.example.arbseat.codex-registry-x-dev.plist com.example.arbseat.codex-project-d-dev.plist \
   com.example.arbseat.codex-project-h-browser-dev.plist com.example.arbseat.codex-project-g-dev.plist \
   com.example.arbseat.codex-project-e-dev.plist \
   com.example.arbseat.asdk-bridge-dev-haiku45.plist com.example.arbseat.asdk-bridge-dev-opus48.plist \
   com.example.asdk-sonnet-bridge.bridge-dev.plist "$BK/"
ls "$BK" | wc -l   # expected: 10
```

- [ ] **Step 2: Generate the two launcher-shape plists (instance arg change)**

```bash
cd ~/Library/LaunchAgents
# Seat 1: codex-bridge-dev -> -sol
sed -e 's/com\.mark\.codex-bridge\.bridge-dev/com.example.codex-bridge.bridge-dev-sol/g' \
    -e 's|/tmp/codex-bridge\.bridge-dev\.launchd|/tmp/codex-bridge.bridge-dev-sol.launchd|g' \
    com.example.codex-bridge.bridge-dev.plist > com.example.codex-bridge.bridge-dev-sol.plist
/usr/libexec/PlistBuddy -c 'Set :ProgramArguments:1 codex-dev-sol' com.example.codex-bridge.bridge-dev-sol.plist
# Seat 2: codex-project-g-consult-dev -> -sol
sed -e 's/com\.mark\.codex-bridge\.project-g-dev/com.example.codex-bridge.project-g-dev-sol/g' \
    -e 's|/tmp/codex-bridge-project-g-dev\.launchd|/tmp/codex-bridge-project-g-dev-sol.launchd|g' \
    com.example.codex-bridge.project-g-dev.plist > com.example.codex-bridge.project-g-dev-sol.plist
/usr/libexec/PlistBuddy -c 'Set :ProgramArguments:1 codex-dev-sol' com.example.codex-bridge.project-g-dev-sol.plist
```

- [ ] **Step 3: Generate the five direct-shape plists (append `--role sol`)**

```bash
cd ~/Library/LaunchAgents
for p in registry-x project-d project-h-browser project-g project-e; do
  old="com.example.arbseat.codex-${p}-dev"
  new="${old}-sol"
  sed "s/${old}/${new}/g; s/codex-${p}-dev\.log/codex-${p}-dev-sol.log/g; s/codex-${p}-dev\.err/codex-${p}-dev-sol.err/g" \
      "${old}.plist" > "${new}.plist"
  /usr/libexec/PlistBuddy -c 'Add :ProgramArguments: string --role' "${new}.plist"
  /usr/libexec/PlistBuddy -c 'Add :ProgramArguments: string sol' "${new}.plist"
done
```

- [ ] **Step 4: Lint all 7 new plists and eyeball the two key fields**

```bash
cd ~/Library/LaunchAgents
for f in com.example.codex-bridge.bridge-dev-sol.plist com.example.codex-bridge.project-g-dev-sol.plist \
         com.example.arbseat.codex-{registry-x,project-d,project-h-browser,project-g,project-e}-dev-sol.plist; do
  plutil -lint "$f"
  echo "$f -> Label=$(/usr/libexec/PlistBuddy -c 'Print :Label' "$f")"
  /usr/libexec/PlistBuddy -c 'Print :ProgramArguments' "$f" | tail -4
done
```
Expected: 7× `OK`; every Label ends `-sol`; the two launcher plists show instance `codex-dev-sol`; the five direct plists end with `--role` / `sol`.

---

### Task 4: Cutover canary — seat 1 (`codex-bridge-dev` → `codex-bridge-dev-example`)

**Interfaces:**
- Consumes: Task 2 (code deployed), Task 3 (plists ready).
- Produces: one fully-verified renamed seat before the other six move (limits blast radius). Task 5 repeats the identical recipe ×6.

- [ ] **Step 1: Busy-check**

Run: `grep -E '\[turn-start\]|\[reply-sent\]' /tmp/codex-bridge.bridge-dev.launchd.log | tail -2`
Expected: last line is `[reply-sent]` (or no output = idle). If a `[turn-start]` is unanswered, WAIT for the turn to finish — do not proceed.

- [ ] **Step 2: Bootout old, bootstrap new, start**

```bash
launchctl bootout gui/501/com.example.codex-bridge.bridge-dev
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.example.codex-bridge.bridge-dev-sol.plist
# RunAtLoad=true on this seat: bootstrap starts it. Confirm:
sleep 5; launchctl list | grep codex-bridge.bridge-dev
```
Expected: exactly one line, `com.example.codex-bridge.bridge-dev-sol` with a PID. (Old label absent.)

- [ ] **Step 3: Verify registration under the NEW id and old-id cleanup on the bus**

```bash
cd /Users/<user>/<workspace>
set -a; source envs/agent-redis-bridge-dev.env; set +a
redis-cli -h "${AGENT_REDIS_HOST:-127.0.0.1}" -p "${AGENT_REDIS_PORT:-6379}" -n "${AGENT_REDIS_DB:-0}" \
  ${AGENT_REDIS_TLS:+--tls} ${AGENT_REDIS_USER:+--user "$AGENT_REDIS_USER"} --no-auth-warning \
  HGET "agent_scratch:registry:codex-bridge-dev-example" pid
redis-cli <same connection flags> EXISTS "agent_scratch:registry:codex-bridge-dev"
```
Expected: first returns the new daemon's engine PID (non-empty); second returns `0` (graceful SIGTERM cleanup removed it). If it returns `1`: the old daemon died un-gracefully — `DEL "agent_scratch:registry:codex-bridge-dev"` manually.

- [ ] **Step 4: Real-dispatch probe through the new id (rename + effort default in one shot)**

```bash
SP=$CLAUDE_SCRATCHPAD   # the executing session's scratchpad dir; any writable tmp dir works
FROM_AGENT_ID=claude-bridge-dev BRANCH=dev \
AGENT_ENV_FILE=/Users/<user>/<workspace>/envs/agent-redis-bridge-dev.env \
/Users/<user>/<workspace>/scripts/dispatch-dev --engine codex \
  --target-id codex-bridge-dev-example --timeout 600 \
  --run-id sol-rename-canary \
  "Reply with exactly the word: ok" > $SP/canary.out 2> $SP/canary.err
```
(Backgrounded via the harness; the dispatcher's exit is the wait.)
Expected: exit 0, payload contains `ok`. Then confirm the effort default survived the rename — find the rollout whose filename contains the reply's `thread_id` under `~/.codex/sessions/$(date +%Y/%m/%d)/` and run `grep -o '"reasoning_effort":"[a-z]*"' <rollout>` → `"medium"`.

- [ ] **Step 5: Verify the asdk trust edge from the renamed sender**

```bash
FROM_AGENT_ID=codex-bridge-dev-example BRANCH=dev \
AGENT_ENV_FILE=/Users/<user>/<workspace>/envs/agent-redis-bridge-dev.env \
/Users/<user>/<workspace>/scripts/dispatch-dev --engine agent-sdk \
  --target-id asdk-bridge-dev-sonnet5 --timeout 600 \
  --run-id sol-rename-trust-probe \
  "Reply with exactly the word: ok" > $SP/trust-probe.out 2> $SP/trust-probe.err
```
Expected — **timing matters**: run this AFTER Task 6 restarts the asdk seats. If run before Task 6 it will (correctly) return `sender-rejected`, which proves the old policy is still loaded, not that the migration failed. The plan sequences this as Task 6 Step 3; it is listed here so the canary section names the edge it creates.

---

### Task 5: Cutover the remaining six seats

**Interfaces:**
- Consumes: Task 4's proven recipe.
- Produces: all 7 seats live under `-sol` ids; zero old codex labels loaded.

- [ ] **Step 1: Busy-check all six**

```bash
for f in /tmp/codex-bridge-project-g-dev.launchd.log \
         /Users/<user>/Library/Logs/agent-bridge/codex-{registry-x,project-d,project-h-browser,project-g,project-e}-dev.log; do
  echo "== $f"; grep -E '\[turn-start\]|\[reply-sent\]' "$f" 2>/dev/null | tail -1
done
```
Expected: every non-empty tail is `[reply-sent]`. Wait out any open turn.

- [ ] **Step 2: Bootout old → bootstrap new, all six**

```bash
cd ~/Library/LaunchAgents
launchctl bootout gui/501/com.example.codex-bridge.project-g-dev
launchctl bootstrap gui/501 com.example.codex-bridge.project-g-dev-sol.plist   # autostart seat: starts itself
for p in registry-x project-d project-h-browser project-g project-e; do
  launchctl bootout gui/501/com.example.arbseat.codex-${p}-dev
  launchctl bootstrap gui/501 com.example.arbseat.codex-${p}-dev-sol.plist
  launchctl kickstart gui/501/com.example.arbseat.codex-${p}-dev-sol      # no-autostart: explicit start
done
```

- [ ] **Step 3: Confirm labels + PIDs**

Run: `launchctl list | grep -E 'com.example.(arbseat.codex|codex-bridge)'`
Expected: 9 lines, all with PIDs: 7 `-sol` labels + `codex-bridge-dev-terra` + `codex-bridge-dev-luna`. NO unsuffixed `codex-<project>-dev` label remains.

- [ ] **Step 4: Registry verification per seat, on each seat's own bus**

```bash
check() { # $1=env-file  $2=old-id  $3=new-id
  ( set -a; source "$1"; set +a
    RC=(redis-cli -h "${AGENT_REDIS_HOST:-127.0.0.1}" -p "${AGENT_REDIS_PORT:-6379}" -n "${AGENT_REDIS_DB:-0}"
        ${AGENT_REDIS_TLS:+--tls} ${AGENT_REDIS_USER:+--user "$AGENT_REDIS_USER"} --no-auth-warning)
    echo "$3 pid=$("${RC[@]}" HGET "agent_scratch:registry:$3" pid) old-exists=$("${RC[@]}" EXISTS "agent_scratch:registry:$2")" )
}
check /Users/<user>/AgentRedisBridge/envs/dev-project-g-consultant.env  codex-project-g-consult-dev     codex-project-g-consult-dev-sol
check /Users/<user>/AgentRedisBridge/envs/registry-x-dev.env         codex-registry-x-dev         codex-registry-x-dev-sol
check /Users/<user>/AgentRedisBridge/envs/project-d-dev.env    codex-project-d-dev    codex-project-d-dev-sol
check /Users/<user>/AgentRedisBridge/envs/project-h-browser-dev.env     codex-project-h-browser-dev     codex-project-h-browser-dev-sol
check /Users/<user>/AgentRedisBridge/envs/project-g-dev.env codex-project-g-dev codex-project-g-dev-sol
check /Users/<user>/project-e/.env.bridge                        codex-project-e-dev      codex-project-e-dev-sol
```
(Env paths read from each plist's `--env-file` argument, verified 2026-07-10.)
Expected per line: `pid=<non-empty>` and `old-exists=0`. Any `old-exists=1` → `DEL` that old registry key on that bus.

- [ ] **Step 5: Real-dispatch probe through one direct-shape fleet seat**

```bash
FROM_AGENT_ID=claude-registry-x-dev BRANCH=dev \
AGENT_ENV_FILE=/Users/<user>/AgentRedisBridge/envs/registry-x-dev.env \
/Users/<user>/<workspace>/scripts/dispatch-dev --engine codex \
  --target-id codex-registry-x-dev-sol --timeout 600 \
  --run-id sol-rename-fleet-probe \
  "Reply with exactly the word: ok" > $SP/fleet-probe.out 2> $SP/fleet-probe.err
```
Expected: exit 0, `ok`; rollout for the reply's `thread_id` records `"reasoning_effort":"medium"`. The other four direct seats share the identical plist transformation + a green registry check — one live probe of the shape is the calibrated gate (same policy as the 2026-07-10 effort rollout).

---

### Task 6: Restart the three asdk seats that trust the renamed sender

**Files:**
- Modify: `~/Library/LaunchAgents/com.example.arbseat.asdk-bridge-dev-haiku45.plist`, `com.example.arbseat.asdk-bridge-dev-opus48.plist`, `com.example.asdk-sonnet-bridge.bridge-dev.plist` (already backed up in Task 3 Step 1)

**Interfaces:**
- Consumes: Task 1 updated the 4 asdk env files; this task updates the 3 plist-embedded copies and reloads the seats.
- Produces: asdk sub/reviewer seats accepting dispatches from `codex-bridge-dev-example`. (Memory `bridge-dev-fleet-launchd`: a plist change needs bootout+bootstrap — kickstart is NOT enough.)

- [ ] **Step 1: Edit the trusted-senders value in each of the 3 plists**

```bash
cd ~/Library/LaunchAgents
for f in com.example.arbseat.asdk-bridge-dev-haiku45.plist \
         com.example.arbseat.asdk-bridge-dev-opus48.plist \
         com.example.asdk-sonnet-bridge.bridge-dev.plist; do
  cur=$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:AGENT_TRUSTED_SENDERS' "$f")
  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:AGENT_TRUSTED_SENDERS ${cur/codex-bridge-dev=trusted/codex-bridge-dev-example=trusted}" "$f"
  plutil -lint "$f"
  /usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:AGENT_TRUSTED_SENDERS' "$f"
done
```
Expected: 3× `OK`, each printed value contains `codex-bridge-dev-example=trusted` and no bare `codex-bridge-dev=trusted`.
Caveat: the asdk plists may keep AGENT_TRUSTED_SENDERS in the env file instead of the plist (`bash -c` token-injection shape) — if `Print` errors "Does Not Exist" for a file, the env-file edit from Task 1 already covers that seat; skip the Set, still restart it.

- [ ] **Step 2: Busy-check + bootout/bootstrap the three asdk seats**

```bash
launchctl list | grep -E 'asdk-bridge-dev-(haiku45|opus48)|asdk-sonnet-bridge'
# For each label with a PID, busy-check its log (plist StandardOutPath), then:
for lbl in com.example.arbseat.asdk-bridge-dev-haiku45 com.example.arbseat.asdk-bridge-dev-opus48 com.example.asdk-sonnet-bridge.bridge-dev; do
  launchctl bootout gui/501/$lbl 2>/dev/null || true   # tolerate not-running (no-autostart seats)
  launchctl bootstrap gui/501 ~/Library/LaunchAgents/$lbl.plist
done
launchctl kickstart gui/501/com.example.asdk-sonnet-bridge.bridge-dev 2>/dev/null || true
```
Note: the two arbseat asdk labels are no-autostart — bootstrap re-registers them; only kickstart the ones that were RUNNING before (match the pre-state observed in the first command; do not newly start a seat Mark had parked).

- [ ] **Step 3: Trust-edge probe (the deferred Task 4 Step 5)**

Run the `FROM_AGENT_ID=codex-bridge-dev-example → asdk-bridge-dev-sonnet5` dispatch from Task 4 Step 5.
Expected: exit 0 with `ok` — NOT `sender-rejected`. This closes the wiki-review-gate loop: `resolve_reviewer("codex")` targets the sonnet seat, and the sonnet seat now trusts the renamed codex seat's replies-to-dispatches direction… (the codex→asdk direction is what the sub-seat envs encode).

---

### Task 7: Bus hygiene, records, and close-out

**Files:**
- Modify: `CHANGELOG.md` (top of `## Unreleased — dev`)
- Modify (memory, not repo): `~/.claude/projects/-Users-mark-<workspace>/memory/manual-seats-promoted-launchd.md`, `bridge-dev-fleet-launchd.md`, `MEMORY.md`

**Interfaces:**
- Consumes: all seats verified under new ids.
- Produces: durable records; no stale keys; both clones at the final SHA.

- [ ] **Step 1: Delete stray old-id keys on every bus (inbox/notify/status)**

For each (env-file, old-id) pair from the seat map, using the `check`-style sourced connection:
```bash
for k in "agent_scratch:agent:${OLD}:inbox" "agent_scratch:agent:${OLD}:notify_inbox" "agent_scratch:agent:${OLD}:status" "agent_scratch:registry:${OLD}"; do
  "${RC[@]}" DEL "$k"
done
```
(An idle inbox is normally length-0/BLPOP-consumed; this is belt-and-suspenders so no envelope ever lands on a dead id unnoticed.)

- [ ] **Step 2: CHANGELOG entry (what AND why, per changelog discipline)**

```markdown
### chore(seats): rename the 7 default codex seats to carry the model tier (`-sol`) (2026-07-10)
- **What:** `codex-<project>-dev` → `codex-<project>-dev-sol` across all 7 default codex
  seats (bridge, project-g-consult, registry-x, project-d, project-h-browser, project-g,
  project-e), via the launcher's role-suffix convention (`--role sol` / instance
  `codex-dev-sol`). Live dispatch targets (wiki-refresh reviewer map, learn-intake quorum,
  arb-memory e2e default), asdk trusted-sender lists, and living docs updated; terra/luna
  unchanged (already suffixed).
- **Why:** a seat's model identity should be readable from its agent-id at a glance and
  self-evident in rosters/audit trails, matching codex-bridge-dev-terra/-luna. The model
  was already pinned per-plist (`AGENT_MODEL=gpt-5.6-sol`); this is naming clarity, not a
  behaviour change.
- **Trust-root lineage note:** `trust_root.json` still records `codex-bridge-dev` as a
  2026-07-09 certifying seat — correct as history. The gate's rotation-disjointness check
  (`gate.py:723-728`) compares seat-id STRINGS, so a future rotation certified by
  `codex-bridge-dev-example` would pass disjointness against that root despite being the same
  lineage. Human approvers must treat `codex-bridge-dev-example` == `codex-bridge-dev` when
  judging seat disjointness.
```

- [ ] **Step 3: Commit, push, pull fleet clone**

```bash
cd /Users/<user>/<workspace> && git add CHANGELOG.md && \
git commit -m "docs(changelog): record the codex seat -sol rename migration" && \
git push origin dev && git -C /Users/<user>/AgentRedisBridge pull --ff-only
```
(Doc-only delta on the fleet clone — no seat restarts needed.)

- [ ] **Step 4: Update memories**

- `manual-seats-promoted-launchd.md`: new dated section — the 7 renames, the new label set, the lineage note, pointer to the backup dir for rollback.
- `bridge-dev-fleet-launchd.md`: the two `com.example.codex-bridge.*` labels now carry `-sol`.
- `MEMORY.md`: refresh the one-line hooks for both.

- [ ] **Step 5: Final sweep**

```bash
launchctl list | grep -cE 'com.example.(arbseat|codex-bridge|asdk|agy|pi-|grok|cursor)'   # fleet count unchanged (25)
launchctl list | grep -E 'codex-(bridge|registry-x|project-d|project-g|planning|ila)' | grep -v -- '-sol\|terra\|luna'
```
Expected: second command prints NOTHING (no unsuffixed codex seat loaded). Delete nothing else; old plist files in `~/Library/LaunchAgents` were already replaced by the new-name files (the old-name files remain on disk only inside the backup dir — remove the originals from `~/Library/LaunchAgents` after the first quiet day, or immediately if preferred; they are inert once booted out, but a stray `bootstrap` of one would resurrect the old id).
Recommended immediate hardening: `mv` the 7 old plist files out of `~/Library/LaunchAgents` into the backup dir (they're copied there already — just remove the originals) so nothing can accidentally re-register an old id.

---

## Rollback (any point after Task 4)

1. For each cut-over seat: `launchctl bootout gui/501/<new-sol-label>`, then `launchctl bootstrap gui/501 <backup-dir>/<old>.plist` (+ `kickstart` for no-autostart seats).
2. Restore the 3 asdk plists from the backup dir, bootout/bootstrap them.
3. `git revert` the Task 1 commit (and Task 7's changelog commit if made), push, pull fleet clone, then restart any codex seat that had already loaded the reverted code — restarts are safe in either direction (all code involved is ≥ `1ade57c`; the effort hazard never re-opens).
4. DEL any `-sol` registry/status keys left on the buses.

## Known windows / accepted risks

- **Between Task 4/5 bootouts and their bootstraps**, a dispatch to an old id has no consumer and will sit until timeout. No cron/launchd job on this host dispatches to codex seats unattended (wiki-refresh and learn-intake run on demand; journey export is prod-side and doesn't target these seats) — verified by reviewing `launchctl list | grep com.mark` before cutover. Mark is the only other dispatcher.
- **arb-watch / visibility dashboards** key on agent-id strings; historical rows keep the old ids, new rows carry `-sol`. Cosmetic discontinuity, no action.
- **Cross-session Claude peers** that memorized `codex-bridge-dev` as a target will get timeouts until they re-read the seat memory — the memory update in Task 7 Step 4 is the fix surface.
