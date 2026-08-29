"""CLI / library entry for ordinary-request enqueue via the dispatch authority.

Bash, Go, ctl, and warm callers invoke this instead of constructing envelopes or
calling Redis ``rpush`` for ordinary ``request`` / ``worktree_run``. Lifecycle
ops (``worktree_arm`` / ``worktree_release``) stay on their narrow paths.

Non-FABA callers must supply a pre-minted ref + target-bound receipt + exact
original brief bytes. FABA may supply a draft (requires ``ARB_MEMORY_REDIS_URL``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from .dispatch_authority import (
    FABA_IDENTITY,
    NON_FABA_IDENTITIES,
    DispatchAuthorityError,
    DraftSource,
    HarnessPublishReceipt,
    PreMintedSource,
    filter_publish_env,
    publish_and_enqueue,
)
from .redis_io import RedisCli, RedisConfig, read_env_file


def _config_from_env(
    env_file: Path | None,
    env: Mapping[str, str],
) -> RedisConfig:
    if env_file is not None:
        file_env = read_env_file(env_file)
        merged = {**file_env, **{k: v for k, v in env.items() if v}}
    else:
        merged = dict(env)
    return RedisConfig(
        host=merged["AGENT_REDIS_HOST"],
        port=merged["AGENT_REDIS_PORT"],
        db=merged["AGENT_REDIS_DB"],
        prefix=merged["AGENT_REDIS_PREFIX"],
        password=merged.get("AGENT_REDIS_PASSWORD") or None,
        user=merged.get("AGENT_REDIS_USER") or None,
        tls=str(merged.get("AGENT_REDIS_TLS", "0")).lower() in {"1", "true", "yes"},
    )


def wrap_instructions_as_brief(instructions: str, *, vantage: str, title: str = "Dispatch brief") -> str:
    """Wrap free-form instructions into a minimal valid dispatch brief.

    Used by in-process callers (seat e2e, wiki, learn) that historically passed
    raw task strings. The assumptions block is explicit-empty (no preconditions
    claimed); real assumptions remain a review residual.
    """
    assumptions = json.dumps({"items": []}, indent=2)
    body = instructions.strip()
    return (
        f"# {title}\n\n"
        f"## Assumptions\n```json\n{assumptions}\n```\n\n"
        f"## Instructions\n\n{body}\n"
    )


def enqueue_pre_minted(
    *,
    target_agent_id: str,
    artefact_id: str,
    version: int,
    receipt: HarnessPublishReceipt | Mapping[str, Any],
    brief_text: str,
    caller_identity: str,
    sender: str,
    branch: str,
    run_id: str,
    redis_cli: RedisCli,
    operation: str = "request",
    worktree_lease: str | None = None,
    worktree: dict[str, Any] | None = None,
    extra_payload: dict[str, Any] | None = None,
    lane: str | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Non-FABA ordinary enqueue. Never accepts a draft or publish credential."""
    if isinstance(receipt, HarnessPublishReceipt):
        receipt_obj = receipt
    else:
        receipt_obj = HarnessPublishReceipt.from_dict(receipt)
    source = PreMintedSource(
        artefact_id=artefact_id,
        version=version,
        receipt=receipt_obj,
        legacy_brief_bytes=brief_text.encode("utf-8"),
    )
    return publish_and_enqueue(
        source,
        target_agent_id=target_agent_id,
        operation=operation,
        run_id=run_id,
        worktree_lease=worktree_lease,
        caller_identity=caller_identity,
        redis_cli=redis_cli,
        sender=sender,
        branch=branch,
        worktree=worktree,
        extra_payload=extra_payload,
        lane=lane,
        dry_run=dry_run,
        env=env if env is not None else filter_publish_env(os.environ),
        request_id=request_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ordinary-request enqueue via dispatch_authority (Slice 1d-iv)."
    )
    parser.add_argument(
        "--caller-identity",
        required=True,
        help=f"closed vocabulary: {FABA_IDENTITY!r} or one of {sorted(NON_FABA_IDENTITIES)}",
    )
    parser.add_argument("--target-agent-id", required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--operation",
        default="request",
        choices=("request", "worktree_run"),
        help="ordinary-path operation vocabulary (matches agent-dispatch/go-client)",
    )
    parser.add_argument("--worktree-lease", default=None)
    parser.add_argument("--lane", default=None)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--request-id", default=None)
    parser.add_argument(
        "--extra-payload-json",
        default=None,
        help=(
            "JSON object merged into the request payload; typed keys "
            "task/operation/worktree/worktree_lease/lane are reserved and ignored"
        ),
    )
    parser.add_argument(
        "--worktree-json",
        default=None,
        help="JSON worktree object for ordinary worktree-spec dispatches",
    )

    # Pre-minted (non-FABA) source
    parser.add_argument("--artefact-id", default=None)
    parser.add_argument("--version", type=int, default=None)
    parser.add_argument("--receipt", type=Path, default=None, help="path to receipt JSON")
    parser.add_argument("--brief", type=Path, default=None, help="path to original brief bytes")

    # FABA draft source
    parser.add_argument("--draft-brief", type=Path, default=None, help="FABA-only draft path")
    parser.add_argument("--redis-url", default=None, help="FABA publish credential override")

    args = parser.parse_args(argv)
    env = dict(os.environ)
    identity = args.caller_identity
    if identity != FABA_IDENTITY and identity not in NON_FABA_IDENTITIES:
        print(
            json.dumps({"ok": False, "error": f"unknown caller_identity: {identity}"}),
            file=sys.stderr,
        )
        return 2

    try:
        config = _config_from_env(args.env_file, env)
    except KeyError as exc:
        print(json.dumps({"ok": False, "error": f"missing redis config: {exc}"}), file=sys.stderr)
        return 2

    redis_cli = RedisCli(config)
    extra_payload = None
    if args.extra_payload_json:
        try:
            extra_payload = json.loads(args.extra_payload_json)
        except json.JSONDecodeError as exc:
            print(
                json.dumps({"ok": False, "error": f"extra-payload-json: {exc}"}),
                file=sys.stderr,
            )
            return 2
        if not isinstance(extra_payload, dict):
            print(json.dumps({"ok": False, "error": "extra-payload-json must be object"}), file=sys.stderr)
            return 2
    worktree = None
    if args.worktree_json:
        try:
            worktree = json.loads(args.worktree_json)
        except json.JSONDecodeError as exc:
            print(
                json.dumps({"ok": False, "error": f"worktree-json: {exc}"}),
                file=sys.stderr,
            )
            return 2
        if not isinstance(worktree, dict):
            print(json.dumps({"ok": False, "error": "worktree-json must be object"}), file=sys.stderr)
            return 2

    try:
        if args.draft_brief is not None:
            if identity != FABA_IDENTITY:
                raise DispatchAuthorityError(
                    f"non-faba draft forbidden (identity={identity})"
                )
            brief_text = args.draft_brief.read_text(encoding="utf-8")
            source: Any = DraftSource(
                brief_text=brief_text,
                artefact_id=args.artefact_id,
            )
            # FABA may hold publish credential.
            result = publish_and_enqueue(
                source,
                target_agent_id=args.target_agent_id,
                operation=args.operation,
                run_id=args.run_id,
                worktree_lease=args.worktree_lease,
                caller_identity=identity,
                redis_cli=redis_cli,
                sender=args.sender,
                branch=args.branch,
                redis_url=args.redis_url,
                worktree=worktree,
                extra_payload=extra_payload,
                lane=args.lane,
                dry_run=args.dry_run,
                env=env,
                request_id=args.request_id,
            )
        else:
            if not args.artefact_id or args.version is None or not args.receipt or not args.brief:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": (
                                "non-FABA ordinary dispatch requires "
                                "--artefact-id --version --receipt --brief"
                            ),
                        }
                    ),
                    file=sys.stderr,
                )
                return 2
            receipt_data = json.loads(args.receipt.read_text(encoding="utf-8"))
            brief_text = args.brief.read_text(encoding="utf-8")
            # Strip ambient publish credentials for non-FABA path.
            safe_env = filter_publish_env(env)
            result = enqueue_pre_minted(
                target_agent_id=args.target_agent_id,
                artefact_id=args.artefact_id,
                version=args.version,
                receipt=receipt_data,
                brief_text=brief_text,
                caller_identity=identity,
                sender=args.sender,
                branch=args.branch,
                run_id=args.run_id,
                redis_cli=redis_cli,
                operation=args.operation,
                worktree_lease=args.worktree_lease,
                worktree=worktree,
                extra_payload=extra_payload,
                lane=args.lane,
                dry_run=args.dry_run,
                env=safe_env,
                request_id=args.request_id,
            )
    except DispatchAuthorityError as exc:
        print(json.dumps({"ok": False, "error": exc.reason}), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
            file=sys.stderr,
        )
        return 1

    if args.dry_run and "envelope" in result:
        # Exact bytes that would be enqueued (stdout contract for agent-dispatch).
        print(result["envelope"])
        return 0

    # Pointer-only metadata for the caller (no body bytes).
    out = {
        "ok": True,
        "request_id": result["request_id"],
        "artefact_id": result["artefact_id"],
        "version": result["version"],
        "target_agent_id": result["target_agent_id"],
        "change_summary": result.get("change_summary", ""),
        "wire": result.get("wire"),
    }
    print(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
