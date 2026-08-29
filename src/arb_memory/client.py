from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from arb_memory import bus


def _redis_client():
    from arb_memory import redis_conn

    return redis_conn.from_url(os.environ["ARB_MEMORY_REDIS_URL"])


def write(
    text,
    *,
    artefact_id=None,
    artefact_content=None,
    repo_pointer=None,
    source="seat",
    author="unknown",
):
    artefact = None
    if artefact_id is not None or artefact_content is not None:
        artefact = {"artefact_id": artefact_id, "content": artefact_content}

    hint = {"text": text}
    if repo_pointer is not None:
        hint["repo_pointer"] = repo_pointer

    return bus.memory_write(
        _redis_client(),
        artefact=artefact,
        hints=[hint],
        source=source,
        author=author,
    )


def query(q, *, k=8, timeout_s=10, transport_only=False, repo_root="."):
    hits = bus.memory_query(_redis_client(), q, k=k, timeout_s=timeout_s)
    if transport_only:
        return hits
    if hits is None:
        return _grep_fallback(q, repo_root)
    return hits


def _grep_fallback(q, repo_root):
    root = Path(repo_root)
    matches = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            if q in line:
                matches.append(
                    {
                        "source": "grep-fallback",
                        "path": str(path.relative_to(root)),
                        "line": line_no,
                        "text": line,
                    }
                )
    return matches


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m arb_memory.client")
    subparsers = parser.add_subparsers(dest="command", required=True)

    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("--text", required=True)
    write_parser.add_argument("--artefact-id")
    write_parser.add_argument("--artefact-content")
    write_parser.add_argument("--repo-pointer")
    write_parser.add_argument("--source", default="seat")
    write_parser.add_argument("--author", default="unknown")

    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("--q", required=True)
    query_parser.add_argument("--k", type=int, default=8)
    query_parser.add_argument("--timeout-s", type=float, default=10)
    query_parser.add_argument("--transport-only", action="store_true")
    query_parser.add_argument("--repo-root", default=".")

    args = parser.parse_args(argv)
    if args.command == "write":
        ulid = write(
            args.text,
            artefact_id=args.artefact_id,
            artefact_content=args.artefact_content,
            repo_pointer=args.repo_pointer,
            source=args.source,
            author=args.author,
        )
        print(ulid)
        return 0

    hits = query(
        args.q,
        k=args.k,
        timeout_s=args.timeout_s,
        transport_only=args.transport_only,
        repo_root=args.repo_root,
    )
    print(json.dumps(hits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
