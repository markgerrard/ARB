from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import logging
import os
import time
import uuid

import psycopg

from arb_memory import graph
from arb_memory import store
from arb_memory.embed import embed as default_embed

logger = logging.getLogger(__name__)

# D-2: log once when ARB_HINT_READ_QUERY_KEY is unset/empty (module-level, not per call).
_query_key_missing_logged = False


class SearchRateLimitExceeded(ValueError):
    """Distinguishable from the query-too-long ValueError so the recorder can bound it (G-08)."""


def _cap(query: str, max_chars: int) -> tuple[str, bool]:
    """Truncate once for recording. Over-cap queries are rejected as reads but still recorded."""
    if len(query) <= max_chars:
        return query, False
    return query[:max_chars], True


def _query_columns(query_text: str) -> tuple[str | None, str | None]:
    """BUILD-CHARTER D-2: (query_hmac, query_text_to_store) from the already-capped query.

    - Key set and non-empty → HMAC-SHA256 hexdigest under ARB_HINT_READ_QUERY_KEY.
    - Key unset/empty → query_hmac None; log once; caller still records (§7 dominates).
    - ARB_HINT_READ_QUERY_RAW=1 → store the capped query text; otherwise None.
    """
    global _query_key_missing_logged
    key = os.environ.get("ARB_HINT_READ_QUERY_KEY") or ""
    if key:
        query_hmac: str | None = hmac.new(
            key.encode("utf-8"),
            query_text.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    else:
        query_hmac = None
        if not _query_key_missing_logged:
            logger.warning(
                "ARB_HINT_READ_QUERY_KEY unset/empty; hint_read.query_hmac will be NULL"
            )
            _query_key_missing_logged = True
    query_text_to_store = (
        query_text if os.environ.get("ARB_HINT_READ_QUERY_RAW") == "1" else None
    )
    return query_hmac, query_text_to_store


def _record_local_read(
    conn,
    *,
    door,
    outcome,
    hit_count,
    query_text,
    query_truncated,
    k,
    hits=(),
):
    """Write one hint_read parent + hint_read_hit children on an autocommit connection.

    Reference: frozen guide §4 local tier (v5 lines ~486–514), with D-2 query columns
    (query_hmac + optional raw query_text) instead of the guide's single query_text.
    Wired into memory_search under a full try/except guard (S3b / G-01 / H-05).
    """
    if not getattr(conn, "autocommit", False):
        # COMMIT precondition failed: this connection cannot satisfy clause 1. Raise here,
        # not silently — memory_search's recording guard turns this into a logged miss
        # rather than a corrupted read.
        raise RuntimeError(
            f"local read recorder requires an autocommit connection; got "
            f"autocommit={getattr(conn, 'autocommit', None)!r}"
        )
    # K-04/J-12 (§11 Q8/H-11): `autocommit` is a PROXY for clause 1; the precondition it stands
    # in for is that the `conn.transaction()` block below takes the OUTER branch, and psycopg
    # decides that on the status, not the flag (`transaction.py`: `self._outer_transaction =
    # self.pgconn.transaction_status == IDLE`). An autocommit connection that is already INTRANS
    # makes the block a SAVEPOINT whose release COMMITs nothing — the recorder would report
    # success and write nothing durable. So assert the status the `conn.transaction()` comment
    # below already claims.
    #
    # FAIL CLOSED on an unreadable status. `getattr` chain because the `conn_factory` seam
    # admits non-psycopg objects, but an absent `.info` yields None and None is NOT IDLE —
    # "the precondition is unknown" must not be treated as "the precondition holds", or the
    # guard reintroduces the very assume-instead-of-assert defect it exists to remove. This
    # costs zero fixture changes: both `FakeConn`s (`test_read_tools.py:11-13`,
    # `test_read_tools_runtime.py:15-17`) define only `closed`, so they are rejected by the
    # FIRST guard and never reach here — which is why the spec's stated reason for declining
    # Q8 ("requires extending both FakeConn fixtures") does not hold.
    #
    # Deliberately NOT wrapped in try/except, unlike bus.py's status read (`bus.py:566-568`):
    # on this path a raising `.info` should propagate to `memory_search`'s recording guard,
    # which logs it as a miss and leaves the served read untouched (G-01 / H-05).
    status = getattr(getattr(conn, "info", None), "transaction_status", None)
    if status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(
            f"local read recorder requires a connection at transaction status IDLE "
            f"(so conn.transaction() COMMITs rather than opening a savepoint); got {status!r}"
        )
    query_hmac, query_text_to_store = _query_columns(query_text)
    read_id = uuid.uuid4()
    with conn.transaction():  # entry is IDLE on an autocommit conn -> outer branch, real COMMIT
        conn.execute(
            "INSERT INTO hint_read (read_id, door, outcome, query_hmac, query_text, "
            "query_truncated, k, hit_count, served_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())",
            (
                read_id,
                door,
                outcome,
                query_hmac,
                query_text_to_store,
                query_truncated,
                k,
                hit_count,
            ),
        )
        for rank, hit in enumerate(hits, start=1):
            inner = hit["hint"]  # H-02: metrics live under store.retrieve's "hint" key
            conn.execute(
                "INSERT INTO hint_read_hit (read_id, rank, hint_id, withheld, "
                "vector_distance, lexical_rank) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    read_id,
                    rank,
                    inner["id"],
                    hit["withheld"],
                    inner.get("vector_distance"),
                    inner.get("lexical_rank"),
                ),
            )


@dataclass
class LocalReadSettings:
    dsn: str
    search_max_query_chars: int = 2000
    search_rate_per_min: int = 30
    graph_rate_per_min: int = 30


class ReadMemoryTools:
    def __init__(self, settings: LocalReadSettings, *, conn_factory=None, embed=None):
        self.settings = settings
        self.conn_factory = conn_factory
        self._uses_default_embed = embed is None
        self.embed = embed or default_embed
        self._search_hits: list[float] = []
        self._graph_hits: list[float] = []
        self._conn_obj = None
        # G-08 / H-08: per-rejection-class 60s bound on error receipts
        self._last_rejection_receipt_at: dict[str, float] = {}

    def _conn(self):
        if self._conn_obj is None or getattr(self._conn_obj, "closed", False):
            self._conn_obj = (
                self.conn_factory()
                if self.conn_factory
                else psycopg.connect(self.settings.dsn, autocommit=True)
            )
        return self._conn_obj

    def close(self) -> None:
        if self._conn_obj is not None and not getattr(self._conn_obj, "closed", False):
            self._conn_obj.close()

    def _check_search_allowed(self) -> None:
        now = time.monotonic()
        window_start = now - 60.0
        self._search_hits = [stamp for stamp in self._search_hits if stamp >= window_start]
        if len(self._search_hits) >= self.settings.search_rate_per_min:
            raise SearchRateLimitExceeded("search rate limit exceeded")
        self._search_hits.append(now)

    def _check_graph_allowed(self) -> None:
        now = time.monotonic()
        window_start = now - 60.0
        self._graph_hits = [stamp for stamp in self._graph_hits if stamp >= window_start]
        if len(self._graph_hits) >= self.settings.graph_rate_per_min:
            raise ValueError("graph rate limit exceeded")
        self._graph_hits.append(now)

    async def memory_search(self, query: str, k: int = 8) -> list[dict]:
        # Cap once for recording. Over-cap queries are still recorded (truncated) even
        # though the read itself is rejected (frozen guide §4 / G-12).
        query_text, truncated = _cap(query, self.settings.search_max_query_chars)
        rejection_class: str | None = None
        try:
            rejection_class = "query_too_long"
            if len(query) > self.settings.search_max_query_chars:
                raise ValueError("query too long")
            rejection_class = "missing_api_key"
            if self._uses_default_embed and not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("memory_search unavailable: OPENAI_API_KEY is not set")
            rejection_class = "rate_limited"
            self._check_search_allowed()  # raises SearchRateLimitExceeded, not bare ValueError
            rejection_class = None  # every guard passed; store.retrieve failures are unbounded
            hits = store.retrieve(self._conn(), query, k=k, embed=self.embed)
        except Exception:
            # Entire recording decision inside one guard (H-05 / G-01): should_record,
            # the call, and the timestamp write. Bare raise preserves the original
            # exception type/message/__cause__/__context__.
            try:
                if rejection_class is not None:
                    last = self._last_rejection_receipt_at.get(rejection_class)
                    should_record = last is None or (time.monotonic() - last) >= 60.0
                else:
                    should_record = True
                if should_record:
                    _record_local_read(
                        self._conn(),
                        door="local",
                        outcome="error",
                        hit_count=0,
                        query_text=query_text,
                        query_truncated=truncated,
                        k=k,
                    )
                    if rejection_class is not None:
                        self._last_rejection_receipt_at[rejection_class] = time.monotonic()
            except Exception as rec_exc:
                # Log without a stack: logger.exception writes "Traceback" to stderr,
                # which breaks the local-MCP stdio contract
                # (test_stdio_search_error_is_structured_without_traceback). Guide
                # names logger.exception; see ERRATA for the deliberate swap.
                logger.error(
                    "local read receipt failed (rejected/errored read unaffected): %s",
                    rec_exc,
                )
            raise
        try:
            _record_local_read(
                self._conn(),
                door="local",
                outcome="ok",
                hit_count=len(hits),
                query_text=query_text,
                query_truncated=truncated,
                k=k,
                hits=hits,
            )
        except Exception as rec_exc:
            logger.error(
                "local read receipt failed (served read unaffected): %s",
                rec_exc,
            )
        return [_json_safe_search_hit(hit) for hit in hits]

    async def memory_related(
        self, artefact_id: str, version: int | None = None, k: int = 5, threshold: float = 0.35
    ) -> list[dict]:
        """Artefacts similar to `artefact_id` (pgvector min-pairwise hint distance),
        nearest-first. Default reads the latest version's live hints; passing an explicit
        `version` switches to the as-written view of that version's hints (including
        hints retired by later versions)."""
        graph.validate_related_params(k, threshold)
        conn = self._conn()
        if not graph.artefact_exists(conn, artefact_id, version):
            raise ValueError("artefact not found")
        self._check_graph_allowed()
        rows = graph.related_artefacts(
            conn, artefact_id, version, k=k, threshold=threshold,
            subject_hints=graph.subject_mode(version),
        )
        return [
            {"artefact_id": aid, "version": ver, "distance": dist}
            for aid, ver, dist in rows
        ]

    async def memory_references(self, artefact_id: str, version: int | None = None) -> dict:
        """Explicit citations, both directions: `references` = ids this artefact's body
        cites (the given version's body; latest if omitted); `referenced_by` = latest
        artefacts citing this id. `version` affects ONLY the outgoing direction —
        backlinks always scan the latest corpus."""
        conn = self._conn()
        if not graph.artefact_exists(conn, artefact_id, version):
            raise ValueError("artefact not found")
        self._check_graph_allowed()
        resolved = version if version is not None else graph.latest_version(conn, artefact_id)
        return graph.references(conn, artefact_id, resolved)

    async def memory_get(self, artefact_id: str, version: int) -> dict | None:
        """Fetch a stored document (artefact) verbatim by its `artefact_id` and `version`.
        `content_hash` is not a digest of `content` alone — it is
        `sha256(b"arbmem:artefact:v1\\0" + content_mime + b"\\0" + kind + b"\\0" + payload)` where `kind` is
        `b"text"` with `payload` = the UTF-8 bytes of `content`, or `b"binary"` with `payload` = the raw bytes,
        and each `\\0` is one NUL byte."""
        return _json_safe_artefact(store.fetch_artefact(self._conn(), artefact_id, version))

    async def memory_recent(self, limit: int = 10) -> list[dict]:
        return [
            _json_safe_artefact(row)
            for row in store.recent_artefacts(self._conn(), min(max(1, limit), 100))
        ]


def _json_safe_artefact(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    content_bytes = out.get("content_bytes")
    if content_bytes is not None:
        out["content_bytes_b64"] = base64.b64encode(content_bytes).decode("ascii")
        out["content_bytes"] = None
    created_at = out.get("created_at")
    if isinstance(created_at, datetime):
        out["created_at"] = created_at.isoformat()
    return out


def _json_safe_search_hit(hit: dict) -> dict:
    out = dict(hit)
    if "artefact" in out:
        out["artefact"] = _json_safe_artefact(out["artefact"])
    hint = out.get("hint")
    if isinstance(hint, dict):
        out["hint"] = _json_safe_datetimes(hint)
    return out


def _json_safe_datetimes(row: dict) -> dict:
    out = dict(row)
    for key, value in out.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out
