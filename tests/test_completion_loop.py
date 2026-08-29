"""Drive-to-completion loop: re-prompt a continuation-capable engine until the
task predicate (expected artifacts present + tree committed) passes."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_redis_bridge.bridge as arb_bridge
from agent_redis_bridge.engines.base import TurnResult

from test_bridge_handle_raw import FakeRedis
from test_bridge_worktree import init_git_repo, make_bridge, request_json


def _commit_all(p: Path, msg: str) -> None:
    run = lambda *a: subprocess.run(["git", "-C", str(p), *a], check=True, capture_output=True)
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)


class ScriptedContinuationEngine:
    """A continuation-capable engine driven by a per-turn script. Each script
    entry is a callable(cwd) -> int (the tool-call count for that turn) that
    performs the turn's side effects (writing/committing files)."""

    supports_continuation = True

    def __init__(self, cwd: str, script) -> None:
        self.cwd = cwd
        self.script = script
        self.turn = 0
        self.started = False
        self.reset_count = 0
        self.prompts: list[str] = []
        self.timeouts: list[int] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def reset_context(self) -> str:
        self.reset_count += 1
        return f"reset-{self.reset_count}"

    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        self.prompts.append(task)
        self.timeouts.append(timeout)
        step = self.script[min(self.turn, len(self.script) - 1)]
        self.turn += 1
        tool_calls = step(Path(self.cwd))
        return TurnResult(ok=True, result=f"turn {self.turn}", stop_reason="end_turn", tool_calls=tool_calls)


class NonContinuationEngine(ScriptedContinuationEngine):
    supports_continuation = False


class TimingOutScriptedEngine(ScriptedContinuationEngine):
    """Runs the script (writes/commits) then returns ok=False with the
    canonical engine-side timeout error. Simulates the rc=124 case where the
    worker did real work before its turn deadline expired."""

    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        step = self.script[min(self.turn, len(self.script) - 1)]
        self.turn += 1
        tool_calls = step(Path(self.cwd))
        return TurnResult(
            ok=False, result="", error=f"turn timed out after {timeout}s",
            stop_reason="failed", tool_calls=tool_calls,
        )


class CompletionLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_git_repo()
        self.last_engine: ScriptedContinuationEngine | None = None

    def _dispatch(self, payload, engine_cls, script, *extra):
        bridge = make_bridge(self.repo, *extra)
        bridge.redis = FakeRedis()  # type: ignore[assignment]

        def factory(args, *, cwd):
            self.last_engine = engine_cls(cwd, script)
            return self.last_engine

        with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=factory):
            bridge.handle_raw(request_json("req-loop", payload=payload))
            bridge.join_active_thread()
        return bridge

    def _reply(self, bridge):
        return json.loads(bridge.redis.replies[-1][1])["payload"]

    def test_multi_turn_completion_drives_to_done(self):
        # Turn 1 writes only file a (incomplete). Continuation writes b + c and commits.
        script = [
            lambda p: (p / "a.txt").write_text("a") or 1,
            lambda p: ((p / "b.txt").write_text("b"), (p / "c.txt").write_text("c"), _commit_all(p, "all"), 3)[-1],
        ]
        bridge = self._dispatch(
            {"task": "make a,b,c", "worktree": {"name": "L1", "cleanup": "keep"},
             "expected_artifacts": ["a.txt", "b.txt", "c.txt"]},
            ScriptedContinuationEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "L1"
        self.assertEqual(self.last_engine.turn, 2, "loop should have re-prompted exactly once")
        for f in ("a.txt", "b.txt", "c.txt"):
            self.assertTrue((wt / f).exists())
        reply = self._reply(bridge)
        self.assertTrue(reply["ok"], "task is complete after continuation -> ok")
        self.assertEqual(reply["completion"]["state"], "committed_clean")

    def test_fresh_context_does_not_reset_between_continuation_attempts(self):
        script = [
            lambda p: (p / "a.txt").write_text("a") or 1,
            lambda p: ((p / "b.txt").write_text("b"), _commit_all(p, "all"), 2)[-1],
        ]
        self._dispatch(
            {
                "task": "make a,b",
                "fresh_context": True,
                "worktree": {"name": "Lfresh", "cleanup": "keep"},
                "expected_artifacts": ["a.txt", "b.txt"],
            },
            ScriptedContinuationEngine, script,
        )

        self.assertEqual(self.last_engine.turn, 2)
        self.assertEqual(self.last_engine.reset_count, 1)

    def test_continuation_turns_inherit_requested_timeout(self):
        def first(cwd):
            (cwd / "a.txt").write_text("a")
            return 1

        def finish(cwd):
            (cwd / "b.txt").write_text("b")
            _commit_all(cwd, "done")
            return 1

        self._dispatch(
            {
                "task": "make files",
                "worktree": {"name": "Ltimeout", "cleanup": "keep"},
                "expected_artifacts": ["a.txt", "b.txt"],
                "turn_timeout": 25,
            },
            ScriptedContinuationEngine,
            [first, finish],
            "--turn-timeout", "60", "--turn-timeout-max", "300",
        )

        self.assertEqual(self.last_engine.timeouts, [25, 25])

    def test_role_profile_is_not_wrapped_on_continuation_attempts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "reviewer.md"
            profile.write_text("Review strictly.", encoding="utf-8")
            script = [
                lambda p: (p / "a.txt").write_text("a") or 1,
                lambda p: ((p / "b.txt").write_text("b"), _commit_all(p, "all"), 2)[-1],
            ]
            self._dispatch(
                {
                    "task": "make a,b",
                    "worktree": {"name": "Lrole", "cleanup": "keep"},
                    "expected_artifacts": ["a.txt", "b.txt"],
                },
                ScriptedContinuationEngine,
                script,
                "--role-profile-file",
                str(profile),
            )

        self.assertEqual(self.last_engine.turn, 2)
        self.assertIn("<system_guidance>\nReview strictly.\n</system_guidance>", self.last_engine.prompts[0])
        self.assertNotIn("<system_guidance>", self.last_engine.prompts[1])

    def test_no_progress_breaker_stops_and_bounces(self):
        # Turn 1 writes a (uncommitted). Continuation does NOTHING (no progress).
        script = [
            lambda p: (p / "a.txt").write_text("a") or 1,
            lambda p: 0,  # no-op turn -> same tree -> no-progress breaker
        ]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "L2", "cleanup": "keep"},
             "expected_artifacts": ["a.txt", "b.txt"]},
            ScriptedContinuationEngine, script,
        )
        # one continuation attempt, then no-progress -> stop (turn count 2, not the full budget of 3+1)
        self.assertLessEqual(self.last_engine.turn, 2)
        reply = self._reply(bridge)
        self.assertFalse(reply["ok"], "still incomplete -> bounce")

    def test_clean_tree_with_missing_artifact_bounces_not_vacuous_green(self):
        # The vacuously-green hole (found live via a no-Bash haiku seat that wrote
        # its file OUTSIDE the worktree): the engine produces NOTHING in the
        # worktree yet returns ok and claims "done". The tree is clean
        # (no_changes_clean), so the dirty-tree gate never fires — but the expected
        # artifact is missing, so the contract was NOT met. Must bounce ok=False
        # (fail loud), never pass green on work that was never produced.
        script = [
            lambda p: 0,  # turn 1: writes nothing in the worktree
            lambda p: 0,  # continuation: still nothing -> no-progress breaker stops
        ]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "VG1", "cleanup": "keep"},
             "expected_artifacts": ["a.txt"]},
            ScriptedContinuationEngine, script,
        )
        reply = self._reply(bridge)
        self.assertFalse(reply["ok"], "no changes + missing expected artifact -> bounce, not vacuous green")
        self.assertIn("a.txt", reply["completion"].get("missing_artifacts", []))

    def test_non_continuation_engine_is_not_looped(self):
        script = [lambda p: (p / "a.txt").write_text("a") or 1]  # writes, never commits
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "L3", "cleanup": "keep"},
             "expected_artifacts": ["a.txt", "b.txt"]},
            NonContinuationEngine, script,
        )
        self.assertEqual(self.last_engine.turn, 1, "no loop for a non-continuation engine")
        reply = self._reply(bridge)
        self.assertFalse(reply["ok"], "incomplete + dirty -> single bounce")

    def test_orchestrator_commits_uncommitted_artifacts(self):
        # The Composer case: engine writes ALL expected files in one turn but
        # never commits. No loop needed (artifacts present); orchestrator commits.
        script = [lambda p: ((p / "a.txt").write_text("a"), (p / "b.txt").write_text("b"), 2)[-1]]
        bridge = self._dispatch(
            {"task": "make a and b", "worktree": {"name": "OC1", "cleanup": "keep"},
             "expected_artifacts": ["a.txt", "b.txt"]},
            ScriptedContinuationEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "OC1"
        self.assertEqual(self.last_engine.turn, 1, "all artifacts present in turn 1 -> no loop")
        reply = self._reply(bridge)
        self.assertTrue(reply["ok"], "orchestrator committed the finished work -> ok")
        self.assertEqual(reply["completion"]["state"], "committed_clean")
        self.assertEqual(subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                        capture_output=True, text=True).stdout.strip(), "")
        log = subprocess.run(["git", "-C", str(wt), "log", "--oneline", "-1"], capture_output=True, text=True).stdout
        self.assertIn("make a and b", log)  # default message derived from the task

    def test_orchestrator_uses_dispatch_commit_message(self):
        script = [lambda p: ((p / "a.txt").write_text("a"), 1)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "OC4", "cleanup": "keep"},
             "expected_artifacts": ["a.txt"], "commit_message": "feat: precise message"},
            ScriptedContinuationEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "OC4"
        log = subprocess.run(["git", "-C", str(wt), "log", "--oneline", "-1"], capture_output=True, text=True).stdout
        self.assertIn("feat: precise message", log)
        self.assertTrue(self._reply(bridge)["ok"])

    def test_orchestrator_commit_disabled_bounces(self):
        script = [lambda p: ((p / "a.txt").write_text("a"), 1)[-1]]
        bridge = make_bridge(self.repo, "--no-auto-commit")
        bridge.redis = FakeRedis()  # type: ignore[assignment]

        def factory(args, *, cwd):
            self.last_engine = ScriptedContinuationEngine(cwd, script)
            return self.last_engine

        with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=factory):
            bridge.handle_raw(request_json("req-loop", payload={
                "task": "x", "worktree": {"name": "OC2", "cleanup": "keep"}, "expected_artifacts": ["a.txt"]}))
            bridge.join_active_thread()
        self.assertFalse(self._reply(bridge)["ok"], "auto-commit off + uncommitted -> bounce")

    def test_orchestrator_does_not_commit_incomplete(self):
        # Only 1 of 2 artifacts present -> orchestrator must NOT commit partial work.
        script = [lambda p: (p / "a.txt").write_text("a") or 1, lambda p: 0]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "OC3", "cleanup": "keep"}, "expected_artifacts": ["a.txt", "b.txt"]},
            ScriptedContinuationEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "OC3"
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, base, "no commit when artifacts incomplete")
        self.assertFalse(self._reply(bridge)["ok"])

    def test_case2_adopts_agent_commit_no_second_commit(self):
        # Agent commits ALL expected files itself -> orchestrator adopts, no 2nd commit.
        script = [lambda p: ((p / "a.txt").write_text("a"), _commit_all(p, "agent: did it"), 1)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "C2", "cleanup": "keep"}, "expected_artifacts": ["a.txt"]},
            ScriptedContinuationEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "C2"
        # `git init -q` honours init.defaultBranch — comparing against the literal "main"
        # breaks on systems where it's still "master". Compare against the source repo's
        # actual HEAD instead (same pattern as test_orchestrator_does_not_commit_incomplete).
        repo_base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
        n_commits = subprocess.run(["git", "-C", str(wt), "rev-list", "--count", f"{repo_base}..HEAD"],
                                   capture_output=True, text=True).stdout.strip()
        self.assertEqual(n_commits, "1", "must NOT create a second commit over the agent's")
        reply = self._reply(bridge)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["completion"]["committed_by"], "agent")

    def test_case3_partial_commit_fails(self):
        # Agent commits a.txt but leaves b.txt dirty (both expected) -> FAIL, no 2nd commit.
        script = [lambda p: ((p / "a.txt").write_text("a"), _commit_all(p, "agent: a only"),
                             (p / "b.txt").write_text("b"), 2)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "C3", "cleanup": "keep"}, "expected_artifacts": ["a.txt", "b.txt"]},
            ScriptedContinuationEngine, script,
        )
        reply = self._reply(bridge)
        self.assertFalse(reply["ok"], "partial commit must fail the run")
        self.assertIn("partial commit", reply["error"])

    def test_case4_unexpected_dirty_file_fails(self):
        # Agent writes the expected file AND an unexpected one -> FAIL (don't commit stray work).
        script = [lambda p: ((p / "a.txt").write_text("a"), (p / "rogue.txt").write_text("x"), 2)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "C4", "cleanup": "keep"}, "expected_artifacts": ["a.txt"]},
            ScriptedContinuationEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "C4"
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        self.assertEqual(head, base, "must not commit when an unexpected file is present")
        reply = self._reply(bridge)
        self.assertFalse(reply["ok"])
        self.assertIn("outside the allowed set", reply["error"])

    def test_allowed_path_prefix_permits_extra_file(self):
        # A file under an --allowed-path prefix (not an exact expected) is permitted.
        script = [lambda p: ((p / "a.txt").write_text("a"),
                             (p / "gen").mkdir(exist_ok=True), (p / "gen" / "x.txt").write_text("g"), 2)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "C5", "cleanup": "keep"},
             "expected_artifacts": ["a.txt"], "allowed_paths": ["gen/"]},
            ScriptedContinuationEngine, script,
        )
        reply = self._reply(bridge)
        self.assertTrue(reply["ok"], "files under an allowed prefix should commit")
        self.assertEqual(reply["completion"]["committed_by"], "orchestrator")

    def test_already_complete_first_turn_no_continuation(self):
        script = [lambda p: ((p / "a.txt").write_text("a"), _commit_all(p, "a"), 1)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "L4", "cleanup": "keep"}, "expected_artifacts": ["a.txt"]},
            ScriptedContinuationEngine, script,
        )
        self.assertEqual(self.last_engine.turn, 1, "complete on first turn -> no re-prompt")
        self.assertTrue(self._reply(bridge)["ok"])

    def test_empty_end_turn_without_tools_is_not_continuable(self):
        result = TurnResult(ok=True, result="", stop_reason="end_turn", tool_calls=0)

        self.assertFalse(arb_bridge.Bridge._continuable(result))


class PostTimeoutAdoptTest(CompletionLoopTest):
    """Engine-side turn timeouts that leave salvageable artifact-contract work
    in the worktree. The hook adopts the writes when the safety predicates
    pass (artifacts present, paths allowed, no partial commits)."""

    def test_post_timeout_adopts_uncommitted_artifacts(self):
        # Engine wrote a + b then timed out. Both expected, no strays.
        # Hook commits them, flips ok=True, preserves timeout_error.
        script = [lambda p: ((p / "a.txt").write_text("a"), (p / "b.txt").write_text("b"), 2)[-1]]
        bridge = self._dispatch(
            {"task": "make a and b", "worktree": {"name": "PT1", "cleanup": "keep"},
             "expected_artifacts": ["a.txt", "b.txt"]},
            TimingOutScriptedEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "PT1"
        reply = self._reply(bridge)
        self.assertTrue(reply["ok"], "adopted timeout-completed work -> ok=True")
        self.assertEqual(reply["completion"]["timeout_adoption"], "committed")
        self.assertEqual(reply["completion"]["committed_by"], "orchestrator")
        self.assertIn("turn timed out after", reply["completion"]["timeout_error"])
        # Exactly one new commit, clean tree
        base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        n = subprocess.run(["git", "-C", str(wt), "rev-list", "--count", f"{base}..HEAD"],
                           capture_output=True, text=True).stdout.strip()
        self.assertEqual(n, "1")
        self.assertEqual(subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                        capture_output=True, text=True).stdout.strip(), "")

    def test_post_timeout_missing_artifacts_no_commit(self):
        # Engine wrote only a (timed out before b). Hook MUST NOT commit;
        # tags missing_artifacts and preserves the worktree for inspection.
        script = [lambda p: (p / "a.txt").write_text("a") or 1]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "PT2", "cleanup": "keep"},
             "expected_artifacts": ["a.txt", "b.txt"]},
            TimingOutScriptedEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "PT2"
        reply = self._reply(bridge)
        self.assertFalse(reply["ok"], "incomplete artifacts -> no flip to ok")
        self.assertEqual(reply["completion"]["timeout_adoption"], "missing_artifacts")
        self.assertIn("b.txt", reply["completion"]["missing_artifacts"])
        # HEAD unmoved
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        self.assertEqual(head, base, "no commit when artifacts missing")

    def test_post_timeout_disallowed_dirty_refuses_commit(self):
        # Engine wrote the expected file AND a rogue file outside allowed_paths.
        # Hook MUST NOT commit; tags disallowed_dirty.
        script = [lambda p: ((p / "a.txt").write_text("a"), (p / "rogue.txt").write_text("x"), 2)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "PT3", "cleanup": "keep"},
             "expected_artifacts": ["a.txt"]},
            TimingOutScriptedEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "PT3"
        reply = self._reply(bridge)
        self.assertFalse(reply["ok"], "stray dirty paths -> no flip to ok")
        self.assertEqual(reply["completion"]["timeout_adoption"], "disallowed_dirty")
        self.assertIn("rogue.txt", reply["completion"]["disallowed_paths"])
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        self.assertEqual(head, base, "no commit when strays present")

    def test_post_timeout_partial_commit_refuses_second(self):
        # Engine partial-committed (a) then dirtied b, then timed out.
        # State == dirty_after_commit. Hook MUST NOT add a second commit.
        script = [lambda p: ((p / "a.txt").write_text("a"), _commit_all(p, "agent: a"),
                             (p / "b.txt").write_text("b"), 2)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "PT4", "cleanup": "keep"},
             "expected_artifacts": ["a.txt", "b.txt"]},
            TimingOutScriptedEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "PT4"
        reply = self._reply(bridge)
        self.assertFalse(reply["ok"], "partial commit must not be adopted")
        self.assertEqual(reply["completion"]["timeout_adoption"], "partial_commit")
        # Exactly the ONE agent commit remains; no orchestrator-added 2nd commit
        repo_base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        n = subprocess.run(["git", "-C", str(wt), "rev-list", "--count", f"{repo_base}..HEAD"],
                           capture_output=True, text=True).stdout.strip()
        self.assertEqual(n, "1", "no second commit on top of agent's partial")

    def test_post_timeout_agent_committed_clean_adopted(self):
        # Engine committed all expected files itself, then timed out
        # (e.g. on its final reply turn). State == committed_clean.
        # Hook adopts as agent_committed; no extra commit; ok stays False
        # (artifact contract met but engine signalled timeout).
        script = [lambda p: ((p / "a.txt").write_text("a"), _commit_all(p, "agent: did it"), 1)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "PT5", "cleanup": "keep"},
             "expected_artifacts": ["a.txt"]},
            TimingOutScriptedEngine, script,
        )
        wt = Path(self.repo) / ".claude" / "worktrees" / "PT5"
        reply = self._reply(bridge)
        self.assertEqual(reply["completion"]["timeout_adoption"], "agent_committed")
        self.assertEqual(reply["completion"]["committed_by"], "agent")
        self.assertIn("turn timed out after", reply["completion"]["timeout_error"])
        # Exactly the ONE agent commit; no orchestrator 2nd commit
        base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        n = subprocess.run(["git", "-C", str(wt), "rev-list", "--count", f"{base}..HEAD"],
                           capture_output=True, text=True).stdout.strip()
        self.assertEqual(n, "1")

    def test_post_timeout_allowed_prefix_adopts(self):
        # Bonus case: extra files under an allowed_paths prefix should commit
        # cleanly via post-timeout adoption, same as the normal success path.
        script = [lambda p: ((p / "a.txt").write_text("a"),
                             (p / "gen").mkdir(exist_ok=True), (p / "gen" / "x.txt").write_text("g"), 2)[-1]]
        bridge = self._dispatch(
            {"task": "x", "worktree": {"name": "PT6", "cleanup": "keep"},
             "expected_artifacts": ["a.txt"], "allowed_paths": ["gen/"]},
            TimingOutScriptedEngine, script,
        )
        reply = self._reply(bridge)
        self.assertTrue(reply["ok"], "allowed prefixes covered the extra file -> commit")
        self.assertEqual(reply["completion"]["timeout_adoption"], "committed")


if __name__ == "__main__":
    unittest.main()
