from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agent_redis_bridge import gate_runner


ROOT = Path(__file__).resolve().parents[1]
RUN_GATE = ROOT / "scripts" / "run-gate"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "gate@test.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Gate Test"], check=True)
    (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture baseline"], check=True)
    return repo


@pytest.fixture
def fake_uv(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if len(sys.argv) != 3 or sys.argv[1] != 'run':\n"
        "    raise SystemExit('expected: uv run <gate>')\n"
        "os.execv(sys.executable, [sys.executable, sys.argv[2]])\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return bin_dir


def write_gate(project: Path, body: str) -> Path:
    gate = project / "gate.py"
    gate.write_text(
        "# /// script\n"
        "# requires-python = '>=3.11'\n"
        "# dependencies = []\n"
        "# ///\n"
        + body,
        encoding="utf-8",
    )
    return gate


def invoke(
    gate: Path,
    project: Path,
    fake_uv: Path,
    *extra: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_uv}{os.pathsep}{env.get('PATH', '')}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [
            sys.executable,
            str(RUN_GATE),
            "--gate",
            str(gate),
            "--project",
            str(project),
            *extra,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def passing_gate(project: Path) -> Path:
    return write_gate(
        project,
        "print('CHECK[ready]: class=invariant')\n"
        "print('PASS[ready]: fixture is ready')\n",
    )


def detached_gate(project: Path, marker: Path) -> Path:
    child_code = (
        "import os, time\n"
        "os.setsid()\n"
        "if os.fork() == 0:\n"
        "    devnull = os.open(os.devnull, os.O_RDWR)\n"
        "    for fd in (0, 1, 2):\n"
        "        os.dup2(devnull, fd)\n"
        "    time.sleep(0.8)\n"
        f"    open({str(marker)!r}, 'w').write('escaped')\n"
        "    os._exit(0)\n"
        "os._exit(0)\n"
    )
    return write_gate(
        project,
        "import os, time\n"
        "if os.fork() == 0:\n"
        f"    exec({child_code!r})\n"
        "time.sleep(10)\n",
    )


def git_admin_path(project: Path, rev_parse_arg: str) -> Path:
    raw_path = subprocess.run(
        ["git", "-C", str(project), "rev-parse", rev_parse_arg],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    path = Path(raw_path)
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def test_digest_refusal_and_repin_append_history(project: Path, fake_uv: Path) -> None:
    gate = passing_gate(project)
    first = invoke(gate, project, fake_uv, "--repin")
    assert first.returncode == 0, first.stderr
    first_digest = hashlib.sha256(gate.read_bytes()).hexdigest()

    gate.write_text(gate.read_text(encoding="utf-8") + "# repaired\n", encoding="utf-8")
    refused = invoke(gate, project, fake_uv)
    assert refused.returncode == 3
    assert "DIGEST REFUSAL" in refused.stderr
    assert first_digest in refused.stderr

    repaired = invoke(gate, project, fake_uv, "--repin")
    assert repaired.returncode == 0, repaired.stderr
    second_digest = hashlib.sha256(gate.read_bytes()).hexdigest()
    history = (project / "gate.py.sha256").read_text(encoding="utf-8").splitlines()
    assert len(history) == 2
    assert history[0].endswith(first_digest)
    assert history[1].endswith(second_digest)
    assert first_digest != second_digest


def test_environment_scrubs_parent_secret(project: Path, fake_uv: Path) -> None:
    gate = write_gate(
        project,
        "import os\n"
        "print('CHECK[secret]: class=invariant')\n"
        "if os.environ.get('RUN_GATE_CANARY_SECRET') is None:\n"
        "    print('PASS[secret]: parent secret is absent')\n"
        "else:\n"
        "    print('FAIL[secret]: expected no parent secret, found one, at environment — scrub it')\n"
        "    raise SystemExit(1)\n",
    )
    result = invoke(
        gate,
        project,
        fake_uv,
        "--repin",
        env_extra={"RUN_GATE_CANARY_SECRET": "must-not-cross-boundary"},
    )
    assert result.returncode == 0, result.stderr
    assert "PASS[secret]: parent secret is absent" in result.stdout
    assert "must-not-cross-boundary" not in result.stdout + result.stderr


def test_valid_baseline_classifies_each_check(project: Path, fake_uv: Path) -> None:
    gate = write_gate(
        project,
        "print('CHECK[new-behavior]: class=delta')\n"
        "print('CHECK[old-contract]: class=invariant')\n"
        "print('FAIL[new-behavior]: expected new behavior, found baseline, at tracked.txt — implement it')\n"
        "print('PASS[old-contract]: existing contract holds')\n"
        "raise SystemExit(1)\n",
    )
    summary = project.parent / "baseline.json"
    result = invoke(gate, project, fake_uv, "--repin", "--baseline", "--json", str(summary))
    assert result.returncode == 0, result.stderr
    assert "BASELINE-VALID" in result.stderr
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["verdict"] == "BASELINE-VALID"
    assert [item["class"] for item in payload["checks"]] == ["delta", "invariant"]
    assert [(item["id"], item["status"]) for item in payload["checks"]] == [
        ("new-behavior", "FAIL"),
        ("old-contract", "PASS"),
    ]


def test_delta_pass_on_baseline_is_red_invalid_and_names_check(
    project: Path, fake_uv: Path
) -> None:
    gate = write_gate(
        project,
        "print('CHECK[vacuous]: class=delta')\n"
        "print('PASS[vacuous]: this incorrectly passes before the build')\n",
    )
    result = invoke(gate, project, fake_uv, "--repin", "--baseline")
    assert result.returncode == 4
    assert "RED-INVALID" in result.stderr
    assert "vacuous" in result.stderr


def test_baseline_exemption_is_valid_and_loud(project: Path, fake_uv: Path) -> None:
    gate = write_gate(
        project,
        "print('CHECK[new-behavior]: class=delta')\n"
        "print('CHECK[docs-only]: class=delta baseline-exempt=no honest pre-change behavior exists')\n"
        "print('FAIL[new-behavior]: expected new behavior, found baseline, at tracked.txt — implement it')\n"
        "print('PASS[docs-only]: documentation invariant inspected')\n"
        "raise SystemExit(1)\n",
    )
    summary = project.parent / "exempt.json"
    result = invoke(gate, project, fake_uv, "--repin", "--baseline", "--json", str(summary))
    assert result.returncode == 0, result.stderr
    assert "BASELINE-EXEMPT[docs-only]: no honest pre-change behavior exists" in result.stderr
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["baseline_exemptions"] == [
        {"id": "docs-only", "reason": "no honest pre-change behavior exists"}
    ]


@pytest.mark.parametrize(
    "body",
    [
        (
            "print('CHECK[old-contract]: class=invariant')\n"
            "print('PASS[old-contract]: existing contract holds')\n"
        ),
        (
            "print('CHECK[docs-only]: class=delta baseline-exempt=no honest pre-change behavior exists')\n"
            "print('PASS[docs-only]: documentation invariant inspected')\n"
        ),
    ],
    ids=["all-invariant", "all-exempt"],
)
def test_baseline_without_non_exempt_delta_is_vacuous(
    project: Path, fake_uv: Path, body: str
) -> None:
    gate = write_gate(project, body)
    summary = project.parent / "vacuous.json"
    result = invoke(gate, project, fake_uv, "--repin", "--baseline", "--json", str(summary))
    assert result.returncode == 7
    assert "VACUOUS" in result.stderr
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["verdict"] == "VACUOUS"


def test_fail_line_passes_through_verbatim(project: Path, fake_uv: Path) -> None:
    fail_line = (
        "FAIL[result]: expected exit 0, found exit 7, at app.py — return success after writing output"
    )
    gate = write_gate(
        project,
        "print('CHECK[result]: class=delta')\n"
        f"print({fail_line!r})\n"
        "raise SystemExit(1)\n",
    )
    result = invoke(gate, project, fake_uv, "--repin")
    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines()[-1] == fail_line


def test_gate_project_write_is_detected(project: Path, fake_uv: Path) -> None:
    gate = write_gate(
        project,
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['GATE_PROJECT'], 'mutation.txt').write_text('changed')\n"
        "print('CHECK[clean]: class=invariant')\n"
        "print('PASS[clean]: claimed no mutation')\n",
    )
    result = invoke(gate, project, fake_uv, "--repin")
    assert result.returncode == 5
    assert "PROJECT MUTATION" in result.stderr
    assert "dirt fingerprints differ" in result.stderr


def test_gate_gitignored_write_is_detected(project: Path, fake_uv: Path) -> None:
    (project / ".gitignore").write_text("ignored-output.txt\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "ignore gate output"], check=True)
    gate = write_gate(
        project,
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['GATE_PROJECT'], 'ignored-output.txt').write_text('changed')\n"
        "print('CHECK[clean]: class=invariant')\n"
        "print('PASS[clean]: claimed no mutation')\n",
    )
    result = invoke(gate, project, fake_uv, "--repin")
    assert result.returncode == 5
    assert "PROJECT MUTATION" in result.stderr
    assert "dirt fingerprints differ" in result.stderr


def test_gate_hook_plant_in_linked_worktree_is_detected(
    project: Path, fake_uv: Path
) -> None:
    linked = project.parent / "linked"
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "--detach", str(linked), "HEAD"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    common_dir = git_admin_path(linked, "--git-common-dir")
    hook = common_dir / "hooks" / "post-checkout"
    gate = write_gate(
        linked,
        "from pathlib import Path\n"
        f"hook = Path({str(hook)!r})\n"
        "hook.parent.mkdir(parents=True, exist_ok=True)\n"
        "hook.write_text('#!/bin/sh\\nprintf planted > /tmp/run-gate-hook-fired\\n')\n"
        "hook.chmod(0o755)\n"
        "print('CHECK[clean]: class=invariant')\n"
        "print('PASS[clean]: claimed no mutation')\n",
    )
    result = invoke(gate, linked, fake_uv, "--repin")
    assert result.returncode == 5
    assert "PROJECT MUTATION" in result.stderr
    assert "dirt fingerprints differ" in result.stderr


def test_gate_common_dir_ref_write_is_detected(project: Path, fake_uv: Path) -> None:
    linked = project.parent / "linked-ref"
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "--detach", str(linked), "HEAD"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    common_dir = git_admin_path(linked, "--git-common-dir")
    head = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    ref = common_dir / "refs" / "heads" / "run-gate-evil"
    ref_contents = head + os.linesep
    gate = write_gate(
        linked,
        "from pathlib import Path\n"
        f"ref = Path({str(ref)!r})\n"
        "ref.parent.mkdir(parents=True, exist_ok=True)\n"
        f"ref.write_text({ref_contents!r})\n"
        "print('CHECK[clean]: class=invariant')\n"
        "print('PASS[clean]: claimed no mutation')\n",
    )
    result = invoke(gate, linked, fake_uv, "--repin")
    assert result.returncode == 5
    assert "PROJECT MUTATION" in result.stderr
    assert "dirt fingerprints differ" in result.stderr


def test_gate_worktree_git_dir_orig_head_write_is_detected(
    project: Path, fake_uv: Path
) -> None:
    linked = project.parent / "linked-orig-head"
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "--detach", str(linked), "HEAD"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    git_dir = git_admin_path(linked, "--git-dir")
    orig_head = git_dir / "ORIG_HEAD"
    orig_head_contents = "0" * 40 + os.linesep
    gate = write_gate(
        linked,
        "from pathlib import Path\n"
        f"Path({str(orig_head)!r}).write_text({orig_head_contents!r})\n"
        "print('CHECK[clean]: class=invariant')\n"
        "print('PASS[clean]: claimed no mutation')\n",
    )
    result = invoke(gate, linked, fake_uv, "--repin")
    assert result.returncode == 5
    assert "PROJECT MUTATION" in result.stderr
    assert "dirt fingerprints differ" in result.stderr


def test_gate_common_config_write_remains_detected(project: Path, fake_uv: Path) -> None:
    config = git_admin_path(project, "--git-common-dir") / "config"
    gate = write_gate(
        project,
        "from pathlib import Path\n"
        f"config = Path({str(config)!r})\n"
        "config.write_text(config.read_text() + '\\n# planted by gate\\n')\n"
        "print('CHECK[clean]: class=invariant')\n"
        "print('PASS[clean]: claimed no mutation')\n",
    )
    result = invoke(gate, project, fake_uv, "--repin")
    assert result.returncode == 5
    assert "PROJECT MUTATION" in result.stderr
    assert "dirt fingerprints differ" in result.stderr


def test_gate_external_core_hooks_path_write_is_detected(
    project: Path, fake_uv: Path
) -> None:
    hooks = project.parent / "external-hooks"
    hooks.mkdir()
    subprocess.run(
        ["git", "-C", str(project), "config", "core.hooksPath", str(hooks)], check=True
    )
    hook = hooks / "post-checkout"
    gate = write_gate(
        project,
        "from pathlib import Path\n"
        f"hook = Path({str(hook)!r})\n"
        "hook.write_text('#!/bin/sh\\nexit 0\\n')\n"
        "hook.chmod(0o755)\n"
        "print('CHECK[clean]: class=invariant')\n"
        "print('PASS[clean]: claimed no mutation')\n",
    )
    result = invoke(gate, project, fake_uv, "--repin")
    assert result.returncode == 5
    assert "PROJECT MUTATION" in result.stderr
    assert "dirt fingerprints differ" in result.stderr


def test_gate_worktree_config_write_is_detected(project: Path, fake_uv: Path) -> None:
    linked = project.parent / "linked-config"
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "--detach", str(linked), "HEAD"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(project), "config", "extensions.worktreeConfig", "true"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(linked), "config", "--worktree", "gate.marker", "baseline"],
        check=True,
    )
    config = git_admin_path(linked, "--git-dir") / "config.worktree"
    gate = write_gate(
        linked,
        "from pathlib import Path\n"
        f"config = Path({str(config)!r})\n"
        "config.write_text(config.read_text() + '\\n# planted by gate\\n')\n"
        "print('CHECK[clean]: class=invariant')\n"
        "print('PASS[clean]: claimed no mutation')\n",
    )
    result = invoke(gate, linked, fake_uv, "--repin")
    assert result.returncode == 5
    assert "PROJECT MUTATION" in result.stderr
    assert "dirt fingerprints differ" in result.stderr


def test_existing_tracked_dirt_does_not_false_fail(project: Path, fake_uv: Path) -> None:
    (project / "tracked.txt").write_text("pre-existing dirt\n", encoding="utf-8")
    gate = passing_gate(project)
    result = invoke(gate, project, fake_uv, "--repin")
    assert result.returncode == 0, result.stderr
    assert "PROJECT MUTATION" not in result.stderr


def test_resource_limiter_never_sets_user_wide_nproc(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[tuple[int, tuple[int, int]]] = []

    class FakeResource:
        RLIM_INFINITY = -1
        RLIMIT_CPU = 101
        RLIMIT_AS = 102
        RLIMIT_FSIZE = 103
        RLIMIT_NOFILE = 104
        RLIMIT_NPROC = 105

        @staticmethod
        def getrlimit(limit_id: int) -> tuple[int, int]:
            return (4096, FakeResource.RLIM_INFINITY)

        @staticmethod
        def setrlimit(limit_id: int, limits: tuple[int, int]) -> None:
            applied.append((limit_id, limits))

    monkeypatch.setattr(gate_runner, "resource", FakeResource)
    limiter = gate_runner._resource_limiter(10)
    assert limiter is not None
    limiter()
    assert applied
    assert all(limit_id != FakeResource.RLIMIT_NPROC for limit_id, _ in applied)


def test_timeout_kills_process_group(project: Path, fake_uv: Path) -> None:
    marker = project.parent / "escaped-child.txt"
    child_code = f"import time; time.sleep(0.8); open({str(marker)!r}, 'w').write('escaped')"
    gate = write_gate(
        project,
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(10)\n",
    )
    result = invoke(gate, project, fake_uv, "--repin", "--timeout", "0.15")
    assert result.returncode == 6
    assert "TIMEOUT" in result.stderr
    time.sleep(0.9)
    assert not marker.exists(), "a gate child survived the process-group timeout kill"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PID namespaces only")
def test_pid_namespace_reaps_double_forked_grandchild(
    project: Path, fake_uv: Path
) -> None:
    env = gate_runner._sandbox_env(project, ())
    env["PATH"] = f"{fake_uv}{os.pathsep}{env['PATH']}"
    process_prefix, process_isolation = gate_runner._process_prefix(env)
    if not process_prefix:
        pytest.skip(process_isolation)

    marker = project.parent / "detached-grandchild.txt"
    gate = detached_gate(project, marker)
    result = invoke(gate, project, fake_uv, "--repin", "--timeout", "0.15")
    assert result.returncode == 6
    assert "TIMEOUT" in result.stderr
    time.sleep(0.9)
    assert not marker.exists(), "a double-forked gate grandchild escaped the PID namespace"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS process-isolation gap only")
def test_macos_reports_double_fork_process_isolation_gap(
    project: Path, fake_uv: Path
) -> None:
    marker = project.parent / "documented-detached-grandchild.txt"
    gate = detached_gate(project, marker)
    result = invoke(gate, project, fake_uv, "--repin", "--timeout", "0.15")
    assert result.returncode == 6
    assert (
        "[run-gate] PROCESS ISOLATION GAP: UNAVAILABLE: macOS cannot reap "
        "double-forked grandchildren; a detached gate subprocess may outlive the timeout"
        in result.stderr
    )
    time.sleep(0.9)


def test_missing_uv_is_attributed_gate_error(project: Path, tmp_path: Path) -> None:
    gate = passing_gate(project)
    digest = hashlib.sha256(gate.read_bytes()).hexdigest()
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(empty_path)
    result = subprocess.run(
        [
            sys.executable,
            str(RUN_GATE),
            "--gate",
            str(gate),
            "--project",
            str(project),
            "--pin",
            digest,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 5
    assert "required executable 'uv' was not found on sanitized PATH" in result.stderr
