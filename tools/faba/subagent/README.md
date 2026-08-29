# FABA subagent form (Workflow A prototype)

Subagent-FABA per design note `art-0f9fa949a90ae634` §4: the same bounded-round
property as the SDK harness (`../faba_launch.py`), rebuilt on Claude Code
subagent primitives. Fresh context per Task dispatch = boundedness; the
SubagentStop hook = the integrity gate; the shared `../round-contract.md` =
one instruction surface for both forms.

## Pieces

- `.claude/agents/faba-round.md` — the round agent (restricted tools, thin:
  the contract arrives embedded in the dispatch brief).
- `subagent_stop_gate.py` — SubagentStop hook, the CONTENT half of the gate
  (contract v2). No-ops unless a round is armed via the pointer file
  `.claude/faba-current-round.json` (parent-authored: request_id, record id,
  workspace, prior_open_ids). Blocks the subagent's stop (exit 2) until
  decision-record.md exists, parses against the schema, and covers the prior
  open findings — no redis needed; loop protection records the round `failed`
  after 3 blocked stops (crash equivalence, the parent re-dispatches).
- `run_probe_round.py` — probe driver and the PUBLISH half of the gate. Mints
  ids, arms the pointer, embeds the contract in the brief, runs a fresh
  headless `claude -p` child session as the warm orchestrator (child env
  carries NO bus credential — PF1 containment), then publishes the record
  ITSELF via `faba_launch.publish_and_gate` (re-validating content, clean-slate
  DEL, own receipt round-trip) and reports both gates. Pointer disarm is
  ownership-checked; audit residue lands in the round workspace.

## Wiring (settings.local.json, not committed)

```json
{
  "hooks": {
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.venv/bin/python \"$CLAUDE_PROJECT_DIR\"/tools/faba/subagent/subagent_stop_gate.py",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

**Hooks and agent definitions snapshot at session start** — a session cannot
test a hook it just wrote. That is why the driver uses a child session, and
why production adoption needs the hook registered before the orchestrating
session starts.

## Known caveats (deliberate, prototype)

1. **PF1 remediated in v2 (parity with the SDK form):** the round agent never
   publishes and never receives the bus credential; the driver validates
   content, clean-slate-DELs the receipt key, publishes itself, and gates on
   its own receipt round-trip. Residual: a child with unrestricted Bash and
   repo read access could still harvest the credential from the repo env file
   — that containment is PF3-class work, deferred by owner (2026-07-18).
2. **Gate scoping greps the subagent's own transcript** (`agent_transcript_path`
   from the SubagentStop payload — NEVER the parent session's `transcript_path`,
   which contains the brief and would match every sibling stop; a finding from an
   early review panel of this prototype). An unscopeable stop is treated
   as foreign and never gated — integrity is preserved by the driver, which
   fails a round whose gate never fired. One armed round at a time; the driver
   refuses to arm over an existing pointer and disarms only its own request_id.
   Every stop while armed is appended to the workspace `gate-log.jsonl`.
3. **Permission surface untouched by owner decision (2026-07-18):** the
   agent's `tools` list restricts the round agent, but the deny-by-default /
   settings-isolation work (PF3 class) is deferred — do not extend this
   prototype in that direction without Mark.

## Proven live (2026-07-18, sonnet child sessions, post credential-containment fix)

- **Probe A**: full happy path — publish, receipt, gate pass on first stop, cold
  re-fetch from an independent session. Boundedness probe (design note §8.3)
  answered: the subagent tool list DOES bound — the round agent got only its
  declared six tools; ToolSearch itself was "not enabled in this context"; the
  ARB Memory connector and WebSearch were unreachable by both direct and
  discovery paths. The subagent form is more bounded than the SDK form (which
  relied on the interactive grant gate).
- **Probe B**: gate-block path — deliberate unpublished first stop was blocked
  (exit 2, `attempts: 1`), the agent obeyed the fed-back instruction, published,
  and the second stop passed.
- **Live payload pinned** (`gate-log.jsonl`): SubagentStop input carries
  `agent_id`, `agent_type`, `agent_transcript_path` alongside the parent's
  `transcript_path` — and probe A logged a real foreign-subagent stop that the
  fixed scoping correctly ignored (the pre-fix code would have gated it).
- **Observed once, treat as unconfirmed:** a hook registered mid-session in
  settings.local.json fired in that same session (probe A, first gate-log
  entry), contradicting the "hooks snapshot at session start" doc claim. The
  fresh-child-session pattern stays the reliable wiring assumption.

## Author rounds (Workflow C, owner-directed 2026-07-19)

`run_author_round.py` is the author twin of the probe driver: same parent-minted
ids, same pointer-armed SubagentStop gate (pointer `kind: "author"` switches the
content check to workspace `artefact.md` under `faba_schema.validate_authored_artefact`
— title + change-summary + stub floor; quality stays the review panel's job),
same harness-publish + receipt discipline (`faba_launch.publish_artefact_and_gate`,
validate-first: an invalid artefact publishes nothing and needs no bus to say so).
The `faba-author` agent def deliberately carries **no model pin** — the author
inherits the child session's model, so the driver's `--child-model` is the
per-stage author-tier lever (inline-cold/Opus/Fable stays the owner-set choice).
The author contract (`../author-contract.md`) is embedded verbatim in the brief;
its load-bearing line is the pointer-only return: the artefact body's ONLY
channel is the workspace file.

For a revision, `--artefact-id` now fetches store HEAD at arm time and stages
`prior-record.md` byte-exact by default. `--prior-record-file` is the explicit
arm-time HEAD-snapshot / forensic-staging override: it wins for staging, while a
reachable divergent HEAD produces a non-fatal warning; the round can arm, but
the publish-time read-back will not allow a divergent override to publish as a
revision. Hash-covered `author-input.json` records `prior_source`,
`prior_sha256`, and, for a store fetch, `store_version` and `fetch_request_id`.
Per the Q5 design-note addendum, publish integrity intentionally remains a
content-only predicate; the round-close, version-only subject spot-diff now makes
an A→B→A version bump visible post-hoc without refusing or rolling back the round.

```sh
.venv/bin/python tools/faba/subagent/run_author_round.py \
  --stage design --subject-summary "..." --task "..." \
  [--artefact-id art-...] [--prior-artefact-id art-...] \
  [--prior-record-id art-...] [--prior-record-file path] \
  [--child-model sonnet|opus|fable] --env-file <env-with-ARB_MEMORY_REDIS_URL>
```

## Probe rounds

```sh
# A: happy path (publish → gate pass → cold re-fetch)
.venv/bin/python tools/faba/subagent/run_probe_round.py \
  --artefact-id art-81438f2f5a5c4955 --round 1 --task "..."

# B: gate-block path (deliberate no-publish first stop; exit-2 block must
# feed back, agent publishes, second stop passes)
... --block-test
```
