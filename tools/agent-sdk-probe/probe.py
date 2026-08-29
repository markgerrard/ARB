"""Per-model mutation probe runner, gated by Stage-0 connectivity/tool-use."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from models import MODELS, ModelSpec, load_key
from scrub import scrub
from spike import _child_env, run_spike
from verifier import verify

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FIXTURE = HERE / "fixture"
RESULTS_DOC = ROOT / "docs" / "agent-sdk-probe-results.md"
MUTATION_DRIVER = r'''
import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

cwd = Path(sys.argv[1])
model = sys.argv[2] or None  # empty -> SDK default model (provider tier-lane maps it)
prompt = (
    "Implement wrap in wordwrap.py so test_contract.py passes. "
    "Edit ONLY wordwrap.py. Run the test to confirm."
)

async def go():
    text = ""
    tools = []
    opts = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=["Read", "Write", "Edit", "Bash"],
        permission_mode="acceptEdits",
        model=model,
    )
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    tools.append(block.name)
                    print(f"TOOL_USE {block.name}", flush=True)
                if isinstance(block, TextBlock):
                    text += block.text
        elif isinstance(msg, ResultMessage):
            if msg.result:
                text += msg.result
    print("TOOLS=" + ",".join(tools), flush=True)
    if text:
        print(text[-1000:], flush=True)

asyncio.run(go())
'''


def _present_secrets() -> list[str]:
    return [os.environ[name] for name in {m.key_env for m in MODELS} if os.environ.get(name)]


def _key_names() -> list[str]:
    return [m.key_env for m in MODELS] + ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]


def _scrub_all(text: str, extra_secrets: list[str] | None = None) -> str:
    return scrub(text, _present_secrets() + list(extra_secrets or []), _key_names())


def _git(repo: Path, args: list[str]) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _fresh_repo() -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="agent-sdk-mutation-"))
    repo = temp_root / "repo"
    shutil.copytree(FIXTURE, repo)
    _git(repo, ["init", "-q"])
    _git(repo, ["add", "-A"])
    _git(repo, ["-c", "user.email=probe@example.invalid", "-c", "user.name=agent-sdk-probe", "commit", "-qm", "baseline"])
    return repo


def _sdk_mutation(tempdir: Path, spec: ModelSpec, key: str) -> tuple[str, str]:
    proc = subprocess.run(
        [sys.executable, "-c", MUTATION_DRIVER, str(tempdir), spec.model_id],
        env=_child_env(spec, key),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    return proc.stdout, proc.stderr


def run_model(spec: ModelSpec) -> dict:
    stage0 = run_spike(spec)
    if not stage0.get("ok"):
        return {
            "name": spec.name,
            "status": "FAIL",
            "reasons": [f"endpoint/auth: {stage0.get('error') or 'Stage-0 failed'}"],
            "trace_excerpt": "",
            "stage0": stage0,
        }

    key = load_key(spec)
    repo = _fresh_repo()
    try:
        stdout, stderr = _sdk_mutation(repo, spec, key)
        trace = _scrub_all(stdout + "\n" + stderr, [key])
        verdict = verify(repo)
        return {
            "name": spec.name,
            "status": verdict.status,
            "reasons": verdict.reasons,
            "trace_excerpt": trace[-1600:],
            "stage0": stage0,
        }
    except Exception as exc:
        return {
            "name": spec.name,
            "status": "FAIL",
            "reasons": [_scrub_all(f"error: {exc}", [key])],
            "trace_excerpt": "",
            "stage0": stage0,
        }


def _markdown(results: list[dict]) -> str:
    lines = [
        "# agent-sdk mutation probe results",
        "",
        "| model | status | reasons |",
        "|---|---|---|",
    ]
    for result in results:
        reasons = "; ".join(str(r) for r in result.get("reasons", []))
        lines.append(f"| {result['name']} | {result['status']} | {reasons} |")
    lines.extend(["", "## Trace Excerpts", ""])
    for result in results:
        excerpt = result.get("trace_excerpt") or ""
        if not excerpt:
            continue
        lines.extend([f"### {result['name']}", "", "```", excerpt, "```", ""])
    return _scrub_all("\n".join(lines) + "\n")


def main() -> int:
    results = []
    for spec in MODELS:
        try:
            result = run_model(spec)
        except Exception as exc:
            result = {
                "name": spec.name,
                "status": "FAIL",
                "reasons": [_scrub_all(f"error: {exc}")],
                "trace_excerpt": "",
            }
        results.append(result)
        print(json.dumps({k: v for k, v in result.items() if k != "trace_excerpt"}, sort_keys=True), flush=True)
    RESULTS_DOC.write_text(_markdown(results), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
