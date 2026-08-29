"""Stage-0 connectivity/tool-use spike for claude-agent-sdk vendor routing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from models import MODELS, ModelSpec, load_key
from scrub import scrub

HERE = Path(__file__).resolve().parent
READING = HERE / "READING.txt"
DRIVER = r'''
import asyncio
import json
import os
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

async def go():
    used = False
    text = ""
    opts = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=["Read"],
        permission_mode="default",
        model=model,
    )
    async for msg in query(prompt="Read READING.txt and quote line 1.", options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    used = True
                if isinstance(block, TextBlock):
                    text += block.text
        elif isinstance(msg, ResultMessage):
            if msg.result:
                text += msg.result
    print(json.dumps({"tool_used": used, "content_ok": "forty-two" in text.lower(), "text_excerpt": text[:240]}))

asyncio.run(go())
'''


def _auth_env_var(auth_style: str) -> str:
    if auth_style in {"auth-token", "bearer", "authorization"}:
        return "ANTHROPIC_AUTH_TOKEN"
    return "ANTHROPIC_API_KEY"


def _child_env(spec: ModelSpec, key: str) -> dict[str, str]:
    keep = {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
    env = {name: value for name, value in os.environ.items() if name in keep and value}
    env["ANTHROPIC_BASE_URL"] = spec.base_url
    env[_auth_env_var(spec.auth_style)] = key
    for k, v in spec.extra_env:  # tier-lane mapping etc. (e.g. z.ai/GLM)
        env[k] = v
    return env


def _scrubbed(text: str, secrets: list[str], var_names: list[str]) -> str:
    return scrub(text, secrets, var_names + ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])


def run_spike(spec: ModelSpec) -> dict:
    key = load_key(spec)
    with tempfile.TemporaryDirectory(prefix=f"agent-sdk-spike-{spec.name}-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "READING.txt").write_text(READING.read_text())
        proc = subprocess.run(
            [sys.executable, "-c", DRIVER, str(tmp_path), spec.model_id],
            env=_child_env(spec, key),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    redacted_stdout = _scrubbed(proc.stdout, [key], [spec.key_env])
    redacted_stderr = _scrubbed(proc.stderr, [key], [spec.key_env])
    if proc.returncode != 0:
        return {
            "name": spec.name,
            "ok": False,
            "tool_used": False,
            "content_ok": False,
            "error": (redacted_stderr or redacted_stdout or f"exit {proc.returncode}")[-1200:],
            "base_url": spec.base_url,
            "model_id": spec.model_id,
            "auth_style": spec.auth_style,
        }
    try:
        last = [line for line in redacted_stdout.splitlines() if line.strip()][-1]
        payload = json.loads(last)
    except (IndexError, json.JSONDecodeError) as exc:
        return {
            "name": spec.name,
            "ok": False,
            "tool_used": False,
            "content_ok": False,
            "error": f"could not parse spike JSON: {exc}; stdout={redacted_stdout[-800:]} stderr={redacted_stderr[-800:]}",
            "base_url": spec.base_url,
            "model_id": spec.model_id,
            "auth_style": spec.auth_style,
        }
    tool_used = bool(payload.get("tool_used"))
    content_ok = bool(payload.get("content_ok"))
    return {
        "name": spec.name,
        "ok": tool_used and content_ok,
        "tool_used": tool_used,
        "content_ok": content_ok,
        "error": None if tool_used and content_ok else f"tool_used={tool_used} content_ok={content_ok}",
        "base_url": spec.base_url,
        "model_id": spec.model_id,
        "auth_style": spec.auth_style,
    }


def main() -> int:
    results = []
    for spec in MODELS:
        try:
            result = run_spike(spec)
        except Exception as exc:
            result = {
                "name": spec.name,
                "ok": False,
                "tool_used": False,
                "content_ok": False,
                "error": _scrubbed(str(exc), [], [spec.key_env]),
                "base_url": spec.base_url,
                "model_id": spec.model_id,
                "auth_style": spec.auth_style,
            }
        results.append(result)
        print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if results and results[0].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
