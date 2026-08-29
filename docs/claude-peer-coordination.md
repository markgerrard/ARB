# Claude ↔ Claude peer coordination (no engine)

The minimum viable use of this bridge: two Claude Code sessions on different hosts coordinate via the shared bus without running an engine on either side. **No bridge daemon, no codex / gemini install, no engine pool.** Just LPUSH/BLPOP and a persistent Monitor.

This pattern is what you want when:
- You have two (or more) Claude Code sessions doing different parts of one project (e.g. one driving a database host on one site, another driving a service on a remote site).
- You're not delegating tasks to another LLM — you just need notifications and small structured updates to flow between the sessions.
- You want to keep the operator-visible failure surface as small as possible — no daemon to crash, no engine to mis-auth, no parallel-dispatch quota math.

If you're delegating *work* to an engine on the other side, use the full `agent-dispatch` recipe in SKILL.md instead.

## Architectural shape

```
host A                        managed Valkey/Redis              host B
┌─────────────────────┐       (TLS, ACL'd, single DB)          ┌─────────────────────┐
│ Claude Code session │       agent_scratch:agent:<id>:inbox   │ Claude Code session │
│ AGENT_ID=claude-… A │ ◄────────────────────────────────────► │ AGENT_ID=claude-… B │
│  - Monitor tool     │       agent_scratch:agent:<id>:status  │  - Monitor tool     │
│    (scripts/agent-  │       (60s TTL heartbeat)              │    (scripts/agent-  │
│    inbox-watcher)   │                                        │    inbox-watcher)   │
│  - raw redis-cli    │                                        │  - raw redis-cli    │
│    LPUSH for sends  │                                        │    LPUSH for sends  │
└─────────────────────┘                                        └─────────────────────┘
```

Each side runs only:
1. **One Monitor task** with `persistent=true`, executing `scripts/agent-inbox-watcher` from this repo. Each `[inbox] {body}` stdout line becomes a notification.
2. **`redis-cli LPUSH`** invocations to send envelopes (peer's inbox key).

That's it. No systemd. No daemon. No engine.

## Day-1 setup

### Step 1 — clone repo + populate .env

```bash
git clone git@github.com:example-org/AgentRedisBridge.git ~/AgentRedisBridge
cp ~/AgentRedisBridge/.env.example ~/AgentRedisBridge/.env
chmod 600 ~/AgentRedisBridge/.env
```

Set in `.env`:

```ini
# Managed bus credentials (you'll be given these)
AGENT_REDIS_HOST=db-...ondigitalocean.com
AGENT_REDIS_PORT=25061
AGENT_REDIS_TLS=1
AGENT_REDIS_USER=default
AGENT_REDIS_PASSWORD=AVNS_...

# CRITICAL: match the peer's DB. Probe it (see Step 2) — DO NOT default to 0.
AGENT_REDIS_DB=12

AGENT_REDIS_PREFIX=agent_scratch:

# Identifier — used as the inbox/status key suffix
AGENT_PROJECT=<short>
AGENT_WORKSPACE=<host-or-workload>
AGENT_DISPATCH_FROM=claude-${AGENT_PROJECT}-${AGENT_WORKSPACE}

# Required by the bridge daemon — but you're not running the daemon. Keep
# these set anyway so the .env is reusable if you later add an engine.
AGENT_WORKDIR=$HOME
AGENT_TRUSTED_SENDERS=claude-<peer-project>-<peer-workspace>=trusted,human-codexctl=human
```

### Step 2 — discover the peer's DB before you write anything

> **Self-hosted bus (`arb-bus.example.com`): SKIP this step — and you cannot run it anyway.** The DB
> is fixed at 12; set `AGENT_REDIS_DB=12`. The `KEYS`/`EVAL KEYS`/`SCAN` probe below is **NOPERM** under
> the per-identity ACLs (browse commands are denied to non-admin creds). This whole section is
> **managed-bus only**. See `docs/self-hosted-bus.md`.

The single biggest first-day gotcha (MANAGED bus): managed Valkey on DO defaults to DB 0 for new clients, but the peer's session was started long ago with `AGENT_REDIS_DB=12`. If you write to DB 0, the peer never sees you and silence looks identical to "no peer". Always probe:

```bash
set -a; source ~/AgentRedisBridge/.env; set +a
for db in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  count=$(REDISCLI_AUTH="$AGENT_REDIS_PASSWORD" redis-cli \
    -h "$AGENT_REDIS_HOST" -p "$AGENT_REDIS_PORT" \
    --user "$AGENT_REDIS_USER" --tls -n "$db" \
    --no-auth-warning \
    EVAL "return #redis.call('KEYS','agent_scratch:agent:claude-*')" 0)
  echo "db=$db claude-* keys=$count"
done
```

Pick the DB with non-zero count. Update `.env` to match. If the bus is brand-new and every DB is empty, pick a DB and announce it to the peer out-of-band.

### Step 3 — launch the inbox monitor

Use the **Monitor tool with `persistent=true`** — not Bash `run_in_background`. The script is designed to loop forever, refresh status on each iteration, and emit `[inbox] <envelope-json>` per message.

In Claude Code:

```
Monitor (persistent=true, description="project-g-chain peer messages"):
  set -a; source <bridge-clone>/.env; set +a
  AGENT_ID=claude-<project>-<workspace> <bridge-clone>/scripts/agent-inbox-watcher 2>&1 | \
    command grep --line-buffered -E "^\[inbox\]|^\[watcher\]|ERROR|FATAL|NOAUTH|WRONGPASS|denied|rejected|connection refused|reconnecting|sender-rejected|envelope-invalid"
```

…where `<bridge-clone>` is the host-specific absolute path (e.g.
`/home/<user>/AgentRedisBridge`). Three deliberate hardenings vs the older form
(field-tested on claude-project-b-1 through a full incident + deploy chain,
2026-06-07): **absolute paths, no `cd ~/...`** (a Monitor respawning after a
session restart can resolve `~`/cwd differently — silently); **an absolute grep
**`command grep`, never bare `grep`** (Claude Code aliases `grep` to a function
re-execing ugrep, whose line-buffering degrades on multi-day pipe streaming — see
§ chunk-shift below). `command` suppresses shell-function lookup, so this reaches
the real binary, and it resolves through PATH — which is what makes it portable.
**Do NOT hardcode `/bin/grep`: it is Linux-only and does not exist on macOS.** A
Mac host following that form exits 127 and the watcher never runs; worse, it can
die *after* the BLPOP, consuming an envelope and losing it to the broken pipe
(hit live on the mini, 2026-08-09 — its prove-the-chain step is what caught it).
**Nor `$(command -v grep)`, which was recommended here until 2026-08-11 and is
broken:** `command -v` on a shell function returns the FUNCTION NAME, so it
resolves to the very thing being bypassed (verified: `command -v grep` prints
`grep`, while `command grep --version` prints GNU grep). For the split
watcher, **`mkdir -p` the inbox dir first** (next
block).

First notification you'll receive is the `[watcher] online at …` self-announcement. From then on, each `[inbox] {envelope}` line is a real peer message.

#### Long envelopes — use the split watcher

Claude Code's Monitor tool truncates the surfaced `<event>` field of each task-notification at a fixed line cap. Long envelopes (multi-paragraph payloads, deep nested structures) lose their tail in the inline preview the session sees — even though the bus delivered the full envelope cleanly.

For Claude consumers expecting non-trivial payloads, swap to **`scripts/agent-inbox-watcher-split`** — same script, but each `[inbox]` line is split into:

1. A short `[inbox-meta] id=… from=… kind=… event=… sent_at=…` line that always fits the Monitor preview.
2. The full envelope JSON written to `$AGENT_BRIDGE_INBOX_DIR/<id>.json` (default `/tmp/agent-bridge-inbox/`).

When you need the full payload, `Read /tmp/agent-bridge-inbox/<id>.json`. No truncation.

Monitor invocation changes only in the script name and the grep filter (`^\[inbox-meta\]` instead of `^\[inbox\]`):

```
Monitor (persistent=true, description="project-g-chain peer messages"):
  set -a; source <bridge-clone>/.env; set +a
  mkdir -p "${AGENT_BRIDGE_INBOX_DIR:-/tmp/agent-bridge-inbox}"
  AGENT_ID=claude-<project>-<workspace> <bridge-clone>/scripts/agent-inbox-watcher-split 2>&1 | \
    command grep --line-buffered -E "^\[inbox-meta\]|^\[watcher\]|ERROR|FATAL|NOAUTH|WRONGPASS|denied|rejected|connection refused|reconnecting|sender-rejected|envelope-invalid"
```

The `mkdir -p` guards a real failure mode: on a fresh boot or after a `/tmp`
cleanup the inbox dir may not exist, the filter's writes silently fail, and you
get meta lines with no on-disk JSON — defeating the split watcher entirely.
Idempotent, costs nothing.

Codex / Gemini consumers don't need this — they go through `scripts/agent-redis-bridge-systemd` and the engine's native protocol, never seeing the Monitor preview.

### Step 4 — send the first envelope

```bash
set -a; source ~/AgentRedisBridge/.env; set +a
NOW=$(date -u --iso-8601=seconds)
UUID=$(cat /proc/sys/kernel/random/uuid)
ENV_JSON=$(python3 -c "
import json
print(json.dumps({
  'id': '$UUID',
  'from': 'claude-<project>-<workspace>',
  'branch': 'dev',
  'to': 'claude-<peer-project>-<peer-workspace>',
  'kind': 'notify',
  'sent_at': '$NOW',
  'payload': {'event': 'hello', 'data': {'from_host': '$(hostname)'}}
}, separators=(',', ':')))
")
REDISCLI_AUTH="$AGENT_REDIS_PASSWORD" redis-cli \
  -h "$AGENT_REDIS_HOST" -p "$AGENT_REDIS_PORT" \
  --user "$AGENT_REDIS_USER" --tls -n "$AGENT_REDIS_DB" \
  LPUSH agent_scratch:agent:claude-<peer-project>-<peer-workspace>:inbox "$ENV_JSON"
```

On the MANAGED bus: if LLEN of the peer's inbox drops back to 0 within a second, they consumed it via their own BLPOP; if it stays at 1+, they're not actively listening.

> **Self-hosted bus: this read-back is GONE.** `LPUSH` to a foreign inbox succeeds, but `LLEN`/`LRANGE`
> on it is **NOPERM** under the per-identity ACLs — you can send, you cannot inspect the peer's queue.
> The recipient's **reply** is the only consumption signal; request an ack if you need confirmation.
> Foreign presence is still readable (`GET`/`TTL` on `:status`). See `docs/self-hosted-bus.md`.

## Envelope shape

Constraints enforced by `src/agent_redis_bridge/envelope.py`:

| Field | Required | Note |
|---|---|---|
| `id` | yes | uuid4 string — used by replies in `in_reply_to` |
| `from` | yes | sender's agent_id |
| `branch` | yes | non-empty string (any value works for coord-only — `"dev"` is fine) |
| `to` | yes | recipient's agent_id |
| `kind` | yes | one of `cancel`, `hello`, `request`, `reply`, `steer`, `notify` |
| `sent_at` | yes | ISO 8601 string |
| `payload` | yes | dict — kind-specific schema |
| `in_reply_to` | only for `kind=reply` | id of the message being replied to |

For peer-coordination, **`kind=notify` is the right choice 95% of the time**. Payload shape: `{"event": "<event-name>", "data": {...}}`. The bridge's `make_notify` helper is the canonical constructor.

`request` and `reply` are for engine-driven dispatch with explicit task / response semantics — don't reach for them in coord-only mode unless you really mean it. `cancel`, `hello`, `steer` are bridge-internal control plane.

## Gotchas hit on day 1 (site-a × remote-site setup, 2026-05-23)

### `BLPOP 0` (indefinite) drops connection on managed bus idle timeout

Managed Valkey/Redis providers (DO, Aiven, AWS) close idle TCP connections after a few minutes. A bare `BLPOP inbox 0` (indefinite block) eventually fails with `Server closed the connection` even though the bus is healthy and the client is healthy — the *bus's NAT/proxy* dropped the idle TCP socket.

**Fix:** use a timeout shorter than the idle limit, in a loop:

```bash
while :; do
  result=$(timeout 130 redis-cli ... BLPOP key 120 2>&1)
  case "$result" in
    ""|*timeout*) continue ;;
    Error:*) sleep 3 ;;
    *) echo "$result"; break ;;
  esac
done
```

…or just use `scripts/agent-inbox-watcher` (already does this, plus heartbeats).

### One-shot Bash `run_in_background` re-arm vs persistent Monitor

If you wrap a single BLPOP in `Bash(run_in_background=true)`, the task ends the moment a message arrives. You then have to re-arm. Two problems:
1. One Bash tool call per peer message — high token cost in a rapid back-and-forth.
2. There's a window between completion notification and your re-arm where another message could arrive but won't be picked up until the next watcher starts.

**Always use the Monitor tool with `persistent=true`** for inbox watching. The watcher script (`scripts/agent-inbox-watcher`) is built for this — it loops, refreshes status, never exits on success.

### Cloned VMs share `/etc/wireguard/` credentials

Unrelated to this bridge but bit us getting the WG side ready. If your peer host is a clone of a template VM, its WG keypair (and SSH host keys, and any `*-credentials` files in `/etc/`) is shared with the source VM. Two machines with the same WG private key compete for the same tunnel — last-connected wins, the other silently doesn't talk.

**Fix:** regenerate keys as part of any VM bootstrap. Treat `/etc/wireguard/` and `~/.ssh/id_*` as per-host identity material, not config to clone.

### Status TTL must be refreshed continuously

`scripts/agent-inbox-watcher` sets `agent_scratch:agent:<id>:status` to `alive:<pid>` with `EX 60` on **every loop iteration** (default BLPOP timeout 30s means refresh every ~30s). If you write a custom watcher, replicate this. Peers use the status key (`TTL > 0`) as the "is the other agent online" signal — if it expires, you go invisible.

### Cleaning up stale watchers

`TaskStop` on a Monitor task may not always kill the subprocess (especially when the subprocess has its own children like `redis-cli`). Before restarting the monitor, run:

```bash
pkill -9 -f agent-inbox-watcher
pkill -9 -f "redis-cli.*BLPOP"
```

Otherwise the orphaned watchers compete with the new one for inbox messages (BLPOP is atomic — first watcher to wake gets the message, the others race).

> **Structural fix:** a load-bearing seat can eliminate this race entirely (no per-session BLPOP to leak) — see [§ Reliable inbox daemon](#reliable-inbox-daemon--when-a-seat-must-never-drop-a-message).

### Long-stream chunk-shift truncation (use `agent-inbox-watcher-split` ≥ 3baa522)

After multi-day uptime on a Monitor task, the meta line surfaced to Claude can degrade to just the bare prefix (e.g. `[inbox-meta]` with no fields and no trailing newline) while the per-id JSON on disk remains intact. Symptom fingerprint:

1. Monitor task-output file ends in `[inbox-meta]` (12 bytes) with no fields/newline.
2. Corresponding `/tmp/agent-bridge-inbox/<id>.json` is **present and complete** (the splitter's disk write isn't affected).
3. Task-notification for that envelope either doesn't fire OR surfaces with a half-line payload.
4. Inbox `LLEN` was 0 when it should have been — the message was consumed but its meta line shifted past the line-tracking boundary.

**Root cause (two contributing factors):**

1. **`print(s, flush=True)` in Python issues TWO writes per line** — one for the string, one for the trailing newline. Downstream consumers (`grep --line-buffered`, then Monitor's task-output writer) read the pipe in chunks that don't necessarily align to newline boundaries. So a single chunk can deliver `<rest-of-line-N>\n<start-of-line-N+1>` — straddling the boundary between two `print`-emitted lines. Monitor's line-position tracking shifts, the task-notification for the chunk either drops or surfaces as a half-line.

2. **The shell's `grep` is a Claude Code injected function** that re-execs `$CLAUDE_CODE_EXECPATH` as `ugrep` — designed for filesystem queries, not multi-day pipe streaming. After ~37h continuous streaming, its line-buffering degrades and exacerbates the chunk misalignment. (`type grep` in any Claude-Code shell shows the function body; `/usr/bin/grep` is the real binary, accessible via explicit path.)

**Primary fix (already shipped, commit `3baa522`):** `scripts/inbox-split-filter.py` now uses a single `sys.stdout.write(line + "\n"); sys.stdout.flush()` for every line emit, replacing `print()`. The newline is inseparable from its line, so the chunk-boundary shift can't happen.

**Defensive secondary fix (now the default):** the canonical Monitor blocks in § Step 3 use `command grep` (which suppresses shell-function lookup, reaching the real binary, and stays portable by resolving through PATH — an absolute `/bin/grep` bypasses the function too but is Linux-only). Promoted from optional to default 2026-06-07 — it's one character-class of difference and removes the footgun for every future setup; `3baa522` remains the primary fix.

**Bottom-line guard rail for any future Python pipe-emitting tool in this repo:** **don't use `print()` to emit logical lines to a downstream line-buffered consumer.** Build a single `emit(line)` helper that does `sys.stdout.write(line + "\n"); flush()` and route all line emissions through it. The one-write-per-line guarantee is cheap (lines are << 4096-byte PIPE_BUF) and avoids the entire class of mid-stream surfacing bugs.

### Cutover window: SIGKILL on the watcher can drop in-flight messages

`pkill -9 -f agent-inbox-watcher` kills the parent watcher script, but the in-flight `redis-cli BLPOP` child can be reparented to init for a brief window before its own SIGKILL lands. If a peer message arrives in that window, `redis-cli` will consume it from the bus (BLPOP is atomic on the Redis side), but with no live parent pipe to write the response to it SIGPIPE-dies — and the message is lost without trace. The watcher's stdout never saw it, the bus already committed it, the receiver thinks nothing arrived.

Hit this 2026-05-24 cutting over from `agent-inbox-watcher` to `agent-inbox-watcher-split`; peer's test envelope `b93dca5d…` vanished mid-swap.

**Safer cutover:**

```bash
# 1. Wait until inbox is drained (avoid the atomic-orphan window)
while [ "$(redis-cli ... LLEN agent_scratch:agent:$AGENT_ID:inbox)" != "0" ]; do
    sleep 1
done

# 2. SIGTERM (not SIGKILL) the watcher so it can finish its current BLPOP
#    cycle gracefully — the loop sees timeout / signal, exits cleanly,
#    no orphaned redis-cli holding an undelivered message.
pkill -TERM -f agent-inbox-watcher
sleep 5  # let the BLPOP timeout (default 30s) elapse if needed

# 3. Now SIGKILL anything still lingering (defence in depth)
pkill -9 -f agent-inbox-watcher
pkill -9 -f "redis-cli.*BLPOP"

# 4. Restart with the new watcher
```

Or coordinate the swap with the peer over a chat channel and confirm no message is sent during the window. Either avoids the loss.

> **Structural fix:** the drain-then-SIGTERM dance is only needed because BLPOP is unreliable. A `BLMOVE :inbox :processing` consumer with a startup recovery sweep keeps the in-flight element durable across SIGKILL — no cutover window to manage. See [§ Reliable inbox daemon](#reliable-inbox-daemon--when-a-seat-must-never-drop-a-message).

### Naming: `agent_id` is host-based or workload-based, both work

The bridge doesn't enforce a convention. `claude-project-g-db-a` (host where Claude runs) and `claude-project-g-db-b` (workload Claude manages) are both valid. **Pick one convention per project and stick to it** — a peer who pushes to `claude-project-g-db-a` when you're listening as `claude-project-g-db-b` will silently fill a ghost queue. The bus has no concept of "agent typo".

When in doubt, write down both names in the first round-trip message so the peer can mirror them back.

### env-loader allow-lists silently break across versions

If your watcher / send helpers source `.env` via an explicit `env_get` allow-list (or any `grep`-then-source pattern that names which keys to load), **adding a new required env var means the loader silently fails to pick it up until the allow-list is updated.** The running process keeps working because the var is already in its environment; the bug surfaces on cold start — usually weeks later, in the worst possible moment (re-arming the Monitor after a session restart, bootstrapping a successor seat, etc.).

This hit the v0 bash watcher twice on the Project A ↔ Project B run:

- **REDIS_USERNAME** wasn't in the loader's allow-list at first. `redis-cli`'s default ACL connection attempt failed with `NOAUTH` on the managed bus; took one session to diagnose because the failure happens at connect time, not envelope-emit time.
- **AGENT_ID** wasn't in the loader's allow-list either, even though `inbox.sh` required it as the inbox-key suffix. Surfaced on `claude-project-b-1` mid-run: their watcher heartbeat went stale, they tried to re-arm the Monitor, and the script bailed with `AGENT_ID must be set` — because the shell that was re-arming hadn't inherited `AGENT_ID` inline, and the loader didn't pick it up from `.env`. Fixed in commit f716aee with an explicit `AGENT_ID=${AGENT_ID:-$(env_get AGENT_ID)}` line in the legacy bash peer's `inbox.sh`.

**The general rule:** be exhaustive on env-loader allow-lists, and treat every required key as a *future cold-start break point* unless you can show the loader will read it from `.env`. The running watcher works because *somebody* exported the var into its environment once; that's not a guarantee about cold start.

If you're using `set -a; source .env; set +a` (the pattern recommended in [§ Step 1 above](#step-1--clone-repo--populate-env) and [§ Quick reference below](#quick-reference--common-operations)) the problem doesn't apply — every key in `.env` is auto-exported. The gotcha is specifically for scripts that opted into an allow-list for selective loading (often for safety reasons — to avoid clobbering critical shell vars from a shared `.env`). The current `scripts/agent-inbox-watcher{,-split}` use the auto-export form for this reason.

## Gotchas hit joining an existing fleet bus (project-a dev box → project-b-a, 2026-07-02)

A second field-tested batch, from standing up a NEW lead session on a bus where a peer
was already live. Different failure surface from day-1 setup: everything below bit
even though the bus itself was healthy.

### The silent inbox: a background watcher produces ZERO notifications

The reliable watcher (`agent-inbox-watcher-reliable`) must run as its own process
(BLMOVE + durable per-message JSON files). Running it via `Bash(run_in_background=true)`
*works* — messages drain, files land — but the harness only notifies on task
COMPLETION, and the watcher never completes. Result: peer messages arrive silently and
sit unread until a scheduled wakeup happens to look. In the field this cost 40 minutes
of dead air on a phase-gate reply and required the human to intervene ("you aren't
getting peer messages").

**The correct composition is BOTH pieces:**
1. the reliable watcher as a background process (unchanged), and
2. a **persistent Monitor** tailing the watcher's stdout for its `[inbox-meta]` lines —
   each line becomes an immediate chat notification:

```bash
tail -n 0 -F <watcher-stdout-file> | grep -E --line-buffered 'inbox-meta|Traceback|fatal' &
TP=$!
while pgrep -f agent-inbox-watcher-reliable >/dev/null; do sleep 20; done
echo "[watcher-died] peer messages are NOT being received; restart the watcher"
kill $TP 2>/dev/null
```

Details that matter: `tail -n 0 -F` (don't replay already-handled messages on arm);
`--line-buffered` (or matches sit in grep's buffer); the `pgrep` loop makes watcher
death an EVENT rather than indistinguishable silence. **Prove the chain before relying
on it**: LPUSH a self-addressed test envelope and confirm the notification arrives.
Arm all of this BEFORE sending your first outbound message, not after.

### Two buses can both match "shared DO Valkey, db 12, prefix agent_scratch:"

A peer's self-description of its bus ("shared DO Valkey, db 12, `agent_scratch:`") is
NOT sufficient to identify the instance — this estate had two managed Valkeys with
identical db/prefix conventions and different populations. The dev-box lead burned a
round-trip connected to the wrong one (registry full of *other* projects' seats, zero
project-b keys). **Hostname is the identity.** Before trusting any credentials:

```bash
R -n 12 TTL 'agent_scratch:agent:<peer-id>:status'    # > 0 = right bus, peer alive
```

Also: `redis-cli KEYS <no-match> | wc -l` returns **1** (a phantom empty line), which
reads as "1 key found" in a loop over DBs. Use `SCAN ... MATCH` and check emptiness
with `od -c` or `grep -c .` instead.

### Envelope dialects differ between peers — read liberally

This repo's canonical envelope is `{id, from, to, kind, sent_at, payload:{text}}`.
The live project-b-a peer replies with `{id, from, to, ts, kind, body}` — `body` instead of
`payload.text`, `ts` instead of `sent_at`, `kind: "ack"` as a valid kind, and no
`in_reply_to` even on direct replies. Peer READERS must accept both:
`text = env.get("body") or env.get("payload", {}).get("text", "")`, and must not key
handling on `in_reply_to` being present. (Writers should stay canonical.)

### Bootstrapping identity for a NEW lead on an established bus

When the peer expects a different lead (`claude-project-a-1`) and a new session
(`claude-project-a-dev`) takes over a workstream, a **human-relayed shared passphrase**
in the first envelope resolves the identity cleanly — the peer verifies it and
switches allegiance for that workstream explicitly. This is session *recognition*, not
security (anyone with bus credentials reads envelopes); its value is preventing the
peer from either ignoring the unknown sender or conflating two leads' sequencing.
State the disambiguation explicitly in the handshake ("the lead for THIS workstream is
X, not Y") — the peer will echo it back, which is your confirmation it took.

## Heartbeat without a daemon

If your session is going to be quiet for stretches longer than the 60s status TTL (peer will see you go offline), either:
1. **Just refresh manually** on each Redis interaction — single `SET ... EX 60` cost is trivial.
2. **Run `scripts/agent-inbox-watcher` even when you don't need to read** — it keeps the heartbeat going. The Monitor + persistent=true model means it's running anyway.

The second is generally easier: launch the monitor at session start, leave it running. Your `:status` stays warm for the whole session.

## Reliable inbox daemon — when a seat must never drop a message

The per-session `agent-inbox-watcher` above is the right default for casual two-peer coordination: no daemon to crash, smallest failure surface. But a **load-bearing seat** — a coordination lead that sequences a fleet, or any seat where a silently-dropped peer reply causes divergence — wants stronger guarantees than discipline patches around a BLPOP watcher. Two failure modes the per-session model only *mitigates procedurally*:

- **Stale-watcher race** (§ Cleaning up stale watchers): leaked watchers race on the atomic BLPOP and split messages. Mitigated by `pkill` discipline; recurs on any unclean session end. On the project-a fleet 2026-06-08, **13** had accumulated — a `trap … TERM` that didn't `exit` meant Monitor teardown never reaped the watcher, so each ended session leaked one — and a peer reply surfaced through the wrong watcher.
- **SIGKILL drops in-flight** (§ Cutover window): BLPOP is structurally unreliable — Redis serves a *blocked* consumer server-side, so a killed/reparented consumer loses the element. Mitigated by the drain-then-SIGTERM cutover dance.

The structural fix makes **both impossible by construction** — split *consume* from *notify*, and make consumption reliable:

```
:inbox ──BLMOVE──▶ inbox-daemon (systemd, SOLE consumer) ──append──▶ *.inbox.jsonl
                          │                                              │ tail -F (non-consuming)
                    :processing (durable in-flight)        session(s) ──▶ "MSG …" notifications
```

- **One always-up daemon is the only consumer** (systemd `--user`, `Restart=always`, linger). Sessions **`tail -F`** the jsonl — reading a file is non-destructive, so any number of sessions are safe and **the stale-watcher race cannot occur**: there is no per-session BLPOP to leak or race. Sessions resume from a per-seat line cursor, so a returning session catches everything the daemon captured while it was away (the queued-message succession property, preserved).
- **Reliable delivery: `BLMOVE :inbox :processing` (not `BLPOP`)** + append + `LREM`-ack, with a **startup recovery sweep** (de-duped by envelope id) that re-drains `:processing`. The in-flight element is durable in `:processing` the instant it leaves `:inbox`, so a daemon that dies/is-killed mid-message — including a *wedged* daemon recovered by **kill** rather than resume — **loses nothing**. This eliminates the SIGKILL-in-flight loss structurally; the cutover dance is no longer needed.

**The trade — be honest about it:** you've **concentrated** consumption into one daemon, a new single point of failure. `Restart=always` covers a crash but NOT a *wedged-but-running* daemon (hung BLMOVE, dead-but-not-exited). So its liveness key must prove **progress, not existence**:

- The daemon refreshes `:daemon` **inside the consume loop** (each BLMOVE cycle), TTL = 3× the block time — an idle inbox still refreshes in time, but a wedged loop lets it lapse.
- The session's `:status` heartbeat (attendance — the existing doorbell) stays **separate**.
- **The health check must page on `:daemon` expiry, independent of `:status`** — else a session reads green (`:status` warm) while the daemon is dead and nothing flows. (project-a's `fleet:health` adds a dedicated `inbox-daemon` row that goes RED on `:daemon` lapse.)

**Fire the failure modes, don't assume them.** The pattern is only "permanent" once you've killed the daemon mid-message, *wedged* it (`SIGSTOP` the whole unit) and confirmed the page fires, and confirmed the recovery sweep re-drains — restore-drill discipline, not happy-path verification.

**Reference implementation + provenance:** project-a repo `tools/agent-comms/` — `inbox-daemon.sh` (daemon), `inbox-tail.sh` (session reader), `systemd/agent-inbox-daemon.service`; runbook `docs/agent-inbox-daemon.md`; negative drill `tools/agent-comms/daemon-negtest.sh` (a re-runnable regression guard, kept in-repo). Built and drill-proven on the project-a↔project-b fleet 2026-06-08 (crash / kill-wedged-with-in-flight / wedge-detect all pass), in response to a review that flagged the concentrated-consumer SPOF. Promote these scripts into this repo's `scripts/` if the pattern is adopted beyond the lead seat.

## Quick reference — common operations

```bash
set -a; source ~/AgentRedisBridge/.env; set +a
R() { REDISCLI_AUTH="$AGENT_REDIS_PASSWORD" redis-cli \
    -h "$AGENT_REDIS_HOST" -p "$AGENT_REDIS_PORT" \
    --user "$AGENT_REDIS_USER" --tls -n "$AGENT_REDIS_DB" \
    --no-auth-warning "$@" ; }

# Is the peer alive?
R GET agent_scratch:agent:<peer-id>:status
R TTL agent_scratch:agent:<peer-id>:status      # >0 = alive, -2 = no key

# How many messages am I behind on (if monitor not running)?
R LLEN agent_scratch:agent:<me>:inbox           # OWN inbox — fine on both buses
R LRANGE agent_scratch:agent:<me>:inbox 0 -1    # peek without consuming (own inbox)

# Send (use the python helper above; raw LPUSH is fine but envelope validation is your responsibility)
R LPUSH agent_scratch:agent:<peer>:inbox "<json>"   # send works on both; foreign LLEN does NOT (self-hosted)

# Manually heartbeat
R SET agent_scratch:agent:<me>:status "alive:$$" EX 600    # longer TTL = forgiving

# Is a specific peer online? (works on both buses)
R GET agent_scratch:agent:<peer-id>:status
R TTL agent_scratch:agent:<peer-id>:status      # >0 = alive

# What other agents are online? (MANAGED bus only — KEYS is NOPERM on the self-hosted bus)
R KEYS 'agent_scratch:agent:claude-*:status'
# Self-hosted: no browse. Check known ids individually with GET/TTL above, or use agent-bridge-ping.
```

## See also

- [orchestrating-claude-peers.md](orchestrating-claude-peers.md) — the workflow layer for N ≥ 3 peers with a coordination lead (field notes from the Project A ↔ Project B run)
- `README.md` § Managed Redis/Valkey buses — full-daemon managed-bus setup
- `skills/using-agent-bridge/SKILL.md` — the full operational skill for engine-driven dispatch
- `src/agent_redis_bridge/envelope.py` — the source-of-truth envelope schema
- `scripts/agent-inbox-watcher` — the watcher script referenced throughout
- `scripts/agent-inbox-watcher-split` + `scripts/inbox-split-filter.py` — truncation-resistant variant; emits short `[inbox-meta]` lines and writes full envelopes to `$AGENT_BRIDGE_INBOX_DIR/<id>.json`
- `SPEC.md` § Protocol — wire-level details
