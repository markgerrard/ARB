"""Live panel dispatch wrapper for diagnose."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable


DispatchFn = Callable[[dict], dict | None]


def run_panel(sealed_briefs: list[dict], dispatch: DispatchFn, work_dir: str | Path, *, repo_root: str | Path | None = None) -> dict:
    work = Path(work_dir)
    replies_dir = work / "panel-replies"
    replies_dir.mkdir(parents=True, exist_ok=True)
    submissions: list[dict] = []
    try:
        for sealed_brief in sealed_briefs:
            dispatched = dispatch(sealed_brief)
            if not dispatched:
                return {"submissions": submissions, "blocking": "incomplete-panel"}
            reply_text = str(dispatched["reply"])
            reply_path = replies_dir / f"{sealed_brief['role']}.txt"
            reply_path.write_text(reply_text, encoding="utf-8")
            bus_reply_ref = f"file://{reply_path}"
            submissions.append(
                {
                    "role": sealed_brief["role"],
                    "seat": str(dispatched.get("from", sealed_brief["role"])),
                    "model": str(dispatched["model"]),
                    "seal": sealed_brief["seal"],
                    "bus_reply_ref": bus_reply_ref,
                    "bus_reply_sha256": hashlib.sha256(reply_text.encode("utf-8")).hexdigest(),
                }
            )
    except Exception:
        return {"submissions": submissions, "blocking": "bridge-unavailable"}
    if len(submissions) != len(sealed_briefs):
        return {"submissions": submissions, "blocking": "incomplete-panel"}
    return {"submissions": submissions, "blocking": None}


def bridge_dispatch(
    target_id: str,
    engine: str,
    *,
    role: str = "reviewer",
    sender: str = "claude-bridge-dev",
    argv_sink=None,
) -> DispatchFn:
    def dispatch(sealed_brief: dict) -> dict | None:
        # Slice 1d-iv: ordinary dispatch requires pre-minted ref+receipt+brief.
        # Publish as short-lived FABA driver, then enqueue without publish credential.
        import tempfile
        from pathlib import Path

        task = json.dumps(sealed_brief, sort_keys=True)
        redis_url = os.environ.get("ARB_MEMORY_REDIS_URL", "").strip()
        if not redis_url:
            print(
                "bridge_dispatch failed: ARB_MEMORY_REDIS_URL required for harness publish "
                "(Slice 1d-iv authority path)",
                file=sys.stderr,
            )
            return None
        with tempfile.TemporaryDirectory(prefix="diagnose-dispatch-") as tmp:
            tmp_p = Path(tmp)
            # Late import keeps diagnose importable without bridge on path for dry stubs.
            try:
                from agent_redis_bridge.dispatch_cli import wrap_instructions_as_brief
            except ImportError:
                wrap_instructions_as_brief = None  # type: ignore[assignment]
            if wrap_instructions_as_brief is None:
                brief_text = (
                    "# Diagnose panel brief\n\n## Assumptions\n```json\n"
                    '{"items":[]}\n```\n\n## Instructions\n\n' + task + "\n"
                )
            else:
                brief_text = wrap_instructions_as_brief(task, vantage="diagnose-panel")
            brief_file = tmp_p / "brief.md"
            brief_file.write_text(brief_text, encoding="utf-8")
            pub = subprocess.run(
                [
                    "scripts/arb-memory-harness-publish",
                    "--target-agent-id",
                    target_id,
                    "--brief",
                    str(brief_file),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={**os.environ, "FROM_AGENT_ID": sender},
            )
            if pub.returncode != 0:
                print(
                    f"bridge_dispatch harness-publish failed: rc={pub.returncode} "
                    f"stderr={pub.stderr.strip()}",
                    file=sys.stderr,
                )
                return None
            receipt = json.loads(pub.stdout)
            receipt_file = tmp_p / "receipt.json"
            receipt_file.write_text(json.dumps(receipt), encoding="utf-8")
            argv = [
                "scripts/agent-dispatch",
                "--workspace",
                "dev",
                "--engine",
                engine,
                "--target-id",
                target_id,
                "--role",
                role,
                "--adhoc",
                "--artefact-id",
                str(receipt["artefact_id"]),
                "--version",
                str(receipt["version"]),
                "--receipt",
                str(receipt_file),
                "--brief",
                str(brief_file),
            ]
            if argv_sink is not None:
                argv_sink(argv)
                return {"model": engine, "from": target_id, "reply": ""}
            from agent_redis_bridge.dispatch_authority import filter_publish_env

            enq_env = filter_publish_env({**os.environ, "FROM_AGENT_ID": sender})
            result = subprocess.run(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=enq_env,
            )
        if result.returncode != 0:
            print(
                "bridge_dispatch failed: "
                f"argv={argv[:-1]!r} returncode={result.returncode} stderr={result.stderr.strip()}",
                file=sys.stderr,
            )
            return None
        try:
            decoded = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"model": engine, "from": target_id, "reply": result.stdout}
        if isinstance(decoded, dict) and "reply" in decoded:
            return {
                "model": str(decoded.get("model", engine)),
                "from": str(decoded.get("from", target_id)),
                "reply": str(decoded["reply"]),
            }
        return {"model": engine, "from": target_id, "reply": result.stdout}

    return dispatch
