# agy-print granular transcript via SQLite-poll — design

> Status: **design, fully spiked + Python-verified.** Upgrades the existing `agy-print` engine to emit
> granular live transcript (command_started/output/finished + model_text, codex parity) WITHOUT tmux — by
> polling agy's conversation SQLite store during the `agy -p` run and parsing its protobuf step blobs.
> **agy-tmux (already merged, `309c59f`) is KEPT** until this is proven over a decent period, then deprecated.
> Lead: the Rust `agy-acp` adapter (openabdev/openab) does the SQLite+protobuf read; we go beyond it (tool
> OUTPUT too) in pure Python.

## Verified mechanics (Python-confirmed against a real conversation db, 2026-06-27)
- agy writes each step LIVE to `~/.gemini/antigravity-cli/conversations/<conversation-id>.db` (SQLite) during
  `agy -p` — the WAL grows incrementally (62KB→2.4MB across a run), checkpoints at end. So it's tailable live
  via WAL-aware read-only SQLite (`file:<db>?mode=ro` + immutable/NO_MUTEX; do NOT lock the writer).
- Table `steps(idx INTEGER, step_type INTEGER, step_payload BLOB)`. Poll `SELECT idx, step_type, step_payload
  FROM steps WHERE idx > <last_emitted> ORDER BY idx`; track last_emitted (incremental, no re-emit).
- **Protobuf extraction** (hand-rolled varint reader — `get_field(blob, n)` reads the length-delimited field n):
  - **model text**: `step_type == 15`; text = field 20 → field 1 (utf-8).
  - **tool call**: `step_type in {5,7,8,9,17,21,33,101,138}`; the call = field 5 → field 4; name = field 2
    (fallback 9); input(JSON args) = field 3.
  - **tool OUTPUT** (beyond agy-acp): in the same tool step, a per-tool RESULT field — verified map:
    `view_file→14`, `list_dir→15`, `grep_search→13`, `run_command→28`. Generalise: the result field is the
    largest length-delimited field that is NOT field 5; keep the explicit map for known tools + the
    largest-non-5 fallback for unknowns.
  - `step_type == 14` field 19 = the USER_INPUT task (carries our nonce — used for correlation; not re-emitted).
    Other step_types (23 planning, 98 metadata, etc.) → ignore.

## Engine upgrade (agy_print.py)
`run_turn_with_progress(task, ..., on_event)`:
1. Inject a unique **nonce** into the task (e.g. dispatch run_id/task_id) so it lands in the conversation's
   USER_INPUT step (as agy-tmux does).
2. Snapshot `conversations/` `.db` set, spawn `agy -p <task-with-nonce> --add-dir <cwd> --dangerously-skip-permissions
   --model <m>` (non-blocking; keep returning its clean stdout as `TurnResult.result`, unchanged).
3. **Poll** the new conversation db(s) (~200–500ms) while the process runs; identify OURS = the db whose
   USER_INPUT step contains our nonce. If >1 candidate matches the nonce → fail-loud (don't bind). Emit each
   new step via `on_event`: tool step → command_started(tool_name=name, content=args) + command_output(content=
   result) + command_finished; type-15 → model_text; seq = idx. Through the EXISTING transcript plane
   (handle_progress/_capture/flusher → trace → transcript_io + redaction) — no new plane.
4. On process exit: drain remaining steps, then return the stdout `TurnResult` as today.

## Robustness — fail-loud on schema drift (the protobuf is reverse-engineered)
Centralise field numbers / step_types / the tool-result-field map as named constants. A startup/first-step
**self-check**: if the steps table is missing, or a known step's known fields don't decode, LOG a clear warning
and **fall back to stdout-result-only** (no granular events) — NEVER emit garbage/partial blobs. (Mirror
agy-acp's "schema changed?" warnings.) Capture stays behind the existing `ARB_TRANSCRIPT_CAPTURE` kill-switch.

## Why this over agy-tmux
Same granular fidelity, but: no tmux, no trust-dialog, no send-keys, no pane busy/idle heuristic, no
per-turn interactive launch cost — it upgrades the EXISTING agy-print seat. Cost: parsing reverse-engineered
protobuf-in-SQLite (fragile → the fail-loud self-check) vs agy-tmux's human-readable JSONL. **Keep agy-tmux
running until agy-print-granular is proven over a decent period; then deprecate agy-tmux.**

## Testing
- Unit: a committed **fixture conversation .db** (a real tool run with run_command/view_file/list_dir/grep) →
  assert the extracted on_event sequence (model_text; per-tool command_started+args / command_output+result /
  command_finished; seq=idx; tool-output field map incl an unknown-tool fallback).
- Protobuf unit tests: get_field/varint against crafted blobs incl truncated/oversize (no panic, returns None).
- Nonce correlation: two candidate dbs, only the nonce-matching one is bound; ambiguous → fail-loud.
- Schema-drift: a db with a renamed/absent steps table or undecodable field → falls back to stdout-only, logs.
- Live E2E: drive an agy-print turn (capture on) with a run_id + tool task → command_*/model_text stream to the
  trace stream live AND land in transcript_io (redacted); stdout result unchanged.
