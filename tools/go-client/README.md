# go-client (Track 1) — Go client-edge for the bridge

Track 1 of `docs/superpowers/specs/2026-06-27-go-client-edge-and-seat-host-container-design.md`:
stateless Go clients (`agent-dispatch`, `ctl`) over the bridge's envelope/Redis contract, so you can
dispatch into / monitor the fleet from any host with zero clone/venv — typed, validated flags kill
the shell-recipe footguns by construction.

## Status — complete + verified end-to-end

A working `dispatch` + `status`/`result` over the bridge's envelope/Redis contract, zero external
dependencies (hand-rolled RESP so it builds to a static binary — the whole point of the client edge).

- **`envelope.go`** — request envelope/payload as Go structs whose field order == the Python
  dispatcher's insertion order; `Marshal()` produces BYTE-IDENTICAL wire output (compact separators,
  `ensure_ascii=False` via `SetEscapeHTML(false)`, no trailing newline). `testdata/golden/` holds 11
  frozen envelopes from `agent-dispatch --dry-run-envelope`; `envelope_test.go` asserts the Go builder
  reproduces all 11 byte-for-byte + an HTML-escaping guard.
- **`resp.go`** — minimal RESP2 client (encode + decode unit-tested): connect (TLS optional), AUTH,
  SELECT, and the LLEN/HSET/EXPIRE/RPUSH/BLPOP/HGETALL/GET the edge needs.
- **`config.go`** — env + `--env-file` resolution (fill-missing-only) and the structural guards:
  `--from` required (no silent legacy default), `--branch` non-empty (no detached-HEAD `""` →
  `invalid-branch`), `--env-file` existence-checked, bus resolved.
- **`dispatch.go`** — observable-queue pre-write, RPUSH to `<prefix>agent:<to>:inbox`, the BLPOP-reply
  loop on the caller inbox with **strict reply filtering** (`kind=="reply" && in_reply_to==self`;
  notify dropped; sibling reply re-queued), exit 0/1/124 mirroring Python. Task body is an `argv`
  string, never shell-interpolated → the `\n`/backtick gotcha is impossible by construction.
- **`ctl.go`** — `status` (HGETALL `task:<id>:status`) / `result` (GET `task:<id>:result`).

**Verified:** `go test ./...` green (envelope corpus + RESP + config guards + reply classification);
`dispatch --dry-run-envelope` is **byte-identical to the Python tool** for the same inputs; and a
**live round-trip** through the binary to a real `codex-bridge-dev` seat returned `{"ok":true,
"result":"ACK"}` and exited 0, with `status` reading back `state:completed`.

`parse_stance` / reconcile / pgvector search stay **Python** — the client edge is pre-engine and never
touches them ([[go-python-boundary]]).

## Usage

```sh
go build -o go-client .
FROM_AGENT_ID=claude-bridge-dev BRANCH=dev \
AGENT_REDIS_HOST=127.0.0.1 AGENT_REDIS_PORT=6379 AGENT_REDIS_DB=12 AGENT_REDIS_PREFIX=agent_scratch: \
  ./go-client dispatch --to codex-bridge-dev --timeout 600 "review the diff and reply"
# or fill connection/identity from a seat env file:
./go-client dispatch --to codex-bridge-dev --env-file envs/agent-redis-bridge-dev.env "..."
./go-client status --task-id <uuid> --env-file envs/agent-redis-bridge-dev.env
./go-client dispatch --to codex-bridge-dev --dry-run-envelope "..."   # print exact bytes, don't send
./go-client dispatch --to codex-bridge-dev --retry-engine-start "..." # re-dispatch ONCE on the engine
                                          # cold-start initialize-timeout flake (DSP-1; opt-in here,
                                          # scripts/dispatch-dev passes it by default)
```

## Build / test

```sh
cd tools/go-client
go test ./...        # corpus byte-match + escaping guard
```
