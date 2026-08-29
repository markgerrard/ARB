"""calibration — close the review loop against OUTCOMES, report-only.

The review machinery decides each case safely but never learns whether its calls
held up. This is the decision-quality calibration loop. It is deliberately
*report-only*: it surfaces per-reviewer / per-decision-type stats and flags
anomalies AS QUESTIONS for a human to investigate. It NEVER auto-tunes reviewer
weights or gates — auto-tuning a "noisy" seat would silently kill the diversity
that catches correlated blind spots (the low-confirmation seat may be the only
one that ever looks at, say, security, and a low rate may mean those findings are
hard to confirm, not that the seat is bad). The loop's job is to make a human
*ask*, not to answer by adjusting a weight.

Design decisions (locked with the maintainer):
- **Ground truth is MANUAL.** "Held" / "regressed" / "confirmed" / "false_positive"
  is a claim about the world; only a human observation backed by an evidence
  artifact establishes it. No commit pattern is treated as fact. (A git-hint layer
  that *suggests* outcomes for a human to confirm is a possible v1.5 — NOT built
  here, and it must earn its place against real annotations before it's trusted.)
- **Capture at close-time.** `record` is run when a review closes, when the
  verdicts / taxonomy / diff range / finding ids are live and free to serialise.
- **No prose backfill.** Records are never parsed out of triage prose (that is the
  prohibited observation->summary->fact workflow). History, if wanted, is
  hand-recorded via `annotate ... --source backfill:<doc>` at the same evidentiary
  standard, and weighted as the weaker, retrospective class it is.
- **Assessment-age is first-class.** A "held" is only held *as of when it was
  looked at*. Every outcome carries assessed_at; the report shows age and flags
  stale "held" marks for re-confirmation rather than collapsing to a bare ratio
  ("held" = "not caught regressing yet", not "correct").

Storage: one append-only JSONL log (default `.calibration/log.jsonl`), entries
typed `decision` | `outcome`.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG = ".calibration/log.jsonl"
FINDING_STATUSES = {"confirmed", "false_positive"}   # was a raised finding real?
DECISION_STATUSES = {"held", "regressed"}            # did an accepted decision hold?
ALL_STATUSES = FINDING_STATUSES | DECISION_STATUSES
STALE_DAYS_DEFAULT = 90
NOISY_MIN_RESOLVED = 3       # need this many resolved findings before judging a seat
NOISY_RATE = 0.5             # confirmation rate below this is flagged AS A QUESTION
MODEL_DRIFT_MIN_RESOLVED = 5
MODEL_DRIFT_DELTA = 0.30
MODEL_DRIFT_EPSILON = 1e-9


def _now(now: str | None) -> str:
    return now if now else datetime.now(timezone.utc).isoformat()


def append(log_path: Path, entry: dict) -> dict:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    return entry


def load(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def record_decision(
    log_path: Path, *, scope: str, decision: str, reviewers: list[tuple[str, str]],
    findings: list[dict], diff_range: str = "", note: str = "",
    decision_id: str | None = None, now: str | None = None,
    seat_models: dict[str, str] | None = None,
) -> dict:
    entry = {
        "type": "decision",
        "id": decision_id or uuid.uuid4().hex[:12],
        "ts": _now(now),
        "scope": scope,
        "decision": decision,
        "diff_range": diff_range,
        "reviewers": [{"name": n, "verdict": v} for n, v in reviewers],
        "findings": findings,
        "note": note,
    }
    if seat_models is not None:
        entry["seat_models"] = dict(seat_models)
    return append(log_path, entry)


def record_outcome(
    log_path: Path, *, decision_id: str, status: str, evidence: str,
    finding_id: str = "", source: str = "live", note: str = "", now: str | None = None,
) -> dict:
    if status not in ALL_STATUSES:
        raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}, got {status!r}")
    if status in FINDING_STATUSES and not finding_id:
        raise ValueError(f"status {status!r} is a finding outcome and needs --finding")
    if status in DECISION_STATUSES and finding_id:
        raise ValueError(f"status {status!r} is a decision outcome; do not pass --finding")
    if not evidence:
        raise ValueError("an outcome needs --evidence (no outcome from an observation alone)")
    entry = {
        "type": "outcome",
        "id": uuid.uuid4().hex[:12],
        "assessed_at": _now(now),
        "decision_id": decision_id,
        "finding_id": finding_id,
        "status": status,
        "evidence": evidence,
        "source": source,
        "note": note,
    }
    return append(log_path, entry)


def _age_days(ts: str, now: str) -> int:
    try:
        then = datetime.fromisoformat(ts)
        cur = datetime.fromisoformat(now)
        return max(0, (cur - then).days)
    except (ValueError, TypeError):
        return 0


def build_report(entries: list[dict], *, now: str | None = None, stale_days: int = STALE_DAYS_DEFAULT) -> dict:
    now = _now(now)
    decisions = [e for e in entries if e.get("type") == "decision"]
    outcomes = [e for e in entries if e.get("type") == "outcome"]

    # Map (decision_id, finding_id) -> raising reviewer. Finding ids are LOCAL
    # labels (e.g. "f1") that collide across decisions, so they MUST be scoped by
    # decision_id — otherwise one review's outcome silently overwrites another's
    # and the wrong reviewer gets credited/blamed.
    finding_reviewer: dict[tuple[str, str], str] = {}
    finding_model: dict[tuple[str, str], str] = {}
    for d in decisions:
        models = d.get("seat_models", {}) or {}
        for f in d.get("findings", []):
            key = (d["id"], f["id"])
            reviewer = f.get("reviewer", "?")
            finding_reviewer[key] = reviewer
            finding_model[key] = models.get(reviewer, "unknown")

    # Latest outcome per (decision_id, finding_id) / per decision (append-only: last write wins).
    finding_outcome: dict[tuple[str, str], dict] = {}
    decision_outcomes: list[dict] = []
    for o in outcomes:
        if o.get("finding_id"):
            finding_outcome[(o.get("decision_id", ""), o["finding_id"])] = o
        else:
            decision_outcomes.append(o)

    # Per-reviewer finding calibration.
    reviewers: dict[str, dict] = {}
    for key, rev in finding_reviewer.items():
        r = reviewers.setdefault(
            rev,
            {
                "raised": 0,
                "confirmed": 0,
                "false_positive": 0,
                "pending": 0,
                "by_model": {},
            },
        )
        model = finding_model.get(key, "unknown")
        mr = r["by_model"].setdefault(
            model,
            {"raised": 0, "confirmed": 0, "false_positive": 0, "pending": 0},
        )
        r["raised"] += 1
        mr["raised"] += 1
        o = finding_outcome.get(key)
        if o is None:
            r["pending"] += 1
            mr["pending"] += 1
        elif o["status"] == "confirmed":
            r["confirmed"] += 1
            mr["confirmed"] += 1
        elif o["status"] == "false_positive":
            r["false_positive"] += 1
            mr["false_positive"] += 1
    for r in reviewers.values():
        resolved = r["confirmed"] + r["false_positive"]
        r["resolved"] = resolved
        r["confirmation_rate"] = (r["confirmed"] / resolved) if resolved else None
        for mr in r["by_model"].values():
            resolved = mr["confirmed"] + mr["false_positive"]
            mr["resolved"] = resolved
            mr["confirmation_rate"] = (mr["confirmed"] / resolved) if resolved else None

    # Per-decision-type calibration + stale "held" tracking.
    dtype: dict[str, dict] = {}
    decision_type = {d["id"]: d.get("decision", "?") for d in decisions}
    latest_decision_outcome: dict[str, dict] = {}
    for o in decision_outcomes:
        latest_decision_outcome[o["decision_id"]] = o
    stale_held: list[dict] = []
    for did, o in latest_decision_outcome.items():
        t = dtype.setdefault(decision_type.get(did, "?"), {"held": 0, "regressed": 0})
        if o["status"] == "held":
            t["held"] += 1
            if _age_days(o["assessed_at"], now) > stale_days:
                stale_held.append({"decision_id": did, "assessed_at": o["assessed_at"],
                                    "age_days": _age_days(o["assessed_at"], now)})
        elif o["status"] == "regressed":
            t["regressed"] += 1
    decisions_pending = [d["id"] for d in decisions if d["id"] not in latest_decision_outcome]

    # Anomalies — phrased as QUESTIONS, never conclusions.
    anomalies: list[str] = []
    for name, r in reviewers.items():
        if r["resolved"] >= NOISY_MIN_RESOLVED and r["confirmation_rate"] is not None and r["confirmation_rate"] < NOISY_RATE:
            anomalies.append(
                f"{name}: {r['confirmed']}/{r['resolved']} findings confirmed "
                f"({r['confirmation_rate']:.0%}). Noisy seat — or are its findings hard to confirm, "
                f"or is the confirmation process lagging? Investigate; do NOT down-weight blindly."
            )
        qualified = {
            model: mr for model, mr in r["by_model"].items()
            if mr["resolved"] >= MODEL_DRIFT_MIN_RESOLVED and mr["confirmation_rate"] is not None
        }
        if len(qualified) >= 2:
            rates = [mr["confirmation_rate"] for mr in qualified.values()]
            if max(rates) - min(rates) >= MODEL_DRIFT_DELTA - MODEL_DRIFT_EPSILON:
                parts = [
                    f"{model}={mr['confirmation_rate']:.0%} ({mr['resolved']} records)"
                    for model, mr in sorted(qualified.items())
                ]
                anomalies.append(
                    f"{name}: model-version drift question: confirmation rates differ by "
                    f"{max(rates) - min(rates):.0%} across qualified models "
                    f"({'; '.join(parts)}). Investigate model-version drift; report-only."
                )
    for t, c in dtype.items():
        if c["regressed"]:
            anomalies.append(
                f"decision-type '{t}': {c['regressed']} regressed vs {c['held']} held. "
                f"What did the regressed calls have in common?"
            )
    if stale_held:
        anomalies.append(
            f"{len(stale_held)} 'held' mark(s) older than {stale_days}d — 'held' means "
            f"'not caught regressing yet', not 'correct'. Re-confirm before trusting them."
        )

    return {
        "generated_at": now,
        "decisions": len(decisions),
        "outcomes": len(outcomes),
        "reviewers": reviewers,
        "decision_types": dtype,
        "decisions_pending_outcome": decisions_pending,
        "stale_held": stale_held,
        "anomalies": anomalies,
    }


def format_report(rep: dict) -> str:
    L = [f"# Calibration report ({rep['generated_at']})",
         f"decisions={rep['decisions']} outcomes={rep['outcomes']}", ""]
    if rep["decisions"] == 0:
        L.append("Insufficient data: the decision log is empty. This is the honest "
                 "starting state — it fills with trustworthy close-time records as reviews close.")
        return "\n".join(L)
    L.append("## Per-reviewer finding calibration")
    if not rep["reviewers"]:
        L.append("(no findings recorded yet)")
    for name, r in sorted(rep["reviewers"].items()):
        rate = "n/a" if r["confirmation_rate"] is None else f"{r['confirmation_rate']:.0%}"
        L.append(f"- {name}: raised {r['raised']} | confirmed {r['confirmed']} | "
                 f"false-positive {r['false_positive']} | pending {r['pending']} | confirm-rate {rate}")
        by_model = r.get("by_model", {})
        if len(by_model) >= 2:
            L.append("  by model:")
            for model, mr in sorted(by_model.items()):
                model_rate = "n/a" if mr["confirmation_rate"] is None else f"{mr['confirmation_rate']:.0%}"
                L.append(f"  - {model}: raised {mr['raised']} | confirmed {mr['confirmed']} | "
                         f"false-positive {mr['false_positive']} | pending {mr['pending']} | "
                         f"confirm-rate {model_rate}")
    L.append("")
    L.append("## Per-decision-type calibration")
    if not rep["decision_types"]:
        L.append("(no decision outcomes recorded yet)")
    for t, c in sorted(rep["decision_types"].items()):
        L.append(f"- {t}: held {c['held']} | regressed {c['regressed']}")
    if rep["decisions_pending_outcome"]:
        L.append("")
        L.append(f"## Pending outcome ({len(rep['decisions_pending_outcome'])})")
        L.append("Decisions with no recorded outcome yet (not counted as held — unknown):")
        L.append("  " + ", ".join(rep["decisions_pending_outcome"]))
    L.append("")
    L.append("## Anomalies to investigate (questions, not verdicts)")
    if not rep["anomalies"]:
        L.append("(none flagged)")
    for a in rep["anomalies"]:
        L.append(f"- {a}")
    L.append("")
    L.append("NOTE: report-only. These are prompts for human investigation; the loop "
             "never adjusts reviewer weights or gates.")
    return "\n".join(L)


def _parse_reviewer(spec: str) -> tuple[str, str]:
    name, _, verdict = spec.partition("=")
    if not name or not verdict:
        raise ValueError(f"--reviewer wants name=verdict, got {spec!r}")
    return name.strip(), verdict.strip()


def _parse_seat_model(spec: str) -> tuple[str, str]:
    seat, _, model = spec.partition("=")
    if not seat or not model:
        raise ValueError(f"--seat-model wants seat=model, got {spec!r}")
    return seat.strip(), model.strip()


def _parse_finding(spec: str) -> dict:
    # ID[:reviewer[:title...]]
    parts = spec.split(":", 2)
    fid = parts[0].strip()
    if not fid:
        raise ValueError(f"--finding wants ID[:reviewer[:title]], got {spec!r}")
    return {"id": fid, "reviewer": (parts[1].strip() if len(parts) > 1 else "?"),
            "title": (parts[2].strip() if len(parts) > 2 else "")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="calib", description="Decision-quality calibration loop (report-only).")
    ap.add_argument("--log", default=DEFAULT_LOG, help=f"append-only JSONL log (default {DEFAULT_LOG})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="record a closed review decision (run at close-time)")
    rec.add_argument("--scope", required=True, help="what was reviewed, e.g. phase3-i5a")
    rec.add_argument("--decision", required=True, help="taxonomy outcome, e.g. consensus-accept|override|request-changes")
    rec.add_argument("--reviewer", action="append", default=[], metavar="NAME=VERDICT")
    rec.add_argument("--seat-model", action="append", default=[], metavar="SEAT=MODEL")
    rec.add_argument("--finding", action="append", default=[], metavar="ID[:reviewer[:title]]")
    rec.add_argument("--diff-range", default="")
    rec.add_argument("--note", default="")
    rec.add_argument("--id", dest="decision_id", default=None, help="override the decision id (else random)")

    ann = sub.add_parser("annotate", help="record an OUTCOME against a decision/finding (manual ground truth)")
    ann.add_argument("--decision", required=True, dest="decision_id", help="decision id to annotate")
    ann.add_argument("--finding", default="", dest="finding_id", help="finding id (for confirmed|false_positive)")
    ann.add_argument("--status", required=True, choices=sorted(ALL_STATUSES))
    ann.add_argument("--evidence", required=True, help="the artifact establishing this outcome")
    ann.add_argument("--source", default="live", help="live (default) or backfill:<doc>")
    ann.add_argument("--note", default="")

    rep = sub.add_parser("report", help="print the calibration report (report-only)")
    rep.add_argument("--stale-days", type=int, default=STALE_DAYS_DEFAULT)
    rep.add_argument("--json", action="store_true", help="emit the raw report JSON")
    rep.add_argument("--strict", action="store_true", help="exit 1 if any anomaly is flagged (CI nag)")

    sub.add_parser("list", help="dump the raw log")

    args = ap.parse_args(argv)
    log_path = Path(args.log)

    try:
        if args.cmd == "record":
            d = record_decision(
                log_path, scope=args.scope, decision=args.decision,
                reviewers=[_parse_reviewer(s) for s in args.reviewer],
                findings=[_parse_finding(s) for s in args.finding],
                diff_range=args.diff_range, note=args.note, decision_id=args.decision_id,
                seat_models=dict(_parse_seat_model(s) for s in args.seat_model) or None,
            )
            print(d["id"])
            return 0
        if args.cmd == "annotate":
            record_outcome(
                log_path, decision_id=args.decision_id, status=args.status,
                evidence=args.evidence, finding_id=args.finding_id, source=args.source, note=args.note,
            )
            print("recorded outcome")
            return 0
        if args.cmd == "report":
            rep_obj = build_report(load(log_path), stale_days=args.stale_days)
            print(json.dumps(rep_obj, indent=2) if args.json else format_report(rep_obj))
            return 1 if (args.strict and rep_obj["anomalies"]) else 0
        if args.cmd == "list":
            for e in load(log_path):
                print(json.dumps(e))
            return 0
    except (ValueError, OSError) as exc:
        print(f"calib: {exc}", file=__import__("sys").stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
