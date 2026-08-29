from __future__ import annotations

import os
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Provenance:
    seat: str
    engine: str
    model_declared: str
    model_verified_via: str
    engine_version: str
    harness_version: str
    corpus_version: str
    config_digest: str = ""
    capability_manifest_digest: str = ""
    reasoning_requested: str = "medium"
    reasoning_effective: str = "medium"
    reasoning_verified_via: str = "provider-runtime-ack"

    def as_dict(self) -> dict[str, str]:
        return {
            "model_declared": self.model_declared,
            "model_verified_via": self.model_verified_via,
            "engine_version": self.engine_version,
            "harness_version": self.harness_version,
            "corpus_version": self.corpus_version,
            "config_digest": self.config_digest,
            "capability_manifest_digest": self.capability_manifest_digest,
            "reasoning_requested": self.reasoning_requested,
            "reasoning_effective": self.reasoning_effective,
            "reasoning_verified_via": self.reasoning_verified_via,
        }


def collect(
    seat: str,
    engine: str,
    repo: Path,
    corpus_version: str = "",
    reply_text: str | None = None,
) -> Provenance:
    del reply_text
    model = _seat_model(seat)
    engine_version = _run([engine, "--version"], cwd=repo)
    harness_version = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    config_digest = hashlib.sha256(json.dumps({"seat": seat, "engine": engine, "model": model}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    capability_digest = hashlib.sha256(b'{"browser":false,"memory":false,"network":false,"read":true,"search":true,"shell":true,"write":true}').hexdigest()
    return Provenance(
        seat=seat,
        engine=engine,
        model_declared=model,
        model_verified_via="config+cli-version+billing-delta",
        engine_version=engine_version,
        harness_version=harness_version,
        corpus_version=corpus_version,
        config_digest=config_digest,
        capability_manifest_digest=capability_digest,
    )


def provenance_record(identity: dict[str, object], provenance: Provenance) -> dict[str, object]:
    """Create the authenticated record payload without serialising diagnostics."""
    from .records import make_identity

    return make_identity(identity, record_type="provenance", payload={
        "model_declared": provenance.model_declared,
        "model_verified_via": provenance.model_verified_via,
        "engine_version": provenance.engine_version,
        "harness_version": provenance.harness_version,
        "corpus_version": provenance.corpus_version,
    })


def _seat_model(seat: str) -> str:
    key = "IMPLBENCH_MODEL_" + seat.upper().replace("-", "_")
    return os.environ.get(key, "unknown")


def _run(args: list[str], cwd: Path | None = None) -> str:
    res = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if res.returncode != 0:
        return res.stderr.strip() or "unknown"
    return (res.stdout or res.stderr).strip()
