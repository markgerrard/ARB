"""Manifest-v2: the controller-owned immutable experiment contract."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .schedule import expand_schedule
from .tasks import corpus_version, load_corpus


class ManifestError(ValueError):
    """Raised for malformed, mutable, or non-authoritative manifests."""


ManifestSchemaError = ManifestError
ManifestImmutableError = ManifestError

DESIGN_COMMIT = "166eb76bcf50ea28d44bfccf3604b8c824bf698a"
DESIGN_BLOB = "c5981bcdc3bf4d52a5cdfaff58a26df336771fb7"
SPEC_COMMIT = "614579031700af95111493b563f7d7bb39065aff"
SPEC_BLOB = "9fbf8bacf045395ec00ca55ba8f5a1e3479f6ad9"
PLAN_COMMIT = "ebc9a46fc562f454da693ff111b0637b9485f4f8"
PLAN_BLOB = "0d1456bcd66bddbb9ffc2f351923d1af34a921e5"

GIT_RPC = {
    "max_frame_bytes": 1048576,
    "max_path_bytes": 4096,
    "max_components_per_path": 256,
    "max_component_bytes": 255,
    "max_paths_per_request": 1024,
    "max_in_flight": 8,
    "status_rate_per_second": 4,
    "status_burst": 8,
}
_EXTENSIONS = {
    "role_profiles": [],
    "project_instruction_files": [],
    "optional_skill_packs": [],
    "memory_mcps": [],
    "unrelated_extensions": [],
}
_CONTROL_NAMES = (
    "reasoning",
    "temperature",
    "top_p",
    "top_k",
    "seed",
    "penalties",
    "maximum_output",
    "stop_behavior",
    "tool_choice",
    "parallel_tool_behavior",
    "retry",
    "backoff",
    "timeouts",
)
_ARM_ROWS = (
    ("GLM", "glm-pi", "pi-sdk", "zai", "glm-5.2", "Pi", "pi-sdk-agentredisbridge-bake-glm52", "PI_CODING_AGENT_DIR", "BRIDGE_PI_RETIRE_AFTER_TURN"),
    ("GLM", "glm-zcode", "openinterpreter", "zai-coding-plan", "glm-5.2", "zcode", "interpreter-agentredisbridge-bake-glm52-zcode", "INTERPRETER_HOME", "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN"),
    ("Kimi", "kimi-pi", "pi-sdk", "kimi-coding", "k2p7", "Pi", "pi-sdk-agentredisbridge-bake-k2p7", "PI_CODING_AGENT_DIR", "BRIDGE_PI_RETIRE_AFTER_TURN"),
    ("Kimi", "kimi-cli", "openinterpreter", "kimi-for-coding", "k2p7", "kimi-cli", "interpreter-agentredisbridge-bake-k2p7-kimicli", "INTERPRETER_HOME", "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN"),
)
_TASK_IDS = (
    "c1-permissive-boundary",
    "c1-token-bucket",
    "c2-parser",
    "c3-refactor",
    "c4-rail",
    "c5-artifact",
    "c6-scope",
    "c7-provenance",
)
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class FrozenManifest(Mapping[str, Any]):
    """Read-only mapping around canonical manifest data."""

    def __init__(self, data: Mapping[str, Any]):
        self._data = _freeze(copy.deepcopy(dict(data)))

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenManifest):
            return self._data == other._data
        if isinstance(other, Mapping):
            return _thaw(self._data) == dict(other)
        return NotImplemented

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self._data)


Manifest = FrozenManifest


def _duplicate_rejecting_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_noncanonical_numbers(value: Any) -> None:
    if isinstance(value, float):
        raise ManifestError("floating-point JSON numbers are not canonical")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_noncanonical_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_noncanonical_numbers(item)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    _reject_noncanonical_numbers(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ManifestError(f"manifest is not canonical JSON: {exc}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if not raw.endswith(b"\n"):
            raise ManifestError("manifest must end with one newline")
        value = json.loads(raw[:-1].decode("utf-8"), object_pairs_hook=_duplicate_rejecting_pairs, parse_constant=lambda value: (_ for _ in ()).throw(ManifestError(value)))
    except ManifestError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid manifest JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError("manifest root must be an object")
    if raw != canonical_json_bytes(value) + b"\n":
        raise ManifestError("manifest is not canonical JSON")
    return value


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=False)
    if result.returncode:
        raise ManifestError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def _ensure_source(root: str | Path) -> tuple[Path, str, str]:
    path = Path(root)
    if not path.is_absolute() or not path.exists() or path.is_symlink():
        raise ManifestError("source root must be an existing absolute non-symlink path")
    real = path.resolve(strict=True)
    if real != path:
        raise ManifestError("source root realpath differs from supplied path")
    if _git(path, "status", "--porcelain"):
        raise ManifestError("source repository is dirty")
    commit = _git(path, "rev-parse", "HEAD")
    tree = _git(path, "rev-parse", "HEAD^{tree}")
    return path, commit, tree


def _controls() -> dict[str, dict[str, str]]:
    controls: dict[str, dict[str, str]] = {}
    for name in _CONTROL_NAMES:
        if name == "reasoning":
            controls[name] = {"requested": "medium", "effective": "medium", "verified_via": "provider-runtime-ack"}
        else:
            controls[name] = {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"}
    return controls


def _fixture_sha(root: Path, task_id: str) -> str:
    # The authoritative fixture SHA is the deterministic commit produced by the
    # existing fixture materializer, not the source checkout's directory tree OID.
    from .fixtures import materialize
    from .tasks import load_task

    task = load_task(root / "bench" / "implbench" / "fixtures" / task_id / "task.yaml")
    with tempfile.TemporaryDirectory(prefix="implbench-fixture-") as temporary:
        fixture_repo = Path(temporary)
        subprocess.run(["git", "init", "-q", str(fixture_repo)], check=True)
        return materialize(task, fixture_repo)


def _validate_sha(value: Any, field: str) -> None:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ManifestError(f"{field} must be a lowercase SHA-1 or SHA-256")


def _validate_controls(controls: Any, where: str) -> None:
    if not isinstance(controls, Mapping) or set(controls) != set(_CONTROL_NAMES):
        raise ManifestError(f"{where} must contain the exhaustive control map")
    for name in _CONTROL_NAMES:
        item = controls[name]
        if not isinstance(item, Mapping) or set(item) != {"requested", "effective", "verified_via"}:
            raise ManifestError(f"{where}.{name} must contain requested/effective/verified_via")
        if not all(isinstance(item[field], str) and item[field] for field in item):
            raise ManifestError(f"{where}.{name} contains an invalid control value")


def _exact_mapping(value: Any, fields: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ManifestError(f"{where} fields mismatch")
    return value


def _validate(data: Mapping[str, Any]) -> None:
    required = {
        "schema_version", "run_id", "design", "spec", "plan", "source", "base_sha", "corpus_sha",
        "tasks", "arms", "controls", "runtime", "pins", "git_rpc", "digest_versions", "capabilities",
        "budgets", "seed", "schedule", "planned_cells", "extensions", "evidence", "ref_namespace",
        "stop_rules", "rerun_rules", "analysis_rules",
    }
    if set(data) != required:
        missing = sorted(required - set(data)); unknown = sorted(set(data) - required)
        raise ManifestError(f"manifest fields mismatch; missing={missing}, unknown={unknown}")
    if data["schema_version"] != "manifest-v2":
        raise ManifestError("unknown manifest schema version")
    for stage, commit, blob in (("design", DESIGN_COMMIT, DESIGN_BLOB), ("spec", SPEC_COMMIT, SPEC_BLOB), ("plan", PLAN_COMMIT, PLAN_BLOB)):
        item = data[stage]
        if not isinstance(item, Mapping) or set(item) != {"commit", "blob"} or item != {"commit": commit, "blob": blob}:
            raise ManifestError(f"{stage} pin mismatch")
    if not isinstance(data["run_id"], str) or not data["run_id"].startswith("oi-pi-bakeoff-"):
        raise ManifestError("invalid bakeoff run ID")
    source = data["source"]
    if not isinstance(source, Mapping) or set(source) != {"realpath", "commit", "tree", "dirty"}:
        raise ManifestError("invalid source identity")
    if not isinstance(source["realpath"], str) or not os.path.isabs(source["realpath"]) or source["realpath"] != os.path.realpath(source["realpath"]):
        raise ManifestError("source realpath is not canonical")
    if source["dirty"] is not False:
        raise ManifestError("source must be clean")
    _validate_sha(source["commit"], "source.commit"); _validate_sha(source["tree"], "source.tree"); _validate_sha(data["base_sha"], "base_sha"); _validate_sha(data["corpus_sha"], "corpus_sha")
    if data["seed"] != str(data["seed"]).lower() or not re.fullmatch(r"[0-9a-f]{64}", data["seed"]):
        raise ManifestError("seed must be 64 lowercase hex characters")
    if data["git_rpc"] != GIT_RPC:
        raise ManifestError("Git RPC constants drifted")
    if data["extensions"] != _EXTENSIONS:
        raise ManifestError("extensions must be explicit empty allowlists")
    _validate_controls(data["controls"], "controls")
    if data["controls"]["reasoning"]["requested"] != "medium" or data["controls"]["reasoning"]["effective"] != "medium":
        raise ManifestError("reasoning must be medium")
    if data["controls"]["reasoning"]["verified_via"] in {"request", "request-echo", "config", "config-echo", "echo", "prose", "model-prose"}:
        raise ManifestError("reasoning verification cannot be request/config echo")
    _exact_mapping(data["runtime"], {"os", "python", "node", "git"}, "runtime")
    if not all(isinstance(value, str) and value for value in data["runtime"].values()):
        raise ManifestError("runtime pins must be non-empty strings")
    pins = _exact_mapping(data["pins"], {"binary", "profile", "importer", "scorer", "battery", "public_suite"}, "pins")
    if not isinstance(pins["binary"], Mapping) or set(pins["binary"]) != {"openinterpreter"}:
        raise ManifestError("binary pins mismatch")
    for name, item in (("openinterpreter", pins["binary"]["openinterpreter"]), ("profile", pins["profile"]), ("importer", pins["importer"]), ("scorer", pins["scorer"]), ("battery", pins["battery"]), ("public_suite", pins["public_suite"])):
        pin = _exact_mapping(item, {"version", "digest"}, f"pins.{name}")
        if not isinstance(pin["version"], str) or not pin["version"] or not isinstance(pin["digest"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", pin["digest"]):
            raise ManifestError(f"pins.{name} is not a content/version pin")
    if data["pins"]["binary"]["openinterpreter"]["version"] != "0.0.21":
        raise ManifestError("Open Interpreter version pin mismatch")
    _exact_mapping(data["digest_versions"], {"manifest", "schedule", "corpus", "final_tree", "analysis"}, "digest_versions")
    if data["digest_versions"] != {"manifest": "manifest-v2", "schedule": "implbench-schedule-v1", "corpus": "implbench-corpus-v1", "final_tree": "final-tree-v1", "analysis": "pair-analysis-v1"}:
        raise ManifestError("digest version mismatch")
    _exact_mapping(data["capabilities"], {"read", "search", "shell", "edit", "write", "network", "browser", "memory"}, "capabilities")
    if not all(isinstance(value, bool) for value in data["capabilities"].values()):
        raise ManifestError("capabilities must be booleans")
    budgets = _exact_mapping(data["budgets"], {"wall_time_s", "max_frame_bytes", "max_paths"}, "budgets")
    if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in budgets.values()):
        raise ManifestError("budgets must be positive integers")
    expected_arms = [row[1] for row in _ARM_ROWS]
    if not isinstance(data["arms"], list) or [arm.get("arm") for arm in data["arms"] if isinstance(arm, Mapping)] != expected_arms:
        raise ManifestError("arm set/order mismatch")
    for arm, row in zip(data["arms"], _ARM_ROWS):
        pair, arm_id, engine, provider, model, harness, prefix, home, retire = row
        expected = {"pair": pair, "arm": arm_id, "engine": engine, "provider": provider, "model": model, "harness": harness, "agent_prefix": prefix, "home": home, "retire_env": retire, "retire_value": "1", "controls": data["controls"]}
        if arm != expected:
            raise ManifestError(f"arm {arm_id} is not the frozen definition")
    if not isinstance(data["tasks"], list) or [task.get("task_id") for task in data["tasks"] if isinstance(task, Mapping)] != list(_TASK_IDS):
        raise ManifestError("corpus task set/order mismatch")
    for task in data["tasks"]:
        if not isinstance(task, Mapping) or set(task) != {"task_id", "cluster", "fixture_sha", "task_yaml_sha", "battery_id"}:
            raise ManifestError("task pin fields mismatch")
        _validate_sha(task["fixture_sha"], f"{task['task_id']}.fixture_sha"); _validate_sha(task["task_yaml_sha"], f"{task['task_id']}.task_yaml_sha")
        if not all(isinstance(task[field], str) and task[field] for field in ("task_id", "cluster", "battery_id")):
            raise ManifestError("task pin contains an invalid string")
    if not isinstance(data["schedule"], list) or data["schedule"] != data["planned_cells"] or len(data["schedule"]) != 128:
        raise ManifestError("schedule/planned cell mismatch")
    tasks_for_schedule = [(task["task_id"], task["fixture_sha"]) for task in data["tasks"]]
    expected_schedule = [cell.as_dict() for cell in expand_schedule(data["seed"], tasks_for_schedule)]
    if data["schedule"] != expected_schedule:
        raise ManifestError("stored schedule is not the independent expansion")
    evidence = _exact_mapping(data["evidence"], {"root", "manifest"}, "evidence")
    if not isinstance(evidence["root"], str) or not os.path.isabs(evidence["root"]) or any(part in evidence["root"].lower() for part in (".env", "credential", "secret")):
        raise ManifestError("evidence root must be an absolute non-secret path")
    if evidence["manifest"] != "manifest.json":
        raise ManifestError("manifest evidence filename mismatch")
    ref_namespace = _exact_mapping(data["ref_namespace"], {"runs", "results", "backend"}, "ref_namespace")
    if ref_namespace["backend"] != "files" or not all(isinstance(ref_namespace[field], str) and ref_namespace[field] for field in ("runs", "results")):
        raise ManifestError("ref namespace mismatch")
    analysis = _exact_mapping(data["analysis_rules"], {"schema", "pairs", "no_rankings", "no_composite_scores", "evidence_shapes"}, "analysis_rules")
    if analysis["schema"] != "pair-analysis-v1" or analysis["pairs"] != ["GLM", "Kimi"] or analysis["no_rankings"] is not True or analysis["no_composite_scores"] is not True:
        raise ManifestError("analysis rules mismatch")
    if analysis["evidence_shapes"] != ["openinterpreter-dominated", "operationally-equivalent", "openinterpreter-adds-capability", "mixed-decorrelated"]:
        raise ManifestError("analysis evidence shapes mismatch")
    if not isinstance(data["stop_rules"], list) or not isinstance(data["rerun_rules"], list) or not isinstance(data["analysis_rules"], Mapping):
        raise ManifestError("stop/rerun/analysis rules have invalid shape")


def create_manifest(source_root: str | Path, run_id: str, seed: str, *, base_sha: str | None = None, evidence_root: str | Path | None = None, pins: Mapping[str, Any] | None = None) -> FrozenManifest:
    root, source_sha, source_tree = _ensure_source(source_root)
    if base_sha is None:
        base_sha = source_sha
    _validate_sha(base_sha, "base_sha")
    if pins is None:
        raise ManifestError("authoritative binary/profile/importer/scorer/battery pins are required")
    fixtures_root = root / "bench" / "implbench" / "fixtures"
    try:
        corpus_tasks = load_corpus(root / "bench" / "implbench")
    except Exception as exc:
        raise ManifestError(f"invalid frozen corpus: {exc}") from exc
    task_rows: list[dict[str, str]] = []
    for task in corpus_tasks:
        task_id = task.task_id
        task_path = fixtures_root / task_id / "task.yaml"
        task_yaml_sha = hashlib.sha256(task_path.read_bytes()).hexdigest()
        task_rows.append({"task_id": task.task_id, "cluster": task.cluster, "fixture_sha": _fixture_sha(root, task_id), "task_yaml_sha": task_yaml_sha, "battery_id": task.battery_id})
    if sorted(path.name for path in fixtures_root.iterdir() if path.is_dir()) != sorted(_TASK_IDS):
        raise ManifestError("fixture root contains missing or extra tasks")
    corpus_sha = corpus_version(root / "bench" / "implbench")
    controls = _controls()
    arms = []
    for pair, arm_id, engine, provider, model, harness, prefix, home, retire in _ARM_ROWS:
        arms.append({"pair": pair, "arm": arm_id, "engine": engine, "provider": provider, "model": model, "harness": harness, "agent_prefix": prefix, "home": home, "retire_env": retire, "retire_value": "1", "controls": controls})
    schedule = [cell.as_dict() for cell in expand_schedule(seed, [(task["task_id"], task["fixture_sha"]) for task in task_rows])]
    manifest = {
        "schema_version": "manifest-v2",
        "run_id": run_id,
        "design": {"commit": DESIGN_COMMIT, "blob": DESIGN_BLOB},
        "spec": {"commit": SPEC_COMMIT, "blob": SPEC_BLOB},
        "plan": {"commit": PLAN_COMMIT, "blob": PLAN_BLOB},
        "source": {"realpath": str(root), "commit": source_sha, "tree": source_tree, "dirty": False},
        "base_sha": base_sha,
        "corpus_sha": corpus_sha,
        "tasks": task_rows,
        "arms": arms,
        "controls": controls,
        "runtime": {"os": "macOS", "python": "3.12", "node": "pinned", "git": "pinned"},
        "pins": copy.deepcopy(dict(pins)),
        "git_rpc": dict(GIT_RPC),
        "digest_versions": {"manifest": "manifest-v2", "schedule": "implbench-schedule-v1", "corpus": "implbench-corpus-v1", "final_tree": "final-tree-v1", "analysis": "pair-analysis-v1"},
        "capabilities": {"read": True, "search": True, "shell": True, "edit": True, "write": True, "network": False, "browser": False, "memory": False},
        "budgets": {"wall_time_s": 900, "max_frame_bytes": GIT_RPC["max_frame_bytes"], "max_paths": GIT_RPC["max_paths_per_request"]},
        "seed": seed,
        "schedule": schedule,
        "planned_cells": schedule,
        "extensions": copy.deepcopy(_EXTENSIONS),
        "evidence": {"root": str(Path(evidence_root) if evidence_root is not None else Path("/Users/Shared/arb-implbench") / run_id), "manifest": "manifest.json"},
        "ref_namespace": {"runs": "refs/implbench/runs/<run_id>/<cell_id>/<attempt_id>", "results": "refs/implbench/results/<run_id>/<cell_id>/<attempt_id>", "backend": "files"},
        "stop_rules": ["wrong-pin", "context-reuse", "hidden-key-exposure", "fixture-sha-mismatch", "source-drift", "malformed-evidence", "unknown-reasoning", "three-infrastructure-failures"],
        "rerun_rules": ["new-attempt-id", "append-only", "infrastructure-unknown-only"],
        "analysis_rules": {"schema": "pair-analysis-v1", "pairs": ["GLM", "Kimi"], "no_rankings": True, "no_composite_scores": True, "evidence_shapes": ["openinterpreter-dominated", "operationally-equivalent", "openinterpreter-adds-capability", "mixed-decorrelated"]},
    }
    _validate(manifest)
    return FrozenManifest(manifest)


def build_manifest(*args: Any, **kwargs: Any) -> FrozenManifest:
    return create_manifest(*args, **kwargs)


def write_manifest(path: str | Path, manifest: Mapping[str, Any] | FrozenManifest) -> None:
    data = manifest.to_dict() if isinstance(manifest, FrozenManifest) else copy.deepcopy(dict(manifest))
    _validate(data)
    payload = canonical_json_bytes(data) + b"\n"
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != payload:
            raise ManifestError("manifest already exists with different bytes; mint a new run ID")
        load_manifest(destination)
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_manifest(path: str | Path, *, source_root: str | Path | None = None) -> FrozenManifest:
    destination = Path(path)
    if not destination.is_file() or stat.S_IMODE(destination.stat().st_mode) != 0o600:
        raise ManifestError("manifest must be a regular mode-0600 file")
    data = _read_json(destination)
    _validate(data)
    root = Path(source_root) if source_root is not None else Path(data["source"]["realpath"])
    root, commit, tree = _ensure_source(root)
    source = data["source"]
    if source["realpath"] != str(root) or source["commit"] != commit or source["tree"] != tree:
        raise ManifestError("source identity drifted")
    return FrozenManifest(data)


def validate_manifest(path: str | Path, *, source_root: str | Path | None = None) -> FrozenManifest:
    return load_manifest(path, source_root=source_root)
