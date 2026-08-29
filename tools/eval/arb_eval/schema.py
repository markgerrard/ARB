"""Scenario schema + the closed defect taxonomy (schema v0.2 §1, §2).

v0 loads JSON scenarios (YAML can be layered later without a hard dependency, so the core
runs anywhere the bridge runs). The taxonomy is closed and shared with the posture oracle
(autonomous-mode/posture-oracle.seed.md) so the two co-evolve.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Closed taxonomy: posture-oracle classes + general. A class outside this set is a load error.
TAXONOMY = frozenset({
    "cors", "tls-transport", "secrets-in-logs", "auth-on-endpoint", "authorization-scoping",
    "input-trust", "pii-logging-retention", "egress",          # posture
    "correctness", "perf", "logic", "test-gap",                # general
})


class ScenarioError(ValueError):
    pass


def cluster_key(locus) -> str:
    """Effective-independence key for a seed OR a control. Two loci are the SAME independent unit iff
    they share a declared `cluster` (same mechanism / same why-clean idiom) OR — untagged — share a
    LOCATION (file, symbol|line). So correlated instances collapse (P1 instance 5) and so do duplicate
    locations, while genuinely-distinct untagged loci each count once. `tag:`/`loc:` namespacing keeps
    a declared cluster label from ever colliding with a location key."""
    if locus.cluster:
        return f"tag:{locus.cluster}"
    loc = locus.location or {}
    return f"loc:{loc.get('file', '')}:{loc.get('symbol') or loc.get('line') or locus.id}"


def _cluster_count_per_class(classes, loci) -> dict[str, int]:
    clusters: dict[str, set[str]] = {cls: set() for cls in classes}
    for locus in loci:
        clusters.setdefault(locus.cls, set()).add(cluster_key(locus))
    return {cls: len(cs) for cls, cs in clusters.items()}


@dataclass(frozen=True)
class Seed:
    id: str
    cls: str
    location: dict
    description: str
    consensus_severity: str = "P2"
    legible: bool = True
    # The defect MECHANISM. Seeds of the same mechanism are correlated; caught's CI is computed on the
    # seed-cluster count, not the nominal seed count, so correlated seeds can't buy a PASS via a falsely
    # narrow caught CI. Defaults to a singleton. The class-level eligibility gate (class_level_ok) keys
    # on distinct MECHANISMS (seed clusters), so a one-mechanism/many-location scenario is INSTANCE-LEVEL
    # — that mechanism-eligibility is what closes the over-claim; do NOT revert it to location-counting.
    cluster: str = ""


@dataclass(frozen=True)
class ControlLocus:
    id: str
    cls: str
    location: dict
    description: str
    # The "why-clean" idiom. Controls sharing a cluster are clean for the SAME reason -> correlated,
    # not independent samples. nu_s's CI is computed on the cluster count, not the nominal control
    # count, so the oracle can never over-claim by treating correlated controls as independent
    # (measurement-principles P1, instance 5). Defaults to the locus id (a singleton cluster).
    cluster: str = ""


@dataclass(frozen=True)
class SeatSpec:
    seat: str
    model: str
    harness: str


@dataclass
class Scenario:
    id: str
    subject: dict
    seeds: list[Seed]
    panel: list[SeatSpec]
    power: dict = field(default_factory=dict)
    control_loci: list[ControlLocus] = field(default_factory=list)
    description: str = ""
    source_path: str = ""

    def classes(self) -> list[str]:
        # preserve first-seen order
        seen: list[str] = []
        for s in self.seeds:
            if s.cls not in seen:
                seen.append(s.cls)
        return seen

    def instances_per_class(self) -> dict[str, int]:
        seen: dict[str, set[tuple[str, object]]] = {}
        for s in self.seeds:
            loc = s.location or {}
            instance = (str(loc.get("file", "")), loc.get("symbol") or loc.get("line") or s.id)
            seen.setdefault(s.cls, set()).add(instance)
        return {cls: len(instances) for cls, instances in seen.items()}

    def class_level_ok(self, i_min: int) -> dict[str, bool]:
        """True where the class has >= I_min distinct independent MECHANISMS (seed clusters) — a
        class-level claim needs that many genuinely-different ways the class manifests, not that many
        copies of one shape at different locations. Else it is reported instance-level (schema P0-A;
        the eligibility gate under decision-panel B — the verdict CI does the rest)."""
        return {c: n >= i_min for c, n in self.seed_cluster_count_per_class().items()}

    def control_loci_for(self, cls: str) -> list[ControlLocus]:
        return [locus for locus in self.control_loci if locus.cls == cls]

    def control_count_per_class(self) -> dict[str, int]:
        counts: dict[str, int] = {cls: 0 for cls in self.classes()}
        for locus in self.control_loci:
            counts[locus.cls] = counts.get(locus.cls, 0) + 1
        return counts

    def control_cluster_count_per_class(self) -> dict[str, int]:
        """Effective independent control count per class = number of distinct why-clean clusters.
        This (not the nominal count) is what the power budget's >= T requirement must be met by, and
        what nu_s's CI is computed over — correlated controls do not buy independent confidence."""
        return _cluster_count_per_class(self.classes(), self.control_loci)

    def seed_cluster_count_per_class(self) -> dict[str, int]:
        """Effective independent seed count per class = number of distinct mechanisms. caught's CI is
        computed over this, not the nominal seed count — correlated seeds buy no independent confidence."""
        return _cluster_count_per_class(self.classes(), self.seeds)

    def control_cluster_of(self) -> dict[str, str]:
        """Map control_id -> cluster key (falls back to a namespaced singleton for an untagged control)."""
        return {locus.id: cluster_key(locus) for locus in self.control_loci}


def load(path: str | Path) -> Scenario:
    path = Path(path)
    data = json.loads(path.read_text())
    return from_dict(data, scenario_path=path)


def from_dict(data: dict, scenario_path: str | Path | None = None) -> Scenario:
    for req in ("id", "subject", "seeded_defects", "panel"):
        if req not in data:
            raise ScenarioError(f"scenario missing required key: {req!r}")
    seeds = []
    for raw in data["seeded_defects"]:
        cls = raw.get("class")
        if cls not in TAXONOMY:
            raise ScenarioError(
                f"seed {raw.get('id')!r} has class {cls!r} not in the closed taxonomy"
            )
        if not raw.get("legible", True):
            raise ScenarioError(f"seed {raw.get('id')!r}: v0 seeds must be legible:true")
        seeds.append(Seed(
            id=raw["id"], cls=cls, location=raw.get("location", {}),
            description=raw.get("description", ""),
            consensus_severity=raw.get("consensus_severity", "P2"),
            legible=raw.get("legible", True),
            cluster=raw.get("cluster", ""),
        ))
    panel = [SeatSpec(p["seat"], p.get("model", "?"), p.get("harness", "?"))
             for p in data["panel"]]
    power = data.get("power", {})
    if power:
        # validate power params at load -> clean ScenarioError, never a raw crash downstream
        from .power import PowerTarget, Unachievable
        t = PowerTarget(**{k: power[k] for k in (
            "V", "nu", "alpha", "I_min", "R_min", "matcher_gate", "matcher_true_recall")
            if k in power})
        try:
            t.validate()
        except Unachievable as e:
            raise ScenarioError(f"invalid power target: {e}") from e
    controls = []
    seen_controls: set[tuple[str, str, object]] = set()
    # A control must be a CLEAN instance: reject any whose (class, file, symbol|line) coincides with
    # a seed of that class — that is a real defect, not a clean locus (D3 gate).
    seed_keys = {
        (s.cls, str(s.location.get("file", "")), s.location.get("symbol") or s.location.get("line"))
        for s in seeds
    }
    raw_controls = data.get("control_loci", [])
    if raw_controls is None:
        raw_controls = []
    if not isinstance(raw_controls, list):
        raise ScenarioError(f"control_loci must be a list of clean-instance loci, got {raw_controls!r}")
    for raw in raw_controls:
        cls = raw.get("class")
        if cls not in TAXONOMY:
            raise ScenarioError(
                f"control locus {raw.get('id')!r} has class {cls!r} not in the closed taxonomy"
            )
        loc = dict(raw.get("location") or {})
        instance = (cls, str(loc.get("file", "")), loc.get("symbol") or loc.get("line"))
        if instance in seed_keys:
            raise ScenarioError(
                f"control locus {raw.get('id')!r} coincides with a seed at {instance!r}; "
                f"a control must be a clean instance, not a seeded defect"
            )
        if instance in seen_controls:
            raise ScenarioError(
                f"control locus {raw.get('id')!r} duplicates class/file/symbol-or-line {instance!r}"
            )
        seen_controls.add(instance)
        controls.append(ControlLocus(
            id=raw["id"], cls=cls, location=loc, description=raw.get("description", ""),
            cluster=raw.get("cluster", ""),
        ))
    if data.get("schema_rev") == 2:
        matcher_window = int(data.get("matcher_window", 10))
        for seed in seeds:
            seed_loc = seed.location or {}
            seed_line = seed_loc.get("line")
            if seed_line is None:
                continue
            for control in controls:
                control_loc = control.location or {}
                control_line = control_loc.get("line")
                if control_line is None:
                    continue
                if (
                    seed.cls == control.cls
                    and seed_loc.get("file") == control_loc.get("file")
                    and abs(int(seed_line) - int(control_line)) <= matcher_window
                ):
                    raise ScenarioError(
                        f"seed {seed.id!r} and control {control.id!r} share class/file within "
                        f"matcher_window={matcher_window}; rev-2 real scenarios require separation"
                    )
    subject = dict(data["subject"])
    repo = subject.get("repo")
    if repo is not None:
        subject["repo_declared"] = repo
        repo_path = Path(repo)
        if not repo_path.is_absolute() and scenario_path is not None:
            repo_path = Path(scenario_path).parent / repo_path
            repo_path = repo_path.resolve()
        subject["repo"] = str(repo_path)
    subject.setdefault("languages", [])
    return Scenario(
        id=data["id"], subject=subject, seeds=seeds, panel=panel,
        power=power, control_loci=controls, description=data.get("description", ""),
        source_path=str(scenario_path) if scenario_path is not None else "",
    )


def validate_subject(scenario: Scenario) -> None:
    repo = Path(str(scenario.subject.get("repo", "")))
    if not repo.exists() or not (repo / ".git").exists():
        raise ScenarioError(f"subject repo/SHA unresolved: repo is not a git checkout: {repo}")
    for key in ("base", "head"):
        ref = scenario.subject.get(key)
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if proc.returncode != 0:
            raise ScenarioError(f"subject repo/SHA unresolved: {key} {ref!r} does not resolve in {repo}")
