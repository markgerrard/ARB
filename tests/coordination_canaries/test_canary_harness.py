"""The harness must be trustworthy before any canary result means anything."""

from __future__ import annotations

import pytest

from .canary_lib import envelope, meta_ids, run_reliable_watcher
from .conftest import Plane, _assert_local


def test_safety_fence_refuses_a_non_loopback_endpoint():
    """The fence is the reason these canaries are safe to run at all."""
    _assert_local("127.0.0.1")  # loopback passes
    with pytest.raises(RuntimeError, match="non-loopback"):
        _assert_local("arb-bus.example.com")


def test_fence_is_enforced_on_plane_handout_not_only_at_construction():
    """A fence checked once at construction can be defeated by mutating the
    field afterwards, which is exactly how a canary ends up pointed at prod."""
    p = Plane(name="x", host="127.0.0.1", port=6379, container="none")
    p.host = "arb-bus.example.com"
    with pytest.raises(RuntimeError, match="non-loopback"):
        p.client
    with pytest.raises(RuntimeError, match="non-loopback"):
        p.script_env("someone", __import__("pathlib").Path("/tmp"))


def test_two_planes_are_genuinely_independent(planes):
    """If the planes shared state, every cross-plane finding would be an artefact."""
    a, b = planes
    assert a.port != b.port
    a.client.set("probe", "a-only")
    assert a.client.get("probe") == "a-only"
    assert b.client.get("probe") is None, "planes share state — they are not independent buses"


def test_real_watcher_consumes_from_a_real_plane(planes, inbox_dir):
    """End-to-end proof the harness drives the actual script, not a stub."""
    a, _ = planes
    env = envelope(frm="peer-x", to="canary-agent", event="harness_smoke")
    a.send(env)
    assert a.depth("canary-agent") == 1

    res = run_reliable_watcher(a, "canary-agent", inbox_dir, iterations=1)

    assert env["id"] in meta_ids(res.stdout), f"watcher surfaced nothing: {res.stdout}\n{res.stderr}"
    assert (inbox_dir / f"{env['id']}.json").exists(), "no envelope written to disk"
    assert a.depth("canary-agent") == 0, "inbox not drained"
    assert a.processing_depth("canary-agent") == 0, ":processing not acked"
