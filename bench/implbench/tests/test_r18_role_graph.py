from __future__ import annotations

import os
import json
import signal
import subprocess
import stat
import sys
import time

import pytest

from implbench.harness.scorer_sandbox import (
    ScorerInputError,
    ScorerSandbox,
    ScorerUidLauncher,
    ScorerProcess,
    ScorerRole,
    ScorerTopology,
    G4ReceiptBinding,
    _RoleGraph,
    _pid_gone,
    _env,
    build_g1_topology,
    role_graph_request,
    post_import_input,
)
from implbench.harness.scorer_launcher import _helper_target, _teardown_helper


def _topology(gate: str) -> ScorerTopology:
    # This directly exercises the real AF_UNIX protocol under the test UID.  UID
    # distinctness is enforced by the public topology builders; using one local
    # UID here lets an unprivileged test prove the kernel peer credential seam.
    roles = (
        (ScorerRole.KEYED_RUNNER, ScorerRole.BROKER, ScorerRole.SUBMITTED_PROGRAM)
        if gate == "G1"
        else (ScorerRole.COORDINATOR, ScorerRole.SUITE_RUNNER_BROKER, ScorerRole.SUBMITTED_CODE)
    )
    return ScorerTopology(gate, tuple(ScorerProcess(role, os.getuid(), _env(role=role)) for role in roles))


def _g4_binding() -> G4ReceiptBinding:
    return G4ReceiptBinding(
        cell_id="cell-" + "a" * 64, attempt_id="attempt-" + "b" * 32,
        commit_oid="c" * 40, public_suite_oid="d" * 40,
        public_suite_digest="e" * 64, public_suite_digest_version="public-suite-v1",
        controller_sequence=1, nonce="f" * 64,
    )


def _g4_binding_two() -> G4ReceiptBinding:
    return G4ReceiptBinding(
        cell_id="cell-" + "a" * 64, attempt_id="attempt-" + "b" * 32,
        commit_oid="1" * 40, public_suite_oid="d" * 40,
        public_suite_digest="e" * 64, public_suite_digest_version="public-suite-v1",
        controller_sequence=2, nonce="0" * 64,
    )


def _as(monkeypatch: pytest.MonkeyPatch, graph: _RoleGraph, role: ScorerRole) -> None:
    monkeypatch.delenv("IMPLBENCH_GRAPH_NONCE", raising=False)
    monkeypatch.delenv("IMPLBENCH_GRAPH_AUTH", raising=False)
    monkeypatch.delenv("IMPLBENCH_BATTERY_KEY", raising=False)
    for key, value in graph.environment_for(role).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("IMPLBENCH_SCORER_ROLE", role.value)


def _ready(monkeypatch: pytest.MonkeyPatch, graph: _RoleGraph, role: ScorerRole) -> None:
    _as(monkeypatch, graph, role)
    assert role_graph_request("ready") == {"ok": True}


def test_g1_real_socket_graph_routes_only_declared_calls_and_controller_owns_verdict(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _RoleGraph(tmp_path, _topology("G1"))
    graph.start()
    try:
        assert all(stat.S_ISSOCK(path.stat().st_mode) for path in graph._endpoints.values())
        for role in (ScorerRole.KEYED_RUNNER, ScorerRole.BROKER, ScorerRole.SUBMITTED_PROGRAM):
            _ready(monkeypatch, graph, role)
        assert graph.wait_ready(0.1)

        _as(monkeypatch, graph, ScorerRole.KEYED_RUNNER)
        assert role_graph_request("send", message_type="g1.request", payload={"input": "declared"}) == {"ok": True}
        _as(monkeypatch, graph, ScorerRole.BROKER)
        assert role_graph_request("receive")["message"]["type"] == "g1.request"
        assert role_graph_request("send", message_type="g1.execute", payload={"input": "declared"}) == {"ok": True}
        _as(monkeypatch, graph, ScorerRole.SUBMITTED_PROGRAM)
        assert role_graph_request("receive")["message"]["type"] == "g1.execute"
        assert role_graph_request("send", message_type="g1.response", payload={"output": "candidate"}) == {"ok": True}
        _as(monkeypatch, graph, ScorerRole.BROKER)
        assert role_graph_request("receive")["message"]["type"] == "g1.response"
        assert role_graph_request("send", message_type="g1.candidate", payload={"output": "candidate"}) == {"ok": True}
        _as(monkeypatch, graph, ScorerRole.KEYED_RUNNER)
        assert role_graph_request("receive")["message"]["type"] == "g1.candidate"
        result = {"g1": "PASS", "g3": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS"}
        assert role_graph_request("send", message_type="g1.verdict", payload=result) == {"ok": True}
        assert graph.controller_result() == result

        _as(monkeypatch, graph, ScorerRole.SUBMITTED_PROGRAM)
        with pytest.raises(ScorerInputError):
            role_graph_request("send", message_type="g1.verdict", payload=result)
    finally:
        graph.close()
    assert not graph._directory.exists()


def test_g4_real_socket_graph_keeps_controller_nonce_and_is_keyless(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _g4_binding()
    graph = _RoleGraph(tmp_path, _topology("G4"), g4_receipt_bindings=(binding,))
    graph.start()
    try:
        for role in (ScorerRole.COORDINATOR, ScorerRole.SUITE_RUNNER_BROKER, ScorerRole.SUBMITTED_CODE):
            _ready(monkeypatch, graph, role)
        _as(monkeypatch, graph, ScorerRole.COORDINATOR)
        assert "IMPLBENCH_BATTERY_KEY" not in os.environ
        assert "IMPLBENCH_GRAPH_AUTH" not in os.environ
        assert "IMPLBENCH_GRAPH_NONCE" not in os.environ
        assert role_graph_request("send", message_type="g4.call", payload={"call": "declared"}) == {"ok": True}
        _as(monkeypatch, graph, ScorerRole.SUITE_RUNNER_BROKER)
        assert role_graph_request("receive")["message"]["type"] == "g4.call"
        assert role_graph_request("send", message_type="g4.execute", payload={"call": "declared"}) == {"ok": True}
        _as(monkeypatch, graph, ScorerRole.SUBMITTED_CODE)
        assert role_graph_request("receive")["message"]["type"] == "g4.execute"
        assert role_graph_request("send", message_type="g4.response", payload={"result": "ok"}) == {"ok": True}
        _as(monkeypatch, graph, ScorerRole.SUITE_RUNNER_BROKER)
        assert role_graph_request("receive")["message"]["type"] == "g4.response"
        assert role_graph_request("send", message_type="g4.outcome", payload={"outcome": "PASS"}) == {"ok": True}
        _as(monkeypatch, graph, ScorerRole.COORDINATOR)
        assert role_graph_request("receive")["message"]["type"] == "g4.outcome"
        assert role_graph_request(
            "send", message_type="g4.receipt", payload={"commit_oid": binding.commit_oid, "outcome_enum": "PASS"}, nonce="untrusted-and-ignored",
        ) == {"ok": True}
        assert role_graph_request("send", message_type="g4.verdict", payload={"g4": "PASS"}) == {"ok": True}
        assert graph.controller_result() == {"g4": "PASS", "g4_receipts": (dict(
            cell_id=binding.cell_id, attempt_id=binding.attempt_id, commit_oid=binding.commit_oid,
            public_suite_oid=binding.public_suite_oid, public_suite_digest=binding.public_suite_digest,
            public_suite_digest_version=binding.public_suite_digest_version, outcome_enum="PASS",
            controller_sequence=binding.controller_sequence, nonce=binding.nonce,
        ),)}
    finally:
        graph.close()


def test_g4_complete_out_of_order_arrival_returns_import_order(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _g4_binding(), _g4_binding_two()
    graph = _RoleGraph(tmp_path, _topology("G4"), g4_receipt_bindings=(first, second))
    graph.start()
    try:
        for role in (ScorerRole.COORDINATOR, ScorerRole.SUITE_RUNNER_BROKER, ScorerRole.SUBMITTED_CODE):
            _ready(monkeypatch, graph, role)
        _as(monkeypatch, graph, ScorerRole.COORDINATOR)
        for binding, outcome in ((second, "FAIL"), (first, "PASS")):
            assert role_graph_request("send", message_type="g4.receipt", payload={"commit_oid": binding.commit_oid, "outcome_enum": outcome}) == {"ok": True}
        assert role_graph_request("send", message_type="g4.verdict", payload={"g4": "PASS"}) == {"ok": True}
        result = graph.controller_result()
        assert [row["commit_oid"] for row in result["g4_receipts"]] == [first.commit_oid, second.commit_oid]
        assert [row["outcome_enum"] for row in result["g4_receipts"]] == ["PASS", "FAIL"]
    finally:
        graph.close()


def test_graph_rejects_precreated_regular_file_endpoint(tmp_path) -> None:
    graph = _RoleGraph(tmp_path, _topology("G1"))
    endpoint = next(iter(graph._endpoints.values()))
    endpoint.write_text("not a socket", encoding="utf-8")
    try:
        with pytest.raises((OSError, ScorerInputError)):
            graph.start()
    finally:
        graph.close()


def test_graph_does_not_admit_a_result_without_all_authenticated_readiness(tmp_path) -> None:
    graph = _RoleGraph(tmp_path, _topology("G1"))
    graph.start()
    try:
        assert graph.wait_ready(0.02) is False
        with pytest.raises(ScorerInputError, match="controller-owned result"):
            graph.controller_result()
    finally:
        graph.close()


def test_graph_rejects_work_before_every_role_is_ready(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _RoleGraph(tmp_path, _topology("G1"))
    graph.start()
    try:
        _ready(monkeypatch, graph, ScorerRole.KEYED_RUNNER)
        with pytest.raises(ScorerInputError):
            role_graph_request("send", message_type="g1.request", payload={"input": "too soon"})
    finally:
        graph.close()


def test_topology_first_failure_does_not_wait_for_earlier_blocked_future_and_reaps_all(tmp_path) -> None:
    materialization = tmp_path / "imported"
    materialization.mkdir()
    topology = build_g1_topology(
        keyed_runner_uid=101, broker_uid=102, submitted_program_uid=103, battery_key="secret",
    )
    reaped: list[int] = []

    class Launcher:
        def run(self, argv, *, uid, cwd, env, timeout, max_output_bytes):
            del argv, cwd, env, timeout, max_output_bytes
            if uid == 101:
                time.sleep(0.5)
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            if uid == 102:
                return type("Completed", (), {"returncode": 7, "stdout": "", "stderr": ""})()
            time.sleep(0.5)
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    sandbox = ScorerSandbox(
        tmp_path, post_import_input(materialization, digest="c" * 64), topology,
        launcher=Launcher(), reaper=reaped.append,
    )
    started = time.monotonic()
    with pytest.raises(ScorerInputError, match="exited 7"):
        sandbox.run_topology({
            ScorerRole.KEYED_RUNNER: ["blocked-first"],
            ScorerRole.BROKER: ["fails-now"],
            ScorerRole.SUBMITTED_PROGRAM: ["blocked-last"],
        })
    assert time.monotonic() - started < 0.3
    assert set(reaped) == {101, 102, 103}


def test_production_launcher_makes_submitted_process_a_broker_child(tmp_path) -> None:
    """The submitted role's actual kernel parent is the trusted broker boundary."""
    child_ppid = tmp_path / "child-ppid"
    parent = [sys.executable, "-c", "import time; time.sleep(.2)"]
    child = [sys.executable, "-c", f"import os; open({str(child_ppid)!r}, 'w').write(str(os.getppid()))"]
    result = ScorerUidLauncher().run_parent_child(
        parent, uid=os.getuid(), cwd=tmp_path, env={"PATH": os.defpath}, child_argv=child,
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=2,
    )
    deadline = time.monotonic() + 1
    while not child_ppid.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert child_ppid.read_text() == str(result.broker_pid)


def test_privileged_pair_setup_is_barrier_gated_before_boundary_uid_drop() -> None:
    """Regression for r19d: role setuid happens in pre-drop stubs, never after it."""
    import inspect
    from implbench.harness import scorer_launcher

    source = inspect.getsource(scorer_launcher._broker_pair)
    assert "os.fork()" in source
    assert source.index("os.fork()") < source.index("os.setuid(config[\"uid\"])")
    assert "os.write(start_write, b\"S\")" in source
    assert "_spawn(" not in source


def test_pair_starts_the_broker_boundary_without_a_uid_dropping_preexec(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The complete production seam must not drop privilege before _broker_pair."""
    from implbench.harness import scorer_launcher

    captured: dict[str, object] = {}
    real_popen = scorer_launcher.subprocess.Popen

    def observe(*args, **kwargs):
        captured.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(scorer_launcher.subprocess, "Popen", observe)
    result = ScorerUidLauncher().run_parent_child(
        [sys.executable, "-c", "pass"], uid=os.getuid(), cwd=tmp_path,
        env={"PATH": os.defpath}, child_argv=[sys.executable, "-c", "pass"],
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=1,
    )
    assert result.broker.returncode == result.child.returncode == 0
    # This captures _pair's real Popen call, rather than inspecting only the
    # inner broker implementation where the old regression was invisible.
    assert "preexec_fn" not in captured


def test_teardown_helper_accepts_only_registered_pid_and_signal_operations() -> None:
    assert _helper_target(b"TB", 41, 42) == (signal.SIGTERM, 41)
    assert _helper_target(b"TC", 41, 42) == (signal.SIGTERM, 42)
    assert _helper_target(b"KB", 41, 42) == (signal.SIGKILL, 41)
    assert _helper_target(b"KC", 41, 42) == (signal.SIGKILL, 42)
    for command in (b"", b"TX", b"KX", b"T1", b"TERM"):
        with pytest.raises(RuntimeError, match="invalid teardown helper command"):
            _helper_target(command, 41, 42)


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_teardown_helper_pipe_reaps_and_fails_closed_on_invalid_operation() -> None:
    """Exercise the helper process/pipe lifecycle, not only its pure decoder."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)
        _teardown_helper(read_fd, 999_991, 999_992)
        os._exit(0)
    os.close(read_fd)
    try:
        os.write(write_fd, b"TB")  # PID allowlist maps to a harmless nonexistent PID.
        os.write(write_fd, b"TX")  # malformed operation must terminate the helper.
    finally:
        os.close(write_fd)
    waited, status = os.waitpid(pid, 0)
    assert waited == pid
    assert _status_for_test(status) != 0


def _status_for_test(status: int) -> int:
    return os.waitstatus_to_exitcode(status)


@pytest.mark.skipif(os.geteuid() != 0, reason="distinct production UID topology requires root")
def test_production_launcher_broker_boundary_has_configured_kernel_uid(tmp_path) -> None:
    """The PID parent assertion must also distinguish the broker UID from root."""
    tmp_path.chmod(0o777)
    report = tmp_path / "submitted-parent.json"
    broker_uid, child_uid = 65534, 65533
    child = [
        sys.executable,
        "-c",
        (
            "import json,os,subprocess; parent=os.getppid(); "
            "uid=int(subprocess.check_output(['ps','-o','uid=','-p',str(parent)], text=True)); "
            f"open({str(report)!r}, 'w').write(json.dumps({{'ppid':parent,'uid':uid}}))"
        ),
    ]
    result = ScorerUidLauncher().run_parent_child(
        [sys.executable, "-c", "import time; time.sleep(.2)"], uid=broker_uid,
        cwd=tmp_path, env={"PATH": os.defpath}, child_argv=child,
        child_uid=child_uid, child_env={"PATH": os.defpath}, timeout=2,
    )
    observed = json.loads(report.read_text())
    assert observed == {"ppid": result.broker_pid, "uid": broker_uid}


def test_production_launcher_returns_submitted_child_failure_after_broker_success(tmp_path) -> None:
    result = ScorerUidLauncher().run_parent_child(
        [sys.executable, "-c", "import time; time.sleep(.2)"], uid=os.getuid(), cwd=tmp_path,
        env={"PATH": os.defpath}, child_argv=[sys.executable, "-c", "import sys; sys.exit(7)"],
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=2,
    )
    assert result.broker.returncode == -15
    assert result.child.returncode == 7


def test_production_launcher_returns_submitted_child_signal_after_broker_success(tmp_path) -> None:
    result = ScorerUidLauncher().run_parent_child(
        [sys.executable, "-c", "import time; time.sleep(.2)"], uid=os.getuid(), cwd=tmp_path,
        env={"PATH": os.defpath}, child_argv=[sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"],
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=2,
    )
    assert result.broker.returncode == -15
    assert result.child.returncode == -15


def test_pair_roles_receive_no_controller_environment_or_status_authority(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROLLER_TEST_SECRET", "ambient-secret")
    report = tmp_path / "roles.jsonl"
    probe = (
        "import json,os; p=" + repr(str(report)) + "; probes=[]; "
        "fds=sorted(int(fd) for fd in os.listdir('/dev/fd')); "
        "exec(\"for fd in fds:\\n if fd > 2:\\n  try:\\n   target=os.readlink('/dev/fd/'+str(fd)); os.write(fd,b'x'); probes.append((fd,target,True))\\n  except OSError:\\n   probes.append((fd,'transient-or-closed',False))\"); "
        "open(p,'a').write(json.dumps({'env':dict(os.environ),'fds':fds,'probes':probes})+'\\n')"
    )
    result = ScorerUidLauncher().run_parent_child(
        [sys.executable, "-c", probe], uid=os.getuid(), cwd=tmp_path,
        env={"PATH": os.defpath}, child_argv=[sys.executable, "-c", probe],
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=2,
    )
    assert result.broker.returncode == result.child.returncode == 0
    reports = [json.loads(line) for line in report.read_text().splitlines()]
    assert len(reports) == 2
    for value in reports:
        assert "ambient-secret" not in value["env"].values()
        assert "IMPLBENCH_GRAPH_AUTH" not in value["env"]
        assert "IMPLBENCH_GRAPH_NONCE" not in value["env"]
        assert "IMPLBENCH_BATTERY_KEY" not in value["env"]
        # There is no named config or status descriptor exposed to either role.
        assert "IMPLBENCH_LAUNCHER_CONFIG_FD" not in value["env"]
        # /dev/fd itself may temporarily consume one directory descriptor while
        # the payload probes it.  Every non-stdio descriptor must nevertheless
        # reject writes, proving no config/status/helper/battery authority leaks.
        assert all(not writable for _fd, _target, writable in value["probes"])


def test_pair_supervisor_reports_real_nonzero_after_forged_zero_output(tmp_path) -> None:
    forged = "import sys; print('{\\\"exit_code\\\":0}'); sys.exit(9)"
    result = ScorerUidLauncher().run_parent_child(
        [sys.executable, "-c", "import time; time.sleep(.1)"], uid=os.getuid(), cwd=tmp_path,
        env={"PATH": os.defpath}, child_argv=[sys.executable, "-c", forged],
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=2,
    )
    assert result.broker.returncode == -15
    assert result.child.returncode == 9


def test_pair_failed_child_terminates_a_blocked_broker_without_outer_timeout(tmp_path) -> None:
    started = time.monotonic()
    result = ScorerUidLauncher().run_parent_child(
        [sys.executable, "-c", "import time; time.sleep(60)"], uid=os.getuid(), cwd=tmp_path,
        env={"PATH": os.defpath}, child_argv=[sys.executable, "-c", "import sys; sys.exit(7)"],
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=.5,
    )
    assert time.monotonic() - started < .5
    assert result.broker.returncode == -15
    assert result.child.returncode == 7


def test_pair_timeout_cancels_and_reaps_the_full_supervised_group(tmp_path) -> None:
    parent_pid, child_pid = tmp_path / "broker.pid", tmp_path / "child.pid"
    parent = [sys.executable, "-c", f"import os,time; open({str(parent_pid)!r}, 'w').write(str(os.getpid())); time.sleep(60)"]
    child = [sys.executable, "-c", f"import os,time; open({str(child_pid)!r}, 'w').write(str(os.getpid())); time.sleep(60)"]
    started = time.monotonic()
    result = ScorerUidLauncher().run_parent_child(
        parent, uid=os.getuid(), cwd=tmp_path, env={"PATH": os.defpath}, child_argv=child,
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=.5,
    )
    assert time.monotonic() - started < 2  # no observer-style 30 second wait
    assert result.broker.returncode == result.child.returncode == -15
    # The controller deadline records the roles it directly observed live;
    # signal exits alone are ambiguous attribution.
    assert result.execution_timeout_roles == frozenset({"broker", "child"})
    assert result.output_limit_role is None
    deadline = time.monotonic() + 1
    while (not parent_pid.exists() or not child_pid.exists()) and time.monotonic() < deadline:
        time.sleep(.01)
    for path in (parent_pid, child_pid):
        assert path.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(path.read_text()), 0)


def test_pair_child_output_limit_is_closed_typed_attribution(tmp_path) -> None:
    result = ScorerUidLauncher().run_parent_child(
        [sys.executable, "-c", "import time; time.sleep(2)"], uid=os.getuid(), cwd=tmp_path,
        env={"PATH": os.defpath},
        child_argv=[sys.executable, "-c", "import sys; sys.stdout.write('x' * 4097)"],
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=1, max_output_bytes=4096,
    )
    assert result.output_limit_role == "child"
    assert result.execution_timeout_roles == frozenset()


def test_pair_failed_broker_reaps_a_term_ignoring_submitted_role_before_outer_timeout(tmp_path) -> None:
    """A failed broker cannot strand a submitted role behind the outer timeout."""
    child_pid = tmp_path / "blocked-child.pid"
    blocked_child = [
        sys.executable,
        "-c",
        f"import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({str(child_pid)!r}, 'w').write(str(os.getpid())); time.sleep(60)",
    ]
    started = time.monotonic()
    result = ScorerUidLauncher().run_parent_child(
        [sys.executable, "-c", "import time,sys; time.sleep(.15); sys.exit(9)"], uid=os.getuid(), cwd=tmp_path,
        env={"PATH": os.defpath}, child_argv=blocked_child,
        child_uid=os.getuid(), child_env={"PATH": os.defpath}, timeout=1.2,
    )
    assert time.monotonic() - started < .8
    assert result.broker.returncode == 9
    assert result.child.returncode == -signal.SIGKILL
    deadline = time.monotonic() + .5
    while not child_pid.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert child_pid.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid.read_text()), 0)


def test_pair_helper_death_fallback_reaps_registered_term_ignoring_role(tmp_path) -> None:
    """The outer privileged fallback owns the exact pre-drop registration."""
    launcher = ScorerUidLauncher()
    child_pid_path = tmp_path / "helper-death-child.pid"
    config = {
        "argv": [sys.executable, "-c", "import time; time.sleep(60)"], "uid": os.getuid(),
        "cwd": str(tmp_path), "env": {"PATH": os.defpath},
        "child_argv": [sys.executable, "-c", (
            f"import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"open({str(child_pid_path)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
        )],
        "child_uid": os.getuid(), "child_env": {"PATH": os.defpath},
        "max_output_bytes": 64 * 1024, "execution_timeout_s": .3,
    }
    config_fd = launcher._config_pipe(config)
    registration_read, registration_write = os.pipe()
    os.set_blocking(registration_read, False)
    process = subprocess.Popen(
        [sys.executable, "-m", "implbench.harness.scorer_launcher", "--config-fd", str(config_fd),
         "--supervisor-fd", str(registration_write)],
        cwd=tmp_path, env=launcher._launcher_environment(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        pass_fds=(config_fd, registration_write),
    )
    os.close(config_fd); os.close(registration_write)
    try:
        registered: tuple[int, ...] = ()
        deadline = time.monotonic() + 1
        while not registered and time.monotonic() < deadline:
            registered = launcher._supervisor_registration(registration_read)
            time.sleep(.01)
        assert len(registered) == 4
        os.kill(registered[1], signal.SIGKILL)  # only the exact helper PID
        stdout, _stderr = process.communicate(timeout=2)
        assert process.returncode != 0 and stdout
        launcher._terminate(process, registered)
        for pid in registered:
            assert _pid_gone(pid)
        assert child_pid_path.exists()
    finally:
        os.close(registration_read)


def test_pid_gone_rejects_an_unreaped_zombie() -> None:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    try:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            state = subprocess.check_output(["ps", "-o", "stat=", "-p", str(pid)], text=True).strip()
            if state.startswith("Z"):
                break
            time.sleep(.01)
        assert state.startswith("Z")
        assert not _pid_gone(pid)
    finally:
        os.waitpid(pid, 0)
