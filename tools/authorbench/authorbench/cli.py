from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from . import bundle, factkey, ledger, run


DEFAULT_PANEL = ["codex", "pi-glm", "m3"]
TARGET_IDS = {
    "codex": "codex-bridge-dev",
    "pi-glm": "pi-sdk-bridge-dev-glm",
    "m3": "pi-sdk-bridge-dev-minimax-m3",
    "agy": "agy-bridge-dev",
}


def _panel(value: str | None) -> list[str]:
    return DEFAULT_PANEL if not value else [item.strip() for item in value.split(",") if item.strip()]


def _agy_unlocked() -> bool:
    return os.environ.get("AUTHORBENCH_AGY_AUTH_SPIKE") == "1"


def _check_panel(panel: list[str]) -> None:
    unknown = [item for item in panel if item not in TARGET_IDS]
    if unknown:
        raise SystemExit(f"unknown judge panel seat(s): {', '.join(unknown)}")
    if "agy" in panel and not _agy_unlocked():
        raise SystemExit("agy judge is locked until agy-auth-spike lands")


def _corpus_bundle(brief: str, corpus_version: str) -> Path:
    return Path("tools") / "authorbench" / "corpus" / corpus_version / brief


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="authorbench")
    sub = parser.add_subparsers(dest="cmd", required=True)

    freeze = sub.add_parser("freeze")
    freeze.add_argument("src")
    freeze.add_argument("--stage", choices=["design", "spec", "plan"], required=True)
    freeze.add_argument("--corpus-version", required=True)
    group = freeze.add_mutually_exclusive_group(required=True)
    group.add_argument("--base-sha")
    group.add_argument("--artefact-commit")

    validate = sub.add_parser("factkey-validate")
    validate.add_argument("--brief", required=True)
    validate.add_argument("--corpus-version", default="v1.0")

    author = sub.add_parser("author")
    author.add_argument("--candidate", required=True)
    author.add_argument("--brief", required=True)
    author.add_argument("--corpus-version", default="v1.0")
    author.add_argument("--override")
    author.add_argument("--seat-lineage")
    author.add_argument("--prior-exposure", default="none-known")

    judge = sub.add_parser("judge")
    judge.add_argument("--run", required=True)
    judge.add_argument("--panel")

    score = sub.add_parser("score")
    score.add_argument("--run", required=True)

    report = sub.add_parser("report")
    report.add_argument("--run", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("--candidate", required=True)
    run_p.add_argument("--corpus-version", default="v1.0")
    run_p.add_argument("--panel")
    run_p.add_argument("--baseline-seat")

    ledger_p = sub.add_parser("ledger")
    ledger_p.add_argument("--run", required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "freeze":
        bundle.freeze(args.src, corpus_version=args.corpus_version, base_sha=args.base_sha, artefact_commit=args.artefact_commit)
        return 0
    if args.cmd == "factkey-validate":
        bdir = _corpus_bundle(args.brief, args.corpus_version)
        fk = factkey.load_factkey(bdir / "fact_key.yaml")
        with tempfile.TemporaryDirectory() as td:
            export = factkey.export_at(fk.base_sha, td)
            errors = factkey.validate(fk, export)
        if errors:
            for error in errors:
                print(error.message, file=sys.stderr)
            return 1
        return 0
    if args.cmd == "author":
        decision = ledger.check_burn(
            args.seat_lineage or args.candidate,
            args.brief,
            args.prior_exposure,
            override=args.override,
        )
        if not decision.proceed:
            raise SystemExit(decision.reason)
        run.author_turn(
            candidate=args.candidate,
            brief=args.brief,
            seat_lineage=args.seat_lineage or args.candidate,
            prior_exposure=args.prior_exposure,
            override=args.override,
        )
        return 0
    if args.cmd == "judge":
        panel = _panel(args.panel)
        _check_panel(panel)
        run.judge_phase(args.run, panel=panel)
        return 0
    if args.cmd == "score":
        run.score_run(args.run)
        return 0
    if args.cmd == "report":
        run.report_run(args.run)
        return 0
    if args.cmd == "run":
        panel = _panel(args.panel)
        _check_panel(panel)
        run.run_bench(args.candidate, args.corpus_version, baseline=args.baseline_seat, panel=panel)
        return 0
    if args.cmd == "ledger":
        print(Path(args.run) / "exposure.ndjson")
        return 0
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
