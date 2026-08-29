"""ctl send routes ordinary requests through dispatch_authority (Slice 1d-iv)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from agent_redis_bridge import ctl


class FakeRedis:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []

    def lpush(self, agent_id: str, body: str) -> None:
        self.pushed.append((agent_id, body))

    def rpush(self, agent_id: str, body: str) -> None:
        self.pushed.append((agent_id, body))


def _args(*argv: str):
    return ctl.build_parser().parse_args(
        [
            "--target",
            "codex-project-c-dev",
            "--sender",
            "claude-project-c-dev",
            "--workdir",
            ".",
            "send",
            "--artefact-id",
            "art-1",
            "--version",
            "1",
            "--receipt",
            "/tmp/receipt.json",
            "--brief",
            "/tmp/brief.md",
            *argv,
        ]
    )


class CtlWorktreeTest(unittest.TestCase):
    def test_send_task_delegates_to_authority_not_rpush(self) -> None:
        redis = FakeRedis()
        receipt = {
            "artefact_id": "art-1",
            "version": 1,
            "target_agent_id": "codex-project-c-dev",
            "registration_generation": "g1",
            "worker_vantage": "v",
            "content_hash": "h",
        }
        brief = "# Dispatch brief\n\n## Assumptions\n```json\n{\"items\":[]}\n```\n\n## Instructions\n\nHi\n"

        def fake_read(self, *a, **k):
            p = str(self)
            if p.endswith("receipt.json"):
                return json.dumps(receipt)
            return brief

        with mock.patch(
            "agent_redis_bridge.dispatch_cli.enqueue_pre_minted",
            return_value={"request_id": "req-from-authority"},
        ) as enq:
            with mock.patch.object(Path, "read_text", fake_read):
                tid = ctl.send_task(
                    redis, _args("--worktree", "task-7", "--worktree-cleanup", "auto")
                )
        self.assertEqual(tid, "req-from-authority")
        self.assertEqual(redis.pushed, [])  # no independent rpush
        enq.assert_called_once()
        kwargs = enq.call_args.kwargs
        self.assertEqual(kwargs["caller_identity"], "ctl")
        self.assertEqual(kwargs["worktree"]["name"], "task-7")
        self.assertEqual(kwargs["worktree"]["cleanup"], "auto")

    def test_send_requires_pre_minted_flags(self) -> None:
        with self.assertRaises(SystemExit):
            ctl.build_parser().parse_args(
                [
                    "--target",
                    "codex-project-c-dev",
                    "--sender",
                    "claude-project-c-dev",
                    "send",
                    "do work",
                ]
            )


if __name__ == "__main__":
    unittest.main()
