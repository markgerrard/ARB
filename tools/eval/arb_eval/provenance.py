"""Run provenance capture for floor evaluations."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


def _git_describe(root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "describe", "--always", "--dirty"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "UNKNOWN"


def _scenario_sha256(scenario) -> str:
    source_raw = str(getattr(scenario, "source_path", "") or "")
    source = Path(source_raw)
    if source.is_file():
        return hashlib.sha256(source.read_bytes()).hexdigest()
    payload = {
        "id": scenario.id,
        "description": scenario.description,
        "subject": scenario.subject,
        "seeded_defects": [getattr(seed, "__dict__", {}) for seed in scenario.seeds],
        "control_loci": [getattr(locus, "__dict__", {}) for locus in scenario.control_loci],
        "panel": [getattr(seat, "__dict__", {}) for seat in scenario.panel],
        "power": scenario.power,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _builder_path(scenario, harness_root: Path) -> Path | None:
    subject = scenario.subject or {}
    explicit = subject.get("builder") or subject.get("builder_script")
    source_raw = str(getattr(scenario, "source_path", "") or "")
    source = Path(source_raw)
    if explicit:
        path = Path(str(explicit))
        if path.is_absolute():
            return path
        if source_raw:
            return (source.parent / path).resolve()
        return (harness_root / path).resolve()

    derived = f"build_{scenario.id.replace('-', '_')}.sh"
    candidate = harness_root / "fixtures" / derived
    return candidate if candidate.exists() else None


def _builder_sha256(scenario, harness_root: Path) -> str:
    path = _builder_path(scenario, harness_root)
    if path and path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return ""


def _package_sha256(root: Path) -> str:
    h = hashlib.sha256()
    pkg = root / "arb_eval"
    for path in sorted(pkg.glob("*.py")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def collect(scenario, *, dispatcher, normalizer, oracle_by_language: dict[str, str],
            gold_versions: dict[str, str], image: str | None, harness_root: Path,
            run_id: str) -> dict[str, Any]:
    model = {}
    for seat in scenario.panel:
        requested = "" if seat.model == "?" else seat.model
        source = "pinned-flag" if requested else "cli-version-only"
        model[seat.seat] = {
            "model_requested": requested,
            "confined_command": f"confined-review seat={seat.seat} model={requested}".strip(),
            "model_reported": None,
            "model_source": source,
        }
    prov = {
        "model": model,
        "engine_versions": {},
        "harness_version": {
            "describe": _git_describe(harness_root),
            "package_sha256": _package_sha256(harness_root),
        },
        "image_digest": image,
        "corpus_version": {
            "builder_sha": _builder_sha256(scenario, harness_root),
            "scenario_sha256": _scenario_sha256(scenario),
            "repo_base": scenario.subject.get("base", ""),
            "repo_head": scenario.subject.get("head", ""),
        },
        "normalizer": {
            "model": getattr(normalizer, "model", type(normalizer).__name__),
            "endpoint": getattr(normalizer, "base_url", ""),
        },
        "matcher": {"window": 10},
        "boundary_oracle": {
            "oracle_by_language": dict(oracle_by_language),
            "coverage": sorted(oracle_by_language),
        },
        "gold_versions": dict(gold_versions),
        "run_id": run_id,
    }
    return prov


def strip_prov_fence(stdout: str, nonce: str) -> tuple[str, dict[str, str]]:
    tag = re.escape(f"ARB_PROV_{nonce}")
    pattern = re.compile(rf"{tag}(\{{.*?\}})</{tag}>")
    match = pattern.search(stdout)
    if not match:
        return stdout, {}
    try:
        versions = json.loads(match.group(1))
    except json.JSONDecodeError:
        return stdout, {}
    cleaned = stdout[:match.start()] + stdout[match.end():]
    return cleaned, dict(versions)


def provenance_key(prov: dict[str, Any]) -> str:
    model_requested = {
        seat: row.get("model_requested", "")
        for seat, row in sorted(prov.get("model", {}).items())
    }
    stable = (
        model_requested,
        prov.get("harness_version", {}).get("describe"),
        prov.get("corpus_version"),
        prov.get("image_digest"),
    )
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def warning_lines(prov: dict[str, Any]) -> list[str]:
    out = []
    for seat, row in sorted(prov.get("model", {}).items()):
        if row.get("model_source") == "cli-version-only":
            out.append(f"WARNING: {seat} model identity is cli-version-only")
    return out


def model_unverified(prov: dict[str, Any]) -> bool:
    return any(
        row.get("model_source") == "cli-version-only"
        for row in prov.get("model", {}).values()
    )
