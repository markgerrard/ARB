# Running Claude Code (CLI) against Kimi / GLM / MiniMax-M3 / OpenRouter

How to make the interactive `claude` CLI talk to a non-Anthropic, Anthropic-compatible model provider
(Moonshot/Kimi, Z.ai/GLM, MiniMax-M3, OpenRouter) via per-provider env files + thin wrapper scripts + shell aliases.

> **Scope.** This is for the **interactive `claude` CLI on your workstation** — a quick way to drive
> Claude Code with a cheaper/alternate model. It is **separate** from the bridge's `agent-sdk` engine
> seats (`src/agent_redis_bridge/engines/agent_sdk_models.py`), which route the *same* providers for
> headless dispatch and use their own base-URLs/model-ids. Don't conflate the two; the CLI setup here is
> standalone and needs no bridge.

> **🔑 Secrets never live in this repo.** The provider env files hold real API tokens and live under
> `~/.config/` (outside any git tree). Never paste a real key into this doc, a committed file, or a PR.
> Every key below is a placeholder.

## How it layers

```
~/.zshrc  (aliases)
   └─ claudeyk → claude-kimi --dangerously-skip-permissions
                    └─ ~/.local/bin/claude-kimi  (wrapper: sources env, exec claude)
                           └─ ~/.config/claude-provider-env/kimi.env  (BASE_URL + token + model)
```

Three pieces per provider: an **env file** (the only place a key lives), a **wrapper** on `PATH`, and an
**alias**. Add a provider by copying the trio.

## 1. Provider env files — `~/.config/claude-provider-env/<provider>.env`

```sh
mkdir -p ~/.config/claude-provider-env
chmod 700 ~/.config/claude-provider-env
```

Create each file, then `chmod 600` it (keys inside). **Replace every `REPLACE_WITH_*` placeholder with your
real token** — do not commit these files.

### `kimi.env` — Moonshot / Kimi
```sh
export ANTHROPIC_BASE_URL="https://api.moonshot.ai/anthropic"
export ANTHROPIC_AUTH_TOKEN="REPLACE_WITH_KIMI_TOKEN"
export API_TIMEOUT_MS="3000000"
export ANTHROPIC_MODEL="kimi-k2.7-code"
export ANTHROPIC_DEFAULT_OPUS_MODEL="kimi-k2.7-code"
export ANTHROPIC_DEFAULT_SONNET_MODEL="kimi-k2.7-code"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="kimi-k2.7-code"
export CLAUDE_CODE_SUBAGENT_MODEL="kimi-k2.7-code"
export ENABLE_TOOL_SEARCH="false"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="262144"
```

> **Token-prefix gotcha.** Moonshot has *two* platforms with separate Anthropic-compatible
> endpoints, and `ANTHROPIC_BASE_URL` depends on which token you hold (the prefix is the tell):
> - `sk-...` (no `kimi-`) — issued via [platform.moonshot.ai](https://platform.moonshot.ai) → use `https://api.moonshot.ai/anthropic` (shown above).
> - `sk-kimi-...` — issued via the **Kimi-for-Coding** platform → use `https://api.kimi.com/coding` instead.
>
> Model id `kimi-k2.7-code` works on both. A mismatched URL/token pair returns
> `401 invalid_authentication_error` in under a second (not a slow timeout) — the endpoint
> rejects the credential structurally, so this is easy to confirm with a one-line `curl`.

### `glm.env` — Z.ai / GLM
```sh
export ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic"
export ANTHROPIC_AUTH_TOKEN="REPLACE_WITH_GLM_TOKEN"
export API_TIMEOUT_MS="3000000"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="1000000"
export ANTHROPIC_MODEL="glm-5.2"
export ANTHROPIC_DEFAULT_OPUS_MODEL="glm-5.2"
export ANTHROPIC_DEFAULT_SONNET_MODEL="glm-4.7"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="glm-4.7"
```

### `minimax.env` — MiniMax-M3
```sh
export ANTHROPIC_BASE_URL="https://api.minimax.io/anthropic"
export ANTHROPIC_AUTH_TOKEN="REPLACE_WITH_MINIMAX_TOKEN"
export API_TIMEOUT_MS="3000000"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
export ANTHROPIC_MODEL="MiniMax-M3"
export ANTHROPIC_DEFAULT_OPUS_MODEL="MiniMax-M3"
export ANTHROPIC_DEFAULT_SONNET_MODEL="MiniMax-M3"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="MiniMax-M3"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="1000000"
```

### `openrouter.env` — OpenRouter (aggregator; here pinned to a stealth model)
```sh
export ANTHROPIC_BASE_URL="https://openrouter.ai/api"
export ANTHROPIC_AUTH_TOKEN="REPLACE_WITH_OPENROUTER_KEY"   # sk-or-v1-...
# Must be blank, NOT unset: a non-empty ANTHROPIC_API_KEY is sent as x-api-key and
# treated as a direct-Anthropic credential, which conflicts with the bearer token.
export ANTHROPIC_API_KEY=""
export API_TIMEOUT_MS="3000000"
export ANTHROPIC_MODEL="stealth/ox-alpha"
export ANTHROPIC_DEFAULT_OPUS_MODEL="stealth/ox-alpha"
export ANTHROPIC_DEFAULT_SONNET_MODEL="stealth/ox-alpha"
# Side calls (WebFetch summaries, titles) — cheap; also the image-turn
# fallback target (see the vision gotcha below).
export ANTHROPIC_SMALL_FAST_MODEL="google/gemini-2.5-flash-lite"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="google/gemini-2.5-flash-lite"
export CLAUDE_CODE_SUBAGENT_MODEL="stealth/ox-alpha"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="1048576"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="1048576"
```

> **OpenRouter differs from the three above in two ways.** (a) The base URL has **no
> `/anthropic` suffix** — it is `https://openrouter.ai/api`, and Claude Code appends
> `/v1/messages`. (b) `ANTHROPIC_API_KEY` must be set to the **empty string**. Unsetting
> is not equivalent: an unset var can be refilled from the claude.ai login/keychain,
> an empty one cannot. The visible cost is a startup warning that claude.ai connectors
> are disabled for that session — expected, not a fault.
>
> **`CLAUDE_CODE_MAX_CONTEXT_TOKENS` is load-bearing for unrecognized slugs.** Claude Code
> does not know `stealth/ox-alpha`, so it assumes a **200k** window and auto-compacts there
> *regardless of `CLAUDE_CODE_AUTO_COMPACT_WINDOW`*. Verified by the startup warning it
> emits when the var is absent. Set both. Model ids are unprefixed OpenRouter slugs
> (`stealth/ox-alpha`, `anthropic/claude-opus-5`); the cookbook's `~anthropic/...` tilde
> form selects OpenRouter's auto-routing variants.
>
> **Stealth models retain your data.** `stealth/ox-alpha` is served by an anonymous
> third-party provider; prompts and completions are **retained by that provider** (not
> used for training) under OpenRouter's Stealth Model Terms. Do not point a
> `--dangerously-skip-permissions` session at ARB, `~/.arb-secrets`, project-d, or any
> tree containing `.env` files. Ref:
> <https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration>
>
> **Vision gotcha (verified 2026-08-21).** OpenRouter's catalog declares `stealth/ox-alpha`
> as image-capable, but the upstream rejects **any** image content with
> `400 Provider returned error` (reproduced with a 1×1 PNG on both wire formats). Every
> main-loop turn carrying an image — Read on screenshots/GIFs/PDFs — fails at the primary
> and falls back; unpinned, that pivot lands on first-party Opus at full price (~7–10¢ per
> turn, since the retry re-sends the whole 1M-window context). The `claude-openrouter`
> wrapper therefore pins `--fallback-model google/gemini-2.5-flash-lite`, which serves the
> same turn for a fraction of a cent. Text-only turns never hit this — they succeed on
> `stealth/ox-alpha` (priced 0). The session transcript always labels these turns with the
> *requested* model; only OpenRouter's activity dashboard shows what actually served.
> Setting `ANTHROPIC_SMALL_FAST_MODEL` / `ANTHROPIC_DEFAULT_HAIKU_MODEL` does not cover it:
> those slots govern side calls (WebFetch summaries, titles), not main-loop turns.
>
> **Per-model OpenRouter variants.** Additional aliases pin other OpenRouter slugs the
> same way — `claudeymuse` → `meta/muse-spark-1.2-contributor` (`muse.env`,
> `claude-muse` wrapper) and `claudeyds4f` → `deepseek/deepseek-v4-flash-vision-exp`
> (`ds4f.env`, `claude-ds4f` wrapper). Copy `openrouter.env`, swap every main-tier
> slug, keep the gemini-lite side-call slots and the `--fallback-model` pin.

```sh
chmod 600 ~/.config/claude-provider-env/*.env
```

**Why these vars:** `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` point Claude Code at the provider's
Anthropic-compatible endpoint. `ANTHROPIC_MODEL` + the `*_OPUS/SONNET/HAIKU_MODEL` map every Claude-Code
model tier onto the provider's model id (so the Opus/Sonnet/Haiku selection all resolve there). The rest are
provider-specific tuning: long `API_TIMEOUT_MS` for slow generations, `AUTO_COMPACT_WINDOW` to match the
provider's context window, `ENABLE_TOOL_SEARCH=false` / `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` for
compatibility/quiet. Model ids (`kimi-k2.7-code`, `glm-5.2`/`glm-4.7`, `MiniMax-M3`) are what each provider
publishes — bump them as providers release new versions.

## 2. Wrapper scripts — `~/.local/bin/claude-<provider>`

Each wrapper sources its env file (auto-exporting via `set -a`) and execs the real `claude`. Make sure
`~/.local/bin` is on `PATH`.

```sh
mkdir -p ~/.local/bin
for p in kimi glm minimax; do
  cat > ~/.local/bin/claude-$p <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
source "\$HOME/.config/claude-provider-env/$p.env"
set +a
exec claude "\$@"
EOF
  chmod +x ~/.local/bin/claude-$p
done
```

`claude-openrouter` is written by hand instead of the loop, because it carries one extra flag:
the fallback pin demanded by the vision gotcha above.

```sh
cat > ~/.local/bin/claude-openrouter <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
source "$HOME/.config/claude-provider-env/openrouter.env"
set +a

# stealth/ox-alpha rejects image content upstream despite declaring vision
# support, so every main-loop turn carrying an image fails and falls back.
# Pin the fallback to a cheap vision-capable model instead of the default,
# which bills first-party Opus rates.
exec claude --fallback-model google/gemini-2.5-flash-lite "$@"
EOF
chmod +x ~/.local/bin/claude-openrouter
```

This yields `~/.local/bin/claude-kimi`, `claude-glm`, `claude-minimax`, `claude-openrouter`. They forward all args to `claude`,
so `claude-kimi -p "..."`, `claude-glm --resume`, etc. all work.

## 3. Aliases — `~/.zshrc`

```sh
# vanilla Claude Code, just skipping the permission prompts
alias claudey="claude --dangerously-skip-permissions"

# alternate-provider Claude Code
alias claudeyk="claude-kimi --dangerously-skip-permissions"     # Kimi
alias claudeyg="claude-glm --dangerously-skip-permissions"      # GLM
alias claudeym="claude-minimax --dangerously-skip-permissions"  # MiniMax-M3
alias claudeym3="claude-minimax --dangerously-skip-permissions" # MiniMax-M3 (clearer name)
alias claudeyo="claude-openrouter --dangerously-skip-permissions" # OpenRouter -> stealth/ox-alpha
```

> `--dangerously-skip-permissions` runs Claude Code without per-tool permission prompts. Use only in a
> trusted local workspace; drop the flag (or use the bare `claude-<provider>` wrappers) if you want prompts.

Then:
```sh
source ~/.zshrc
claudeyk    # Claude Code via Kimi
claudeyg    # Claude Code via GLM
claudeym    # Claude Code via MiniMax-M3
claudeym3   # MiniMax-M3 (clearer alias)
claudeyo    # Claude Code via OpenRouter (stealth/ox-alpha)
```

## Escape hatch — `claude-opus`

`~/.local/bin/claude-opus` returns a session to real Anthropic. It **`unset`s** every
override rather than merely declining to source one: if a provider var is already
exported in the parent shell (a prior `claudeyo` in the same terminal, or a stray rc
line), a plain `claude` inherits it silently.

```bash
#!/usr/bin/env bash
set -euo pipefail
unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY API_TIMEOUT_MS
unset ANTHROPIC_MODEL ANTHROPIC_DEFAULT_FABLE_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL
unset ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL
unset CLAUDE_CODE_SUBAGENT_MODEL CLAUDE_CODE_AUTO_COMPACT_WINDOW
unset CLAUDE_CODE_MAX_CONTEXT_TOKENS CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
unset CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK
unset ENABLE_TOOL_SEARCH
exec claude "$@"
```

Note `ANTHROPIC_API_KEY` is **unset** here, the inverse of the OpenRouter env file which
blanks it — unsetting is what restores the claude.ai login path and its connectors.

**Verify it with a differential test, not a self-report.** Poison the environment with an
unreachable base URL so a leak is unmistakable; the control must fail and the test must
pass:

```sh
# CONTROL — must fail with ConnectionRefused
env ANTHROPIC_BASE_URL="http://127.0.0.1:1" ANTHROPIC_AUTH_TOKEN=poison \
    claude -p "Reply with exactly: LEAKED"
# TEST — same env, must answer normally
env ANTHROPIC_BASE_URL="http://127.0.0.1:1" ANTHROPIC_AUTH_TOKEN=poison \
    claude-opus -p "Reply with exactly: ANTHROPIC_OK"
```

A hatch tested only against a clean environment proves nothing: with no override present,
sourcing nothing and unsetting everything are indistinguishable.

## Adding / updating a provider

1. Drop a new `~/.config/claude-provider-env/<name>.env` (base URL + token + model tiers), `chmod 600`.
2. Add a wrapper `~/.local/bin/claude-<name>` (copy the loop body above).
3. Add an alias in `~/.zshrc`, `source ~/.zshrc`.

To **rotate a key** or **bump a model id**, edit only the provider's `.env` — wrappers and aliases are
generic and don't change.

## Verifying it works

```sh
claude-kimi -p "say hi and name the model you are"   # one-shot; should answer as Kimi, not Claude
```
If you get an auth error, re-check the token in the provider `.env`. If it answers *as Claude/Anthropic*,
the env file wasn't sourced (check the wrapper path / `~/.local/bin` on `PATH`).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Answers as Claude/Anthropic, not the provider | env not sourced | confirm `~/.local/bin` on `PATH`; run the wrapper directly, not `claude` |
| `401` / auth error | wrong/expired token, wrong auth header, **or token issued for a different provider platform than the `ANTHROPIC_BASE_URL` points at** (most common: `sk-kimi-*` Kimi-for-Coding token against `api.moonshot.ai/anthropic` — see the Kimi token-prefix callout above) | re-check `ANTHROPIC_AUTH_TOKEN` in the `.env`; all three providers use `ANTHROPIC_AUTH_TOKEN` (not `ANTHROPIC_API_KEY`); confirm the base-URL matches the token's issuing platform |
| Truncated/early-compacted long runs | context window mismatch | set `CLAUDE_CODE_AUTO_COMPACT_WINDOW` to the provider's real window |
| Hangs on slow models | default timeout too short | raise `API_TIMEOUT_MS` (the env files use `3000000` = 50 min) |
| Model id rejected | provider renamed/retired the model | update `ANTHROPIC_MODEL` + the `*_MODEL` tier vars to the current published id |
| Compacts at ~200k despite a 1M-context model | slug unrecognized by Claude Code, so it assumes 200k and ignores `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | also set `CLAUDE_CODE_MAX_CONTEXT_TOKENS` to the real window (startup warning names this) |
| `⚠ claude.ai connectors are disabled…` at startup | `ANTHROPIC_API_KEY=""` is set-but-empty, as OpenRouter requires | expected on provider sessions; use `claude-opus` when you need connectors |
| `[claude-code:unrecognized_model]` line | model-registry lookup misses a non-Anthropic slug | cosmetic; the call still routes. Ignore, or map it in the `modelOverrides` setting |

## Relationship to the bridge's agent-sdk seats

The bridge runs these same providers headlessly as `agent-sdk` engine seats
(`src/agent_redis_bridge/engines/agent_sdk_models.py`, `MODELS`). That path uses its own per-model
`base_url`/`model_id`/`key_env`/`auth_style` and an **isolated, key-scrubbed subprocess env** — it is the
right mechanism for *dispatched* work and decorrelated review seats. This CLI setup is the *interactive*
counterpart and is intentionally independent (different base-urls/model-ids in places, e.g. Kimi here uses
`api.moonshot.ai/anthropic` + `kimi-k2.7-code`, the seat uses `api.kimi.com/coding/` + `kimi-for-coding`).
Keep them separate; don't cross-wire keys or URLs.
