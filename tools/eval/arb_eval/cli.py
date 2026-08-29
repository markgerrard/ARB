"""CLI — `plan` / `list` / `run` (verbs lifted from ccswarm's harness shape).

`plan` and `list` are fully runnable now (deterministic, no dispatch). `run` is wired to the
pipeline whose dispatch/normalize/match stages are the next increment — it fails loud rather
than pretending, so the suite never emits a floor verdict it did not actually measure.

Error handling (build-review): an unachievable target prints UNACHIEVABLE and exits non-zero
(never a cap-as-budget); malformed/missing scenarios print one clean line, not a traceback.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import boundary, gold, pipeline, power, publish, report, schema


def _die(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _target_from(sc) -> power.PowerTarget:
    p = sc.power or {}
    d = power.PowerTarget()
    return power.PowerTarget(
        V=p.get("V", d.V), nu=p.get("nu", d.nu), alpha=p.get("alpha", d.alpha),
        I_min=p.get("I_min", d.I_min), R_min=p.get("R_min", d.R_min),
        matcher_gate=p.get("matcher_gate", d.matcher_gate),
        matcher_true_recall=p.get("matcher_true_recall", d.matcher_true_recall),
    )


def _plan(args) -> int:
    sc = None
    t = power.PowerTarget()
    if args.scenario:
        try:
            sc = schema.load(args.scenario)
        except FileNotFoundError:
            return _die(f"scenario not found: {args.scenario}")
        except (schema.ScenarioError, json.JSONDecodeError) as e:
            return _die(f"invalid scenario {args.scenario}: {e}")
        t = _target_from(sc)
    try:
        b = power.compute(t)
    except power.Unachievable as e:
        print("ARB Instrument 1 — power budget")
        print(f"\n  target:  V={t.V}  noise≈{t.nu}  alpha={t.alpha}")
        return _die(f"UNACHIEVABLE — {e}")
    print("ARB Instrument 1 — power budget (derived from one target; do not set piecemeal)\n")
    print(f"  target:  V={t.V}  noise≈{t.nu}  alpha={t.alpha}  "
          f"I_min={t.I_min}  R_min={t.R_min}  matcher_gate={t.matcher_gate}")
    print(f"  derived: trials/class T = {b.trials}")
    print(f"           split          = {b.instances} instances x {b.repeats} repeats "
          f"(= {b.instances * b.repeats} >= {b.trials})")
    print(f"           gold/seat G_s  = {b.gold_per_seat}  (per-seat-stratified)")
    print(f"           control clusters: {b.control_loci_required} per class = FULL POWER "
          f"(EFFECTIVE controls = distinct why-clean clusters; correlated controls don't buy "
          f"independent confidence). Below it is small-n: the Wilson CI is wide, so only genuinely "
          f"separated data PASSes -- NOT a hard gate (repeats are pooled, no under-T veto). Low-cluster "
          f"UNKNOWNs mean too-few-INDEPENDENT-controls, not weak seats.")
    print(f"           expected-UNKNOWN band = a true-rate within +{b.expected_unknown_band} of noise "
          f"is expected to resolve UNKNOWN at full power")
    if sc is not None:
        ok = sc.class_level_ok(t.I_min)
        print(f"\n  scenario {sc.id!r}: classes {sc.classes()}")
        for c, good in ok.items():
            tag = "class-level OK" if good else f"INSTANCE-LEVEL only (< I_min={t.I_min} seeds)"
            print(f"    - {c}: {sc.instances_per_class()[c]} seed(s) -> {tag}")
        clusters = sc.control_cluster_count_per_class()
        nominal = sc.control_count_per_class()
        for c in sc.classes():
            actual = clusters.get(c, 0)
            print(f"    - {c}: {actual} effective control clusters ({nominal.get(c, 0)} nominal loci)"
                  + ("" if actual >= b.control_loci_required
                     else f"  WARNING: < required {b.control_loci_required} -> expect UNKNOWN "
                          f"(too few INDEPENDENT controls; adding correlated loci won't help)"))
        for language in sc.subject.get("languages", []):
            oracle = boundary.best_oracle_for_language(language)
            if oracle == "heuristic":
                print(
                    f"WARNING: boundary oracle for {language} is 'heuristic' "
                    f"(methods/nested/decorated defs may mis-resolve)"
                )
    return 0


def _list(args) -> int:
    d = Path(args.dir)
    if not d.exists():
        return _die(f"no scenarios dir at {d}")
    found = sorted(d.glob("*.json"))
    if not found:
        print(f"(no *.json scenarios in {d})")
        return 0
    for f in found:
        try:
            sc = schema.load(f)
            print(f"  {f.name}: {sc.id} — {len(sc.seeds)} seeds, {len(sc.panel)} seats, "
                  f"classes={sc.classes()}")
        except (schema.ScenarioError, json.JSONDecodeError) as e:
            print(f"  {f.name}: INVALID — {e}")
    return 0


def _run(args) -> int:
    try:
        sc = schema.load(args.scenario)
    except FileNotFoundError:
        return _die(f"scenario not found: {args.scenario}")
    except (schema.ScenarioError, json.JSONDecodeError) as e:
        return _die(f"invalid scenario {args.scenario}: {e}")
    try:
        schema.validate_subject(sc)
    except schema.ScenarioError as e:
        return _die(str(e))
    normalizer = None
    if args.normalizer:
        # e.g. --normalizer anthropic:MiniMax-M3 (off-quorum direct-API normalizer; resolves P-1)
        kind, _, model = args.normalizer.partition(":")
        if kind != "anthropic":
            return _die(f"unknown --normalizer kind {kind!r} (expected 'anthropic[:model]')")
        try:
            normalizer = pipeline.AnthropicNormalizer(model=model or "MiniMax-M3")
        except pipeline.Parked as e:
            return _die(str(e))
    dispatcher = None
    if args.confined:
        # Option C: confined dispatch — each seat runs jailed to the fixture (answer-key absent by
        # construction; canary proves it per dispatch). The proof is the canary, not a settable var.
        dispatcher = pipeline.ContainerDispatcher(scenario_path=args.scenario)
    seats = tuple(args.seat) if args.seat else None
    try:
        result = pipeline.run_floor(
            sc,
            dispatcher=dispatcher,
            normalizer=normalizer,
            normalizer_seat=args.normalizer_seat,
            output_root=args.output_root,
            repeats=args.repeats,
            seats=seats,
        )
    except pipeline.Parked as e:
        return _die(str(e))
    print(result.report_text)
    print(f"\nreport: {result.report_path}")
    print(f"events: {result.events_path}")
    return 0


def _gold(args) -> int:
    if args.gold_cmd == "export":
        gold.export(args.seat, args.out)
        return 0
    if args.gold_cmd == "rate":
        return _die("gold rate requires a configured rater callable; use the module API in this build")
    if args.gold_cmd == "ingest":
        gold.ingest(args.rater_file)
        return 0
    if args.gold_cmd == "score":
        summary = gold.score(args.seat)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    return _die(f"unknown gold command {args.gold_cmd!r}")


def _publish(args) -> int:
    run_dir = Path(args.run)
    if not run_dir.exists():
        run_dir = Path(args.output_root) / "runs" / args.run
    try:
        artifact = publish.build_artifact(run_dir, seat=args.seat)
    except (publish.PublishError, report.WallBreach) as e:
        return _die(str(e))
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="arb-eval", description="ARB Instrument 1 (floor capability)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="compute + print the power budget (and per-class validity)")
    p.add_argument("--scenario", help="scenario JSON (optional; defaults to the worked budget)")
    p.set_defaults(fn=_plan)

    p = sub.add_parser("list", help="list discovered scenarios")
    p.add_argument("--dir", default=str(Path(__file__).resolve().parents[1] / "scenarios"))
    p.set_defaults(fn=_list)

    p = sub.add_parser("run", help="run a scenario")
    p.add_argument("--scenario", required=True)
    p.add_argument("--normalizer-seat", default=pipeline.UNRESOLVED_OFFQUORUM,
                   help="off-quorum normalizer seat; default parks live run")
    p.add_argument("--normalizer", default=None,
                   help="direct-API normalizer, e.g. 'anthropic:MiniMax-M3' (off-quorum, temp=0)")
    p.add_argument("--repeats", type=int, default=None,
                   help="override repeats/seat (default = power budget); use 1 for a cheap first smoke")
    p.add_argument("--confined", action="store_true",
                   help="confined dispatch (Option C): each seat runs jailed to the fixture, answer-key "
                        "absent by construction; canary-proven per dispatch")
    p.add_argument("--seat", action="append", default=None,
                   help="restrict the panel to these seats (repeatable; e.g. --seat codex)")
    p.add_argument("--output-root", default="floor")
    p.set_defaults(fn=_run)

    p = sub.add_parser("gold", help="gold matcher-validation workflow")
    gold_sub = p.add_subparsers(dest="gold_cmd", required=True)
    gp = gold_sub.add_parser("export")
    gp.add_argument("--seat", required=True)
    gp.add_argument("--out", required=True)
    gp.set_defaults(fn=_gold)
    gp = gold_sub.add_parser("rate")
    gp.add_argument("--rater", required=True)
    gp.add_argument("--packets", required=True)
    gp.add_argument("--out", required=True)
    gp.set_defaults(fn=_gold)
    gp = gold_sub.add_parser("ingest")
    gp.add_argument("rater_file")
    gp.set_defaults(fn=_gold)
    gp = gold_sub.add_parser("score")
    gp.add_argument("--seat", required=True)
    gp.set_defaults(fn=_gold)

    p = sub.add_parser("publish", help="emit a change-event artifact for a run")
    p.add_argument("--run", required=True)
    p.add_argument("--seat", default=None)
    p.add_argument("--output-root", default="floor")
    p.set_defaults(fn=_publish)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
