# Retention purge deployment notes

Apply setup-schema before grants; the four span tables and their sequences must
exist before apply_eval_grants and apply_retention_grants run.

Provision ARB_RETENTION_ROLE as a dedicated login role and put its DSN in
ARB_RETENTION_DSN. The purge script connects to the raw tables as that role,
pins both retention windows to 56 days, and never runs as the schema owner.

Install deploy/systemd/arb-retention.service and .timer on the production
host, or use the launchd fragment on the development host. Do not add a compose
service for this one-shot.

Keep exactly one active EvalConsumer per consumer group. The epoch-ledger and
append-order assumptions are not valid with multiple active consumers splitting
the same group.

The eval-consumer needs column-level `SELECT (stream_entry_id)` on `span_deadletter` for the deadletter `ON CONFLICT`; prod already carries the manual grant, and re-running `grants` is idempotent.
