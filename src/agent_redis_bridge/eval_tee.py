"""Extract-only allowlist for the eval tee. Build the durable payload by COPYING ONLY these
keys out of the source event into a fresh dict — never forward the source minus a denylist.
Raw tool I/O (command/args/output, model_text/_thinking) is absent by construction (= eval_io OFF)."""

# Frozen eval event schema version. Stamped by the bridge producer onto every eval record and stored
# as a top-level column by the consumer. Bump ONLY on a breaking change to the 6 correlation fields or
# the payload contract; add a migration when you do. Slice 1 freezes this at "1".
EVAL_SCHEMA_VERSION = "1"

EVAL_ALLOWLIST = frozenset({
    # turn/usage metadata only — NO free text, NO command/output.
    # Bounded scalars: tool_name is an identifier (not user text); ok/exit_code/attempt are
    # bounded (bool/int) and carried by the task_finished/command_finished vocabulary. `status`
    # is intentionally EXCLUDED (plan-panel codex P2: free-ish string; not in v3's pinned list).
    "tool_name", "tool_call_count", "turn_index",
    "stop_reason", "finish_reason",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "latency_ms", "exit_code", "ok", "attempt",
    "tool_call_id", "attempt_epoch", "event_ts", "turn_started_ts", "turn_clock_monotonic",
    "finality_evidence", "observed_inode", "observed_size",
})


def extract_eval_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in EVAL_ALLOWLIST if k in data}
