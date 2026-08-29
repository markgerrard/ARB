# GROK-1 V1 protocol probe — raw wire captures (grok 0.2.93, model grok-4.5, 2026-07-10)

Backlog gate for `docs/BACKLOG.md § GROK-1`: drive the real `grok agent stdio` (ACP,
stdio) with a permission-requiring task and pin the permission-callback wire shapes +
discriminate H1/H2/H3 empirically. Each `run-*.jsonl` is the complete bidirectional
message log (`{"ts", "dir": "in"|"out"|"in-raw"|"summary", "msg"}`); `run-*.stderr.txt`
is that run's raw stderr. The task asked grok to write the word `probe` to an
**out-of-cwd** sentinel file (session cwd is `probe-repo/`, target is a sibling
`out-of-cwd/`), so "did the operation execute" is a filesystem fact, not an inference.
The probe speaks raw JSON-RPC (it does **not** use `GrokAcpEngine`) so each run controls
the exact reply shape to `session/request_permission`.

## Per-run observations

- **Run A — current-bridge reply `{"outcome":{"outcome":"approved"}}`.** The
  `session/request_permission` callback **arrived** (1 ask, full options offered). We
  replied with the bridge's current shape. Grok emitted `interaction_resolved` then
  `turn_completed stop_reason=cancelled`; the file was **not** written; the
  `session/prompt` response returned `stopReason:"cancelled"` cleanly (no `-32603`). The
  malformed outcome is silently coerced to "not approved" → the edit is skipped and the
  turn ends cancelled.
- **Run B — spec-correct accept `{"outcome":{"outcome":"selected","optionId":"allow-once"}}`.**
  Same task, same ask. File **written** (`probe\n`), `stopReason=end_turn`, turn ok. The
  only variable changed vs Run A was the reply shape.
- **Run C — spec-correct deny `{"outcome":{"outcome":"cancelled"}}`.** File **not**
  written, `stopReason=cancelled`, turn completes gracefully (no `-32603`). Clean
  deny-and-continue.
- **Run D — JSON-RPC error reply `{"code":-32601,"message":"test"}` to the ask.** File
  **not** written; grok emitted `interaction_resolved` → `turn_completed cancelled` and
  the prompt returned `stopReason:"cancelled"` cleanly. **error ⇒ no execution** (the
  fail-closed claim holds on this binary), and the error reply does **not** crash the
  turn with `-32603` — it degrades to cancelled.
- **Run E — DEFAULT mode (not yolo), out-of-cwd write, spec-correct accept.** Still
  exactly 1 permission ask; file written; `end_turn`. Default vs yolo made **no
  difference** to whether the out-of-cwd write asks.
- **Run E2 — DEFAULT mode, out-of-cwd READ of a sibling file, spec-correct accept
  handler.** **0** `session/request_permission` requests reached the client. Grok ran a
  `read_file` tool; a `pending_interaction (kind=permission)` sessionUpdate appeared and
  **auto-resolved internally** without ever emitting a client-facing ask. Turn completed
  `end_turn` (no `-32603`). Out-of-cwd reads do **not** route a permission ask to the
  client and do **not** kill the turn.
- **Run F — fs capabilities `{readTextFile:true, writeTextFile:true}` at initialize,
  yolo, out-of-cwd write, spec-correct accept.** Grok first asked
  `session/request_permission` (accepted), then tried to **delegate I/O to the client**
  via `fs/read_text_file` (id 1) and `fs/write_text_file` (id 2). Our probe rejected both
  with `-32601`; grok **fell back to its own write tool**, asked permission a second time
  (accepted), and the file was written (`probe`, no trailing newline — grok's own tool,
  not the delegated content). `end_turn`, 2 asks. Declaring fs caps changes routing to
  add client-delegation round-trips but never produced `-32603`; the ask still reaches
  the client.

## KEY EMPIRICAL FACTS

1. **The permission callback IS delivered to the ACP client.** Method name:
   **`session/request_permission`** (server→client request). Every write run (A, B, C, D,
   E, F) received it; it never routed through a dead internal worker and never produced
   `-32603`. This **refutes H2**.

2. **Ask params shape** (`params` of `session/request_permission`):
   `{ sessionId, toolCall: { toolCallId, kind:"edit", title, rawInput:{variant,file_path,content}, _meta:{"x.ai/tool":{...}} },
   options: [ {optionId,name,kind} ... ] }`. Observed options for a write:
   - `{"optionId":"allow-edits-session","name":"Yes, allow all edits during this session","kind":"allow_always"}`
   - `{"optionId":"allow-once","name":"Yes","kind":"allow_once"}`
   - `{"optionId":"reject-once","name":"No, and tell Grok what to do differently","kind":"reject_once"}`
   Options carry the ACP-standard `kind` (`allow_once`/`allow_always`/`reject_once`), so
   the cursor-engine `_select_allow_option` picker works verbatim — prefer `allow_once`,
   fall back to `allow_always`.

3. **Accepted reply shape (executes the op):**
   `{"outcome":{"outcome":"selected","optionId":"<an offered allow optionId>"}}` — i.e.
   the ACP-spec shape, identical to cursor-acp. `optionId:"allow-once"` executed and
   completed `end_turn` (Runs B, E, F).

4. **Rejected / non-executing reply shapes** (op NOT executed, turn ends `cancelled`,
   NOT `-32603`):
   - `{"outcome":{"outcome":"approved"}}` — **the current `grok_acp.py` reply**. `"approved"`
     is not a valid ACP outcome; grok coerces it to non-approval → **edit skipped, turn
     cancelled** (Run A). **This is the bug.**
   - `{"outcome":{"outcome":"cancelled"}}` — canonical deny (Run C).
   - JSON-RPC `error` reply (`-32601`) — **error ⇒ no execution** (Run D); degrades to
     `cancelled`, does not crash the turn.

5. **Mode behavior.** `session/set_mode` accepted `modeId:"yolo"` (result `{"meta":null}`,
   no error) and `"default"`. **`session/new` advertises NO modes** (result keys are
   `sessionId`, `models`, `_meta` — no `modes`/`availableModes`; modeIds are accepted
   blind). **yolo does NOT suppress the permission ask for out-of-cwd writes** — the ask
   fires identically in yolo (Run A) and default (Run E). So the fix cannot rely on a mode;
   it must answer the ask correctly. (In-cwd writes were not probed; yolo may suppress
   those — the bridge's normal in-cwd operation is consistent with that.)

6. **Out-of-cwd READS do not ask and do not die.** An out-of-cwd sibling read (Run E2)
   generated zero client-facing asks (auto-resolved internally) and completed `end_turn`.
   Reads are not the failure mode.

7. **The stderr worker-fatal is BENIGN and uncorrelated with turn outcome.**
   `ERROR worker quit with fatal: Transport channel closed, when Auth(AuthorizationRequired)`
   appears in **every** run's stderr — including the **successful** Run B and Run E. It
   does **not** predict turn death and is **not** the cause of the cancelled turns. This
   overturns the long-standing attribution in `art-d893502c280b1740` / BACKLOG § GROK-1
   that permission-requiring turns die *because* this worker is dead.

8. **Server-initiated request id namespace starts at 0**, overlapping the client's ids
   (client starts at 1): permission ask `id:0`, then `fs/read_text_file id:1`,
   `fs/write_text_file id:2`, next permission `id:3` (Run F). The engine's per-side
   id guard (`"method" not in message` disambiguates a response from an agent request)
   is load-bearing — a raw `id ==` match would misfire.

## Hypothesis verdict

- **H1 (malformed reply → turn death): SUPPORTED — root cause.** Controlled A/B with the
  reply shape as the single variable: `"approved"` → cancelled/no-write (A);
  `"selected"+optionId` → executes/end_turn (B, E). The fix is exactly the CDX-1/cursor
  machinery: under `trusted` policy pick an offered allow `optionId` and reply
  `{"outcome":{"outcome":"selected","optionId":...}}`; otherwise `cancelled`.
- **H2 (ask never reaches client; -32603 regardless): REFUTED.** The ask reaches the
  client every time and a correct answer executes the op.
- **H3 (yolo doesn't suppress asks): SUPPORTED as a secondary fact.** yolo is accepted but
  does not suppress the out-of-cwd-write ask; mode selection is not a substitute for
  answering the ask.

## Surprises

- **The observed failure is `stopReason=cancelled`, NOT `-32603`.** On grok 0.2.93 the
  current bridge's `"approved"` reply degrades gracefully to a cancelled turn; it does not
  reproduce the `-32603 Internal error` recorded in BACKLOG § GROK-1. The `-32603` the
  seat logs attributed to permission ops is either version-specific or came from a
  different path — but the turn-death symptom (permission-requiring op → turn dies, no
  work done) IS reproduced and IS fixed by the correct optionId reply. The malformed reply
  is the actionable cause regardless of the exact terminal code.
- **The `Auth(AuthorizationRequired)` worker-fatal is a red herring** — present on
  successful turns — so the whole "dead internal worker eats permission requests" mental
  model is wrong; the permission path is a normal client-side ACP callback we were simply
  answering with an invalid outcome string.

Probe script: `grok_probe.py` (in this dir); the JSONL/stderr captures are the evidence.
Re-run any cell with `python3 grok_probe.py <A|B|C|D|E|E2|F>`.

## Addendum — runs G, H, I (2026-07-10 evening, orchestrator-run)

Driver scripts lived in the session scratchpad; they reuse `grok_probe.py` as a module and
only change the launch argv (G) or compose multi-step sessions (H, I). Raw captures:
`run-G/H/I.jsonl` + stderr in this dir.

- **Run G — `--always-approve` is INERT over ACP.** Launched `grok --always-approve agent
  stdio` (the flag is top-level only; `agent stdio --help` does not list it), same
  out-of-cwd write, handler answers `cancelled`. The `session/request_permission` ask
  STILL arrived (1 ask) and the cancel blocked the write (`file_written: false`,
  `stopReason: cancelled`). Conclusion: the TUI permission surface (`--always-approve`,
  `[ui]` config, `/always-approve`) does not gate the ACP server; the ACP client is the
  sole permission authority.
- **Run H — `session/new` works on a live process; sessions are context-isolated;
  sessionId correlates asks.** One process: session A planted "codeword OSPREY-42"
  (`end_turn`); `session/new` returned a NEW sessionId on the same process; session B,
  asked to recall, replied exactly `no-codeword`; B's out-of-cwd write asked once, the ask
  params carried B's sessionId, write executed, `end_turn`. Conclusion (0.2.93): fresh
  session per dispatch on a warm process is viable, blank-context, and every ask is
  attributable to its session.
- **Run I — the `allow_always` option is COSMETIC.** Write #1 answered by selecting
  `allow-edits-session` (kind allow_always): executed. Write #2, same session: grok asked
  AGAIN (2 further asks). After `session/new`, write #3: 3 further asks. No suppression
  within the session, no persistence across sessions. Conclusion: there is no
  grant-once-quiet-forever mode; every ask must be answered on its own.

Design consequence (v1.3): opt-out grok seats keep the warm PROCESS but rotate to a fresh
session per dispatch, and the adapter denies any ask whose `sessionId` is not the current
session's — stale-ask authorization leakage becomes impossible by correlation, not by
ordering assumptions.
