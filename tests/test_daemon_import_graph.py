"""Reachability guard: nothing on the daemon import path may reach coincurve.

`pyproject.toml` records an accepted divergence — the two DEPLOYED clones
(`/Users/<user>/<workspace>`, `/Users/<user>/AgentRedisBridge`) run Python 3.14.5, and
coincurve publishes no cp314 wheel on any version. Those clones are daemon hosts
only; the suite runs on the 3.12 venvs, which DO have coincurve. The divergence is
harmless for exactly one reason: `bridge.py` cannot reach
`arb_registration.nip_oa` or `buzz_ops.nip_oa`, the two module-level
`from coincurve import ...` sites. That is a property of today's import graph, not
a guarantee. This file turns it into an enforced one.

WHY NOT "assert the import raises". Every venv that runs this suite has coincurve
installed, so a does-it-blow-up check passes vacuously and forever. The assertion
that can actually fail is ABSENCE FROM `sys.modules` after importing the daemon
entrypoint.

WHY A SUBPROCESS. In-process, other test modules have already imported `nip_oa`,
so the check would measure pytest's import graph and go red for the wrong reason.

HOW THIS DIFFERS FROM `test_agent_sdk_lazy_import.py`. That one blocks an optional
dep and asserts bridge still imports — it guards against an EAGER import of a
package that may be missing. This one guards REACHABILITY, and reports the
file:line that pulled a watched module in. A lazy `import coincurve` inside
`nip_oa` would satisfy that test and still break this one's premise, because the
note's claim is about the daemon never reaching those modules at all.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Module roots the daemon path must not pull in. `coincurve` is the dependency
#: itself; the two `nip_oa` modules are the only module-level importers of it.
WATCHED = ("coincurve", "arb_registration.nip_oa", "buzz_ops.nip_oa")

_PROBE = textwrap.dedent(
    '''
    import json, sys, traceback

    WATCHED = %r

    def _is_watched(name):
        return any(name == w or name.startswith(w + ".") for w in WATCHED)

    stacks = {}

    class _StackRecorder:
        """A meta_path finder that never claims a module — it only witnesses.

        Returning None on every call defers to the real finders, so this changes
        nothing about what gets imported. It exists so a failure can name the
        file:line that reached for a watched module, instead of only reporting
        that something did.
        """

        def find_spec(self, fullname, path=None, target=None):
            if _is_watched(fullname) and fullname not in stacks:
                # Drop this frame; the caller is what we want to name.
                stacks[fullname] = "".join(traceback.format_stack()[:-1])
            return None

    sys.meta_path.insert(0, _StackRecorder())

    target = sys.argv[1]
    __import__(target)

    print(json.dumps({
        # Harness self-check: an empty `loaded` proves nothing if the target
        # never imported. See the assertions in the test.
        "target_imported": target in sys.modules,
        "module_count": len(sys.modules),
        "loaded": sorted(n for n in sys.modules if _is_watched(n)),
        "stacks": stacks,
    }))
    '''
) % (WATCHED,)


def _probe(target: str) -> dict:
    """Import `target` in a clean interpreter; report watched modules reached."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, target],
        cwd=str(REPO),
        env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"probe failed to import {target!r}:\n"
        f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    report = json.loads(result.stdout)
    # Verify the harness before trusting its output: a probe that silently did
    # nothing would report an empty `loaded` and read as a pass.
    assert report["target_imported"], f"probe did not actually import {target!r}"
    assert report["module_count"] > 50, (
        f"implausibly small import graph for {target!r} "
        f"({report['module_count']} modules) — probe likely misconfigured"
    )
    return report


def test_daemon_path_does_not_reach_coincurve():
    """`bridge.py` must not pull in coincurve or either nip_oa module.

    If this goes red, the deployed 3.14.5 clones are broken — and it will present
    as a ModuleNotFoundError inside a seat, not as a dependency problem.
    """
    report = _probe("agent_redis_bridge.bridge")
    assert report["loaded"] == [], (
        "the daemon import path now reaches coincurve/nip_oa, which the deployed "
        "3.14.5 clones cannot install (no cp314 wheel). Reached: "
        f"{report['loaded']}\n\nPulled in by:\n"
        + "\n".join(f"--- {n} ---\n{s}" for n, s in report["stacks"].items())
    )


def test_probe_detects_a_real_coincurve_import():
    """Positive control — proves the guard above can fail.

    Without this, a probe that stopped detecting imports would leave
    `test_daemon_path_does_not_reach_coincurve` green forever while protecting
    nothing. `arb_registration.nip_oa` imports coincurve at module scope, so the
    probe MUST see it. This is permanent, not scaffolding: it is what discharges
    "a check must be able to fail" without mutating the tree.
    """
    pytest.importorskip("coincurve", reason="probe control needs coincurve installed")
    report = _probe("arb_registration.nip_oa")
    assert "coincurve" in report["loaded"], (
        "the probe failed to observe a known module-level coincurve import — "
        "the reachability guard is no longer able to fail"
    )
    assert "coincurve" in report["stacks"], "stack recorder did not witness the import"
