from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

import psycopg


ART_ID_RE = re.compile(r"(?<![A-Za-z0-9_-])art-[0-9a-f]{16}(?![A-Za-z0-9_-])")
WIKI_TICK_RE = re.compile(r"`(wiki-[A-Za-z0-9._-]+)`")
SNAPSHOT_NAME = "graph.json"
NO_SNAPSHOT_ERROR = "no snapshot yet — run arb-journey-export"
ASCII_REF_BOUNDARY_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _tags(metadata) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    tags = metadata.get("tags") or []
    if not isinstance(tags, list):
        return []
    return sorted({str(tag) for tag in tags if tag})


def _is_ascii_ref_char(value: str) -> bool:
    return value in ASCII_REF_BOUNDARY_CHARS


def extract_refs(content: str, artefact_id: str, known_ids: set[str]) -> tuple[set[str], int]:
    if not isinstance(content, str):
        return set(), 0
    found = ART_ID_RE.findall(content)
    found.extend(match.group(1) for match in WIKI_TICK_RE.finditer(content))
    resolved = {ref for ref in found if ref in known_ids and ref != artefact_id}
    dangling = sum(1 for ref in found if ref not in known_ids)
    return resolved, dangling


def node_title(content, artefact_id: str) -> str:
    if not isinstance(content, str):
        return artefact_id
    fallback = None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return stripped[2:].strip()[:120] or artefact_id
        if fallback is None:
            fallback = stripped
    return (fallback or artefact_id)[:120]


def node_kind(artefact_id: str, mime: str | None) -> str:
    if artefact_id.startswith("wiki-") and artefact_id.endswith("-manifest"):
        return "wiki-manifest"
    if artefact_id.startswith("wiki-"):
        return "wiki-page"
    if artefact_id.startswith("learn-"):
        return "learn-proposal"
    if artefact_id.startswith("howto-"):
        return "howto"
    return "note"


def _latest_artefacts(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT artefact_id, version, content, content_mime, source, author, created_at
        FROM (
            SELECT a.*, row_number() OVER (
                PARTITION BY artefact_id ORDER BY version DESC
            ) AS rn
            FROM artefacts a
        ) latest
        WHERE rn = 1
        ORDER BY artefact_id
        """
    ).fetchall()
    cols = ("artefact_id", "version", "content", "content_mime", "source", "author", "created_at")
    return [dict(zip(cols, row)) for row in rows]


def _anchored_hint_stats(conn) -> tuple[dict[tuple[str, int], list[str]], dict[tuple[str, int], int]]:
    rows = conn.execute(
        """
        SELECT artefact_id, artefact_version, metadata
        FROM hints
        WHERE deleted_at IS NULL AND artefact_id IS NOT NULL
        """
    ).fetchall()
    tags_by_ref: dict[tuple[str, int], set[str]] = {}
    counts: dict[tuple[str, int], int] = {}
    for artefact_id, version, metadata in rows:
        key = (artefact_id, version)
        counts[key] = counts.get(key, 0) + 1
        tags_by_ref.setdefault(key, set()).update(_tags(metadata))
    return {key: sorted(tags) for key, tags in tags_by_ref.items()}, counts


def _free_hints(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, created_at, metadata
        FROM hints
        WHERE deleted_at IS NULL AND artefact_id IS NULL
        ORDER BY id
        """
    ).fetchall()
    return [{"id": row[0], "created_at": _iso(row[1]), "tags": _tags(row[2])} for row in rows]


def build_snapshot(conn) -> dict:
    artefacts = _latest_artefacts(conn)
    known_ids = {row["artefact_id"] for row in artefacts}
    tags_by_ref, hint_counts = _anchored_hint_stats(conn)
    edges: set[tuple[str, str]] = set()
    dangling = 0
    node_refs: dict[str, set[str]] = {}
    for row in artefacts:
        refs, row_dangling = extract_refs(row["content"], row["artefact_id"], known_ids)
        dangling += row_dangling
        node_refs[row["artefact_id"]] = refs
        edges.update((row["artefact_id"], ref) for ref in refs)

    in_degree = {artefact_id: 0 for artefact_id in known_ids}
    out_degree = {artefact_id: 0 for artefact_id in known_ids}
    for src, dst in edges:
        out_degree[src] += 1
        in_degree[dst] += 1

    nodes = []
    for row in artefacts:
        ref = (row["artefact_id"], row["version"])
        nodes.append(
            {
                "id": row["artefact_id"],
                "title": node_title(row["content"], row["artefact_id"]),
                "tags": tags_by_ref.get(ref, []),
                "kind": node_kind(row["artefact_id"], row["content_mime"]),
                "source": row["source"],
                "author": row["author"],
                "created_at": _iso(row["created_at"]),
                "version": row["version"],
                "in_degree": in_degree[row["artefact_id"]],
                "out_degree": out_degree[row["artefact_id"]],
                "anchored_hint_count": hint_counts.get(ref, 0),
            }
        )

    free_hints = _free_hints(conn)
    generated_at = datetime.now(timezone.utc).isoformat()
    isolated = sum(1 for artefact_id in known_ids if in_degree[artefact_id] == 0 and out_degree[artefact_id] == 0)
    return {
        "generated_at": generated_at,
        "nodes": nodes,
        "edges": [list(edge) for edge in sorted(edges)],
        "free_hints": free_hints,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "dangling": dangling,
            "isolated": isolated,
            "free_hints": len(free_hints),
            "generated_at": generated_at,
        },
    }


@dataclass
class VerificationResult:
    ok: bool
    message: str = ""


def _verify_scan(content, artefact_id: str, known_ids: set[str]) -> tuple[set[str], int]:
    if not isinstance(content, str):
        return set(), 0
    found: list[str] = []
    for match in re.finditer(r"art-[0-9a-f]{16}", content):
        start, end = match.span()
        before = content[start - 1] if start > 0 else ""
        after = content[end] if end < len(content) else ""
        if before and _is_ascii_ref_char(before):
            continue
        if after and _is_ascii_ref_char(after):
            continue
        found.append(match.group(0))
    index = 0
    while True:
        start = content.find("`wiki-", index)
        if start == -1:
            break
        end = content.find("`", start + 1)
        if end == -1:
            break
        candidate = content[start + 1 : end]
        if re.fullmatch(r"wiki-[A-Za-z0-9._-]+", candidate):
            found.append(candidate)
        index = end + 1
    return {ref for ref in found if ref in known_ids and ref != artefact_id}, sum(1 for ref in found if ref not in known_ids)


def _verify_refs(conn, snapshot: dict) -> VerificationResult:
    artefacts = _latest_artefacts(conn)
    known_ids = {row["artefact_id"] for row in artefacts}
    expected_edges: set[tuple[str, str]] = set()
    dangling = 0
    for row in artefacts:
        refs, count = _verify_scan(row["content"], row["artefact_id"], known_ids)
        dangling += count
        expected_edges.update((row["artefact_id"], ref) for ref in refs)
    observed_edges = {tuple(edge) for edge in snapshot.get("edges", [])}
    if observed_edges != expected_edges:
        return VerificationResult(False, f"edge mismatch expected={sorted(expected_edges)} observed={sorted(observed_edges)}")
    observed_dangling = (snapshot.get("counts") or {}).get("dangling")
    if observed_dangling != dangling:
        return VerificationResult(False, f"dangling mismatch expected={dangling} observed={observed_dangling}")
    return VerificationResult(True)


def write_snapshot(snapshot: dict, snapshot_dir: str | Path) -> Path:
    target_dir = Path(snapshot_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = target_dir / f"{SNAPSHOT_NAME}.tmp"
    final_path = target_dir / SNAPSHOT_NAME
    tmp_path.write_text(json.dumps(snapshot, default=_json_default, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, final_path)
    return final_path


def _latest_write_at(conn):
    row = conn.execute(
        """
        SELECT max(ts)
        FROM (
            SELECT max(created_at) AS ts FROM artefacts
            UNION ALL
            SELECT max(created_at) AS ts FROM hints
            UNION ALL
            SELECT max(deleted_at) AS ts FROM hints
        ) latest
        WHERE ts IS NOT NULL
        """
    ).fetchone()
    return row[0] if row else None


def _parse_generated_at(snapshot: dict):
    value = snapshot.get("generated_at") or (snapshot.get("counts") or {}).get("generated_at")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_snapshot(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _snapshot_dir(raw: str | None) -> str:
    return raw or os.environ.get("ARB_JOURNEY_SNAPSHOT_DIR") or "/journey-snapshot"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", nargs="?", const="", default=None)
    parser.add_argument("--snapshot-dir")
    args = parser.parse_args(argv)
    dsn = os.environ.get("ARB_MEMORY_DSN")
    if not dsn:
        print("ARB_MEMORY_DSN is required", file=sys.stderr)
        return 2

    snapshot_dir = _snapshot_dir(args.snapshot_dir)
    verify_path = args.verify_only
    if verify_path == "":
        verify_path = str(Path(snapshot_dir) / SNAPSHOT_NAME)

    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            if verify_path is not None:
                snapshot = _load_snapshot(verify_path)
                latest_write = _latest_write_at(conn)
                generated_at = _parse_generated_at(snapshot)
                if latest_write is not None and generated_at is not None and generated_at < latest_write:
                    print("snapshot is stale; latest store write is newer", file=sys.stderr)
                    return 0
                result = _verify_refs(conn, snapshot)
                if not result.ok:
                    print(f"verification failed: {result.message}", file=sys.stderr)
                    return 1
                return 0

            snapshot = build_snapshot(conn)
            result = _verify_refs(conn, snapshot)
            if not result.ok:
                print(f"verification failed: {result.message}", file=sys.stderr)
                return 1
            write_snapshot(snapshot, snapshot_dir)
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
