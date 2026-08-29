"""Shared graph-edge logic for ARB Memory.

Single implementation consumed by BOTH the vault exporter (vault_export.py) and the
MCP read tools on both doors (mcp/read_tools.py local, mcp/tools.py connector).
E1 = explicit textual references; E2 = pgvector min-pairwise similarity.
"""
from __future__ import annotations

import re


ART_ID_RE = re.compile(r"art-[0-9a-f]{16}")
REF_LEAD = r"(?<![A-Za-z0-9._/-])"
REF_TRAIL = r"(?![A-Za-z0-9_/-]|\.[A-Za-z0-9._/-])"


def subject_mode(version: int | None) -> str:
    """'live' vs 'as_written' — a pure function of caller intent, NEVER of DB state."""
    return "live" if version is None else "as_written"


def validate_related_params(k: int, threshold: float) -> None:
    """Shared k/threshold range checks — the ONE implementation both doors call
    (spec fold 14: validation semantics must not drift between doors)."""
    if not 1 <= k <= 20:
        raise ValueError("k must be between 1 and 20")
    if not 0.0 < threshold <= 2.0:
        raise ValueError("threshold must be in (0, 2]")


def reference_targets(body: str, artefact_id: str, export_ids: set[str]) -> list[str]:
    targets = []
    for candidate in export_ids:
        if candidate == artefact_id or candidate not in body:
            continue
        escaped = re.escape(candidate)
        patterns = [f"`{escaped}`"]
        if ART_ID_RE.fullmatch(candidate) or "_" in candidate:
            patterns.append(REF_LEAD + escaped + REF_TRAIL)
        if any(re.search(pattern, body) for pattern in patterns):
            targets.append(candidate)
    return sorted(targets)


def related_artefacts(
    conn,
    artefact_id: str,
    version: int | None,
    *,
    k: int,
    threshold: float,
    subject_hints: str = "live",
) -> list[tuple[str, int, float]]:
    """E2 edges: (artefact_id, latest_version, distance), nearest-first, id tie-break.

    subject_hints is set from caller intent ONLY (subject_mode()); 'live' keeps
    deleted_at IS NULL on the subject and resolves version=None to latest INSIDE this
    statement (single snapshot). 'as_written' filters only on (artefact_id, version)
    and requires an explicit version. The corpus side is always live-latest-only.
    """
    if subject_hints not in ("live", "as_written"):
        raise ValueError(f"invalid subject_hints {subject_hints!r}")
    if subject_hints == "as_written" and version is None:
        raise ValueError("as_written mode requires an explicit version")
    subject_predicate = (
        "AND deleted_at IS NULL" if subject_hints == "live" else ""
    )
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT DISTINCT ON (artefact_id) artefact_id, version
            FROM artefacts ORDER BY artefact_id, version DESC
        ),
        mine AS (
            SELECT embedding FROM hints
            WHERE artefact_id = %(aid)s
              AND artefact_version = COALESCE(
                  %(version)s,
                  (SELECT version FROM latest WHERE artefact_id = %(aid)s)
              )
              {subject_predicate}
        ),
        others AS (
            SELECT h.artefact_id AS aid, l.version AS version, h.embedding
            FROM hints h JOIN latest l
              ON h.artefact_id = l.artefact_id AND h.artefact_version = l.version
            WHERE h.deleted_at IS NULL AND h.artefact_id <> %(aid)s
        )
        SELECT o.aid, o.version, MIN(o.embedding <=> m.embedding) AS dist
        FROM others o CROSS JOIN mine m
        GROUP BY o.aid, o.version
        HAVING MIN(o.embedding <=> m.embedding) <= %(threshold)s
        ORDER BY dist ASC, o.aid ASC
        LIMIT %(k)s
        """,
        {"aid": artefact_id, "version": version, "threshold": threshold, "k": k},
    ).fetchall()
    return [(row[0], int(row[1]), float(row[2])) for row in rows]


def artefact_exists(conn, artefact_id: str, version: int | None = None) -> bool:
    """Fail-loud probe ONLY. Never resolves None; gates raise-vs-proceed, never mode."""
    if version is None:
        row = conn.execute(
            "SELECT 1 FROM artefacts WHERE artefact_id = %s LIMIT 1", (artefact_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM artefacts WHERE artefact_id = %s AND version = %s",
            (artefact_id, version),
        ).fetchone()
    return row is not None


def latest_version(conn, artefact_id: str) -> int | None:
    """Used by memory_references ONLY (see spec: memory_related passes None verbatim)."""
    row = conn.execute(
        "SELECT MAX(version) FROM artefacts WHERE artefact_id = %s", (artefact_id,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def references(conn, artefact_id: str, version: int) -> dict:
    """E1 edges, both directions. version is concrete here (wrapper resolves None).

    Backlinks: latest bodies resolved FIRST (DISTINCT ON), then exact-substring
    strpos prefilter (no pattern semantics -> pure over-approximation), then the
    authoritative reference_targets regex confirmation in Python.
    """
    row = conn.execute(
        "SELECT content FROM artefacts WHERE artefact_id = %s AND version = %s",
        (artefact_id, version),
    ).fetchone()
    body = (row[0] if row else None) or ""

    latest_ids = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT artefact_id FROM artefacts"
        ).fetchall()
    }
    outgoing = reference_targets(body, artefact_id, latest_ids)

    candidates = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (artefact_id) artefact_id, content
            FROM artefacts ORDER BY artefact_id, version DESC
        )
        SELECT artefact_id, content FROM latest
        WHERE artefact_id <> %(aid)s
          AND content IS NOT NULL
          AND strpos(content, %(aid)s) > 0
        """,
        {"aid": artefact_id},
    ).fetchall()
    referenced_by = sorted(
        cand_id
        for cand_id, cand_body in candidates
        if artefact_id in reference_targets(cand_body, cand_id, {artefact_id})
    )
    return {"references": outgoing, "referenced_by": referenced_by}
