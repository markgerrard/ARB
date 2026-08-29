# Claude Code provider launchers — glm / kimi / minimax / openrouter / opus

Claude Code reads its model backend from environment variables at startup.
This is a thin wrapper pattern — **no patching of Claude Code itself** — that
swaps the backend by sourcing a per-provider env file before `exec claude`.
One launcher script per provider, backed by one env file, plus an `opus`
escape hatch that `unset`s every override to return to real Anthropic.

These launchers drive an **interactive local Claude Code session**, not a
bridge engine. They are the operator-side complement to the bridge worker
seats documented in [`qwen-worker-seats.md`](qwen-worker-seats.md): same idea
(retarget the model backend via env), different surface (a login shell vs a
bus seat).

## Architecture

| Layer | Path | Role |
|---|---|---|
| **Launcher** | `~/.local/bin/claude-<provider>` | `source`s a provider env file, then `exec claude "$@"`. |
| **Env file** | `~/.config/claude-provider-env/<provider>.env` (mode `0600`) | Exports `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and the model-tier mapping. |
| **Escape hatch** | `~/.local/bin/claude-opus` | `unset`s every override so a session falls back to real Anthropic / Claude. |

`exec claude` replaces the wrapper shell process, so the sourced exports
become Claude Code's startup environment. Because Claude Code resolves
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / the `ANTHROPIC_DEFAULT_*_MODEL`
vars at launch, the swap is total and requires no flag fiddling on the
`claude` invocation — args pass straight through (`"$@"`).

## Providers

| Provider | `ANTHROPIC_BASE_URL` | Opus / Sonnet model | Haiku model | Ctx window | Notes |
|---|---|---|---|---|---|
| **glm** | `https://api.z.ai/api/anthropic` | `glm-5.2[1m]` | `glm-4.5-air` | 1M | Zhipu. Cheaper Haiku tier for background calls. |
| **kimi** | `https://api.moonshot.ai/anthropic` | `kimi-k2.7-code` | `kimi-k2.7-code` | 256K | Moonshot. `ENABLE_TOOL_SEARCH=false`; subagent model pinned. |
| **minimax** | `https://api.minimax.io/anthropic` | `MiniMax-M3` | `MiniMax-M3` | 512K | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. |
| **openrouter** | `https://openrouter.ai/api` *(no `/anthropic`)* | `stealth/ox-alpha` | `google/gemini-2.5-flash-lite` | 1M | Aggregator. `ANTHROPIC_API_KEY=""` (blank, not unset) + `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. Stealth model **retains prompts**. Vision-capable fallback pinned on the launcher (`--fallback-model`); see the vision gotcha below. |
| **opus** (escape) | *(unset → Anthropic)* | real Claude | real Claude | — | Unsets every override; see below. |

The glm / kimi / minimax base URLs use an **Anthropic-compatible `/anthropic`
endpoint**, so Claude Code speaks its native Messages API and the provider
translates. OpenRouter is the exception: its base URL carries **no `/anthropic`
suffix** (Claude Code appends `/v1/messages` to `https://openrouter.ai/api`).
`ANTHROPIC_AUTH_TOKEN` (not `ANTHROPIC_API_KEY`) is the header all four expect —
and OpenRouter additionally needs `ANTHROPIC_API_KEY` explicitly **blanked**, since
a non-empty value is sent as `x-api-key` and read as a direct-Anthropic credential.

## The launcher script (one per provider)

Identical body for glm / kimi / minimax — only the sourced filename differs:

```bash
#!/usr/bin/env bash
set -euo pipefail

source "${HOME}/.config/claude-provider-env/glm.env"   # kimi.env / minimax.env
exec claude "$@"
```

`claude-openrouter` is the one launcher that adds a flag: it pins the
model-fallback target, because its primary model cannot take images (see the
vision gotcha below):

```bash
#!/usr/bin/env bash
set -euo pipefail

source "${HOME}/.config/claude-provider-env/openrouter.env"
exec claude --fallback-model google/gemini-2.5-flash-lite "$@"
```

Install once:

```bash
mkdir -p ~/.local/bin ~/.config/claude-provider-env
chmod 700 ~/.config/claude-provider-env
# write claude-glm / claude-kimi / claude-minimax into ~/.local/bin, then:
chmod +x ~/.local/bin/claude-glm ~/.local/bin/claude-kimi ~/.local/bin/claude-minimax
```

`~/.local/bin` must be on `PATH` (it is on this host; the `claude` symlink
itself lives there → `~/.local/share/claude/versions/<ver>`).

## The env files

Mode `0600` — each holds a live API token. Never commit these.

### `glm.env`

```dotenv
export ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic"
export ANTHROPIC_AUTH_TOKEN="<your-z-ai-token>"
export API_TIMEOUT_MS="3000000"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="1000000"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="glm-4.5-air"
export ANTHROPIC_DEFAULT_SONNET_MODEL="glm-5.2[1m]"
export ANTHROPIC_DEFAULT_OPUS_MODEL="glm-5.2[1m]"
```

### `kimi.env`

```dotenv
export ANTHROPIC_BASE_URL="https://api.moonshot.ai/anthropic"
export ANTHROPIC_AUTH_TOKEN="<your-moonshot-token>"
export API_TIMEOUT_MS="3000000"
export ANTHROPIC_MODEL="kimi-k2.7-code"
export ANTHROPIC_DEFAULT_OPUS_MODEL="kimi-k2.7-code"
export ANTHROPIC_DEFAULT_SONNET_MODEL="kimi-k2.7-code"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="kimi-k2.7-code"
export CLAUDE_CODE_SUBAGENT_MODEL="kimi-k2.7-code"
export ENABLE_TOOL_SEARCH="false"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="262144"
```

### `minimax.env`

```dotenv
export ANTHROPIC_BASE_URL="https://api.minimax.io/anthropic"
export ANTHROPIC_AUTH_TOKEN="<your-minimax-token>"
export API_TIMEOUT_MS="3000000"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
export ANTHROPIC_MODEL="MiniMax-M3"
export ANTHROPIC_DEFAULT_OPUS_MODEL="MiniMax-M3"
export ANTHROPIC_DEFAULT_SONNET_MODEL="MiniMax-M3"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="MiniMax-M3"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="512000"
```

### `openrouter.env`

```dotenv
export ANTHROPIC_BASE_URL="https://openrouter.ai/api"
export ANTHROPIC_AUTH_TOKEN="<your-openrouter-key>"   # sk-or-v1-...
export ANTHROPIC_API_KEY=""                           # blank, not unset
export API_TIMEOUT_MS="3000000"
export ANTHROPIC_MODEL="stealth/ox-alpha"
export ANTHROPIC_DEFAULT_OPUS_MODEL="stealth/ox-alpha"
export ANTHROPIC_DEFAULT_SONNET_MODEL="stealth/ox-alpha"
# Side calls (WebFetch summaries, titles) — cheap, and the fallback target
# for image turns (see the vision gotcha below).
export ANTHROPIC_SMALL_FAST_MODEL="google/gemini-2.5-flash-lite"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="google/gemini-2.5-flash-lite"
export CLAUDE_CODE_SUBAGENT_MODEL="stealth/ox-alpha"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="1048576"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="1048576"
```

### Per-provider tweaks worth knowing

- **Model tiers**: `glm` sets only the three `ANTHROPIC_DEFAULT_*_MODEL` tiers
  (cheaper Haiku). `kimi` and `minimax` additionally pin `ANTHROPIC_MODEL` and
  (kimi) `CLAUDE_CODE_SUBAGENT_MODEL`, so every lane — including subagents —
  resolves to the same model.
- **`API_TIMEOUT_MS=3000000`** (50 min) across all three: these proxies are
  slower than direct Anthropic and long agentic turns can exceed the default
  timeout.
- **`CLAUDE_CODE_AUTO_COMPACT_WINDOW`** is set per provider to the model's real
  context budget — GLM 1M, MiniMax 512K, Kimi 256K — so auto-compact triggers
  at the right point instead of assuming a 200K Claude window.
- **`CLAUDE_CODE_MAX_CONTEXT_TOKENS`** (openrouter) is required *in addition*, not
  instead: when the slug is unrecognized, Claude Code assumes 200K and auto-compacts
  there even with `AUTO_COMPACT_WINDOW` set. Its startup warning names this var.
- **Vision gotcha (openrouter, verified 2026-08-21).** OpenRouter's catalog declares
  `stealth/ox-alpha` as `text+image→text`, but the upstream provider rejects **any**
  image content with `400 Provider returned error` — a 1×1 PNG on both the
  chat-completions and Messages endpoints reproduces it. Every main-loop turn that
  carries an image (Read tool on screenshots, GIFs, PDFs) therefore fails at the
  primary and falls back. Unpinned, that pivot lands on first-party Opus at full
  price: ~7–10¢ per image turn, because the retry re-sends the whole 1M-window
  context. The launcher pins `--fallback-model google/gemini-2.5-flash-lite`
  ($0.10/$0.40 per Mtok, vision-capable), turning the same turn into a fraction of
  a cent. Text-only turns are unaffected — they succeed on `stealth/ox-alpha`, which
  prices at 0. Note the session transcript always labels these turns with the
  *requested* model (`stealth/ox-alpha`); only OpenRouter's activity dashboard shows
  what actually served. `ANTHROPIC_SMALL_FAST_MODEL` / `ANTHROPIC_DEFAULT_HAIKU_MODEL`
  do NOT cover this: those slots govern side calls (WebFetch summaries, titles), not
  main-loop turns — pinning them is not enough on its own.
- **Data retention** (openrouter): `stealth/ox-alpha` is served by an anonymous
  provider that retains prompts and completions (not for training) under the Stealth
  Model Terms. Keep `--dangerously-skip-permissions` sessions on it away from ARB,
  `~/.arb-secrets`, and any tree with `.env` files.
- **Per-model OpenRouter variants**: `claude-muse` (`meta/muse-spark-1.2-contributor`)
  and `claude-ds4f` (`deepseek/deepseek-v4-flash-vision-exp`) copy `openrouter.env`
  with a different `ANTHROPIC_MODEL` slug on every tier plus their own wrapper/alias;
  both keep the gemini-lite side-call slots and the `--fallback-model` pin as image
  insurance.
- **`ENABLE_TOOL_SEARCH=false`** (kimi) and
  **`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`** (minimax): turn off features
  these backends don't serve well (on-demand tool schema loading and
  background telemetry pings respectively).

## The escape hatch — `claude-opus`

Note `ANTHROPIC_API_KEY` is **unset** here — the inverse of `openrouter.env`, which
blanks it. Unsetting is what restores the claude.ai login path and its connectors;
a set-but-empty value keeps them disabled.

`unset`, not "don't source". If a provider override is already exported in
the parent shell (a prior `claude-glm` in the same terminal, or a line in a
shell rc), a plain `claude` would inherit it. The explicit unset guarantees a
clean return to real Anthropic:

```bash
#!/usr/bin/env bash
set -euo pipefail

unset ANTHROPIC_BASE_URL
unset ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_API_KEY
unset API_TIMEOUT_MS
unset ANTHROPIC_MODEL
unset ANTHROPIC_DEFAULT_OPUS_MODEL
unset ANTHROPIC_DEFAULT_SONNET_MODEL
unset ANTHROPIC_DEFAULT_HAIKU_MODEL
unset CLAUDE_CODE_SUBAGENT_MODEL
unset CLAUDE_CODE_AUTO_COMPACT_WINDOW
unset CLAUDE_CODE_MAX_CONTEXT_TOKENS
unset CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
unset CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY
unset CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK
unset ENABLE_TOOL_SEARCH

exec claude "$@"
```

## How to add a new provider

No code change — three files.

1. **Env file** at `~/.config/claude-provider-env/<provider>.env` (mode `0600`):

   ```dotenv
   export ANTHROPIC_BASE_URL="https://<provider-host>/anthropic"
   export ANTHROPIC_AUTH_TOKEN="<your-token>"
   export API_TIMEOUT_MS="3000000"
   export ANTHROPIC_DEFAULT_OPUS_MODEL="<model>"
   export ANTHROPIC_DEFAULT_SONNET_MODEL="<model>"
   export ANTHROPIC_DEFAULT_HAIKU_MODEL="<model-or-cheaper-tier>"
   export CLAUDE_CODE_AUTO_COMPACT_WINDOW="<ctx-window>"
   ```

2. **Launcher** at `~/.local/bin/claude-<provider>` (the generic body above,
   sourcing `<provider>.env`), then `chmod +x`.

3. **Smoke test** (next section).

If the new provider offers a cheaper small model, put it on the Haiku tier
(used for background/quick calls) and keep the flagship on Opus + Sonnet —
that's the `glm` split (`glm-4.5-air` vs `glm-5.2[1m]`).

## How to verify which backend a session is using

The session banner prints the resolved model id on startup — e.g.
`glm-5.2[1m]` confirms `claude-glm` routed correctly. From inside a session:

```bash
env | grep -E 'ANTHROPIC_BASE_URL|ANTHROPIC_DEFAULT_OPUS_MODEL'
```

**Do not trust the model's self-report.** Like the qwen bridge seats (see the
gotcha in [`qwen-worker-seats.md`](qwen-worker-seats.md)), GLM / Kimi / MiniMax
will misidentify themselves when asked "what model are you" — they pick up
Claude Code's harness branding or hallucinate a vendor. Confirm routing via
the banner / env, or via the provider's usage dashboard, not the model's claim.

## Gotchas

- **`[1m]` is a context-window tag, not part of the model id elsewhere.** It's
  specific to the z.ai model selector for GLM's 1M-token tier; copy it verbatim
  or the lookup fails.
- **`ANTHROPIC_AUTH_TOKEN` vs `ANTHROPIC_API_KEY`.** These proxies authenticate
  via `ANTHROPIC_AUTH_TOKEN` (sent as the bearer). Setting `ANTHROPIC_API_KEY`
  instead typically yields auth failures that look like network errors.
- **`exec` matters.** Without it the wrapper would fork and the sourced env
  would leak into the parent shell after the session exits. `exec claude "$@"`
  replaces the process so the overrides die with the session.
- **Subagents inherit the override.** Because the tier defaults (and, for kimi,
  `CLAUDE_CODE_SUBAGENT_MODEL`) are set, Task/Agent subagents spawned inside
  the session also hit the chosen provider — there is no silent fall-through to
  real Claude. If a subagent must use Anthropic, start from `claude-opus`.

## Infrastructure pointers (where things live)

- Launchers: `~/.local/bin/claude-{glm,kimi,minimax,openrouter,opus}`.
- Env files: `~/.config/claude-provider-env/{glm,kimi,minimax,openrouter}.env` (`0600`;
  not under any repo, never committed).
- Real binary: `~/.local/bin/claude` → `~/.local/share/claude/versions/<ver>`.
- Bridge-side sibling pattern for bus seats: [`qwen-worker-seats.md`](qwen-worker-seats.md).
- Shared env-override table (bridge dispatch, not these launchers):
  [`fragments/env-overrides.md`](fragments/env-overrides.md).
