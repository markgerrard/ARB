from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from .hash import artefact_hash, hint_hash


def rrf_fuse(vector_ranked: list[int], lexical_ranked: list[int], k_limit: int, rrf_k: int = 60) -> list[int]:
    scores: dict[int, float] = {}
    for rank, hint_id in enumerate(vector_ranked):
        scores[hint_id] = scores.get(hint_id, 0.0) + 1.0 / (rrf_k + rank + 1)
    for rank, hint_id in enumerate(lexical_ranked):
        scores[hint_id] = scores.get(hint_id, 0.0) + 1.0 / (rrf_k + rank + 1)
    return sorted(scores, key=lambda hint_id: (-scores[hint_id], hint_id))[:k_limit]


def _register_vector(conn):
    register_vector(conn)


def upsert_artefact(
    conn,
    artefact_id,
    *,
    content=None,
    content_bytes=None,
    mime="text/plain",
    repo_pointer=None,
    source="seat",
    author="unknown",
    expected_version: int | None = None,
) -> tuple[str, int, str]:
    """Persist one artefact version.

    When ``expected_version`` is set, compare against the head *before* dedup:
    refuse with ``refused_version_mismatch`` if the head moved (or is absent when
    the caller expected a version >= 1). A refusal's ``version`` is observational
    head state (``0`` = no versions exist), never a version this request created.
    ``expected_version=0`` is create-only: store only if no version exists yet.
    Absent ``expected_version`` preserves today's unguarded behaviour byte for byte.
    """
    _register_vector(conn)
    content_hash = artefact_hash(content, content_bytes, mime)
    # Dedup against the LATEST version only: content that matches a merely-historical
    # version must create a new version, or a reverted artefact (A -> B -> A) silently
    # strands every latest-version consumer (the vault export) on stale content forever.
    row = conn.execute(
        "SELECT version, content_hash FROM artefacts"
        " WHERE artefact_id = %s ORDER BY version DESC LIMIT 1",
        (artefact_id,),
    ).fetchone()
    # Optimistic concurrency: compare first, then dedup (design §4). A moved base
    # must refuse regardless of whether the winner's content happens to match ours.
    if expected_version is not None:
        if row is None:
            # Observational head version 0 = no versions exist.
            if expected_version != 0:
                return artefact_id, 0, "refused_version_mismatch"
        elif row[0] != expected_version:
            return artefact_id, row[0], "refused_version_mismatch"
    if row is not None and row[1] == content_hash:
        return artefact_id, row[0], "deduped"

    # Guarded path: allocate expected+1 only (design §4 PK backstop). A concurrent
    # writer that advanced the head since the compare then collides on PRIMARY KEY
    # (23505) → poison redelivery → refusal. max() here would advance past the
    # winner and commit a clobber while the tool layer reports guard_not_live.
    # Unguarded path keeps today's max() allocation byte-identical.
    if expected_version is not None:
        version = expected_version + 1
    else:
        row = conn.execute(
            "SELECT COALESCE(max(version), 0) + 1 FROM artefacts WHERE artefact_id = %s",
            (artefact_id,),
        ).fetchone()
        version = row[0]
    conn.execute(
        """
        INSERT INTO artefacts (
            artefact_id, version, content, content_bytes, content_mime, repo_pointer,
            content_hash, source, author
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            artefact_id,
            version,
            content,
            content_bytes,
            mime,
            repo_pointer,
            content_hash,
            source,
            author,
        ),
    )
    return artefact_id, version, "stored"


def upsert_hint(
    conn,
    text,
    embedding,
    *,
    artefact_id=None,
    artefact_version=None,
    repo_pointer=None,
    metadata=None,
    source="seat",
    author="unknown",
) -> tuple[int, bool]:
    _register_vector(conn)
    content_hash = hint_hash(text, artefact_id, artefact_version, repo_pointer)
    row = conn.execute(
        """
        INSERT INTO hints (
            text, embedding, metadata, source, author,
            artefact_id, artefact_version, repo_pointer, content_hash
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (content_hash) DO NOTHING
        RETURNING id
        """,
        (
            text,
            embedding,
            Jsonb(metadata or {}),
            source,
            author,
            artefact_id,
            artefact_version,
            repo_pointer,
            content_hash,
        ),
    ).fetchone()
    if row is not None:
        return row[0], True

    row = conn.execute("SELECT id FROM hints WHERE content_hash = %s", (content_hash,)).fetchone()
    return row[0], False


def write_artefact_and_hints(conn, *, artefact=None, hints=()) -> dict:
    _register_vector(conn)
    with conn.transaction():
        artefact_ref = None
        if artefact is not None:
            artefact_ref = upsert_artefact(
                conn,
                artefact["artefact_id"],
                content=artefact.get("content"),
                content_bytes=artefact.get("content_bytes"),
                mime=artefact.get("mime", "text/plain"),
                repo_pointer=artefact.get("repo_pointer"),
                source=artefact.get("source", "seat"),
                author=artefact.get("author", "unknown"),
                expected_version=artefact.get("expected_version"),
            )
            # Refusal short-circuit: return before upsert_hint and before the
            # artefact_index retirement UPDATE. On refusal, artefact_ref[1] is
            # observational head state (not a version this request created); the
            # mechanism establishes only that neither hint writes nor retirement
            # run for this call.
            if artefact_ref[2] == "refused_version_mismatch":
                return {
                    "artefact_outcome": "refused_version_mismatch",
                    "artefact_id": artefact_ref[0],
                    "version": artefact_ref[1],
                    "hints_stored": 0,
                }

        hints_stored = 0
        replaced_index_for = set()
        for hint in hints:
            artefact_id = artefact_ref[0] if artefact_ref else hint.get("artefact_id")
            artefact_version = artefact_ref[1] if artefact_ref else hint.get("artefact_version")
            hint_id, inserted = upsert_hint(
                conn,
                hint["text"],
                hint["embedding"],
                artefact_id=artefact_id,
                artefact_version=artefact_version,
                repo_pointer=hint.get("repo_pointer"),
                metadata=hint.get("metadata"),
                source=hint.get("source", "seat"),
                author=hint.get("author", "unknown"),
            )
            hints_stored += inserted
            if (hint.get("metadata") or {}).get("kind") == "artefact_index" and artefact_id:
                replaced_index_for.add((artefact_id, artefact_version))

        # A new artefact_index hint supersedes its predecessors: retire (soft-delete) older
        # index hints for the same artefact so search never keeps serving a stale version
        # (retrieve() pins the artefact at the hint's version, and the lexical tie-break by
        # ascending hint id made the OLDEST hint win on near-identical text — live-observed
        # as a refreshed wiki page whose v1 kept outranking v2 indefinitely). Scoped to
        # kind=artefact_index: pinned evidence/audit hints keep their faithful history, and
        # a version write with NO replacement index hint retires nothing.
        for artefact_id, artefact_version in replaced_index_for:
            if artefact_version is None:
                continue
            conn.execute(
                """
                UPDATE hints SET deleted_at = now()
                WHERE artefact_id = %s AND artefact_version < %s AND deleted_at IS NULL
                  AND metadata->>'kind' = 'artefact_index'
                """,
                (artefact_id, artefact_version),
            )
        return {
            "artefact_outcome": artefact_ref[2] if artefact_ref else "none",
            "artefact_id": artefact_ref[0] if artefact_ref else None,
            "version": artefact_ref[1] if artefact_ref else None,
            "hints_stored": hints_stored,
        }


def search_hints(conn, query_text, k=8, *, embed) -> list[dict]:
    _register_vector(conn)
    query_embedding = embed(query_text)
    fetch_k = max(50, k * 5)

    vector_rows = conn.execute(
        """
        SELECT id
        FROM hints
        WHERE deleted_at IS NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (query_embedding, fetch_k),
    ).fetchall()
    vector_ids = [row[0] for row in vector_rows]

    lexical_rows = conn.execute(
        """
        WITH q AS (SELECT plainto_tsquery('simple', %s) AS tsq)
        SELECT h.id
        FROM hints h, q
        WHERE h.deleted_at IS NULL
          AND h.search_tsv @@ q.tsq
        ORDER BY ts_rank(h.search_tsv, q.tsq) DESC, h.id
        LIMIT %s
        """,
        (query_text, fetch_k),
    ).fetchall()
    lexical_ids = [row[0] for row in lexical_rows]

    fused_ids = rrf_fuse(vector_ids, lexical_ids, k_limit=k)
    if not fused_ids:
        return []

    rows = conn.execute(
        """
        WITH q AS (
            SELECT %s::vector AS embedding, plainto_tsquery('simple', %s) AS tsq
        )
        SELECT
            h.id, h.created_at, h.text, h.metadata, h.source, h.author,
            h.artefact_id, h.artefact_version, h.repo_pointer, h.content_hash,
            h.deleted_at,
            h.embedding <=> q.embedding AS vector_distance,
            ts_rank(h.search_tsv, q.tsq) AS lexical_rank
        FROM hints h, q
        WHERE h.deleted_at IS NULL
          AND h.id = ANY(%s)
        """,
        (query_embedding, query_text, fused_ids),
    ).fetchall()
    cols = (
        "id",
        "created_at",
        "text",
        "metadata",
        "source",
        "author",
        "artefact_id",
        "artefact_version",
        "repo_pointer",
        "content_hash",
        "deleted_at",
        "vector_distance",
        "lexical_rank",
    )
    by_id = {row[0]: dict(zip(cols, row)) for row in rows}
    return [by_id[hint_id] for hint_id in fused_ids if hint_id in by_id]


def fetch_artefact(conn, artefact_id, version) -> dict | None:
    _register_vector(conn)
    row = conn.execute(
        """
        SELECT artefact_id, version, content, content_bytes, content_mime,
               repo_pointer, content_hash, source, author, created_at
        FROM artefacts
        WHERE artefact_id = %s AND version = %s
        """,
        (artefact_id, version),
    ).fetchone()
    if row is None:
        return None
    cols = (
        "artefact_id",
        "version",
        "content",
        "content_bytes",
        "content_mime",
        "repo_pointer",
        "content_hash",
        "source",
        "author",
        "created_at",
    )
    return dict(zip(cols, row))


def recent_artefacts(conn, limit=10) -> list[dict]:
    rows = conn.execute(
        """
        SELECT artefact_id, version, content, content_bytes, content_mime,
               repo_pointer, content_hash, source, author, created_at
        FROM artefacts
        ORDER BY created_at DESC, artefact_id, version DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    cols = (
        "artefact_id",
        "version",
        "content",
        "content_bytes",
        "content_mime",
        "repo_pointer",
        "content_hash",
        "source",
        "author",
        "created_at",
    )
    return [dict(zip(cols, row)) for row in rows]


def retrieve(conn, query_text, k=8, *, embed) -> list[dict]:
    out = []
    for hint in search_hints(conn, query_text, k=k, embed=embed):
        artefact = None
        repo_pointer = hint["repo_pointer"]
        # learn proposals carry EXTERNAL, pre-eval text: their hint is a metadata summary by
        # design, and the raw body must not be ambiently re-delivered to every searching
        # client via the artefact attachment — explicit memory_get only (2026-07-07, the
        # injection-screening eval's surviving finding).
        withhold = bool((hint.get("metadata") or {}).get("learn_proposal"))
        if hint["artefact_id"] is not None and not withhold:
            artefact = fetch_artefact(conn, hint["artefact_id"], hint["artefact_version"])
        # D-3 / F-09: withheld is part of the retrieve element (outer level), not inferred
        # later by consumers. Same value already computed for artefact attachment above.
        out.append(
            {
                "hint": hint,
                "artefact": artefact,
                "repo_pointer": repo_pointer,
                "withheld": withhold,
            }
        )
    return out
