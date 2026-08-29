"""Fail-closed command surface for the isolated bakeoff."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from . import report
from .evidence import EvidencePackageError, EvidencePackage, validate_evidence_package
from .manifest import ManifestError, load_manifest
from .phases import CalibrationError, PilotError, run_calibration, run_pilot
from .ref_protection import RefProtectionError, prune_protected_refs
from .runner import RunnerError, run_full_matrix
from .readiness import run_production_preflight
from .runtime import ProductionRuntimeUnavailable, build_production_controller, build_production_runtime


class CLIError(ValueError):
    pass


def _unbound(_manifest: Mapping[str, Any], seat: str | None = None) -> int:
    del seat
    raise CLIError("production phase handler is provided by the phase runner")


def _preflight_handler(manifest: Mapping[str, Any], seat: str | None = None) -> int:
    del seat
    try:
        runtime = production_runtime(manifest)
    except ProductionRuntimeUnavailable as exc:
        raise CLIError(str(exc)) from exc
    result = run_production_preflight(manifest, runtime=runtime)
    if result.status != "PASS":
        raise CLIError(f"production readiness is {result.status}")
    return 0


def _calibrate_handler(manifest: Mapping[str, Any], seat: str | None = None) -> Any:
    if not seat:
        raise CLIError("calibrate requires a seat")
    try:
        return run_calibration(manifest, seat, runtime=production_runtime(manifest))
    except ProductionRuntimeUnavailable as exc:
        raise CLIError(str(exc)) from exc
    except CalibrationError as exc:
        raise CLIError(str(exc)) from exc


def _pilot_handler(manifest: Mapping[str, Any], seat: str | None = None) -> Any:
    del seat
    try:
        return run_pilot(manifest, runtime=production_runtime(manifest))
    except ProductionRuntimeUnavailable as exc:
        raise CLIError(str(exc)) from exc
    except PilotError as exc:
        raise CLIError(str(exc)) from exc


def _run_handler(manifest: Mapping[str, Any], seat: str | None = None) -> Any:
    del seat
    try:
        return run_full_matrix(manifest, runtime=production_runtime(manifest))
    except ProductionRuntimeUnavailable as exc:
        raise CLIError(str(exc)) from exc
    except RunnerError as exc:
        raise CLIError(str(exc)) from exc


HANDLERS: dict[str, Callable[..., Any]] = {
    "preflight": _preflight_handler,
    "calibrate": _calibrate_handler,
    "pilot": _pilot_handler,
    "run": _run_handler,
}


def production_runtime(manifest: Mapping[str, Any]) -> Any:
    return build_production_runtime(manifest, controller=build_production_controller(manifest))


def load_manifest_guard(path: str | Path, *, require_open: bool = False) -> Mapping[str, Any]:
    manifest = load_manifest(path)
    root = Path(manifest["evidence"]["root"])
    package = EvidencePackage.open(root)
    package.validate(require_sealed=False)
    if require_open and package.is_sealed:
        raise CLIError("sealed evidence package is immutable")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="implbench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--evidence")
    validate.add_argument("--manifest")
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--manifest", required=True)
    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("--manifest", required=True)
    calibrate.add_argument("--seat", required=True)
    calibrate.add_argument("--concurrency", type=int, default=1)
    pilot = sub.add_parser("pilot")
    pilot.add_argument("--manifest", required=True)
    pilot.add_argument("--concurrency", type=int, default=1)
    run = sub.add_parser("run")
    run.add_argument("--manifest", required=True)
    run.add_argument("--concurrency", type=int, default=1)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--evidence", required=True)
    prune = sub.add_parser("prune")
    prune.add_argument("--before", required=True)
    prune.add_argument("--evidence-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        ns = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        if ns.cmd == "validate":
            if ns.evidence:
                validate_evidence_package(ns.evidence)
                return 0
            if ns.manifest:
                load_manifest_guard(ns.manifest)
                return 0
            from .validate import run_validate

            result = run_validate()
            return 0 if getattr(result, "ok", False) else 1
        if ns.cmd == "report":
            print(report.render(ns.evidence))
            return 0
        if ns.cmd == "prune":
            for ref in prune_protected_refs(Path("."), ns.before, evidence_root=ns.evidence_root):
                print(ref)
            return 0
        if getattr(ns, "concurrency", 1) != 1:
            print("implbench requires --concurrency 1", file=sys.stderr)
            return 2
        manifest = load_manifest_guard(ns.manifest, require_open=True)
        handler = HANDLERS[ns.cmd]
        result = handler(manifest, getattr(ns, "seat", None))
        return int(result) if isinstance(result, int) else 0
    except (CLIError, EvidencePackageError, ManifestError, RefProtectionError, OSError, ValueError) as exc:
        print(f"implbench: {exc}", file=sys.stderr)
        return 1


def _task_paths(task_ids: list[str] | None = None) -> list[Path]:
    root = Path("bench/implbench/fixtures")
    paths = sorted(root.glob("*/task.yaml"))
    if task_ids:
        wanted = set(task_ids)
        paths = [path for path in paths if path.parent.name in wanted]
    return paths
