from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class AgentDispatchTest(unittest.TestCase):
    def _env_file(self):
        env_file = tempfile.NamedTemporaryFile("w", delete=False)
        env_file.write(
            "AGENT_REDIS_HOST=127.0.0.1\nAGENT_REDIS_PORT=6390\n"
            "AGENT_REDIS_DB=12\nAGENT_REDIS_PREFIX=agent_scratch:\n"
            "AGENT_WORKSPACE=dev\nAGENT_PROJECT=project-c\n"
            "AGENT_DISPATCH_FROM=claude-project-c-dev\nAGENT_DISPATCH_BRANCH=dev\n"
        )
        env_file.close()
        return Path(env_file.name)

    def test_worktree_operation_payloads_are_isolated_and_thread_run_id(self) -> None:
        """Lifecycle arm/release remain taskless independent envelopes.

        worktree_run is ordinary and requires pre-minted flags (authority path).
        """
        root = Path(__file__).resolve().parents[1]
        env_file = self._env_file()
        try:
            common = [str(root / "scripts" / "agent-dispatch"), "--dry-run-envelope",
                      "--target-id", "codex-project-c-dev", "--run-id", "round-1"]
            arm = subprocess.run(
                common + ["--operation", "worktree_arm", "--worktree", "faba-round-1",
                          "--worktree-base", "abc", "--lease-ttl", "3600"],
                env={**os.environ, "AGENT_ENV_FILE": str(env_file)}, capture_output=True,
                text=True, check=True,
            )
            release = subprocess.run(
                common + ["--operation", "worktree_release", "--worktree-lease", "lease-1"],
                env={**os.environ, "AGENT_ENV_FILE": str(env_file)}, capture_output=True,
                text=True, check=True,
            )
            run_missing = subprocess.run(
                common + ["--operation", "worktree_run", "--worktree-lease", "lease-1"],
                env={**os.environ, "AGENT_ENV_FILE": str(env_file)}, capture_output=True,
                text=True,
            )
        finally:
            env_file.unlink(missing_ok=True)
        arm_payload = json.loads(arm.stdout)["payload"]
        release_payload = json.loads(release.stdout)["payload"]
        self.assertEqual(arm_payload, {
            "operation": "worktree_arm",
            "worktree": {"name": "faba-round-1", "cleanup": "keep", "base_ref": "abc"},
            "run_id": "round-1", "lease_ttl": 3600,
        })
        self.assertEqual(release_payload, {
            "operation": "worktree_release", "worktree_lease": "lease-1", "run_id": "round-1",
        })
        self.assertNotEqual(run_missing.returncode, 0)
        self.assertIn("artefact-id", run_missing.stderr)

    def test_ordinary_without_pre_minted_is_refused(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env_file = self._env_file()
        try:
            result = subprocess.run(
                [
                    str(root / "scripts" / "agent-dispatch"),
                    "--dry-run-envelope",
                    "--run-id", "r1",
                    "free form task body",
                ],
                env={**os.environ, "AGENT_ENV_FILE": str(env_file)},
                capture_output=True,
                text=True,
            )
        finally:
            env_file.unlink(missing_ok=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("artefact-id", result.stderr)
        self.assertIn("dispatch_authority", result.stderr)

    def test_legacy_tool_capability_argv_is_rejected(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [str(root / "scripts" / "agent-dispatch"), "--tool-capability", "a" * 64, "task"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown option: --tool-capability", result.stderr)
        self.assertNotIn("tool_capability", result.stdout)

    def test_legacy_tool_capability_fd_is_rejected(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [str(root / "scripts" / "agent-dispatch"), "--tool-capability-fd", "9", "task"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown option: --tool-capability-fd", result.stderr)
        self.assertNotIn("tool_capability", result.stdout)


if __name__ == "__main__":
    unittest.main()
