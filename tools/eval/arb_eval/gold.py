"""Gold matcher-validation workflow."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import stats

GOLD_ROOT = Path(__file__).resolve().parents[1] / "gold"
PRIMARY_RATERS = ("mark", "model-alias-1")
TIE_BREAKER = "grok"


def _seat_dir(seat: str) -> Path:
    return GOLD_ROOT / seat


def _read_pairs(seat: str) -> list[dict[str, Any]]:
    path = _seat_dir(seat) / "pairs.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_pairs(seat: str, pairs: list[dict[str, Any]]) -> None:
    path = _seat_dir(seat) / "pairs.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(p, sort_keys=True) for p in pairs) + ("\n" if pairs else ""))


def _gold_version(seat: str) -> str:
    path = _seat_dir(seat) / "pairs.ndjson"
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def _snippet(pair: dict[str, Any]) -> str:
    loc = ((pair.get("pipeline") or {}).get("normalized") or {}).get("location") or {}
    if not loc and isinstance(pair.get("raw_finding"), dict):
        loc = (pair.get("raw_finding") or {}).get("location") or {}
    repo = Path(str((pair.get("context") or {}).get("repo", "")))
    file = loc.get("file")
    line = loc.get("line")
    if not file or not line:
        return ""
    path = repo / file
    if not path.exists():
        return ""
    lines = path.read_text(errors="ignore").splitlines()
    idx = int(line) - 1
    lo = max(0, idx - 1)
    hi = min(len(lines), idx + 2)
    return "\n".join(lines[lo:hi])


def export(seat: str, out: str | Path) -> None:
    packets = []
    for pair in _read_pairs(seat):
        packets.append({
            "pair_id": pair["pair_id"],
            "seat": pair.get("seat", seat),
            "raw_finding": pair.get("raw_finding"),
            "snippet": _snippet(pair),
            "context": pair.get("context", {}),
        })
    Path(out).write_text("\n".join(json.dumps(p, sort_keys=True) for p in packets) + ("\n" if packets else ""))


def _parse_verdict(text: str) -> str | None:
    m = re.search(r"VERDICT:\s*(match|nonmatch|ambiguous)\b", text, re.I)
    return m.group(1).lower() if m else None


def rate(rater: str, packets: str | Path, out: str | Path,
         call: Callable[[dict[str, Any]], str] | None = None) -> None:
    if call is None:
        raise RuntimeError("gold.rate requires an injected rater callable in this environment")
    rows = []
    for line in Path(packets).read_text().splitlines():
        if not line.strip():
            continue
        packet = json.loads(line)
        raw = call(packet)
        verdict = _parse_verdict(raw)
        if verdict is None:
            raw = call(packet)
            verdict = _parse_verdict(raw)
        row = {"pair_id": packet["pair_id"], "rater": rater, "ts": time.time()}
        if verdict is None:
            row["verdict"] = "rater_error"
            row["raw_response"] = raw
        else:
            row["verdict"] = verdict
            row["raw_response"] = raw
        rows.append(row)
    Path(out).write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + ("\n" if rows else ""))


def ingest(rater_file: str | Path) -> None:
    rows = [json.loads(line) for line in Path(rater_file).read_text().splitlines() if line.strip()]
    if not rows:
        return
    pair_to_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pair_to_rows.setdefault(row["pair_id"], []).append(row)
    seats = {row.get("seat") for row in rows if row.get("seat")}
    if not seats:
        for seat_dir in GOLD_ROOT.iterdir():
            if not seat_dir.is_dir():
                continue
            pairs = _read_pairs(seat_dir.name)
            if any(p["pair_id"] in pair_to_rows for p in pairs):
                seats.add(seat_dir.name)
    for seat in seats:
        pairs = _read_pairs(str(seat))
        for pair in pairs:
            for row in pair_to_rows.get(pair["pair_id"], []):
                if row.get("verdict") == "rater_error":
                    continue
                pair.setdefault("adjudications", []).append({
                    "rater": row["rater"], "verdict": row["verdict"], "ts": row.get("ts", time.time())
                })
            pair["final_verdict"] = _final_verdict(pair.get("adjudications", []))
        _write_pairs(str(seat), pairs)


def _final_verdict(adjudications: list[dict[str, Any]]) -> str | None:
    votes = {a["rater"]: a["verdict"] for a in adjudications if a.get("verdict") != "rater_error"}
    prim = [votes[r] for r in PRIMARY_RATERS if r in votes]
    if len(prim) == 2 and prim[0] == prim[1]:
        return prim[0]
    if TIE_BREAKER in votes:
        return votes[TIE_BREAKER]
    return prim[0] if len(prim) == 1 else None


def score(seat: str, matcher_gate: float = 0.85) -> dict[str, Any]:
    pairs = _read_pairs(seat)
    gold_matches = [p for p in pairs if p.get("final_verdict") == "match"]
    system_matches = [p for p in pairs if (p.get("pipeline") or {}).get("match_outcome") == "detected"]
    true_system_matches = [p for p in system_matches if p.get("final_verdict") == "match"]
    recall_i = stats.wilson(len(true_system_matches), len(gold_matches))
    precision_i = stats.wilson(len(true_system_matches), len(system_matches))
    primary_a, primary_b = [], []
    for pair in pairs:
        votes = {a["rater"]: a["verdict"] for a in pair.get("adjudications", [])}
        if all(r in votes for r in PRIMARY_RATERS):
            primary_a.append(votes[PRIMARY_RATERS[0]])
            primary_b.append(votes[PRIMARY_RATERS[1]])
    if primary_a:
        raw_agreement = sum(1 for a, b in zip(primary_a, primary_b) if a == b) / len(primary_a)
        kappa = stats.cohen_kappa(primary_a, primary_b)
    else:
        raw_agreement = 0.0
        kappa = 0.0
    summary = {
        "seat": seat,
        "gold_version": _gold_version(seat),
        "n_pairs": len(pairs),
        "recall": {"k": len(true_system_matches), "n": len(gold_matches),
                   "lo": recall_i.lo, "hi": recall_i.hi},
        "precision": {"k": len(true_system_matches), "n": len(system_matches),
                      "lo": precision_i.lo, "hi": precision_i.hi},
        "kappa": kappa,
        "raw_agreement": raw_agreement,
        "matcher_gate": matcher_gate,
    }
    path = _seat_dir(seat) / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def load_summary(seat: str) -> dict[str, Any] | None:
    path = _seat_dir(seat) / "summary.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())
