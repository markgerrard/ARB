from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.e2e import spine
from tests.e2e.corpus import iter_corpus
from tests.e2e import h2_harness
from tests.e2e.h2_harness import assert_case_expected
from tests.e2e.spine import E2EResult, E2EStatus


EXIT_CODES = {
    E2EStatus.PASS: 0,
    E2EStatus.BLOCK_FAIL: 1,
    E2EStatus.BLOCK_UNRUN: 2,
}


def run_suite(
    *,
    case_ids: list[str] | None = None,
    tmp: Path,
    status_path: Path = Path("e2e_status.json"),
) -> E2EResult:
    corpus = dict(iter_corpus())
    selected = corpus if case_ids is None else {case_id: corpus[case_id] for case_id in case_ids if case_id in corpus}
    passed = 0
    block_fail = 0
    block_unrun = 0
    details: list[str] = []
    for case_id, case in selected.items():
        try:
            h2_harness.assert_real_boundary_symbols()
            out = h2_harness.run_case(case, tmp / _safe_case_dir(case_id))
            spine.assert_h2_boundary_honest(out)
            assert_case_expected(case, out)
        except spine.BoundaryCanaryError as exc:
            block_unrun += 1
            details.append(f"{case_id}: {exc}")
        except AssertionError as exc:
            block_fail += 1
            details.append(f"{case_id}: {exc}")
        except Exception as exc:
            block_unrun += 1
            details.append(f"{case_id}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
    result = E2EResult.from_counts(
        case_count=len(selected),
        passed=passed,
        block_fail=block_fail,
        block_unrun=block_unrun,
        detail="; ".join(details),
    )
    status_path.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _safe_case_dir(case_id: str) -> str:
    return case_id.replace("/", "__")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tmp", type=Path, default=Path(".e2e-tmp"))
    parser.add_argument("--status-path", type=Path, default=Path("e2e_status.json"))
    args = parser.parse_args(argv)
    args.tmp.mkdir(parents=True, exist_ok=True)
    # The runner owns its hermeticity (GLM/codex review P1): invoked outside pytest (e.g. the §7
    # flip-gate "invokes the runner itself") there is no _hermetic fixture, so we MUST redirect the
    # shadow-log env here or run_case would append test records to the REAL production log
    # (~/.local/state/arb/h2-shadow-log.jsonl) and sum pre-existing records into the count assertion.
    import os
    state = args.tmp / "state"
    state.mkdir(parents=True, exist_ok=True)
    (args.tmp / "home").mkdir(parents=True, exist_ok=True)
    os.environ["ARB_H2_SHADOW_LOG"] = str(state / "h2-shadow-log.jsonl")
    os.environ["XDG_STATE_HOME"] = str(state)
    os.environ["HOME"] = str(args.tmp / "home")
    result = run_suite(tmp=args.tmp, status_path=args.status_path)
    return EXIT_CODES[result.status]


if __name__ == "__main__":
    raise SystemExit(main())
