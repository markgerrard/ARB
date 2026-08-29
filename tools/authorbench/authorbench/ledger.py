from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


HARD_BURNED_LINEAGES = {"warm-orchestrator", "memory-granted", "repo-history-granted"}
OVERRIDE_REQUIRED = {"possible(public-mirror)", "possible(fork-known)"}


@dataclass(frozen=True)
class BurnDecision:
    proceed: bool
    flagged: bool
    reason: str
    override_recorded: bool = False


def _valid_prior(prior_exposure: str) -> bool:
    return prior_exposure == "none-known" or (
        prior_exposure.startswith("possible(") and prior_exposure.endswith(")") and len(prior_exposure) > len("possible()")
    )


def check_burn(seat_lineage, brief, prior_exposure, *, override) -> BurnDecision:
    prior_exposure = str(prior_exposure)
    if not _valid_prior(prior_exposure):
        raise ValueError(f"invalid prior_exposure: {prior_exposure}")
    lineage = str(seat_lineage)
    if lineage in HARD_BURNED_LINEAGES or "warm-orchestrator" in lineage or "memory-granted" in lineage or "repo-history-granted" in lineage:
        return BurnDecision(False, True, "hard-burned-lineage", override_recorded=bool(override))
    if prior_exposure in OVERRIDE_REQUIRED and not override:
        return BurnDecision(False, True, "prior-exposure-requires-override")
    if prior_exposure in OVERRIDE_REQUIRED and override:
        return BurnDecision(True, True, "override-recorded", override_recorded=True)
    if prior_exposure != "none-known":
        return BurnDecision(True, True, "possible-other", override_recorded=bool(override))
    return BurnDecision(True, False, "none-known", override_recorded=bool(override))


def record(run, seat_lineage, brief, prior_exposure, override=None) -> None:
    if not _valid_prior(str(prior_exposure)):
        raise ValueError(f"invalid prior_exposure: {prior_exposure}")
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    row = {
        "seat_lineage": str(seat_lineage),
        "brief_id": str(brief),
        "prior_exposure": str(prior_exposure),
        "authored_at": time.time(),
        "override": override,
    }
    with (run / "exposure.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
