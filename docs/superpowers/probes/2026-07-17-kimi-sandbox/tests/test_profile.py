"""Tests for the Seatbelt profile renderer + its adversarial deny-proof (v4 §3, §6, §8).

These run against REAL /usr/bin/sandbox-exec on macOS — the whole arc's lesson is that
a profile that parses is not a profile that denies. Every 'denies' assertion is paired
with an unsandboxed control that must SUCCEED, or the proof is vacuous
([[vacuously-green-guard-fail-loud]], [[deny-proofs-need-adversarial-verification]]).
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from sbprofile import BootstrapProfile, render, SandboxError  # noqa: E402

pytestmark = pytest.mark.skipif(
    subprocess.run(["uname"], capture_output=True, text=True).stdout.strip() != "Darwin",
    reason="Seatbelt is macOS-only",
)

BOX = "/private/tmp/kimi-probe-test-box"


def _run(profile_text, argv):
    return subprocess.run(["/usr/bin/sandbox-exec", "-p", profile_text, *argv],
                          capture_output=True, text=True)


def setup_module():
    os.makedirs(f"{BOX}/worktree", exist_ok=True)
    os.makedirs(f"{BOX}/outside", exist_ok=True)


# ---- the renderer rejects the traps the arc already paid for ----

def test_symlinked_tmp_path_is_rejected():
    # /tmp is a symlink to /private/tmp; Seatbelt matches resolved paths, so a
    # /tmp/... write_path silently denies everything. The renderer must refuse it.
    with pytest.raises(SandboxError):
        BootstrapProfile(worktree="/tmp/x", tmpdir="/private/tmp/y",
                         kimi_home="/private/tmp/z").render()


def test_relative_path_is_rejected():
    with pytest.raises(SandboxError):
        BootstrapProfile(worktree="relative/x", tmpdir="/private/tmp/y",
                         kimi_home="/private/tmp/z").render()


# ---- the profile actually ENFORCES (paired with unsandboxed control) ----

def test_deny_default_blocks_a_write_outside_the_box():
    # the deny-proof: a write outside every write_path must FAIL...
    p = BootstrapProfile(worktree=f"{BOX}/worktree", tmpdir=f"{BOX}/tmp",
                         kimi_home=f"{BOX}/kimi").render()
    r = _run(p, ["/usr/bin/touch", f"{BOX}/outside/escape.txt"])
    assert r.returncode != 0, "write outside the box should be DENIED"


def test_control_the_same_write_succeeds_unsandboxed():
    # ...and the ADVERSARIAL control: the identical write must SUCCEED with no sandbox,
    # or the deny above proves nothing (the file/path could just be unwritable).
    target = f"{BOX}/outside/escape_control.txt"
    if os.path.exists(target):
        os.remove(target)
    r = subprocess.run(["/usr/bin/touch", target], capture_output=True)
    assert r.returncode == 0 and os.path.exists(target)


def test_write_inside_the_worktree_is_allowed():
    p = BootstrapProfile(worktree=f"{BOX}/worktree", tmpdir=f"{BOX}/tmp",
                         kimi_home=f"{BOX}/kimi").render()
    r = _run(p, ["/usr/bin/touch", f"{BOX}/worktree/allowed.txt"])
    assert r.returncode == 0, r.stderr


def test_profile_can_actually_exec_a_binary():
    # ARB's profile could NOT (rc=71: no /usr/lib, no dyld). The bootstrap must run one.
    p = BootstrapProfile(worktree=f"{BOX}/worktree", tmpdir=f"{BOX}/tmp",
                         kimi_home=f"{BOX}/kimi").render()
    r = _run(p, ["/bin/echo", "hi"])
    assert r.returncode == 0 and r.stdout.strip() == "hi", r.stderr
