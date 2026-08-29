from __future__ import annotations

import json
import logging

# Structured audit logger for ARB Files mutation events (delete / force-overwrite).
# Emitting through the stdlib logger makes the audit half of the delete-safety plane LIVE in prod
# (launchd/journald captures it) rather than dormant. A deployment that wants queryable audit can
# pass its own sink (e.g. one that writes to the fleet `audit_events` table) into FilesStore.
_audit_log = logging.getLogger("arb_files.audit")


def default_audit_sink(event: dict) -> None:
    """Emit a mutation audit event as one structured JSON log line.

    Raises are intentionally NOT swallowed by the caller (`FilesStore._emit_audit` lets them
    propagate) so an audit failure aborts the destructive op rather than silently dropping evidence
    (memory: evidence-store-no-silent-drop).
    """
    _audit_log.info("arb_files_audit %s", json.dumps(event, sort_keys=True, default=str))
