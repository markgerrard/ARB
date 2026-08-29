# Go client-edge dispatcher + seat-host containerisation — design (next worktrack)

> Status: **design / decision record.** Not yet built. Captures the conclusion of the
> 2026-06-27 Go-conversion discussion so it can become the next worktrack.

## Motivation

The friction in the fleet/orchestration layer is distribution: standing up and reaching the
bridge across hosts (mac-mini, `host-d`/project-a, etc.). The question was which Python →
Go conversions are actually worth it. The answer reshapes "rewrite the bridge in Go" into a
much sharper, smaller move.

## The decomposition (the load-bearing move)

"The fleet layer" is two layers with **opposite Go-fitness**:

- **A — transport / lifecycle spine:** envelope in/out, inbox `BLPOP`, trusted-sender policy,
  `task:*` status/events/result keys, parallelism gating, the stream tees. Pure transport, no
  ML/pgvector, and **concurrency-native** — the one place in the system where Go's concurrency is
  a genuine win rather than a non-reason. Squarely Go-able.
- **B — engine adapters:** spawn and drive codex (CLI), agy/gemini (ACP), pi (Node SDK + MCP
  bridge) over stdio/ACP/MCP. Heterogeneous, **partly inherently Node/CLI**. A Go core does not
  *escape* this zoo — it sits *above* it and orchestrates it. B stays a messy polyglot boundary
  regardless of language.

So the honest shape is **"A is the distributable spine; B stays a messy adapter boundary
regardless,"** not "bridge → Go."

## The seam

The boundary between A↔B and between bridge↔clients is **the envelope on Redis (`SPEC.md`)** —
already a *serialization* boundary. Principle: **cross a language boundary at a serialization
seam, never mid-logic.** Because the envelope is already bytes-on-the-wire with a defined
contract, a Go component on one side and Python on the other coexist cleanly *if the envelope
contract holds*.

The one mid-logic trip: the bridge runs `parse_stance` for vote extraction on task-finish. A Go
spine would either reimplement it (shared algorithm → drift) or **emit the raw reply and let
Python parse downstream**. Decision: **emit raw, parse in Python.** `parse_stance` is *compute*,
so it stays Python; the Go spine stays *transport*. (This keeps A genuinely pure-transport.)

## Decision: Go at the client edge, NOT the daemon spine

Build **`agent-dispatch` and `ctl` as Go binaries** — stateless clients over the envelope/Redis
protocol. Client-edge-first is right on **three axes at once**:

1. **Lowest risk** — a bug in a stateless dispatch client breaks *a dispatch*, not *the fleet*.
   A bug in the bridge spine breaks *everything*. Smallest blast radius to get the port discipline
   wrong.
2. **Highest distribution leverage** — `agent-dispatch`/`ctl` have **no repo and no engine
   dependency**: they put an envelope on Redis or read status/events off it. All they need is
   network reach to Redis + the contract. A static binary lets you **dispatch into / monitor the
   fleet from any host (laptop, phone, random box) with zero clone/venv.**
3. **Kills the shell footguns by construction** — turning the fragile shell recipe into **typed,
   validated flags** removes a whole class of operator error (structural safety over discipline).

Crucially, the **client edge has zero compute coupling**: it never parses a stance, never touches
PG, never runs `reconcile`. Its entire surface is the envelope. That makes it the ideal proving
ground for the envelope-contract discipline before betting the bridge on it.

## Why distribution does NOT justify the daemon-spine port

The catch that punctures the "distribute everywhere" dream for seat-hosting:

- **Engines (layer B) need the repo + their runtimes on the host, regardless of bridge language.**
  A static Go bridge binary does not make a bare host able to host seats — the zoo (codex/pi/agy +
  working tree) still has to be present.
- **The container subsumes the bridge's language entirely.** Once you containerise the seat-host
  (engine zoo + runtimes + bridge baked into the image, repo as a mounted volume), the bridge
  being Go-vs-Python is just "one binary vs a venv" — both baked in, nobody clones either.

Therefore **distribution can never justify the daemon-spine port.** A spine port would have to
stand entirely on concurrency-fit / maintainability — a much weaker case. **Park it.**

## Two pains, two answers

| Distribution pain | Right lever | Go's role |
|---|---|---|
| Dispatch / monitor *into* the fleet from many places | **Go client tools** (`agent-dispatch`/`ctl`) | the whole win |
| Stand up *seat-hosts* on many hosts | **containerise the seat-host** | Go barely helps |

If the pain is dispatch-from-anywhere → the Go client tools may be the *whole* solution and the
daemon stays Python. If the pain is seat-host ceremony → client tools first (cheap de-risk), then
the lever is **a container image, not a Go rewrite.** The two pains have *different* right answers;
do not let "rewrite the bridge in Go" be the answer to the second.

## Track 1 — the Go dispatcher (`agent-dispatch` / `ctl`)

**Step zero (before any Go): freeze the envelope contract as a golden fixture corpus.** `SPEC.md`
is prose; the Python `agent-dispatch` honors the protocol partly by convention. Extract
`(inputs → exact envelope bytes + exact Redis ops)` fixtures that the *current Python* tool passes,
then port Go to byte-match them. The port's **first dividend is a tested protocol spec**,
independent of Go — and any place the contract is leakier than `SPEC.md` admits surfaces here, on
the cheap stateless piece.

**Footgun → typed-flag map:**
- `FROM_AGENT_ID` → required flag, validated against the trusted-sender list (no silent legacy default).
- `BRANCH` → non-empty guard built in (no detached-HEAD `""` → `envelope-invalid invalid-branch`).
- `AGENT_ENV_FILE` → existence + readability checked before dispatch.
- task body → passed as an `argv` string, never shell-interpolated → the `\n`/backtick gotcha is
  **impossible by construction**.
- wait → the BLPOP-reply loop **is the binary's job** → no background-vs-wrapper trap; the tool *is*
  the waiter.
- reply filtering → strict `kind == "reply" && in_reply_to == self` (filters the `kind=notify`
  per-tool-call storm).

`parse_stance` stays Python — clients are pre-engine and never touch it.

## Track 2 — the seat-host container

Same container pattern already running for the observability consumers/gateway (deployed via
`docker compose` on arb-prod), extended to seat-hosts.

- **Image** = engine zoo (codex / agy / pi+Node) + runtimes (Python, optionally the Go bridge) +
  the bridge. `docker pull` replaces "install each engine after every clone" — *that* is the
  ceremony you skip.
- **Repo = bind mount, not baked.** Canonical container path `/repos`; the working tree stays a
  host mount (it must — worktree-per-dispatch makes it mutable shared state). Per-host `REPO_ROOT`
  maps the host layout onto `/repos`:
  - `host-d`: `-v /home/<user>:/repos` → all 20-30 repos appear under `/repos/<name>`.
  - mac: `-v /Users/<user>:/repos`, or multiple mounts for scattered repos
    (`-v /Users/<user>/<workspace>:/repos/<workspace>` …).
  - The only per-host config is `REPO_ROOT`; the image, bridge config, and `AGENT_WORKDIR` are
    byte-identical everywhere.
- **Auth = device-code login once per host, token in a named volume.** Headless OAuth (device
  code → approve in a browser anywhere) works identically inside a container. Persist the CLI
  config/token dir to a volume (`-v codex-auth:/home/app/.codex`) → survives restarts → **once per
  host**, exactly as today. The container adds no auth steps.
- **UID:** run as the host user (`--user $(id -u)`) so ownership on the mounted working tree
  matches — else git/edits hit permission errors.
- **Convention change (the gotcha):** dispatch briefs and `AGENT_WORKDIR` must use **container
  paths** (`/repos/<project>/…`), not host-absolute paths. Current habit uses host paths
  (`/Users/<user>/<workspace>/.claude/worktrees/…`); a host-absolute path that does not exist in the
  container fails silently as "file not found." This convention shift is the migration's one real
  footgun.

## What stays Python (the boundary, reaffirmed)

- **pgvector / embedding / RRF dual-arm search** — reimplementation drifts silently.
- **`reconcile` / anti-laundering** — correctness-critical shared algorithm.
- **`parse_stance`** — compute; lives on the Python side of the seam ("emit raw, parse downstream").
- **Engine adapters (B)** — heterogeneous, partly Node/CLI.

## Open question that sets sequencing

Which pain bites? **"I want to dispatch/watch the fleet from a laptop without a clone"** (→ Go
client tools may be the whole answer, daemon stays Python) vs **"every seat-host is a clone/venv/
launchd ceremony I maintain"** (→ client tools first as the cheap de-risk, then the
**seat-host container** is the real lever — Go does not save the clone there). Either way the
**first build is the client tools**; the answer only decides whether the container track is the
primary destination.
