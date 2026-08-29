# The self-hosted ARB bus (per-identity ACLs)

Status: operational reference, 2026-08-09. The ARB bus is migrating off the shared managed DO Valkey
(`db-valkey-agent-bridge`, one shared `default` user) onto a self-hosted Valkey 8.1.9 on the
arb-memory droplet, where every identity holds its own scoped credential. This doc is the repo-native
guide for that bus: how to tell which bus you're on, what changed operationally, how to onboard, and
how Workflow 1 (engine dispatch + audit panels) behaves under scoped ACLs.

It folds two authored artefacts into the repo so this does NOT depend on ARB Memory being reachable:
the onboarding runbook (ARB Files `agent-files/artefacts/orch-onboarding-selfhosted-bus-2026-08-09.md`)
and the Workflow-1 compatibility analysis (`…/wf1-selfhosted-bus-analysis-2026-08-09.md`). The
authoritative design is bus spec `art-6f7a85f3dd5a6067` v2 (ARB Memory); section references below
(§4, §5, …) are to that spec.

> **MIGRATION IN PROGRESS.** Both buses are live. Each host flips independently; the managed bus is
> the warm fallback until Mark destroys it after soak. So the managed-bus idioms in
> `claude-peer-coordination.md` and `using-agent-bridge` SKILL are NOT wrong yet — they are wrong
> *on the self-hosted bus*. Know which one your `.env` points at before trusting any idiom.

## Which bus am I on?

- `AGENT_REDIS_HOST=arb-bus.example.com`, `AGENT_REDIS_PORT=6379`, a per-identity `AGENT_REDIS_USER`
  (e.g. `claude-orch-<host>`) → **self-hosted**. This doc applies.
- `AGENT_REDIS_HOST=…digitalocean.com`, `AGENT_REDIS_PORT=25061`, `AGENT_REDIS_USER=default` → **managed**
  (retiring). The older docs' KEYS/SCAN/LLEN idioms still work there.

## Bus identity card (self-hosted)

- Endpoint `arb-bus.example.com:6379`, **TLS-only**, no plaintext port. The bus runs on its own
  host, reachable by hostname over both the public listener and a private mesh (below).
- Trust: **system CA** (public Let's Encrypt) — do NOT pin a descriptor `ca` field. Certs are
  renewed on a schedule with sealed re-delivery to dependents (§3). A missed renewal takes down
  every plane at once with a TLS handshake error that looks like nothing else here — calendar it.
- DBs: 12 = coordination (`agent_scratch:`), 5 = memory+audit (`arbmem:`), 6 = eval (`eval:`),
  7 = trace (`arbmem:trace`). **`SELECT`/DB is NOT an ACL boundary on 8.1.9** — isolation rides
  key-NAME prefixes (§4, §10). A right-prefix wrong-DB write is NOT blocked; it succeeds and mis-homes
  where no consumer reads it. The only defense is end-to-end presence checks and URL hygiene.
- Reachability is gated by a cloud firewall in front of the bus (source-IP allowlist). Connection
  **timeout** = your host's egress IP isn't allowlisted (the operator adds it; allowlist changes are
  operator-only). Auth/NOPERM error = firewall is fine, creds are the problem. Never debug creds
  against a timeout.
- **EXCEPTION — hosts on a given site are NOT allowlisted; they use a mesh VPN instead.** Design
  rule: a site whose hosts egress through multiple, shifting NAT addresses can't be allowlisted by
  IP without a rule per interface that stays fragile as those addresses move (the
  observed-not-guaranteed residual, §10). Since the mesh path works, that's the answer for the whole
  site — **do not request a bus firewall allowlist entry for a mesh-affiliated host.** Reach the bus
  over the mesh instead: pin the bus hostname to its mesh-side address in `/etc/hosts` (the bus binds
  the mesh address in addition to public; the LE cert validates on the NAME, so keep using the
  hostname and never a bare IP without SNI). Non-mesh hosts continue to use the public listener +
  allowlist as normal.

## Credential model (standing pattern)

- **One orchestrator credential per orch TYPE per host** — ACL user `claude-orch-<host>` /
  `codex-orch-<host>` / `pi-orch-<host>` / `grok-orch-<host>`. NOT per session.
- Concurrent sessions of one orch type on a host (e.g. five Claude CLIs, one per repo) **share that one
  credential** but run distinct bus `agent_id`s: `claude-orch-<host>-<repo>`. The own-namespace grants
  use **suffix-wildcard** patterns (`~agent_scratch:agent:<self>*:*`), so one cred yields separate
  inboxes and no BLPOP race. This is the pattern for all future orch mints (proven live on the three
  host-b orch identities 2026-08-09).
- **Seats a host puts on the shared bus** use the single per-host `arb-worker-<host>` credential (§5) —
  own worker-inbox consume on `agent_scratch:agent:worker-*:inbox`, LPUSH-send, the mandatory
  audit-emit grant, and DENY on KEYS/SCAN. Never hand a seat the orchestrator's own credential.
- **Seats on a host's LOCAL loopback redis** (most hosts, for performance — the 2026-05-13
  persistence-backpressure fix) stay on that local bus, untouched. Only MAIN-bus references migrate.
- Delivery is **sealed** (ARB Secrets, X25519, fingerprint-vouched); plaintext creds never travel the
  bus. On receipt: atomic exclusive-create to a mode-600 file, echo only a fingerprint/label, never
  `cat` the value. Rotation = re-seal + daemon restart, not a file edit (§6).

### Ad-hoc (nohup) seats on the shared bus

An orchestrator can launch a seat ad-hoc (`nohup`, not launchd/systemd) under the shared
`arb-worker-<host>` credential — the ACL only sees env vars, so the launch method is irrelevant to
auth. Two things must be right, both easy to trip:

1. **Do NOT inherit the orchestrator's identity.** A `nohup` child inherits your shell env, which has
   *your* orchestrator `AGENT_REDIS_USER`/`AGENT_REDIS_PASSWORD` exported — the seat would then
   authenticate **as the orchestrator** (wrong identity; the "never give a seat the orch cred" line).
   Launch in a clean subshell that overrides them with the worker cred.
2. **Give it a `worker-<host-slug>-` agent_id via `--agent-id`**, or it NOPERMs on its own inbox and
   the dispatch hangs silently (the F2 trap) — the grant only matches
   `~agent_scratch:agent:worker-<host-slug>-*:inbox`.

> **`AGENT_ID=` IN THE ENVIRONMENT DOES NOT WORK FOR A SEAT.** The bridge daemon resolves its
> identity as `args.agent_id or derive_agent_id(tool, project, workspace, role)`
> (`src/agent_redis_bridge/bridge.py:504`) — there is **no `AGENT_ID` env fallback**. Exporting
> `AGENT_ID` is inert: the seat silently comes up on its *derived* id (e.g. `grok-emailhub-dev`),
> which does not match the worker grant, so it NOPERMs on its own inbox and every dispatch to it
> hangs. `AGENT_ID=` is correct only for `scripts/agent-inbox-watcher` (the Claude-peer watcher),
> which is a different program — do not carry the habit across. Pass **`--agent-id`** on the
> seat's command line. (This paragraph corrects a defect in the first published version of this
> snippet, caught 2026-08-09 by the mini-dev host review.)

```bash
( set -a
  source /path/to/arb-worker-<host>.env          # OVERRIDES your AGENT_REDIS_USER/PASSWORD with the worker cred
  set +a
  # PATH MUST keep the venv or dispatch_authority dies pre-enqueue (silent ModuleNotFoundError)
  nohup <bridge-clone>/scripts/<engine-seat-launcher> \
      --agent-id "worker-<host-slug>-<seat-id>" \
      >/path/to/<seat>.log 2>&1 &
)
```

**Launcher caveat:** this only works if the launcher actually forwards `--agent-id` to the daemon.
Some profile/wrapper scripts do not, and the derivation can never produce a `worker-` prefix — such
a seat physically cannot take a canonical worker id until its launcher passes the flag through.
Check your launcher before assuming; if it swallows the flag, fix the launcher rather than working
around it.

Verify it came up as the worker and not as you: from the seat's cred, an own-inbox round-trip
(`worker-<host-slug>-<seat-id>:inbox`) succeeds AND a foreign-inbox BLPOP NOPERMs. A passing PING
alone proves nothing — it would pass as either identity.

**Lifetime caveat:** `nohup` may NOT survive your session/cgroup teardown (observed 2026-08-09: a
one-shot watcher's exit killed its sibling heartbeat *despite* `nohup`; the fix was moving it to
user systemd). Fine for a seat that only needs to live for the current burst of work; for anything
that must persist, use launchd/systemd, not `nohup`.

## What CHANGED: managed-bus idioms that are now dead

On the managed bus every client was `default` — **NOPERM did not exist**, and browse commands worked.
Under scoped ACLs several long-standing idioms silently break:

| Idiom (managed) | Status on self-hosted | Do instead |
|---|---|---|
| Foreign inbox `LLEN`/`LRANGE` to see if a peer consumed | **NOPERM** — LPUSH succeeds, LLEN is denied. Send-then-read-back is gone. | The recipient's **reply** is the only consumption signal. Request an ack. |
| `KEYS 'agent_scratch:agent:*:status'` to list who's online | **NOPERM** (KEYS denied to non-admin) | Per-id `GET`/`TTL`/`PTTL`/`EXISTS` on a known `:status` key; or `agent-bridge-ping`. |
| `SCAN`/`EVAL KEYS` DB-probe to discover the peer's DB | **NOPERM** | DB is fixed at 12; set `AGENT_REDIS_DB=12`. No probing. |
| Writing to any DB "works" (SELECT as a boundary) | Writes succeed but ACL doesn't scope by DB | Get the prefix AND the DB right; verify end-to-end, not by the write returning OK. |
| `LLEN`/`LRANGE` on your OWN inbox | **Still fine** (own namespace) | Unchanged — pollution-drain and "how far behind am I" on your own inbox still work. |

## Onboarding a new orchestrator (condensed)

Full runbook: ARB Files `agent-files/artefacts/orch-onboarding-selfhosted-bus-2026-08-09.md`.

0. **Firewall** — get your egress IP (`curl -4s ifconfig.me`) added to the `arb-bus` DO firewall by
   Mark. Until then TCP 6379 times out.
1. **Pubkey + vouch** — mint an X25519 keypair, register the pubkey at
   `agent_scratch:secrets:pubkey:<agent_id>` (TOFU); the lead (`claude-arbcomms-arbbuzz`) vouches the
   fingerprint OUT-OF-BAND before any cred is sealed to it.
2. **Request + sealed receipt** — request minting from the lead over the bus (or Mark); minting runs on
   the droplet by `codex-arbmem-prod` (§9). Delivery is sealed; handle per the hygiene rule above.
2a. **BEFORE editing any env file: find out who ELSE loads it.** A seat's watcher often *borrows*
   another identity's env file with an `AGENT_ID` override at launch, so the file you are about to
   edit may not be "yours". Editing the six coordination variables in place then flips **both**
   identities: the borrower (intended) and the file's real owner (not intended) — the owner lands on
   the new bus authenticating as the *wrong* ACL user, and **nothing errors at the moment of the
   edit**. Live example (2026-08-09, db-a): the file `claude-arbdb-mssql`'s watcher loaded was
   `AgentRedisBridge/.env`, whose header declares it as `claude-project-g-db-a` — a separate seat. An
   in-place edit would have silently dragged `claude-project-g-db-a` onto the self-hosted bus under the
   wrong identity. (Severity footnote, corrected by the reporting seat the same day: that particular
   owner turned out to be **orphaned**, not active, so the real cost would have been misconfiguring a
   defunct identity rather than severing a live channel. The rule is unchanged and the reason is
   unchanged — the borrowing is invisible in the file itself, and *nobody knows which case they are in
   until they look*.)
   **Rule: give the flipping identity its OWN env file (mode 0600) and leave shared files
   byte-untouched.** Verify afterwards that the shared file still resolves to its original bus. When
   reaping watchers, kill only the flipping identity's processes — check by PID, not by pattern.

3. **Env wiring** — coordination vars (`AGENT_REDIS_HOST/PORT/USER/PASSWORD/DB=12/TLS=1/PREFIX=agent_scratch:`)
   vs memory-plane (`ARB_MEMORY_REDIS_URL` + role creds). Prefix must be exactly `agent_scratch:` or you
   NOPERM at runtime, not deploy (§4). Injection footguns (§6): publish needs `ARB_MEMORY_REDIS_URL`
   **sourced**, dispatch needs it **unset**; PATH must keep the venv or `dispatch_authority` dies with a
   silent ModuleNotFoundError; there is no single "the env file" (30/44 plists leave `AGENT_ENV_FILE`
   unset — check what each unit actually injects).
   - **Audit credential precedence — read before setting `ARB_AUDIT_REDIS_URL`.** Since dev commit
     `08d5652e`, `resolve_audit_redis` (`bridge.py`) resolves the audit-emit bus as
     `os.environ ARB_AUDIT_REDIS_URL` → env-file `ARB_AUDIT_REDIS_URL` → `os.environ ARB_MEMORY_REDIS_URL`
     → env-file `ARB_MEMORY_REDIS_URL`. `ARB_AUDIT_REDIS_URL` **takes precedence** — it exists so the
     long-lived audit-emitter can flip buses independently of the FABA memory-writer. The trap: a host
     that sets `ARB_AUDIT_REDIS_URL` (plausibly copy-pasted) **silently overrides** a correct
     `ARB_MEMORY_REDIS_URL` for audit emits, and a misdirected vote vanishes and resurfaces as
     `refused_reconcile` at verdict close. If you only need one bus for both, set `ARB_MEMORY_REDIS_URL`
     alone and leave `ARB_AUDIT_REDIS_URL` UNSET (the fallback keeps audit working). Set both only when
     you deliberately want them on different buses.
4. **Seats** — shared-bus seats under `arb-worker-<host>`; concurrent orch sessions each set
   `AGENT_ID=<type>-orch-<host>-<repo>`; local-loopback seats stay put.
5. **Verify** — see the checklist below. Every check must be able to FAIL.

## Workflow 1 under scoped ACLs (condensed)

Full analysis: ARB Files `agent-files/artefacts/wf1-selfhosted-bus-analysis-2026-08-09.md`. The theme:
a write that used to succeed can now NOPERM — and the dangerous ones fail-soft and surface two steps
later. Flow by flow:

- **Audit votes** (`--audit-panel` → `INCR arbmem:audit:run:*:seq` + `XADD arbmem:audit`, the
  **audit-emitter** grant, MANDATORY on every vote-emitter). A missing grant NOPERMs the emit, which is
  logged only in the unwatched seat daemon log; the seat's reply looks normal. **It surfaces at verdict
  close as `refused_reconcile` (exit 4)** — this is why the grant is mandatory. Triage: `gaps` contains
  `audit-consumer-incomplete` → consumer lag, retry first; `gaps` names a missing/unrostered seat that
  retry doesn't clear → emit-side denial, grep THAT seat's daemon log for NOPERM around vote time and
  check its `ARB_MEMORY_REDIS_URL` user. Recovery is the no-supersede recipe (new run-id, re-emit
  manifest + verbatim votes, close with `supersedes:`).
- **Verdict close** (`arb-audit-close-request` over the bus, **audit-close-client** role): unchanged in
  shape — you publish a close-request and the DSN-holding consumer reconciles; you still never need a
  DSN. Failures here are FRIENDLY (foreground, immediate). New ambiguity: exit 7 can now also mean your
  host can't reach the bus — `PING` before falling back to break-glass.
- **Transcripts/trace** (`XADD arbmem:trace`, DB 7, **trace-emitter**) and **eval tee** (`eval:events`,
  DB 6, **eval-emitter**): the §4 wrong-DB residual bites hardest here — a mis-homed write succeeds and
  vanishes silently. Check END-TO-END presence, never the XADD return.
- **Memory publishes** (`arb-memory-harness-publish`, **memory-writer**): the reply-key BLPOP
  (`arbmem:reply:*`/`write_result:*`/`artefact:fetch_result:*`) IS in-grant, so request→block-for-result
  works. A missing grant NOPERMs loudly in the foreground.
- **Coordination/dispatch**: `task:*` is a global UUID namespace (KEYS/SCAN denied → guess-only, but ids
  still leak via stderr/`/tmp`); keep `BRIDGE_NOTIFY_INBOX=0` on **every** host (the routing decision is
  made by the processing bridge, so one unmigrated host floods your `:inbox`).

## Verification checklist (every check must be able to FAIL)

Run per credential. Assert the SPECIFIC code, never a bare refusal.

1. **TLS PING** per cred → `PONG`. Timeout = firewall; WRONGPASS/NOAUTH = cred.
2. **Own-inbox round-trip** — LPUSH a canonical envelope to your own inbox, BLPOP it back. Proves the
   suffix-wildcard grant covers your full session id, not just the cred stem.
3. **Foreign-BLPOP deny** — `BLPOP agent_scratch:agent:<someone-else>:inbox 1` MUST return exactly
   `NOPERM No permissions to access a key`. `KEYS`/`SCAN`/`CONFIG GET` MUST return their exact
   `NOPERM ... no permissions to run the command` shapes. If a deny does NOT fire, STOP — report the
   over-grant before sending anything.
4. **Foreign-LLEN is NOPERM after a successful LPUSH** — confirm the send/read-back asymmetry; it is
   expected, not a bug.
5. **Heartbeat TTL** — `SET …:status … EX 60`, `TTL` > 0; the watcher refreshes it each loop.
   **A live TTL is NOT proof anyone is listening — beware the zombie seat.** An orphaned watcher
   lineage keeps refreshing `:status` (so presence reads ONLINE) while nothing surfaces what it
   reads: its parent session is gone, nothing holds the read end of its stdout, and its `BLPOP`
   still pops messages **atomically** — so work routed to it is consumed and vanishes unseen. Found
   2026-08-09 on db-a: `claude-project-g-db-a`'s watchers had run 75 days reparented to PID 1, with a
   dead counterpart and an online-looking presence key. Any liveness check resting on `:status`
   alone will call that healthy. **Pair presence with evidence of consumption** — a round-trip that
   requires a reply, not a TTL — before routing work to a seat. Same deaf-not-loud family as the
   leaked-watcher and cross-bus-deafness traps.
   Sharpened by the reap of that same seat minutes later: after its last process was killed, the
   presence key still read ONLINE for ~30s until the TTL expired. So presence **lags reality by up
   to one TTL window** — a green `:status` cannot even establish that a process EXISTS, let alone
   that anything is listening. Two distinct failures defeat TTL-based liveness: a live process with
   no listener (zombie), and no process at all inside the expiry window (lag).
6. **Watcher arm** — Monitor `persistent=true`, absolute paths, split watcher with `mkdir -p`; reap
   leaked watchers first; then LPUSH a self-addressed test and confirm the notification actually
   surfaces. Arm BEFORE the first outbound send. Two portability traps, both hit live on 2026-08-09:
   - **Use `command grep`. Do NOT hardcode `/bin/grep`** — it is Linux-only, and on a Mac it exits
     127, the pipeline dies, and the watcher never runs. Worse, it can die *after* the BLPOP: the
     mini's first chain-proof envelope was consumed and then lost to the broken stdout pipe. The
     absolute path was there to dodge the Claude Code `grep` shell function; `command` suppresses
     function lookup and reaches the real binary while still resolving through PATH, so it is
     portable. **`$(command -v grep)` was recommended here until 2026-08-11 and does NOT work** —
     `command -v` on a shell function returns the function name, so it resolves to the very thing
     being bypassed. **This is why step 4's prove-the-chain exists** — it is what
     caught it.
   - **`pgrep -af agent-inbox-watcher` SELF-MATCHES the shell running it**, returning a different PID
     each call; piping that to `kill` kills your own shell. Use the bracket trick —
     `ps -eo pid,args | awk '/[a]gent-inbox-watcher/'` — then kill by PID, per
     `kill-processes-by-pid-never-by-pattern`.

## Residuals (do not paper over)

- **Attribution, not isolation, within a host:** all of a host's seats share `arb-worker-<host>`;
  which-seat-voted survives only via the app-layer `source` field. True within-host isolation needs the
  deferred OS-level separation (§6, §10).
- **Prefix-not-DB isolation** (§4): a right-prefix wrong-DB write succeeds silently forever.
- **Firewall sources are observed NAT egresses** (§10): an ISP-side egress change strands a host with
  connect-timeouts indistinguishable from bus-down.
- **Cert horizon 2026-11-06** — one renewal point for the whole bus.

## See also

- `docs/claude-peer-coordination.md` — Claude↔Claude coordination (bus-aware notes flag managed-only idioms)
- `skills/using-agent-bridge/SKILL.md` — engine dispatch; § "Self-hosted bus (per-identity ACLs)"
- ARB Memory `art-6f7a85f3dd5a6067` v2 (spec), `art-da2ae12534abf69b` (deny-proof completeness)
