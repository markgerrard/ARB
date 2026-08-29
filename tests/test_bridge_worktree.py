from __future__ import annotations

from argparse import Namespace  # noqa: F401 - parity with sibling tests
from pathlib import Path
import json
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from claude_agent_sdk import project_key_for_directory

# Resolve Bridge / WorktreeError / build_parser through the module at call time,
# not via import-time binding: test_bridge_max_parallel does importlib.reload on
# this module, which replaces these class objects. A stale import-time reference
# to WorktreeError would no longer match the (reloaded) class the bridge raises,
# breaking assertRaises only under full discovery (where max_parallel runs first).
import agent_redis_bridge.bridge as arb_bridge
from agent_redis_bridge.engines.base import TurnResult

from test_bridge_handle_raw import FakeRedis


class FakeEngine:
    """Records its cwd and writes a marker file there on a turn, so a test can
    prove WHICH directory the engine actually ran in (base vs worktree)."""

    MARKER = "agent-wrote-this.txt"
    supports_thread_resume = True

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.started = False
        self.stopped = False
        self.resumed_threads: list[str] = []
        self.forked_threads: list[str] = []
        self.thread_id: str | None = None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def resume_thread(self, thread_id: str) -> str:
        self.resumed_threads.append(thread_id)
        self.thread_id = thread_id
        return thread_id

    def fork_thread(self, thread_id: str) -> str:
        self.forked_threads.append(thread_id)
        self.thread_id = f"child-of-{thread_id}"
        return self.thread_id

    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        (Path(self.cwd) / self.MARKER).write_text("agent output", encoding="utf-8")
        return TurnResult(ok=True, result=f"ran in {self.cwd}")


class SessionKeyEngine(FakeEngine):
    """A continuation fake that refuses if its cwd cannot find the stored session."""

    def __init__(self, cwd: str, sessions_by_project_key: dict[str, set[str]]) -> None:
        super().__init__(cwd)
        self.sessions_by_project_key = sessions_by_project_key

    def resume_thread(self, thread_id: str) -> str:
        key = project_key_for_directory(self.cwd)
        if thread_id not in self.sessions_by_project_key.get(key, set()):
            raise RuntimeError(f"session {thread_id} absent for project key {key}")
        return super().resume_thread(thread_id)


class EscapingEngine(FakeEngine):
    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        base = Path(self.cwd).parents[2]
        (base / "escaped.txt").write_text("escaped", encoding="utf-8")
        return TurnResult(ok=True, result="wrote outside cwd")


class DirtyPathEscapingEngine(FakeEngine):
    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        base = Path(self.cwd).parents[2]
        (base / "seed.txt").write_text("escaped over preexisting dirt", encoding="utf-8")
        return TurnResult(ok=True, result="modified preexisting dirty base path")


class IssuingSessionEngine(FakeEngine):
    def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
        self.thread_id = "issued-session"
        return TurnResult(ok=True, result="created session")


def _commit_marker(self, task, *, timeout, policy, on_event) -> TurnResult:
    """An engine that writes the marker AND commits it — leaves a clean tree."""
    p = Path(self.cwd)
    (p / FakeEngine.MARKER).write_text("agent output", encoding="utf-8")
    run = lambda *a: subprocess.run(["git", "-C", str(p), *a], check=True, capture_output=True)
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "agent commit")
    return TurnResult(ok=True, result=f"committed in {p}")


def init_git_repo() -> str:
    """A temp git repo with one commit, so HEAD exists to base a worktree on."""
    repo = tempfile.mkdtemp(prefix="wt-bridge-")
    run = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (Path(repo) / "seed.txt").write_text("seed", encoding="utf-8")
    run("add", "seed.txt")
    run("commit", "-q", "-m", "seed")
    return repo


def _seed_gitignored_venv(repo: str) -> Path:
    """Give the base checkout a .venv (with a bin/python marker) and gitignore it,
    mirroring the real repos whose venvs are untracked."""
    venv = Path(repo) / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/usr/bin/env fake\n", encoding="utf-8")
    run = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)
    (Path(repo) / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    run("add", ".gitignore")
    run("commit", "-q", "-m", "ignore venv")
    return venv


def make_bridge(workdir: str, *extra: str) -> Bridge:
    with tempfile.TemporaryDirectory() as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text(
            "AGENT_REDIS_HOST=127.0.0.1\nAGENT_REDIS_PORT=6390\nAGENT_REDIS_DB=12\n"
            "AGENT_REDIS_PREFIX=agent_scratch:\nAGENT_WORKSPACE=dev\nAGENT_PROJECT=project-c\n"
        )
        args = arb_bridge.build_parser().parse_args(
            ["--env-file", str(env_file), "--workdir", workdir,
             "--sender-policy", "claude-project-c-dev=trusted", *extra]
        )
        return arb_bridge.Bridge(args)


def request_json(
    request_id: str,
    *,
    payload: dict,
    sender: str = "claude-project-c-dev",
    recipient: str = "codex-project-c-dev",
) -> str:
    return json.dumps({
        "id": request_id, "from": sender, "branch": "manual", "to": recipient,
        "kind": "request", "sent_at": "2026-05-31T19:00:00+01:00", "payload": payload,
    }, separators=(",", ":"))


def steer_json(task_id: str, message: str = "go", sender: str = "claude-project-c-dev") -> str:
    return json.dumps({
        "id": "steer-" + task_id, "from": sender, "branch": "manual", "to": "codex-project-c-dev",
        "kind": "steer", "sent_at": "2026-05-31T19:00:00+01:00",
        "payload": {"message": message, "task_id": task_id},
    }, separators=(",", ":"))


class SteerRecordingEngine:
    """Records steer/cancel and blocks in the turn until released, so a test can
    send a control message while the task is genuinely in flight."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.steers: list[str] = []
        self.forked_threads: list[str] = []
        self.thread_id: str | None = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def steer(self, message):
        self.steers.append(message)
        return "turn-1"

    def interrupt(self):
        self.steers.append("__cancel__")
        return "turn-1"

    def fork_thread(self, thread_id: str) -> str:
        self.forked_threads.append(thread_id)
        self.thread_id = f"child-of-{thread_id}"
        return self.thread_id

    def run_turn_with_progress(self, task, *, timeout, policy, on_event):
        self.entered.set()
        self.release.wait(timeout=5)
        return TurnResult(ok=True, result="done")


class WorktreeSpecParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_git_repo()
        self.bridge = make_bridge(self.repo)

    def _spec(self, payload):
        from agent_redis_bridge.envelope import Envelope
        return self.bridge.parse_worktree_spec(Envelope.from_json(request_json("x", payload=payload)))

    def test_absent_returns_none(self):
        self.assertIsNone(self._spec({"task": "t"}))

    def test_valid_defaults_cleanup_keep_and_base_head(self):
        spec = self._spec({"task": "t", "worktree": {"name": "review-1"}})
        self.assertEqual(spec, {"name": "review-1", "base_ref": "HEAD", "cleanup": "keep"})

    def test_path_traversal_name_rejected(self):
        for bad in ["../evil", "a/b", ".", "..", "-x", ""]:
            with self.assertRaises(arb_bridge.WorktreeError):
                self._spec({"task": "t", "worktree": {"name": bad}})

    def test_option_like_base_ref_rejected(self):
        with self.assertRaises(arb_bridge.WorktreeError):
            self._spec({"task": "t", "worktree": {"name": "ok", "base_ref": "--exec=evil"}})

    def test_bad_cleanup_rejected(self):
        with self.assertRaises(arb_bridge.WorktreeError):
            self._spec({"task": "t", "worktree": {"name": "ok", "cleanup": "delete-everything"}})


class WorktreeVenvLinkTest(unittest.TestCase):
    """A worktree lacks the base checkout's untracked .venv, so seats running
    plain pytest there silently lose test execution (memory
    seat-worktree-python-env-gap). create_worktree closes the gap by symlinking
    a GITIGNORED base .venv into the worktree — gitignored-only, so the link
    can neither trip the completion gate nor be committed by an impl seat."""

    def setUp(self) -> None:
        self.repo = init_git_repo()
        self.bridge = make_bridge(self.repo)

    def _create(self, name: str) -> Path:
        return self.bridge.create_worktree({"name": name, "base_ref": "HEAD"})

    def test_gitignored_base_venv_is_linked_into_worktree(self):
        venv = _seed_gitignored_venv(self.repo)
        wt = self._create("venv-a")
        link = wt / ".venv"
        # A real directory, not a symlink: the conventional ignore pattern is
        # ".venv/" (directory-only), which does NOT match a symlink — a symlink
        # would sit untracked and bounce every turn off the completion gate.
        self.assertTrue(link.is_dir(), "worktree did not receive a .venv")
        self.assertFalse(link.is_symlink(), ".venv must be a real dir so the '.venv/' ignore pattern matches")
        py = link / "bin" / "python"
        self.assertTrue(py.exists(), ".venv/bin/python not reachable through the link")
        self.assertEqual(py.resolve(), (venv / "bin" / "python").resolve())
        status = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True, check=True
        )
        self.assertEqual(status.stdout, "", "venv link must leave the worktree clean")

    def test_no_base_venv_no_symlink(self):
        wt = self._create("venv-b")
        self.assertFalse((wt / ".venv").exists())

    def test_unignored_base_venv_is_not_linked(self):
        # Without a gitignore entry the link would show up as untracked — every
        # turn would bounce dirty and an impl seat could commit the symlink.
        venv = Path(self.repo) / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/usr/bin/env fake\n", encoding="utf-8")
        wt = self._create("venv-c")
        self.assertFalse((wt / ".venv").exists())

    def _seed_editable_site_packages(self) -> Path:
        """Extend the seeded venv with a site-packages holding an editable hook
        (a .pth naming the BASE checkout's src — the real setuptools/uv editable
        mechanism), a normal package dir, and a workdir-neutral .pth."""
        venv = _seed_gitignored_venv(self.repo)
        sp = venv / "lib" / "python3.14" / "site-packages"
        sp.mkdir(parents=True)
        (sp / "__editable__.somepkg-0.1.pth").write_text(f"{self.repo}/src\n", encoding="utf-8")
        (sp / "__editable___somepkg_finder.py").write_text(
            f"MAPPING = {{'somepkg': '{self.repo}/src/somepkg'}}\n", encoding="utf-8"
        )
        (sp / "distutils-precedence.pth").write_text("import _distutils_hack\n", encoding="utf-8")
        (sp / "requests").mkdir()
        (sp / "requests" / "__init__.py").write_text("", encoding="utf-8")
        return venv

    def test_editable_hooks_rewritten_to_worktree_rest_symlinked(self):
        # F2 (P1, panel-faba-econ): symlinked site-packages carries the BASE
        # checkout's editable .pth, so a mirrored test run imports base source
        # and never the worktree's own. Copy-on-write: files referencing the
        # base workdir are COPIED with the path rewritten to the worktree;
        # everything else stays a symlink.
        venv = self._seed_editable_site_packages()
        wt = self._create("venv-cow")
        sp = wt / ".venv" / "lib" / "python3.14" / "site-packages"
        self.assertTrue(sp.is_dir() and not sp.is_symlink(), "site-packages must be a real dir")
        pth = sp / "__editable__.somepkg-0.1.pth"
        self.assertFalse(pth.is_symlink(), "editable .pth must be a rewritten copy, not a symlink")
        self.assertEqual(pth.read_text(encoding="utf-8"), f"{wt}/src\n")
        finder = sp / "__editable___somepkg_finder.py"
        self.assertFalse(finder.is_symlink())
        self.assertIn(f"'{wt}/src/somepkg'", finder.read_text(encoding="utf-8"))
        self.assertTrue((sp / "requests").is_symlink(), "regular packages stay symlinked")
        self.assertTrue((sp / "distutils-precedence.pth").is_symlink(), "workdir-neutral .pth stays symlinked")
        # Base hooks untouched.
        self.assertEqual(
            (venv / "lib" / "python3.14" / "site-packages" / "__editable__.somepkg-0.1.pth").read_text(encoding="utf-8"),
            f"{self.repo}/src\n",
        )

    def test_partial_mirror_rolled_back_on_failure(self):
        # F3 (P2): a mid-loop OSError must not leave a half-linked .venv for the
        # worktree's whole lifetime — the target is removed entirely.
        self._seed_editable_site_packages()  # multiple entries => a genuinely partial mirror
        real_symlink_to = Path.symlink_to
        calls = {"n": 0}

        def flaky(self, target, *a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("boom")
            return real_symlink_to(self, target, *a, **k)

        with mock.patch.object(Path, "symlink_to", flaky):
            wt = self._create("venv-partial")
        self.assertFalse((wt / ".venv").exists(), "partial mirror must be rolled back, not left half-linked")

    def test_base_venv_survives_worktree_remove(self):
        # F6 (P2): pin that `git worktree remove --force` does not traverse the
        # mirror's symlinks into the shared base venv (the F5 repro, as a test).
        venv = self._seed_editable_site_packages()
        wt = self._create("venv-rm")
        self.assertTrue((wt / ".venv").is_dir())
        self.bridge.remove_worktree(wt)
        self.assertFalse(wt.exists())
        self.assertTrue((venv / "bin" / "python").exists(), "base venv lost bin/python after worktree remove")
        self.assertTrue(
            (venv / "lib" / "python3.14" / "site-packages" / "requests" / "__init__.py").exists(),
            "base site-packages damaged by worktree remove",
        )

    def test_check_ignore_uses_worktree_ref_not_base_tip(self):
        # F4 (P2, downgraded codex P1): the ignore decision must follow the
        # WORKTREE's own checked-out .gitignore (its base_ref), not the base
        # checkout's current tip — a base_ref predating the ignore rule would
        # otherwise get a mirror its own git status flags as untracked.
        run = lambda *a: subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True)
        pre_ignore = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        _seed_gitignored_venv(self.repo)  # commits .gitignore AFTER pre_ignore
        wt_old = self.bridge.create_worktree({"name": "venv-oldref", "base_ref": pre_ignore})
        self.assertFalse((wt_old / ".venv").exists(), "ref without the ignore rule must not receive a mirror")
        wt_new = self._create("venv-newref")
        self.assertTrue((wt_new / ".venv").is_dir(), "ref carrying the ignore rule must receive the mirror")

    def test_check_ignore_git_error_is_not_treated_as_unignored(self):
        # F12 (P2): rc==1 means "not ignored"; rc>1 means git itself errored.
        # An error must be logged as an error and must not mirror.
        _seed_gitignored_venv(self.repo)
        real_run = subprocess.run

        def erroring_run(cmd, *a, **k):
            if "check-ignore" in cmd:
                return subprocess.CompletedProcess(cmd, 128, stdout=b"", stderr=b"fatal: not a git repository")
            return real_run(cmd, *a, **k)

        with mock.patch("agent_redis_bridge.bridge.subprocess.run", side_effect=erroring_run):
            with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as logs:
                wt = self._create("venv-gerr")
        self.assertFalse((wt / ".venv").exists())
        self.assertTrue(any("ignore-check-failed" in line for line in logs.output))

    def test_mirrored_python_imports_worktree_source(self):
        # F13 (P2) / F2 proof by execution: a REAL venv whose editable-style
        # .pth names <base>/src; the worktree edits its copy of the module; the
        # mirrored interpreter must import the WORKTREE's version.
        import sys
        run = lambda *a: subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True)
        src = Path(self.repo) / "src"
        src.mkdir()
        (src / "wtmarker.py").write_text("VERSION = 'base'\n", encoding="utf-8")
        (Path(self.repo) / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        run("add", ".gitignore", "src/wtmarker.py")
        run("commit", "-q", "-m", "src + ignore")
        subprocess.run([sys.executable, "-m", "venv", str(Path(self.repo) / ".venv")], check=True, capture_output=True)
        venv = Path(self.repo) / ".venv"
        sp = next((venv / "lib").glob("python*")) / "site-packages"
        (sp / "__editable__.wtmarker.pth").write_text(f"{self.repo}/src\n", encoding="utf-8")
        wt = self._create("venv-exec")
        (wt / "src" / "wtmarker.py").write_text("VERSION = 'worktree'\n", encoding="utf-8")
        out = subprocess.run(
            [str(wt / ".venv" / "bin" / "python"), "-c", "import wtmarker; print(wtmarker.VERSION)"],
            capture_output=True, text=True, cwd=wt,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "worktree",
                         "mirrored interpreter imported base source instead of the worktree's")


class WorktreeIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_git_repo()
        self.created: list[FakeEngine] = []

    def _patch_engine(self):
        def factory(args, *, cwd):
            eng = FakeEngine(cwd)
            self.created.append(eng)
            return eng
        return mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=factory)

    def _dispatch(self, payload, *extra):
        bridge = make_bridge(self.repo, *extra)
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        with self._patch_engine():
            bridge.handle_raw(request_json("req-wt", payload=payload))
            bridge.join_active_thread()
        return bridge

    def _verify_fake_snapshot(self, change):
        bridge = make_bridge(self.repo)
        envelope = arb_bridge.Envelope(
            id="req-fake-snapshot",
            sender="claude-project-c-dev",
            branch="manual",
            recipient="codex-project-c-dev",
            kind="request",
            sent_at="2026-07-20T12:00:00+01:00",
            payload={"task": "test"},
        )
        with mock.patch(
            "agent_redis_bridge.bridge.completion_gate.compare_checkout_snapshot",
            return_value=change,
        ):
            return bridge._verify_base_isolation(
                envelope,
                TurnResult(ok=True, result="done"),
                base_snapshot_before={"head": "before"},
                base_busy_at_snapshot=0,
                base_gen_at_snapshot=bridge.base_cwd_turn_gen,
            )

    def test_transient_only_snapshot_is_proven_with_info(self):
        result, preserve = self._verify_fake_snapshot({
            "state": "no_changes_clean",
            "head_before": "same",
            "head_after": "same",
            "dirty_files": [],
            "new_dirty_files": [],
            "transient_changed": [".claude/faba-current-round.json"],
            "sentinel_changed": [],
        })

        self.assertTrue(result.ok)
        self.assertFalse(preserve)
        self.assertEqual(
            result.completion["isolation_transient_changed"],
            [".claude/faba-current-round.json"],
        )

    def test_sentinel_dominates_and_preserves_transient_info(self):
        result, preserve = self._verify_fake_snapshot({
            "state": "no_changes_clean",
            "head_before": "same",
            "head_after": "same",
            "dirty_files": [],
            "new_dirty_files": [],
            "transient_changed": [".claude/faba-current-round.json"],
            "sentinel_changed": [".claude/settings.local.json"],
        })

        self.assertFalse(result.ok)
        self.assertTrue(preserve)
        self.assertEqual(result.completion["state"], "worktree_escape")
        self.assertIn(".claude/settings.local.json", result.completion["escaped_paths"])
        self.assertEqual(
            result.completion["isolation_transient_changed"],
            [".claude/faba-current-round.json"],
        )

    def test_sentinel_with_git_error_is_still_an_escape(self):
        result, preserve = self._verify_fake_snapshot({
            "state": "fingerprint_unverifiable",
            "head_before": "same",
            "head_after": "same",
            "dirty_files": [],
            "new_dirty_files": [],
            "transient_changed": [],
            "sentinel_changed": [".claude/settings.local.json"],
        })

        self.assertFalse(result.ok)
        self.assertTrue(preserve)
        self.assertEqual(result.completion["state"], "worktree_escape")
        self.assertEqual(
            result.completion["escaped_paths"],
            [".claude/settings.local.json"],
        )

    def test_sentinel_with_overlap_is_named_but_unverifiable(self):
        bridge = make_bridge(self.repo)
        envelope = arb_bridge.Envelope(
            id="req-sentinel-overlap",
            sender="claude-project-c-dev",
            branch="manual",
            recipient="codex-project-c-dev",
            kind="request",
            sent_at="2026-07-20T12:00:00+01:00",
            payload={"task": "test"},
        )
        with mock.patch(
            "agent_redis_bridge.bridge.completion_gate.compare_checkout_snapshot",
            return_value={
                "state": "base_checkout_changed",
                "head_before": "same",
                "head_after": "same",
                "dirty_files": [],
                "new_dirty_files": [],
                "transient_changed": [],
                "sentinel_changed": [".claude/settings.local.json"],
            },
        ):
            result, preserve = bridge._verify_base_isolation(
                envelope,
                TurnResult(ok=True, result="done"),
                base_snapshot_before={"head": "before"},
                base_busy_at_snapshot=1,
                base_gen_at_snapshot=bridge.base_cwd_turn_gen,
            )

        self.assertTrue(result.ok)
        self.assertFalse(preserve)
        self.assertEqual(result.completion["isolation"], "unverifiable")
        self.assertEqual(
            result.completion["isolation_reason"],
            "sentinel_changed_with_overlap",
        )
        self.assertEqual(
            result.completion["sentinel_changed"],
            [".claude/settings.local.json"],
        )

    def test_sentinel_and_real_dirty_path_are_both_named(self):
        result, _ = self._verify_fake_snapshot({
            "state": "base_checkout_changed",
            "head_before": "same",
            "head_after": "same",
            "dirty_files": ["escaped.txt"],
            "new_dirty_files": ["escaped.txt"],
            "transient_changed": [],
            "sentinel_changed": [".claude/settings.local.json"],
        })

        self.assertCountEqual(
            result.completion["escaped_paths"],
            ["escaped.txt", ".claude/settings.local.json"],
        )

    def test_head_change_with_transient_is_still_an_escape(self):
        result, preserve = self._verify_fake_snapshot({
            "state": "base_checkout_changed",
            "head_before": "before",
            "head_after": "after",
            "dirty_files": [],
            "new_dirty_files": [],
            "transient_changed": [".claude/faba-current-round.json"],
            "sentinel_changed": [],
        })

        self.assertFalse(result.ok)
        self.assertTrue(preserve)
        self.assertIn("<base HEAD changed>", result.completion["escaped_paths"])
        self.assertEqual(
            result.completion["isolation_transient_changed"],
            [".claude/faba-current-round.json"],
        )

    def test_engine_runs_in_worktree_base_untouched(self):
        self._dispatch({"task": "do work", "worktree": {"name": "taskA", "cleanup": "keep"}})
        wt = Path(self.repo) / ".claude" / "worktrees" / "taskA"
        self.assertTrue((wt / FakeEngine.MARKER).exists(), "agent did not write inside the worktree")
        # By construction: the base checkout is untouched.
        self.assertFalse((Path(self.repo) / FakeEngine.MARKER).exists(), "base checkout was modified")

    def test_worktree_escape_is_detected_and_fails_turn(self):
        def factory(args, *, cwd):
            return EscapingEngine(cwd)

        bridge = make_bridge(self.repo)
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=factory):
            bridge.handle_raw(request_json("req-wt", payload={"task": "escape", "worktree": {"name": "escape"}}))
            bridge.join_active_thread()

        payload = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["completion"]["state"], "worktree_escape")
        self.assertIn("escaped.txt", payload["completion"]["escaped_paths"])

    def test_worktree_escape_modifying_preexisting_dirty_path_is_detected(self):
        (Path(self.repo) / "seed.txt").write_text("dirty before turn", encoding="utf-8")

        bridge = make_bridge(self.repo)
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        with mock.patch(
            "agent_redis_bridge.bridge.build_engine",
            side_effect=lambda args, *, cwd: DirtyPathEscapingEngine(cwd),
        ):
            bridge.handle_raw(
                request_json("req-wt-dirty", payload={"task": "escape", "worktree": {"name": "escape-dirty"}})
            )
            bridge.join_active_thread()

        payload = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["completion"]["state"], "worktree_escape")
        # seed.txt was dirty BEFORE the turn, so it cannot be named as a newly
        # escaped path — the report carries the content-change marker instead.
        self.assertIn(
            "<content change to a pre-existing dirty path>",
            payload["completion"]["escaped_paths"],
        )
        self.assertNotIn("seed.txt", payload["completion"]["escaped_paths"])

    def test_base_change_with_overlapping_base_turn_is_not_an_escape(self):
        bridge = make_bridge(self.repo)
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        repo = Path(self.repo)

        class OverlappingEscapeEngine(FakeEngine):
            def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
                # Simulate a base-cwd task that started and finished during this
                # worktree turn, plus the base write it made.
                with bridge.active_lock:
                    bridge.base_cwd_turn_gen += 1
                (repo / "concurrent.txt").write_text("neighbour wrote this", encoding="utf-8")
                return TurnResult(ok=True, result="clean worktree turn")

        with mock.patch(
            "agent_redis_bridge.bridge.build_engine",
            side_effect=lambda args, *, cwd: OverlappingEscapeEngine(cwd),
        ):
            bridge.handle_raw(
                request_json("req-wt-overlap", payload={"task": "t", "worktree": {"name": "overlap"}})
            )
            bridge.join_active_thread()

        payload = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertTrue(payload["ok"], payload.get("error"))
        completion = payload.get("completion") or {}
        self.assertNotEqual(completion.get("state"), "worktree_escape")
        # The degraded check must be visible on the reply surface, not log-only.
        self.assertEqual(completion.get("isolation"), "unverifiable")
        self.assertEqual(completion.get("isolation_reason"), "base_changed_with_overlap")

    def test_escape_during_failed_engine_start_is_still_reported(self):
        repo = Path(self.repo)

        class StartEscapeEngine(FakeEngine):
            def start(self) -> None:
                # The pooled engine (cwd == base) starts fine; only the fresh
                # worktree engine writes to the base and then dies — the exact
                # hook-escapes-then-raises shape from the r3 P1.
                if Path(self.cwd).resolve() != repo.resolve():
                    (repo / "start-escape.txt").write_text("escaped during start", encoding="utf-8")
                    raise arb_bridge.EngineError("start failed after write")
                super().start()

        bridge = make_bridge(self.repo)
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        with mock.patch(
            "agent_redis_bridge.bridge.build_engine",
            side_effect=lambda args, *, cwd: StartEscapeEngine(cwd),
        ):
            bridge.handle_raw(
                request_json("req-wt-startfail", payload={"task": "t", "worktree": {"name": "startfail"}})
            )
            bridge.join_active_thread()

        payload = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertFalse(payload["ok"])
        self.assertIn("worktree setup failed", payload["error"])
        self.assertEqual(payload["completion"]["state"], "worktree_escape")
        self.assertIn("start-escape.txt", payload["completion"]["escaped_paths"])

    def test_cleanup_auto_removes_worktree(self):
        # Cleanup mechanics: with the completion gate OFF, an auto worktree is
        # removed after the turn even though FakeEngine left an uncommitted marker.
        self._dispatch(
            {"task": "do work", "worktree": {"name": "taskB", "cleanup": "auto"}},
            "--no-enforce-completion",
        )
        self.assertFalse((Path(self.repo) / ".claude" / "worktrees" / "taskB").exists())

    def test_dirty_turn_is_bounced_and_worktree_preserved(self):
        # Gate ON (default): FakeEngine writes a marker without committing, so the
        # turn is INCOMPLETE — the bridge must flip ok=False, attach a completion
        # block, and preserve the worktree so the edits survive (cleanup=auto skipped).
        bridge = self._dispatch({"task": "do work", "worktree": {"name": "taskC", "cleanup": "auto"}})
        wt = Path(self.repo) / ".claude" / "worktrees" / "taskC"
        self.assertTrue(wt.exists(), "dirty worktree must be preserved, not auto-removed")
        self.assertTrue((wt / FakeEngine.MARKER).exists(), "uncommitted edits must survive")
        payload = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertFalse(payload["ok"], "dirty turn must be reported ok=False")
        self.assertIn("uncommitted", (payload["error"] or ""))
        self.assertEqual(payload["completion"]["state"], "dirty_uncommitted")
        self.assertIn(FakeEngine.MARKER, payload["completion"]["dirty_files"])

    def test_committed_turn_passes_and_cleans_up(self):
        # Gate ON: an engine that COMMITS its work leaves a clean tree → committed_clean
        # → ok stays True and cleanup=auto removes the worktree.
        with mock.patch.object(FakeEngine, "run_turn_with_progress", autospec=True, side_effect=_commit_marker):
            self._dispatch({"task": "do work", "worktree": {"name": "taskD", "cleanup": "auto"}})
        self.assertFalse((Path(self.repo) / ".claude" / "worktrees" / "taskD").exists())

    def test_default_no_worktree_runs_in_base(self):
        self._dispatch({"task": "do work"})  # no worktree key
        self.assertTrue((Path(self.repo) / FakeEngine.MARKER).exists())
        self.assertFalse((Path(self.repo) / ".claude" / "worktrees").exists())

    def test_gate_and_auto_cleanup_unaffected_by_venv_link(self):
        # A gitignored .venv symlink must be invisible to the completion gate
        # (committed_clean, not dirty) and must not break worktree removal.
        _seed_gitignored_venv(self.repo)
        with mock.patch.object(FakeEngine, "run_turn_with_progress", autospec=True, side_effect=_commit_marker):
            bridge = self._dispatch({"task": "do work", "worktree": {"name": "venv-gate", "cleanup": "auto"}})
        payload = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertTrue(payload["ok"], f"venv link tripped the gate: {payload}")
        self.assertFalse((Path(self.repo) / ".claude" / "worktrees" / "venv-gate").exists())

    def test_codex_thread_id_worktree_resumes_on_worktree_engine(self):
        bridge = self._dispatch(
            {
                "task": "do work",
                "thread_id": "thread-abc",
                "worktree": {"name": "taskThread", "cleanup": "auto"},
            },
            "--no-enforce-completion",
        )

        payload = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertTrue(payload["ok"])
        self.assertEqual(self.created[0].resumed_threads, [])
        self.assertEqual(self.created[1].resumed_threads, ["thread-abc"])

    def test_agent_sdk_trusted_continuation_reuses_recorded_worktree_project_key(self):
        bridge = make_bridge(
            self.repo,
            "--engine",
            "agent-sdk",
            "--agent-sdk-session-root",
            str(Path(self.repo) / "session-store"),
            "--no-enforce-completion",
        )
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        worktree = bridge.create_worktree({"name": "persist-session", "base_ref": "HEAD", "cleanup": "keep"})
        thread_id = "session-abc"
        stored_sessions = {project_key_for_directory(str(worktree)): {thread_id}}
        bridge.agent_sdk_continuation_store().record(
            thread_id=thread_id,
            sender="claude-project-c-dev",
            worktree_name="persist-session",
        )
        created: list[SessionKeyEngine] = []

        def factory(args, *, cwd):
            engine = SessionKeyEngine(cwd, stored_sessions)
            created.append(engine)
            return engine

        bridge.pool.acquire = mock.Mock(return_value=mock.Mock())  # type: ignore[method-assign]
        with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=factory):
            bridge.handle_raw(
                request_json(
                    "sdk-resume",
                    payload={"task": "continue", "thread_id": thread_id},
                    recipient=bridge.agent_id,
                )
            )
            bridge.join_active_thread()

        self.assertEqual(len(created), 1)
        self.assertEqual(Path(created[0].cwd).resolve(), worktree.resolve())
        self.assertEqual(created[0].resumed_threads, [thread_id])
        reply = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertTrue(reply["ok"])

    def test_agent_sdk_initial_persistent_worktree_records_session_routing(self):
        bridge = make_bridge(
            self.repo,
            "--engine",
            "agent-sdk",
            "--agent-sdk-session-root",
            str(Path(self.repo) / "session-store"),
            "--no-enforce-completion",
        )
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        bridge.pool.acquire = mock.Mock(return_value=mock.Mock())  # type: ignore[method-assign]

        with mock.patch(
            "agent_redis_bridge.bridge.build_engine",
            side_effect=lambda args, *, cwd: IssuingSessionEngine(cwd),
        ):
            bridge.handle_raw(
                request_json(
                    "sdk-initial",
                    payload={"task": "start", "worktree": {"name": "new-session", "cleanup": "keep"}},
                    recipient=bridge.agent_id,
                )
            )
            bridge.join_active_thread()

        record = bridge.agent_sdk_continuation_store().load("issued-session")
        self.assertIsNotNone(record)
        self.assertEqual(record.sender, "claude-project-c-dev")
        self.assertEqual(record.worktree_name, "new-session")

    def test_agent_sdk_escaped_turn_does_not_record_continuation(self):
        bridge = make_bridge(
            self.repo,
            "--engine",
            "agent-sdk",
            "--agent-sdk-session-root",
            str(Path(self.repo) / "session-store"),
        )
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        bridge.pool.acquire = mock.Mock(return_value=mock.Mock())  # type: ignore[method-assign]
        repo = Path(self.repo)

        class EscapingSessionEngine(FakeEngine):
            def run_turn_with_progress(self, task, *, timeout, policy, on_event) -> TurnResult:
                # Issues a session AND escapes into the base, but keeps its own
                # worktree clean — so only the isolation check can fail the turn,
                # and the continuation record must not survive that verdict.
                self.thread_id = "escaped-session"
                (repo / "sdk-escape.txt").write_text("escaped", encoding="utf-8")
                return TurnResult(ok=True, result="issued + escaped")

        with mock.patch(
            "agent_redis_bridge.bridge.build_engine",
            side_effect=lambda args, *, cwd: EscapingSessionEngine(cwd),
        ):
            bridge.handle_raw(
                request_json(
                    "sdk-escape",
                    payload={"task": "t", "worktree": {"name": "sdk-escape", "cleanup": "keep"}},
                    recipient=bridge.agent_id,
                )
            )
            bridge.join_active_thread()

        payload = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["completion"]["state"], "worktree_escape")
        self.assertIsNone(bridge.agent_sdk_continuation_store().load("escaped-session"))

    def test_agent_sdk_continuation_refuses_other_trusted_sender(self):
        bridge = make_bridge(
            self.repo,
            "--engine",
            "agent-sdk",
            "--agent-sdk-session-root",
            str(Path(self.repo) / "session-store"),
        )
        bridge.sender_policies["claude-other-dev"] = "trusted"
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        bridge.create_worktree({"name": "owned-session", "base_ref": "HEAD", "cleanup": "keep"})
        bridge.agent_sdk_continuation_store().record(
            thread_id="session-owned",
            sender="claude-project-c-dev",
            worktree_name="owned-session",
        )

        with mock.patch.object(bridge.pool, "acquire", side_effect=AssertionError("pool touched")):
            self.assertFalse(
                bridge.handle_raw(
                    request_json(
                        "sdk-owner-mismatch",
                        sender="claude-other-dev",
                        payload={"task": "continue", "thread_id": "session-owned"},
                        recipient=bridge.agent_id,
                    )
                )
            )

        reply = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertEqual(reply["error"], "continuation-worktree-owner-mismatch")

    def test_agent_sdk_continuation_refuses_caller_supplied_worktree(self):
        bridge = make_bridge(self.repo, "--engine", "agent-sdk")
        bridge.redis = FakeRedis()  # type: ignore[assignment]

        with mock.patch.object(bridge.pool, "acquire", side_effect=AssertionError("pool touched")):
            self.assertFalse(
                bridge.handle_raw(
                    request_json(
                        "sdk-caller-worktree",
                        payload={
                            "task": "continue",
                            "thread_id": "session-any",
                            "worktree": {"name": "attacker-choice"},
                        },
                        recipient=bridge.agent_id,
                    )
                )
            )

        reply = json.loads(bridge.redis.replies[-1][1])["payload"]
        self.assertEqual(reply["error"], "continuation-worktree-must-be-omitted")

    def test_fork_worktree_steer_routes_to_the_worktree_engine_not_the_pool_slot(self):
        # Regression (codex review): a worktree task runs on a fresh engine, but
        # handle_control resolved the engine via pool.get -> the pooled slot-token
        # engine, so steer/cancel hit the wrong engine. Must route to the engine
        # actually running the task.
        created: list[SteerRecordingEngine] = []

        def factory(args, *, cwd):
            e = SteerRecordingEngine(cwd)
            created.append(e)
            return e

        bridge = make_bridge(self.repo)
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=factory):
            bridge.handle_raw(
                request_json(
                    "wtc",
                    payload={
                        "task": "t",
                        "fork_from_thread_id": "thread-base",
                        "worktree": {"name": "wtc", "cleanup": "auto"},
                    },
                )
            )
            # wait until the worktree engine's turn is actually running
            wt = None
            for _ in range(500):
                wt = next((e for e in created if "worktrees" in e.cwd and e.entered.is_set()), None)
                if wt:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(wt, "worktree engine never entered its turn")
            # steer while the task is in flight
            bridge.handle_raw(steer_json("wtc"))
            pooled = [e for e in created if "worktrees" not in e.cwd]
            self.assertEqual(wt.steers, ["go"], "steer did not reach the worktree engine")
            self.assertEqual(wt.forked_threads, ["thread-base"], "fork did not run on the worktree engine")
            self.assertTrue(all(p.steers == [] for p in pooled), "steer wrongly hit the pooled slot engine")
            self.assertTrue(all(p.forked_threads == [] for p in pooled), "fork wrongly hit the pooled slot engine")
            wt.release.set()
            bridge.join_active_threads()

    def test_invalid_spec_replies_error_and_makes_no_worktree(self):
        bridge = make_bridge(self.repo)
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        bridge.handle_raw(request_json("req-bad", payload={"task": "t", "worktree": {"name": "../evil"}}))
        bridge.join_active_thread()
        replies = [json.loads(b) for _, b in fake.replies if json.loads(b)["kind"] == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertFalse(replies[0]["payload"]["ok"])
        self.assertIn("worktree spec invalid", replies[0]["payload"]["error"])
        self.assertFalse((Path(self.repo) / ".claude" / "worktrees").exists())


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 2026-08-08: a dispatch died with
#     git worktree add failed: fatal: invalid reference: feat/muse-runner-spec
# The branch existed on origin. It did not exist LOCALLY in the seat's clone,
# whose last fetch was two days old, and `git worktree add <path> <bare-name>`
# does not fall back to a remote-tracking ref.
#
# So the failure is not about one branch. Seat workdirs are long-lived deployed
# clones that nothing fetches, so ANY dispatch naming a branch created since the
# seat's last fetch hits this -- and the error names the ref rather than the
# staleness, which sends you looking in the wrong place.
# ---------------------------------------------------------------------------


def init_git_repo_with_origin() -> tuple[str, str]:
    """A clone whose origin has a branch the clone has NOT fetched yet.

    Reproduces the real shape: seat workdir is a real clone of a real origin,
    the branch is pushed to origin after the seat's last fetch.
    """
    origin = tempfile.mkdtemp(prefix="wt-origin-")
    run_o = lambda *a: subprocess.run(["git", "-C", origin, *a], check=True, capture_output=True)
    run_o("init", "-q")
    run_o("config", "user.email", "t@t")
    run_o("config", "user.name", "t")
    (Path(origin) / "seed.txt").write_text("seed", encoding="utf-8")
    run_o("add", "seed.txt")
    run_o("commit", "-q", "-m", "seed")

    clone = tempfile.mkdtemp(prefix="wt-clone-")
    subprocess.run(["git", "clone", "-q", origin, clone], check=True, capture_output=True)
    run_c = lambda *a: subprocess.run(["git", "-C", clone, *a], check=True, capture_output=True)
    run_c("config", "user.email", "t@t")
    run_c("config", "user.name", "t")

    # origin gains a branch AFTER the clone was taken -- the clone never fetches
    run_o("checkout", "-q", "-b", "feat/later")
    (Path(origin) / "later.txt").write_text("later", encoding="utf-8")
    run_o("add", "later.txt")
    run_o("commit", "-q", "-m", "later")
    run_o("checkout", "-q", "-")
    return clone, origin


def test_worktree_base_ref_resolves_a_branch_the_clone_has_not_fetched():
    """The reported failure, reproduced end to end."""
    clone, _origin = init_git_repo_with_origin()
    bridge = make_bridge(clone)
    assert bridge.resolve_base_ref_oid("feat/later"), (
        "a branch present on origin but not yet fetched must still resolve"
    )


def test_worktree_base_ref_prefers_a_local_ref_over_the_remote_one():
    """The fallback must not shadow a real local branch of the same name."""
    clone, _origin = init_git_repo_with_origin()
    subprocess.run(["git", "-C", clone, "fetch", "-q", "origin"], check=True, capture_output=True)
    subprocess.run(["git", "-C", clone, "checkout", "-q", "-b", "feat/later", "HEAD"],
                   check=True, capture_output=True)
    local_oid = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                               check=True, capture_output=True, text=True).stdout.strip()
    bridge = make_bridge(clone)
    assert bridge.resolve_base_ref_oid("feat/later") == local_oid


def test_worktree_base_ref_error_names_the_staleness_not_just_the_ref():
    """The old message was `fatal: invalid reference: <ref>`, which reads as
    'you typed the wrong branch' and sends you to the dispatcher. The clone
    being stale is the far more likely cause and must be in the text."""
    clone, _origin = init_git_repo_with_origin()
    bridge = make_bridge(clone)
    try:
        bridge.resolve_base_ref_oid("feat/does-not-exist-anywhere")
    except Exception as exc:  # noqa: BLE001 - the type is asserted by the message
        msg = str(exc)
    else:
        raise AssertionError("an unresolvable base_ref must raise")
    assert "feat/does-not-exist-anywhere" in msg
    assert "origin/" in msg, "must say the remote form was tried"
    assert "fetch" in msg.lower(), "must point at staleness as the likely cause"


def test_create_worktree_succeeds_on_an_unfetched_branch():
    """End to end through the path that actually failed."""
    clone, _origin = init_git_repo_with_origin()
    _seed_gitignored_venv(clone)
    bridge = make_bridge(clone)
    path = bridge.create_worktree({"name": "wt-unfetched", "base_ref": "feat/later",
                                   "cleanup": "keep"})
    assert path.is_dir()
    assert (path / "later.txt").is_file(), "worktree must hold the branch's content"
