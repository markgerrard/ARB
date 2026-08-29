"""Live seat dispatcher — store-before-send over the bridge's own scripts.

Slice 1d-iv removed free-form task strings from the bus, so the live path is
two subprocesses: `arb-memory-harness-publish` (publishes the brief, returns
the receipt) then `agent-dispatch` with the pre-minted quartet. Dispatch
logic stays outside the orch process; this class only composes, invokes, and
parses.

Env hygiene is asymmetric on purpose (using-agent-bridge skill, failure-shape
table): the publish step NEEDS `ARB_MEMORY_REDIS_URL` in its environment (it
is the publish credential); the dispatch step must run WITHOUT it. The
dispatch env also carries FROM_AGENT_ID (sender policy), BRANCH (envelope
invalid-branch check), AGENT_ENV_FILE (bus params), and the bridge clone's
venv bin on PATH (dispatch_authority needs the venv's redis module).

The brief's `## Assumptions` section carries NOTHING but the JSON fence —
prose after the fence inside the section is refused by faba_schema
(env-trap B4(i)).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .dispatch import DispatchResult

SEAT_PREFIX_TO_ENGINE: dict[str, str] = {
    "codex": "codex",
    "agy": "agy-print",
    "grok": "grok-acp",
    "cursor": "cursor-acp",
    "devin": "devin-acp",
    "cline": "cline-acp",
    "asdk": "agent-sdk",
    "pi": "pi-rpc",
}


class DispatchError(Exception):
    """Live-dispatch failure with a specific problem code in its message."""


def engine_for_seat(seat: str) -> str:
    prefix = seat.split("-", 1)[0]
    engine = SEAT_PREFIX_TO_ENGINE.get(prefix)
    if engine is None:
        raise DispatchError(f"dispatch-unknown-seat-engine: no engine mapping for seat {seat!r}")
    return engine


@dataclass(frozen=True)
class LiveDispatchConfig:
    bridge_clone: Path
    from_agent_id: str
    branch: str
    brief_dir: Path
    env_file: Path | None = None
    timeout: int = 1800


class SubprocessSeatDispatcher:
    def __init__(
        self,
        config: LiveDispatchConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.config = config
        self.runner = runner

    # ------------------------------------------------------------ compose

    def _mint_run_id(self, seat: str) -> str:
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"warmorch-{seat}-{stamp}-{uuid.uuid4().hex[:6]}"

    def _write_brief(self, run_id: str, seat: str, lens: str, task: str) -> Path:
        self.config.brief_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.brief_dir / f"{run_id}.md"
        path.write_text(
            f"# warm-orch dispatch to {seat}\n"
            "\n"
            "## Assumptions\n"
            "\n"
            "```json\n"
            '{"items": []}\n'
            "```\n"
            "\n"
            "## Instructions\n"
            "\n"
            f"Lens for this dispatch: {lens}\n"
            "\n"
            f"{task}\n",
            encoding="utf-8",
        )
        return path

    # ------------------------------------------------------------- steps

    def _venv_path(self, base: dict[str, str]) -> str:
        venv_bin = self.config.bridge_clone / ".venv" / "bin"
        return f"{venv_bin}:{base.get('PATH', '')}"

    def _publish(self, seat: str, brief_path: Path, run_id: str) -> tuple[dict, Path]:
        argv = [
            str(self.config.bridge_clone / "scripts" / "arb-memory-harness-publish"),
            "--target-agent-id",
            seat,
            "--brief",
            str(brief_path),
        ]
        # Publish KEEPS ARB_MEMORY_REDIS_URL (it is the credential) but still
        # needs the bridge venv's python first on PATH — the script resolves
        # `python3` and dies on `import redis` under system python.
        env = dict(os.environ)
        env["PATH"] = self._venv_path(env)
        out = self.runner(argv, env=env, capture_output=True, text=True)
        if out.returncode != 0:
            raise DispatchError(
                f"dispatch-publish-failed: rc={out.returncode} {out.stderr.strip()[-500:]}"
            )
        try:
            receipt = json.loads(out.stdout)
        except json.JSONDecodeError as exc:
            raise DispatchError(f"dispatch-publish-failed: unparseable receipt: {exc}") from exc
        receipt_path = self.config.brief_dir / f"{run_id}.receipt.json"
        receipt_path.write_text(out.stdout, encoding="utf-8")
        return receipt, receipt_path

    def _dispatch_env(self) -> dict[str, str]:
        from agent_redis_bridge.dispatch_authority import pop_publish_env

        env = dict(os.environ)
        pop_publish_env(env)
        env["FROM_AGENT_ID"] = self.config.from_agent_id
        env["BRANCH"] = self.config.branch
        if self.config.env_file is not None:
            env["AGENT_ENV_FILE"] = str(self.config.env_file)
        env["PATH"] = self._venv_path(env)
        return env

    # ---------------------------------------------------------- dispatch

    def dispatch(self, seat: str, lens: str, task: str) -> DispatchResult:
        engine = engine_for_seat(seat)
        run_id = self._mint_run_id(seat)
        brief_path = self._write_brief(run_id, seat, lens, task)
        receipt, receipt_path = self._publish(seat, brief_path, run_id)
        argv = [
            str(self.config.bridge_clone / "scripts" / "agent-dispatch"),
            "--engine",
            engine,
            "--target-id",
            seat,
            "--timeout",
            str(self.config.timeout),
            "--run-id",
            run_id,
            "--artefact-id",
            str(receipt["artefact_id"]),
            "--version",
            str(receipt["version"]),
            "--receipt",
            str(receipt_path),
            "--brief",
            str(brief_path),
        ]
        out = self.runner(argv, env=self._dispatch_env(), capture_output=True, text=True)
        if out.returncode == 124:
            return DispatchResult(
                status="timeout",
                seat=seat,
                reply=f"dispatch-timeout: no reply within {self.config.timeout}s (run {run_id})",
            )
        payload: dict = {}
        if out.stdout.strip():
            try:
                payload = json.loads(out.stdout)
            except json.JSONDecodeError:
                payload = {"raw": out.stdout[-2000:]}
        if out.returncode != 0:
            detail = payload.get("error") or out.stderr.strip()[-500:] or "no detail"
            return DispatchResult(status="failed", seat=seat, reply=f"dispatch-failed: {detail}")
        reply = payload.get("result")
        if not isinstance(reply, str):
            reply = json.dumps(payload)
        return DispatchResult(status="completed", seat=seat, reply=reply)
