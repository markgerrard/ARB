"""Close-discipline serving tests for ARB-B2 (design v3).

Criteria covered here: A1, A2, A4-hermetic, A4-host, A5(i)/(ii)/(iii), A7,
plus harness self-check. A3 (live interactive) and A6 live legs are host-scoped;
A6 pure-evaluator oracle is exercised when the PiExtensions detector is reachable.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import os as _os
import pytest as _pytest
pytestmark = _pytest.mark.skipif(
    not _os.environ.get("ARB_PIEXT_TESTS"),
    reason="opt-in: requires a PiExtensions checkout; set ARB_PIEXT_TESTS=1 (then unreachable => FAIL, never skip)",
)

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "arb-pi-orch"
PROMPT_B = ROOT / "prompts" / "arb-close-discipline.md"
PROMPT_FIXTURE_A = ROOT / "tests" / "fixtures" / "arb-close-discipline-route-a.md"
ROUTE_A_HOST = Path.home() / ".claude" / "arb-close-discipline.md"
NOTES = ROOT / "prompts" / "arb-close-discipline.NOTES.md"
PROJECT_B_CONSOLES = [
    ROOT / "scripts" / "pi-project-b-a-console",
    ROOT / "scripts" / "pi-project-b-b-console",
    ROOT / "scripts" / "pi-project-b-1-console",
]
EXPECTED_SHA256 = "e0484f2c450f04a15014c7e2c63d7f40df82664246beb5414589c3f10de7cf6c"
LIVE_ANCHORS = (
    "proposal, not a close",
    "no polish exemption",
    "pinning test",
)
STAGED_MARKERS = (
    "uploaded_by allowlist",
    "randomised [E] claim",
)
_PI_SDK_REL = Path("@earendil-works/pi-coding-agent/dist/index.js")


def _pi_sdk_search_roots() -> list[Path]:
    """`node_modules` roots to search for the pi SDK, most-authoritative first.

    The install PREFIX is host-specific — Homebrew (`/opt/homebrew/lib/node_modules`),
    a user-level npm prefix (`~/.npm-global/lib/node_modules`), `/usr/local`, nvm,
    bun, or a repo-local `node_modules`. Hardcoding one prefix makes A5(ii) pass on
    one machine and hard-fail on every other, which is exactly what happened: pi was
    installed under the user prefix here, so the test reported "SDK entry
    unreachable" on a host where the SDK had been present all along.

    `npm root -g` is asked first because it is the authoritative answer for the
    active toolchain; the static sweep is the fallback when npm is absent.
    """
    roots: list[Path] = []
    npm = shutil.which("npm")
    if npm:
        try:
            proc = subprocess.run(
                [npm, "root", "-g"], capture_output=True, text=True, timeout=15
            )
            if proc.returncode == 0 and proc.stdout.strip():
                roots.append(Path(proc.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            pass  # fall through to the static sweep
    roots.extend(
        [
            Path("/opt/homebrew/lib/node_modules"),
            Path("/usr/local/lib/node_modules"),
            Path.home() / ".npm-global" / "lib" / "node_modules",
            Path.home() / ".bun" / "install" / "global" / "node_modules",
            ROOT / "tools" / "pi-sdk-host" / "node_modules",
            ROOT / "node_modules",
        ]
    )
    ordered: list[Path] = []
    for root in roots:
        if root not in ordered:
            ordered.append(root)
    return ordered


def _resolve_pi_sdk_entry() -> Path | None:
    """The pi SDK entry file, or None if it is not installed on this host.

    `ARB_PI_SDK_ENTRY` overrides the search for unusual layouts (and is the escape
    hatch for CI). Returning None rather than a guessed path is deliberate: the
    caller must FAIL loudly, never skip — see the A5(ii) guard.
    """
    override = os.environ.get("ARB_PI_SDK_ENTRY", "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    for root in _pi_sdk_search_roots():
        candidate = root / _PI_SDK_REL
        if candidate.is_file():
            return candidate
    return None


SDK_ENTRY = _resolve_pi_sdk_entry()

# The PiExtensions checkout is a SEPARATE repo. It is NOT pip/npm-installable, so
# there is no package manager to ask — but the previous single hardcoded default
# (`/Volumes/<workspace>/repos/PiExtensions`) is a drive that lives permanently on
# ONE host, which made these guards unpassable everywhere else. The pi SDK half of
# this file was made portable (see `_pi_sdk_search_roots`) while this half was not,
# in the same change; that asymmetry is what this search closes.
_PIEXT_VOLUME_DEFAULT = Path("/Volumes/<workspace>/repos/PiExtensions")


def _piext_search_roots() -> list[Path]:
    """Candidate PiExtensions checkouts, most-local first."""
    roots = [
        ROOT.parent / "PiExtensions",
        Path.home() / "PiExtensions",
        Path.home() / "repos" / "PiExtensions",
        _PIEXT_VOLUME_DEFAULT,
    ]
    ordered: list[Path] = []
    for root in roots:
        if root not in ordered:
            ordered.append(root)
    return ordered


def _resolve_piext_root() -> Path:
    """The PiExtensions checkout, or the volume default if none is reachable.

    `ARB_PIEXT_ROOT` overrides the search (the escape hatch for CI and unusual
    layouts). A root is only accepted if it actually contains the detector, so a
    stale empty directory cannot shadow a real checkout.

    Falls back to the volume default rather than None so the guards keep naming a
    concrete path in their failure text. They still FAIL — never skip — when the
    detector is unreachable; the point of the search is that the red means "not
    installed anywhere on this host", not "you are not on one specific Mac".
    """
    override = os.environ.get("ARB_PIEXT_ROOT", "").strip()
    if override:
        return Path(override)
    for root in _piext_search_roots():
        if (root / "extensions" / "arb-close-discipline.ts").is_file():
            return root
    return _PIEXT_VOLUME_DEFAULT


PIEXT_ROOT = _resolve_piext_root()


def _piext_unreachable_detail() -> str:
    """Why PiExtensions was not found, listing every path tried.

    Without this the failure reads as a code defect. It is usually "this checkout
    only exists on the host that holds the Workspace drive" — a reachability
    fact, not a broken assertion.
    """
    searched = "\n  ".join(str(r) for r in _piext_search_roots())
    return (
        f"PiExtensions checkout not reachable on this host. Resolved PIEXT_ROOT="
        f"{PIEXT_ROOT}.\nSearched:\n  {searched}\n"
        "Note the last candidate is an external volume that lives on one specific "
        "machine, so this fails on every other host until you clone PiExtensions "
        "locally or set ARB_PIEXT_ROOT. Do NOT convert this to pytest.skip (ARB-B9)."
    )


PIEXT_DETECTOR = PIEXT_ROOT / "extensions" / "arb-close-discipline.ts"
PIEXT_EVAL_MJS = PIEXT_ROOT / "extensions" / "arb-close-discipline-eval.mjs"
PIEXT_RUNNER_TS = PIEXT_ROOT / "extensions" / "arb-subagents" / "runner.ts"
# Exit code: PI_BIN bare name unresolvable / not absolute-executable (R-0).
PI_BIN_REFUSE_CODE = 79


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _run_launcher(
    args: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    launcher: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [str(launcher or LAUNCHER), *(args or [])]
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=merged,
        timeout=10,
        check=False,
    )


def _write_pi_stub(path: Path, marker: Path) -> None:
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'launched\\n' > "{marker}"
            # Record full argv (including $0) so R-1 can assert flag injection.
            printf '%s\\n' "$0" "$@" >> "{marker}"
            exit 0
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _make_checkout_with_prompt(tmp_path: Path) -> Path:
    """Minimal checkout: scripts/arb-pi-orch + prompts/arb-close-discipline.md."""
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    prompts = checkout / "prompts"
    scripts.mkdir(parents=True)
    prompts.mkdir(parents=True)
    shutil.copy2(LAUNCHER, scripts / "arb-pi-orch")
    (scripts / "arb-pi-orch").chmod(
        (scripts / "arb-pi-orch").stat().st_mode | stat.S_IXUSR
    )
    shutil.copy2(PROMPT_B, prompts / "arb-close-discipline.md")
    return checkout


def _run_eval_mjs(system_prompt: str) -> subprocess.CompletedProcess[str]:
    """Drive A6 through the thin CLI that imports the SHIPPED .ts detector."""
    return subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            str(PIEXT_EVAL_MJS),
            "--prompt-stdin",
        ],
        input=system_prompt,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


# ---------------------------------------------------------------------------
# Harness self-check
# ---------------------------------------------------------------------------


def test_harness_collects_this_module():
    """Broken harness (0 collected) must not look green."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__)),
            "--collect-only",
            "-q",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    # pytest -q --collect-only ends with "N tests collected"
    match = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout + proc.stderr)
    assert match is not None, f"collect output unparseable:\n{proc.stdout}\n{proc.stderr}"
    assert int(match.group(1)) > 0, "harness collected zero tests"


# ---------------------------------------------------------------------------
# A1 — resolver
# ---------------------------------------------------------------------------


def test_a1_print_prompt_path_resolves_readable_file():
    assert LAUNCHER.is_file(), f"launcher missing: {LAUNCHER}"
    proc = _run_launcher(["--print-prompt-path"])
    assert proc.returncode == 0, proc.stderr
    path = Path(proc.stdout.strip())
    assert path.is_absolute()
    assert str(path).endswith("/prompts/arb-close-discipline.md")
    assert path.is_file() and path.stat().st_size > 0
    assert os.access(path, os.R_OK)


# ---------------------------------------------------------------------------
# A2 — dangling path refuses (exit 78, no exec)
# ---------------------------------------------------------------------------


def test_a2_dangling_path_exits_78_and_does_not_exec_pi(tmp_path: Path):
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    # No prompts/ directory → unreadable close-discipline path.
    shutil.copy2(LAUNCHER, scripts / "arb-pi-orch")
    (scripts / "arb-pi-orch").chmod(
        (scripts / "arb-pi-orch").stat().st_mode | stat.S_IXUSR
    )

    marker = tmp_path / "pi-launched.marker"
    stub = tmp_path / "pi-stub"
    _write_pi_stub(stub, marker)

    proc = _run_launcher(
        launcher=scripts / "arb-pi-orch",
        env={"PI_BIN": str(stub)},
    )
    assert proc.returncode == 78, (
        f"expected 78, got {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "close-discipline-unreadable path=" in proc.stderr
    assert "refusing to launch" in proc.stderr
    assert not marker.exists(), "PI_BIN stub ran — guard failed to refuse before exec"


def test_a2_empty_prompt_exits_78_and_does_not_exec_pi(tmp_path: Path):
    """An EMPTY discipline file must refuse, not launch (r4, cold-Opus).

    -r alone admits a zero-byte or truncated file; pi would then serve an empty
    append and the seat launches looking configured with no discipline at all.
    Mutant: drop the `-s` guard from arb-pi-orch → this test goes green-to-red.
    """
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    prompts = checkout / "prompts"
    scripts.mkdir(parents=True)
    prompts.mkdir(parents=True)
    # Readable but ZERO BYTES — passes -r, must fail -s.
    (prompts / "arb-close-discipline.md").write_text("", encoding="utf-8")
    shutil.copy2(LAUNCHER, scripts / "arb-pi-orch")
    (scripts / "arb-pi-orch").chmod(
        (scripts / "arb-pi-orch").stat().st_mode | stat.S_IXUSR
    )

    marker = tmp_path / "pi-launched.marker"
    stub = tmp_path / "pi-stub"
    _write_pi_stub(stub, marker)

    proc = _run_launcher(
        launcher=scripts / "arb-pi-orch",
        env={"PI_BIN": str(stub)},
    )
    assert proc.returncode == 78, (
        f"expected 78, got {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "close-discipline-empty path=" in proc.stderr, (
        "must name the EMPTY condition specifically, not the generic unreadable one: "
        f"{proc.stderr}"
    )
    assert not marker.exists(), "PI_BIN stub ran — empty file was allowed to launch"


def test_a2_positive_path_logs_intent_and_execs(tmp_path: Path):
    """Control: readable prompt → intent log + PI_BIN invoked (not a full pi)."""
    checkout = _make_checkout_with_prompt(tmp_path)
    scripts = checkout / "scripts"

    marker = tmp_path / "pi-launched.marker"
    stub = tmp_path / "pi-stub"
    _write_pi_stub(stub, marker)

    proc = _run_launcher(
        ["--mode", "text", "-p", "ping"],
        launcher=scripts / "arb-pi-orch",
        env={"PI_BIN": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    assert re.search(
        r"close-discipline-intent sha256=[0-9a-f]{64} bytes=\d+", proc.stderr
    ), proc.stderr
    assert marker.exists(), "PI_BIN stub should have been exec'd"


# ---------------------------------------------------------------------------
# R-0 — PI_BIN resolution: never prepend cwd; refuse unresolvable bare names
# ---------------------------------------------------------------------------


def test_r0_default_pi_bin_ignores_cwd_decoy(tmp_path: Path):
    """Default PI_BIN=pi must not exec a decoy `pi` sitting in cwd (cwd-PATH shadow)."""
    checkout = _make_checkout_with_prompt(tmp_path)
    launcher = checkout / "scripts" / "arb-pi-orch"

    real_dir = tmp_path / "real-bin"
    real_dir.mkdir()
    real_marker = tmp_path / "real.marker"
    decoy_marker = tmp_path / "decoy.marker"
    real_pi = real_dir / "pi"
    _write_pi_stub(real_pi, real_marker)

    # Work in an isolated cwd that holds the decoy named `pi`.
    work = tmp_path / "cwd"
    work.mkdir()
    decoy = work / "pi"
    _write_pi_stub(decoy, decoy_marker)

    env = {
        **os.environ,
        "PATH": f"{real_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
    }
    env.pop("PI_BIN", None)  # force default bare name "pi"

    proc = subprocess.run(
        [str(launcher), "--mode", "text", "-p", "ping"],
        cwd=str(work),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, (
        f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    # Decy first: the R-0 mutant (cwd prepended to PATH) execs the decoy and
    # must go RED on this assertion, not a secondary "real missing" message.
    assert not decoy_marker.exists(), (
        "cwd decoy `pi` executed — PATH cwd-prepending regression (R-0)"
    )
    assert real_marker.exists(), "real PATH pi should have been exec'd"


def test_r0_absolute_pi_bin_still_works(tmp_path: Path):
    """Negative control: absolute PI_BIN keeps working after the resolution fix."""
    checkout = _make_checkout_with_prompt(tmp_path)
    marker = tmp_path / "abs.marker"
    stub = tmp_path / "abs-pi"
    _write_pi_stub(stub, marker)
    proc = _run_launcher(
        ["--mode", "text"],
        launcher=checkout / "scripts" / "arb-pi-orch",
        env={"PI_BIN": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists()


def test_r0_unresolvable_pi_bin_exits_79(tmp_path: Path):
    """Bare unresolvable PI_BIN refuses with exit 79 (not a bare nonzero)."""
    checkout = _make_checkout_with_prompt(tmp_path)
    env = {
        **os.environ,
        "PI_BIN": "pi-definitely-not-on-path-r0-zzzz",
        "PATH": f"/usr/bin{os.pathsep}/bin",
    }
    proc = _run_launcher(
        launcher=checkout / "scripts" / "arb-pi-orch",
        env=env,
    )
    assert proc.returncode == PI_BIN_REFUSE_CODE, (
        f"expected {PI_BIN_REFUSE_CODE}, got {proc.returncode}\nstderr={proc.stderr}"
    )
    assert "pi-bin-unresolvable" in proc.stderr


# ---------------------------------------------------------------------------
# R-1 — positive criterion: launcher actually injects --append-system-prompt
# ---------------------------------------------------------------------------


def test_r1_launcher_injects_append_system_prompt_and_preserves_caller_args(
    tmp_path: Path,
):
    """Recorded argv must contain --append-system-prompt <abs prompt> and caller's args."""
    checkout = _make_checkout_with_prompt(tmp_path)
    scripts = checkout / "scripts"
    expected_prompt = (checkout / "prompts" / "arb-close-discipline.md").resolve()

    marker = tmp_path / "argv.marker"
    stub = tmp_path / "pi-stub"
    _write_pi_stub(stub, marker)

    caller_args = ["--mode", "text", "-p", "ping", "--append-system-prompt", "/role.md"]
    proc = _run_launcher(
        caller_args,
        launcher=scripts / "arb-pi-orch",
        env={"PI_BIN": str(stub)},
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.exists(), "stub never ran"
    recorded = marker.read_text(encoding="utf-8")
    # marker format: line1 "launched", then one arg per line (incl $0)
    argv_lines = recorded.splitlines()[1:]
    assert "--append-system-prompt" in argv_lines, (
        f"missing --append-system-prompt in argv:\n{recorded}"
    )
    # Last --append-system-prompt must be the close-discipline one (second append).
    # Find the final occurrence and assert the next token is the absolute prompt path.
    indices = [i for i, a in enumerate(argv_lines) if a == "--append-system-prompt"]
    assert indices, "flag absent"
    last = indices[-1]
    assert last + 1 < len(argv_lines), "flag with no following path"
    assert argv_lines[last + 1] == str(expected_prompt), (
        f"flag not followed by abs prompt path:\n"
        f"  got={argv_lines[last + 1]!r}\n  want={str(expected_prompt)!r}\n"
        f"full argv={argv_lines}"
    )
    # Caller's own args preserved (second-append property, not replace).
    for a in caller_args:
        assert a in argv_lines, f"caller arg lost: {a!r}\nargv={argv_lines}"
    # Exactly: caller's --append-system-prompt /role.md still present AND the
    # trailing close-discipline one — so at least two flag occurrences when
    # caller also supplied one.
    assert argv_lines.count("--append-system-prompt") >= 2


# ---------------------------------------------------------------------------
# A4-hermetic — anchors, staged absence, equality vs repo fixture (never skips)
# ---------------------------------------------------------------------------


def test_a4_hermetic_content_anchors_on_route_b():
    text = PROMPT_B.read_text(encoding="utf-8")
    for anchor in LIVE_ANCHORS:
        assert anchor in text, f"live anchor missing from route B: {anchor!r}"


def test_a4_hermetic_staged_bullets_absent_from_route_b():
    text = PROMPT_B.read_text(encoding="utf-8")
    for marker in STAGED_MARKERS:
        assert marker not in text, f"staged marker leaked into served file: {marker!r}"
    # NOTES still carries them (guard that we looked at the right strings).
    notes = NOTES.read_text(encoding="utf-8")
    for marker in STAGED_MARKERS:
        assert marker in notes, f"fixture drift: staged marker gone from NOTES: {marker!r}"


def test_a4_hermetic_route_a_fixture_equals_route_b():
    assert PROMPT_FIXTURE_A.is_file(), f"route-A fixture missing: {PROMPT_FIXTURE_A}"
    a = PROMPT_FIXTURE_A.read_bytes()
    b = PROMPT_B.read_bytes()
    assert _sha256_bytes(a) == _sha256_bytes(b)
    assert a == b
    assert len(b) > 0


# ---------------------------------------------------------------------------
# A4-host — NON-SKIPPING; missing route A is a FAILURE
# ---------------------------------------------------------------------------


def test_a4_host_route_a_matches_route_b():
    """Deployed-host closure. A missing/unreadable ~/.claude symlink FAILS (never skips)."""
    route_a = ROUTE_A_HOST
    if not route_a.exists() or not os.access(route_a, os.R_OK):
        pytest.fail(
            f"A4-host FAIL: missing or unreadable route A path={route_a} "
            f"(expected symlink to the served close-discipline file)"
        )
    try:
        a_bytes = route_a.read_bytes()  # follows symlink
    except OSError as exc:
        pytest.fail(f"A4-host FAIL: unreadable route A path={route_a}: {exc}")
    b_bytes = PROMPT_B.read_bytes()
    assert _sha256_bytes(a_bytes) == _sha256_bytes(b_bytes), (
        f"A4-host sha mismatch: A={_sha256_bytes(a_bytes)} B={_sha256_bytes(b_bytes)}"
    )
    assert a_bytes == b_bytes


# ---------------------------------------------------------------------------
# A5(i) — cold panel / build_task_prompt system_guidance has zero close-discipline
# ---------------------------------------------------------------------------


def test_a5_i_cold_panel_system_guidance_has_zero_close_discipline():
    sys.path.insert(0, str(ROOT / "src"))
    from agent_redis_bridge.protocol import build_task_prompt  # noqa: WPS433

    role_path = ROOT / "roles" / "team-seat.md"
    role = role_path.read_text(encoding="utf-8") if role_path.is_file() else "team seat"
    prompt = build_task_prompt("do the task", system_prompt=role)
    assert "<system_guidance>" in prompt
    for anchor in LIVE_ANCHORS:
        assert prompt.count(anchor) == 0, f"close-discipline leaked into system_guidance: {anchor!r}"
    # Sentinel from the served file must also be absent.
    assert "Close discipline" not in prompt


# ---------------------------------------------------------------------------
# A5(ii) — warm child loader seam via PRODUCTION createChildResourceLoader
# ---------------------------------------------------------------------------


def test_a5_ii_child_loader_append_sources_have_zero_close_discipline():
    """Invoke PiExtensions createChildResourceLoader (not a self-built double)."""
    if SDK_ENTRY is None:
        searched = "\n  ".join(str(r / _PI_SDK_REL) for r in _pi_sdk_search_roots())
        pytest.fail(
            "A5(ii) NOT RUN: pi SDK entry unreachable — @earendil-works/pi-coding-agent "
            "is not installed under any known node_modules root. Install it, or set "
            "ARB_PI_SDK_ENTRY to its dist/index.js.\nSearched:\n  "
            f"{searched}\n"
            "Do not substitute a top-level-key assertion."
        )
    if not PIEXT_RUNNER_TS.is_file():
        pytest.fail(
            f"A5(ii) NOT RUN: production factory unreachable at {PIEXT_RUNNER_TS}. "
            "Self-constructed DefaultResourceLoader is not an acceptable substitute."
        )

    # bun loads the .ts production factory; we pass the same SDK the host uses.
    node_script = textwrap.dedent(
        f"""\
        import {{ pathToFileURL }} from "node:url";

        const entry = {SDK_ENTRY.as_posix()!r};
        const runnerPath = {PIEXT_RUNNER_TS.as_posix()!r};
        const promptPath = {PROMPT_B.as_posix()!r};
        const anchors = [
          "proposal, not a close",
          "no polish exemption",
          "pinning test",
        ];

        const sdk = await import(pathToFileURL(entry).href);
        if (typeof sdk.DefaultResourceLoader !== "function") {{
          console.error("NO_LOADER");
          process.exit(2);
        }}
        const runner = await import(pathToFileURL(runnerPath).href);
        if (typeof runner.createChildResourceLoader !== "function") {{
          console.error("NO_FACTORY_EXPORT");
          process.exit(2);
        }}

        // PRODUCTION seam — not a separately constructed DefaultResourceLoader.
        const childLoader = await runner.createChildResourceLoader(
          sdk,
          {{ cwd: process.cwd() }},
          "a5ii-probe",
        );
        if (!childLoader) {{
          console.error("FACTORY_RETURNED_UNDEFINED");
          process.exit(2);
        }}
        // CAPABILITY CONTROL (restored r4): without this, a moved/renamed seam
        // yields [] and reads as "child is clean" — absence of the probe and
        // absence of the defect become the same green.
        if (typeof childLoader.getAppendSystemPrompt !== "function") {{
          console.error("NO_APPEND_ACCESSOR — cannot observe the seam; not a clean result");
          process.exit(4);
        }}
        const append = childLoader.getAppendSystemPrompt();
        const sys = childLoader.getSystemPrompt?.() ?? "";
        const joinedAppend = Array.isArray(append) ? append.join("\\n") : String(append ?? "");
        const childSurface = joinedAppend + "\\n" + String(sys ?? "");
        const hits = anchors.filter((a) => childSurface.includes(a));
        if (hits.length !== 0) {{
          console.error("CHILD_HAS_CLOSE_DISCIPLINE:" + hits.join(","));
          process.exit(3);
        }}
        console.log("CHILD_APPEND_BYTES=" + Buffer.byteLength(joinedAppend, "utf8"));
        console.log("CHILD_SYS_BYTES=" + Buffer.byteLength(String(sys ?? ""), "utf8"));
        console.log("USED_PRODUCTION_FACTORY=1");
        console.log("MUTANT_DETECTABLE=1");
        """
    )
    # bun can import the .ts runner; node --experimental-strip-types also works.
    # shutil.which FIRST: subprocess.run(["bun", ...]) raises FileNotFoundError when
    # bun is absent, so the fallback below was unreachable on a bun-less host (r4).
    proc = None
    if shutil.which("bun"):
        proc = subprocess.run(
            ["bun", "-e", node_script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    if proc is None or (proc.returncode != 0 and "bun" in (proc.stderr or "").lower()):
        proc = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", node_script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert proc.returncode == 0, (
        f"A5(ii) production factory probe failed rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "USED_PRODUCTION_FACTORY=1" in proc.stdout
    # The probe must prove it could SEE the seam. Without this, a renamed or removed
    # accessor returns nothing and the "no close-discipline found" result below is
    # vacuous (r4 finding: absence of the probe read as absence of the defect).
    assert "MUTANT_DETECTABLE=1" in proc.stdout, (
        "A5(ii) capability control absent — the probe cannot distinguish a clean "
        f"child from an unobservable seam.\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    m = re.search(r"CHILD_APPEND_BYTES=(\d+)", proc.stdout)
    assert m is not None, proc.stdout
    # Zero close-discipline: no anchor hits already asserted in the probe.
    # Append list should be empty under the production factory defaults.
    assert int(m.group(1)) == 0, (
        f"child append sources non-empty ({m.group(1)} bytes):\n{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# A5(iii) — project-b consoles must NOT carry the close-discipline flag
# ---------------------------------------------------------------------------


def test_a5_iii_project_b_consoles_have_no_close_discipline_flag():
    for console in PROJECT_B_CONSOLES:
        assert console.is_file(), f"missing console script: {console}"
        src = console.read_text(encoding="utf-8")
        assert "arb-close-discipline" not in src, (
            f"{console.name} carries close-discipline (negative control violated)"
        )
        assert "close-discipline" not in src, (
            f"{console.name} carries close-discipline token"
        )


# ---------------------------------------------------------------------------
# A7 — pin the served sha256 (a promotion moves this pin in the same commit)
# ---------------------------------------------------------------------------


def test_a7_served_sha256_pinned():
    digest = _sha256_file(PROMPT_B)
    assert digest == EXPECTED_SHA256, (
        f"A7 fail: served sha256 changed (bullet promoted or file drifted): {digest}"
    )
    assert PROMPT_B.stat().st_size == 1268


def test_a7_git_diff_on_served_file_is_empty():
    proc = subprocess.run(
        ["git", "diff", "--", "prompts/arb-close-discipline.md"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"A7 fail: served file has uncommitted diff:\n{proc.stdout}"


# ---------------------------------------------------------------------------
# A6 pure-evaluator oracle (host-scoped detector; pure logic must be reachable)
# ---------------------------------------------------------------------------


def test_a6_c_digest_oracle_independent_of_emitter():
    """A6(c): recompute sha256/bytes from a captured systemPrompt; match emitted fields.

    Drives the SHIPPED detector (.ts) via the thin eval CLI re-export — not a
    duplicated digest implementation. Constant-digest and off-by-one-length
    mutants applied to the .ts must fail this check.
    Does NOT compare to A7's served-artefact hash (different object).
    """
    if not PIEXT_DETECTOR.is_file():
        pytest.fail(
            f"A6(c) NOT RUN: shipped detector missing at {PIEXT_DETECTOR} "
            f"(detector not landed, or: {_piext_unreachable_detail()})"
        )
    if not PIEXT_EVAL_MJS.is_file():
        pytest.fail(
            f"A6(c) NOT RUN: thin eval CLI missing at {PIEXT_EVAL_MJS}"
        )

    # Full assembled system prompt — larger and different from the served artefact.
    artefact = PROMPT_B.read_text(encoding="utf-8")
    system_prompt = (
        "You are a coding assistant.\n\n"
        + artefact
        + "\n\n# Role\nYou orchestrate ARB closes.\n"
    )
    independent_sha = _sha256_bytes(system_prompt.encode("utf-8"))
    independent_bytes = len(system_prompt.encode("utf-8"))
    assert independent_bytes != 1268, "oracle must not collapse to the artefact size"

    proc = _run_eval_mjs(system_prompt)
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.strip().splitlines()[-1]
    assert line.startswith("close-discipline-served "), line
    m = re.search(r"sha256=([0-9a-f]{64}) bytes=(\d+) anchors=3/3", line)
    assert m is not None, f"emit line malformed: {line}"
    emitted_sha, emitted_bytes = m.group(1), int(m.group(2))
    assert emitted_sha == independent_sha, (
        f"A6(c) sha mismatch: emitted={emitted_sha} independent={independent_sha}"
    )
    assert emitted_bytes == independent_bytes, (
        f"A6(c) bytes mismatch: emitted={emitted_bytes} independent={independent_bytes}"
    )


def test_a6_absent_when_anchors_missing():
    if not PIEXT_DETECTOR.is_file() or not PIEXT_EVAL_MJS.is_file():
        pytest.fail(
            f"A6 absent-path NOT RUN: shipped detector or thin CLI missing "
            f"(ts={PIEXT_DETECTOR.is_file()} mjs={PIEXT_EVAL_MJS.is_file()}). "
            f"{_piext_unreachable_detail()}"
        )
    system_prompt = "You are a coding assistant. No discipline here."
    proc = _run_eval_mjs(system_prompt)
    assert proc.returncode == 0, proc.stderr
    assert "close-discipline-absent" in proc.stdout


def test_a6_b_path_string_fail_open_emits_absent():
    """A6(b): assembled prompt contains the literal path and no anchors → absent."""
    if not PIEXT_DETECTOR.is_file() or not PIEXT_EVAL_MJS.is_file():
        pytest.fail(
            f"A6(b) NOT RUN: shipped detector or thin CLI missing "
            f"(ts={PIEXT_DETECTOR.is_file()} mjs={PIEXT_EVAL_MJS.is_file()}). "
            f"{_piext_unreachable_detail()}"
        )
    dangling = str(ROOT / "prompts" / "arb-close-discipline.md")
    # Simulate pi resolvePromptInput fail-open: path string occupies the slot.
    system_prompt = f"You are a coding assistant.\n\n{dangling}\n"
    assert "proposal, not a close" not in system_prompt
    proc = _run_eval_mjs(system_prompt)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "close-discipline-absent" in out
    assert "close-discipline-served" not in out


# ---------------------------------------------------------------------------
# The PiExtensions resolver itself — a search that never finds anything is just
# a hardcoded path with extra steps, so prove it resolves.
# ---------------------------------------------------------------------------


def _make_fake_piext(tmp_path: Path) -> Path:
    root = tmp_path / "PiExtensions"
    (root / "extensions").mkdir(parents=True)
    (root / "extensions" / "arb-close-discipline.ts").write_text("// fake detector\n")
    return root


def test_piext_resolver_finds_a_checkout_on_the_search_path(tmp_path, monkeypatch):
    fake = _make_fake_piext(tmp_path)
    monkeypatch.delenv("ARB_PIEXT_ROOT", raising=False)
    monkeypatch.setattr(
        sys.modules[__name__], "_piext_search_roots", lambda: [tmp_path / "nope", fake]
    )
    assert _resolve_piext_root() == fake


def test_piext_resolver_ignores_a_root_without_the_detector(tmp_path, monkeypatch):
    # An empty directory of the right NAME must not shadow a real checkout —
    # otherwise a stray ~/PiExtensions silently disables these guards.
    empty = tmp_path / "PiExtensions"
    empty.mkdir()
    monkeypatch.delenv("ARB_PIEXT_ROOT", raising=False)
    monkeypatch.setattr(sys.modules[__name__], "_piext_search_roots", lambda: [empty])
    assert _resolve_piext_root() == _PIEXT_VOLUME_DEFAULT


def test_piext_resolver_honours_the_env_override(tmp_path, monkeypatch):
    fake = _make_fake_piext(tmp_path)
    monkeypatch.setenv("ARB_PIEXT_ROOT", str(fake))
    assert _resolve_piext_root() == fake


def test_piext_unreachable_detail_names_every_path_and_forbids_skip(monkeypatch):
    # The failure text is the whole point of the change: a red must say "not on
    # this host", not read as a broken assertion.
    detail = _piext_unreachable_detail()
    for root in _piext_search_roots():
        assert str(root) in detail
    assert "ARB_PIEXT_ROOT" in detail
    assert "pytest.skip" in detail  # the ARB-B9 warning stays attached
