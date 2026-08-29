"""Permanent structural gate for the B01--B22 production ledger.

This is intentionally an AST gate, not a word-presence check.  It only accepts
the concrete factory/call graph which composes the repository helper, child
crossings, authenticated release, managed ACL, preflight, and scorer sink.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]


class CompositionGateError(AssertionError):
    pass


def _classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _functions(node: ast.AST) -> dict[str, ast.FunctionDef]:
    return {child.name: child for child in getattr(node, "body", ()) if isinstance(child, ast.FunctionDef)}


def _called(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(call, ast.Call)
        and ((isinstance(call.func, ast.Name) and call.func.id == name)
             or (isinstance(call.func, ast.Attribute) and call.func.attr == name))
        for call in ast.walk(node)
    )


def _nontrivial(function: ast.FunctionDef, label: str) -> None:
    body = [node for node in function.body if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Constant)]
    if not body or all(isinstance(node, ast.Pass) or (isinstance(node, ast.Return) and node.value is None) for node in body):
        raise CompositionGateError(f"{label} is a no-op")


def _method(classes: dict[str, ast.ClassDef], class_name: str, method: str) -> ast.FunctionDef:
    function = _functions(classes.get(class_name, ast.Module(body=[], type_ignores=[]))).get(method)
    if function is None:
        raise CompositionGateError(f"{class_name}.{method} is missing")
    return function


def validate_production_composition(source: str) -> None:
    """Map every named production plane to executable, non-no-op AST structure."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CompositionGateError("production source is not parseable") from exc
    classes = _classes(tree)
    helper = classes.get("_SystemPlaneProvisioner")
    cell = classes.get("_ProductionCell")
    if helper is None or cell is None:
        raise CompositionGateError("production helper/cell factory is missing")
    for node in ast.walk(cell):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Popen" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"):
            raise CompositionGateError("direct subprocess launch in production factory")
    for method, action in (("reserve_identities", "reserve"), ("provision_planes", "provision"),
                           ("start_seat_daemon", "start-seat"), ("stop_seat_daemon", "stop-seat"),
                           ("prove_absent", "census")):
        function = _method(classes, "_SystemPlaneProvisioner", method)
        _nontrivial(function, f"plane helper {method}")
        if not _called(function, "_call") or not any(isinstance(node, ast.Constant) and node.value == action for node in ast.walk(function)):
            raise CompositionGateError(f"plane helper {method} does not bind {action}")
    if not _called(_method(classes, "_SystemPlaneProvisioner", "start_seat_daemon"), "_probe_seat_endpoint"):
        raise CompositionGateError("control/tool seat IPC is not probed")
    probe = _method(classes, "_SystemPlaneProvisioner", "_probe_seat_endpoint")
    if not _called(probe, "connect") or not _called(probe, "sendall") or not _called(probe, "recv"):
        raise CompositionGateError("seat probe is not a bounded IPC protocol")
    git = _method(classes, "_ProductionCell", "open_attempt_git_service")
    importer = _method(classes, "_ProductionCell", "import_descriptor_child")
    if not (_called(git, "ChildAttemptGitServiceServer") and _called(git, "helper_spawn")):
        raise CompositionGateError("Git child does not cross the plane helper")
    if not (_called(importer, "import_from_descriptor_child") and _called(importer, "helper_spawn")):
        raise CompositionGateError("importer child does not cross the plane helper")
    release = _method(classes, "_ProductionCell", "append_pre_scorer_attestation")
    if not _called(release, "append_pre_scorer_attestation") or not any(isinstance(node, ast.Return) for node in ast.walk(release)):
        raise CompositionGateError("pre-scorer authenticated reread wrapper is missing")
    post_release = _method(classes, "_ProductionCell", "append_post_g4_attestation")
    if not _called(post_release, "append_post_g4_attestation") or not any(isinstance(node, ast.Return) for node in ast.walk(post_release)):
        raise CompositionGateError("post-G4 authenticated reread wrapper is missing")
    if "ValkeyACLBackend" not in classes or not any(isinstance(node, ast.FunctionDef) and node.name == "run_production_preflight" for node in tree.body):
        raise CompositionGateError("managed ACL or production preflight is missing")
    helper_functions = _functions(tree)
    for name in ("_reserve_identities", "_start_seats", "_stop_seats", "_processes", "_seat_loop"):
        function = helper_functions.get(name)
        if function is None:
            raise CompositionGateError(f"repository plane helper {name} is missing")
        _nontrivial(function, f"repository plane helper {name}")
    if not (_called(helper_functions["_start_seats"], "fork") and _called(helper_functions["_start_seats"], "socketpair")
            and _called(helper_functions["_seat_loop"], "accept")):
        raise CompositionGateError("repository plane helper has no real control/tool process protocol")
    controller = classes.get("ScoredCloseRuntime")
    if controller is None:
        raise CompositionGateError("scored close controller is missing")
    close = _method(classes, "ScoredCloseRuntime", "import_and_score")
    if not (_called(close, "append_pre_scorer") and _called(close, "canonical_json_bytes") and _called(close, "scorer")):
        raise CompositionGateError("B08 authenticated scorer release is not composed")
    if not (_called(close, "append_attestation") and any(
        isinstance(node, ast.Constant) and node.value == "append_post_g4_attestation"
        for node in ast.walk(close)
    )):
        raise CompositionGateError("B08 post-G4 durability boundary is not composed")
    scorer_factory = helper_functions.get("build_production_scorer")
    if scorer_factory is None:
        raise CompositionGateError("production scorer factory is missing")
    for call in (
        "build_g1_topology", "build_g4_topology", "ScorerSandbox", "generate_profile",
        "run_topology", "delete_tree_descriptor_safe",
    ):
        if not _called(scorer_factory, call):
            raise CompositionGateError(f"production scorer factory does not compose {call}")
    scorer_profiles = [
        node for node in ast.walk(scorer_factory)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id == "generate_profile")
             or (isinstance(node.func, ast.Attribute) and node.func.attr == "generate_profile"))
    ]
    if not any(
        {keyword.arg for keyword in call.keywords} >= {"process_exec_paths", "runtime_read_paths"}
        for call in scorer_profiles
    ):
        raise CompositionGateError("production scorer profile cannot execute its pinned runtime")


def validate_scorer_release(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CompositionGateError("scorer source is not parseable") from exc
    functions = _functions(tree)
    drain = functions.get("_drain")
    normal = functions.get("_normal")
    pair = functions.get("_broker_pair")
    if drain is None or normal is None or pair is None:
        raise CompositionGateError("scorer launcher sink functions are missing")
    if not (_called(drain, "TemporaryFile") and _called(drain, "write") and _called(drain, "close")):
        raise CompositionGateError("raw scorer output is not directly sunk in launcher")
    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "communicate" for function in (drain, normal, pair) for node in ast.walk(function)):
        raise CompositionGateError("scorer role bytes are materialized with communicate")
    classes = _classes(tree)
    sandbox = classes.get("ScorerSandbox")
    if sandbox is None:
        raise CompositionGateError("scorer sandbox is missing")
    for method in ("run", "run_parent_child"):
        function = _method(classes, "ScorerSandbox", method)
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "communicate" for node in ast.walk(function)):
            raise CompositionGateError("scorer role bytes are materialized with communicate")
        if not any(isinstance(node, ast.Attribute) and node.attr in {"stdout", "stderr"} for node in ast.walk(function)):
            raise CompositionGateError(f"{method} does not reject raw launcher release")
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == "ScorerRunResult") or (isinstance(node.func, ast.Attribute) and node.func.attr == "ScorerRunResult"))):
            if any(isinstance(arg, ast.Attribute) and arg.attr in {"stdout", "stderr"} for arg in call.args):
                raise CompositionGateError("raw scorer output can cross the controller boundary")
    launcher = classes.get("ScorerUidLauncher")
    if launcher is None:
        raise CompositionGateError("scorer UID launcher is missing")
    command = _method(classes, "ScorerUidLauncher", "_command")
    command_strings = {
        node.value for node in ast.walk(command)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    if "/usr/bin/sandbox-exec" not in command_strings or "implbench.harness.scorer_profile_helper" not in command_strings:
        raise CompositionGateError("scorer launcher does not bind enforcement and structural profiles")
    role_graph = classes.get("_RoleGraph")
    if role_graph is None:
        raise CompositionGateError("scorer role graph is missing")
    graph_start = _method(classes, "_RoleGraph", "start")
    graph_handle = _method(classes, "_RoleGraph", "_handle")
    if not (_called(graph_start, "bind") and _called(graph_start, "listen") and _called(graph_handle, "_peer_uid")):
        raise CompositionGateError("scorer role graph lacks kernel-authenticated socket IPC")
    if not _called(normal, "_spawn") or not _called(pair, "fork"):
        raise CompositionGateError("scorer launcher does not create both normal and paired role processes")


def validate_core_plane_composition(
    bridge_source: str,
    dispatch_source: str,
    runtime_source: str,
    plane_source: str,
    dispatcher_source: str,
    pi_broker_source: str,
) -> None:
    """Require the scored production path to cross the core control/tool processes."""

    bridge_classes = _classes(ast.parse(bridge_source))
    bind = _method(bridge_classes, "Bridge", "_bind_scored_tool_plane")
    if _called(bind, "RemoteGitService") or _called(bind, "from_git_service"):
        raise CompositionGateError("bridge constructs the Git-backed tool plane in-process")
    if not (_called(bind, "from_endpoint") or any(
        isinstance(node, ast.Attribute) and node.attr == "from_endpoint" for node in ast.walk(bind)
    )):
        raise CompositionGateError("bridge does not bind the external tool-plane endpoint")
    scored_control = _method(bridge_classes, "ScoredBridgeControl", "run")
    if not _called(scored_control, "build_engine"):
        raise CompositionGateError("external scored bridge control does not own engine construction")

    dispatch_functions = _functions(ast.parse(dispatch_source))
    run_task = dispatch_functions.get("run_task")
    dispatch = dispatch_functions.get("_dispatch")
    if run_task is None or dispatch is None:
        raise CompositionGateError("scored dispatch entry points are missing")
    lifecycle_names = {
        node.value for node in ast.walk(run_task)
        if isinstance(node, ast.Constant) and node.value in {"start_attempt_planes", "dispatch_through_control"}
    }
    lifecycle_names.update(
        name for name in ("start_attempt_planes", "dispatch_through_control") if _called(run_task, name)
    )
    if lifecycle_names != {"start_attempt_planes", "dispatch_through_control"}:
        raise CompositionGateError("scored dispatch does not cross the production control lifecycle")
    scored_fail_closed = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "scored"
        and any(isinstance(child, ast.Raise) for child in ast.walk(node))
        for node in dispatch.body
    )
    if not scored_fail_closed:
        raise CompositionGateError("host subprocess dispatch remains reachable for scored work")

    runtime_classes = _classes(ast.parse(runtime_source))
    start = _method(runtime_classes, "_ProductionCell", "start_attempt_planes")
    launch_roles = {
        node.value
        for node in ast.walk(start)
        if isinstance(node, ast.Constant) and node.value in {"control", "tool"}
    }
    helper_calls = sum(
        1 for node in ast.walk(start)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id == "helper_spawn")
             or (isinstance(node.func, ast.Attribute) and node.func.attr == "helper_spawn"))
    )
    if launch_roles != {"control", "tool"} or helper_calls < 2 or not _called(start, "_authority_descriptor"):
        raise CompositionGateError("production cell does not fork/exec both descriptor-bound planes")
    if not _called(start, "open_control_secret_for_cell") or not all(
        token in runtime_source for token in ("--config-fd", "--secret-fd", "config_digest")
    ):
        raise CompositionGateError("production control launch does not bind config and secret descriptors")
    if "--listener-fd" not in runtime_source or not (_called(start, "bind") and _called(start, "listen")):
        raise CompositionGateError("production control launch does not pre-bind the control listener for inheritance")
    dispatch_control = _method(runtime_classes, "_ProductionCell", "dispatch_through_control")
    if not all(_called(dispatch_control, call) for call in ("socket", "connect", "sendall", "recv")) or not any(
        isinstance(node, ast.Attribute) and node.attr == "control_endpoint"
        for node in ast.walk(dispatch_control)
    ):
        raise CompositionGateError("production control dispatch does not cross its bounded socket")

    plane_functions = _functions(ast.parse(plane_source))
    tool = plane_functions.get("_tool")
    control = plane_functions.get("_control")
    if tool is None or control is None:
        raise CompositionGateError("core scored plane entry points are missing")
    if not (_called(tool, "RemoteGitService") and _called(tool, "from_git_service")):
        raise CompositionGateError("external tool process does not own the Git-backed broker")
    if not _called(tool, "execute_tool") or not all(
        f'"{name}"' in plane_source and f'name: "{name}"' in pi_broker_source
        for name in ("read", "write", "edit", "bash")
    ):
        raise CompositionGateError("external tool process does not implement the scored model tool surface")
    if not (_called(tool, "_project_model_text") and _called(tool, "_project_model_streams")):
        raise CompositionGateError("external tool process does not bound model-visible results")
    if not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_project_model_result"
        and any(
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id == "execute_tool"
            for arg in node.args
        )
        for node in ast.walk(tool)
    ):
        raise CompositionGateError("external tool process does not bound every model-visible tool result")
    serve = plane_functions.get("_serve")
    if serve is None or not _called(serve, "chmod") or not any(
        isinstance(node, ast.Constant) and node.value == 0o660 for node in ast.walk(tool)
    ):
        raise CompositionGateError("external plane socket is not shared-GID accessible")
    serve_params = {arg.arg for arg in (*serve.args.args, *serve.args.kwonlyargs)}
    if (
        "listener_fd" not in serve_params
        or not _called(serve, "getsockname")
        or not any(isinstance(node, ast.Attribute) and node.attr == "SO_TYPE" for node in ast.walk(serve))
    ):
        raise CompositionGateError("control plane does not validate and serve an inherited pre-bound listener")
    if not (_called(control, "from_endpoint") and _called(control, "ScoredBridgeControl") and _called(control, "_control_inputs")):
        raise CompositionGateError("external control process does not own the engine/client crossing")
    if "--listener-fd" not in plane_source or not any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "_serve")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "_serve")
        )
        and any(keyword.arg == "listener_fd" for keyword in node.keywords)
        for node in ast.walk(control)
    ):
        raise CompositionGateError("external control process does not serve the inherited control listener")
    if "tool_capability" in dispatcher_source or "TOOL_CAPABILITY" in dispatcher_source:
        raise CompositionGateError("dispatcher can serialize Git authority into the control envelope")


def _production_sources() -> str:
    files = (
        "bench/implbench/harness/runtime.py",
        "bench/implbench/harness/controller.py",
        "bench/implbench/harness/readiness.py",
        "bench/implbench/plane_helper.py",
    )
    return "\n\n".join((ROOT / path).read_text(encoding="utf-8") for path in files)


def test_static_composition_gate_accepts_production_sources() -> None:
    validate_production_composition(_production_sources())
    validate_scorer_release((ROOT / "bench/implbench/harness/scorer_launcher.py").read_text(encoding="utf-8") + "\n" + (ROOT / "bench/implbench/harness/scorer_sandbox.py").read_text(encoding="utf-8"))
    validate_core_plane_composition(
        (ROOT / "src/agent_redis_bridge/bridge.py").read_text(encoding="utf-8"),
        (ROOT / "bench/implbench/harness/dispatch.py").read_text(encoding="utf-8"),
        (ROOT / "bench/implbench/harness/runtime.py").read_text(encoding="utf-8"),
        (ROOT / "src/agent_redis_bridge/scored_plane.py").read_text(encoding="utf-8"),
        (ROOT / "scripts/agent-dispatch").read_text(encoding="utf-8"),
        (ROOT / "tools/pi-sdk-host/cell-broker.mjs").read_text(encoding="utf-8"),
    )


@pytest.mark.parametrize("source, message", [
    ("required words: helper_spawn import_from_descriptor_child append_pre_scorer_attestation", "production source|helper/cell"),
    ("import subprocess\nclass _ProductionCell:\n def x(self): subprocess.Popen(['bad'])", "helper/cell"),
    ("class _SystemPlaneProvisioner:\n def reserve_identities(self): pass\n", "helper/cell"),
])
def test_static_composition_gate_red_for_nonstructural_or_noop_production(source: str, message: str) -> None:
    with pytest.raises(CompositionGateError, match=message):
        validate_production_composition(source)


def test_static_composition_gate_red_for_raw_output_materialization() -> None:
    source = """
import subprocess
def _drain(): pass
def _normal(): pass
def _broker_pair(): pass
class ScorerSandbox:
 def run(self): return subprocess.Popen(['x']).communicate()
 def run_parent_child(self): return None
"""
    with pytest.raises(CompositionGateError, match="directly sunk|materialized"):
        validate_scorer_release(source)


def test_static_scorer_gate_red_for_missing_profile_and_role_graph() -> None:
    source = """
import tempfile
def _drain():
 sink = tempfile.TemporaryFile(); sink.write(b'x'); sink.close()
def _normal(): _spawn()
def _broker_pair(): fork()
class ScorerSandbox:
 def run(self):
  value.stdout; value.stderr
  return ScorerRunResult(0, 0, '', '')
 def run_parent_child(self):
  value.stdout; value.stderr
  return ScorerRunResult(0, 0, '', '')
"""
    with pytest.raises(CompositionGateError, match="UID launcher"):
        validate_scorer_release(source)


def test_static_core_gate_red_for_in_process_git_broker() -> None:
    bridge = """
class Bridge:
 def _bind_scored_tool_plane(self):
  service = RemoteGitService()
  return CellToolPlaneBroker.from_git_service(service)
class ScoredBridgeControl:
 def run(self): build_engine()
"""
    dispatch = """
def run_task():
 start_attempt_planes(); dispatch_through_control()
def _dispatch(scored):
 if scored: raise ValueError()
"""
    runtime = """
class _ProductionCell:
 def start_attempt_planes(self):
  _authority_descriptor(); open_control_secret_for_cell(); build_launch_spec('tool'); build_launch_spec('control'); helper_spawn(); helper_spawn(); print('--config-fd --secret-fd config_digest')
"""
    plane = """
def _tool(): RemoteGitService(); CellToolPlaneBroker.from_git_service(); execute_tool(); print("read", "write", "edit", "bash")
def _control(): CellToolPlaneBroker.from_endpoint(); _control_inputs(); ScoredBridgeControl()
"""
    with pytest.raises(CompositionGateError, match="in-process"):
        validate_core_plane_composition(bridge, dispatch, runtime, plane, "agent-dispatch", 'name: "read" name: "write" name: "edit" name: "bash"')


def test_static_core_gate_red_for_scored_host_subprocess_fallback() -> None:
    bridge = "class Bridge:\n def _bind_scored_tool_plane(self): CellToolPlaneBroker.from_endpoint()\nclass ScoredBridgeControl:\n def run(self): build_engine()"
    dispatch = "def run_task(): start_attempt_planes(); dispatch_through_control()\ndef _dispatch(scored): subprocess.run([])"
    runtime = "class _ProductionCell:\n def start_attempt_planes(self): _authority_descriptor(); open_control_secret_for_cell(); build_launch_spec('tool'); build_launch_spec('control'); helper_spawn(); helper_spawn(); print('--config-fd --secret-fd config_digest')"
    plane = "def _tool(): RemoteGitService(); CellToolPlaneBroker.from_git_service(); execute_tool(); print(\"read\", \"write\", \"edit\", \"bash\")\ndef _control(): CellToolPlaneBroker.from_endpoint(); _control_inputs(); ScoredBridgeControl()"
    with pytest.raises(CompositionGateError, match="subprocess"):
        validate_core_plane_composition(bridge, dispatch, runtime, plane, "agent-dispatch", 'name: "read" name: "write" name: "edit" name: "bash"')


def test_static_core_gate_red_for_noop_control_dispatch() -> None:
    bridge = "class Bridge:\n def _bind_scored_tool_plane(self): CellToolPlaneBroker.from_endpoint()\nclass ScoredBridgeControl:\n def run(self): build_engine()"
    dispatch = "def run_task(): start_attempt_planes(); dispatch_through_control()\ndef _dispatch(scored):\n if scored: raise ValueError()"
    runtime = """
class _ProductionCell:
 def start_attempt_planes(self):
  _authority_descriptor(); open_control_secret_for_cell(); control_listener = socket.socket(); control_listener.bind('x'); control_listener.listen(8); build_launch_spec('tool'); build_launch_spec('control'); helper_spawn(); helper_spawn(); print('--config-fd --secret-fd --listener-fd config_digest')
 def dispatch_through_control(self):
  return {'status': 'ok'}
"""
    plane = """
MAX_MODEL_RESULT_BYTES = 4096
def _project_model_text(): pass
def _project_model_streams(): pass
def _serve(): chmod(0o660)
def _tool():
 RemoteGitService(); CellToolPlaneBroker.from_git_service(); execute_tool(); _project_model_text(); _project_model_streams(); print("read", "write", "edit", "bash")
def _control(): CellToolPlaneBroker.from_endpoint(); _control_inputs(); ScoredBridgeControl()
"""
    with pytest.raises(CompositionGateError, match="control dispatch"):
        validate_core_plane_composition(
            bridge, dispatch, runtime, plane, "agent-dispatch",
            'name: "read" name: "write" name: "edit" name: "bash"',
        )


# Minimal synthetic sources that satisfy every core-plane property.  Each RED
# fixture below removes exactly one property from this known-good baseline, so
# a gate failure is calibrated to the removed property rather than to ambient
# missing structure.

_VALID_CORE_BRIDGE = """
class Bridge:
 def _bind_scored_tool_plane(self): CellToolPlaneBroker.from_endpoint()
class ScoredBridgeControl:
 def run(self): build_engine()
"""

_VALID_CORE_DISPATCH = """
def run_task():
 start_attempt_planes(); dispatch_through_control()
def _dispatch(scored):
 if scored: raise ValueError()
"""

_VALID_CORE_RUNTIME = """
class _ProductionCell:
 def start_attempt_planes(self):
  _authority_descriptor(); open_control_secret_for_cell()
  control_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  control_listener.bind(str(self.control_endpoint))
  os.chmod(self.control_endpoint, 0o600)
  control_listener.listen(8)
  build_launch_spec('tool'); build_launch_spec('control'); helper_spawn(); helper_spawn()
  print('--config-fd --secret-fd --listener-fd config_digest')
 def dispatch_through_control(self):
  connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  connection.connect(str(self.control_endpoint))
  connection.sendall(b'x')
  connection.recv(1)
"""

_VALID_CORE_PLANE = """
MAX_MODEL_RESULT_BYTES = 4096
def _project_model_text(): pass
def _project_model_streams(): pass
def _project_model_result(value): pass
def execute_tool(request): pass
def normalize(payload): pass
def _serve(endpoint, handler, mode=0o600, listener_fd=None):
 owns_endpoint = listener_fd is None
 if owns_endpoint:
  endpoint.unlink(missing_ok=True)
  listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
 else:
  listener = socket.socket(fileno=listener_fd)
 if owns_endpoint:
  listener.bind(str(endpoint))
  os.chmod(endpoint, mode)
  listener.listen(8)
 elif listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM or listener.getsockname() != str(endpoint):
  raise ValueError("inherited control listener is malformed")
def _tool():
 RemoteGitService()
 service = CellToolPlaneBroker.from_git_service()
 def handle(request):
  return _project_model_result(execute_tool(normalize(request["payload"])))
 _project_model_text(); _project_model_streams()
 _serve(endpoint, handle, mode=0o660)
 print("read", "write", "edit", "bash")
def _control():
 CellToolPlaneBroker.from_endpoint(); _control_inputs(); ScoredBridgeControl()
 _serve(endpoint, handle, listener_fd=listener_fd)
def main():
 print('--listener-fd')
"""

_VALID_CORE_PI_BROKER = 'name: "read" name: "write" name: "edit" name: "bash"'


def test_static_core_gate_accepts_minimal_valid_sources() -> None:
    """GREEN calibration: the RED fixtures below fail only for the removed property."""
    validate_core_plane_composition(
        _VALID_CORE_BRIDGE, _VALID_CORE_DISPATCH, _VALID_CORE_RUNTIME,
        _VALID_CORE_PLANE, "agent-dispatch", _VALID_CORE_PI_BROKER,
    )


def test_static_core_gate_red_for_unbounded_common_tool_result() -> None:
    """r24 Sol P0 shape: tool/git results returned without the common projector."""
    plane = _VALID_CORE_PLANE.replace(
        '_project_model_result(execute_tool(normalize(request["payload"])))',
        'execute_tool(normalize(request["payload"]))',
    )
    with pytest.raises(CompositionGateError, match="bound every model-visible tool result"):
        validate_core_plane_composition(
            _VALID_CORE_BRIDGE, _VALID_CORE_DISPATCH, _VALID_CORE_RUNTIME,
            plane, "agent-dispatch", _VALID_CORE_PI_BROKER,
        )


def test_static_core_gate_red_for_control_self_bound_socket() -> None:
    """r24 GLM P0 shape: control child binds its own owner-only socket."""
    plane = _VALID_CORE_PLANE.replace(
        "def _serve(endpoint, handler, mode=0o600, listener_fd=None):",
        "def _serve(endpoint, handler, mode=0o600):",
    ).replace(
        "_serve(endpoint, handle, listener_fd=listener_fd)",
        "_serve(endpoint, handle)",
    )
    with pytest.raises(CompositionGateError, match="inherited pre-bound listener"):
        validate_core_plane_composition(
            _VALID_CORE_BRIDGE, _VALID_CORE_DISPATCH, _VALID_CORE_RUNTIME,
            plane, "agent-dispatch", _VALID_CORE_PI_BROKER,
        )


def test_static_core_gate_red_for_missing_controller_prebind() -> None:
    """Controller no longer pre-binds/passes the listener to the control child."""
    runtime = _VALID_CORE_RUNTIME.replace(
        "  control_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "  control_listener.bind(str(self.control_endpoint))\n"
        "  os.chmod(self.control_endpoint, 0o600)\n"
        "  control_listener.listen(8)\n",
        "",
    ).replace("--listener-fd ", "")
    with pytest.raises(CompositionGateError, match="pre-bind the control listener"):
        validate_core_plane_composition(
            _VALID_CORE_BRIDGE, _VALID_CORE_DISPATCH, runtime,
            _VALID_CORE_PLANE, "agent-dispatch", _VALID_CORE_PI_BROKER,
        )
