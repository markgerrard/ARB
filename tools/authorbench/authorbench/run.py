from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import bundle, factkey, jail, judge, ledger, normalize, report, rubric, score, store


class BurnRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    candidate: str
    corpus: str
    baseline: str | None
    run_dir: str | None = None
    stages: list[str] = field(default_factory=list)


def freeze_check(corpus) -> None:
    path = Path(corpus)
    if path.exists() and not (path / "bundle.yaml").exists() and not any(path.glob("*/bundle.yaml")):
        raise FileNotFoundError(f"no authorbench bundles under {path}")


def _run_dir(run_dir=None, run_id=None) -> Path:
    if run_dir is not None:
        return Path(run_dir)
    if run_id is not None:
        return Path(run_id)
    return Path("tools") / "authorbench" / "runs" / f"run-{int(time.time())}"


def _bundle_from(value, corpus_version: str = "v1.0") -> bundle.Bundle:
    path = Path(str(value))
    if not path.exists():
        path = Path("tools") / "authorbench" / "corpus" / corpus_version / str(value)
    return bundle.load(path)


def _brief_id(src: bundle.Bundle) -> str:
    return str(src.manifest["id"])


def _bundle_dir(run_dir: Path, brief_id: str | None) -> Path:
    brief_bundle = run_dir / "bundles" / brief_id
    if brief_bundle.exists():
        return brief_bundle
    return run_dir / "bundle"


def _copy_bundle(src: bundle.Bundle, run_dir: Path) -> Path:
    dest = run_dir / "bundles" / _brief_id(src)
    if dest.exists():
        return dest
    shutil.copytree(src.path, dest)
    return dest


def _write_author_visible(profile: jail.JailProfile, run_dir: Path, brief_id: str) -> Path:
    dest = run_dir / "author-visible" / brief_id
    dest.mkdir(parents=True, exist_ok=True)
    mount = next(mount for mount in profile.mounts if mount.name == "author_visible")
    for name, value in (mount.files or {}).items():
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding="utf-8")
    return dest


def _mount_path(profile: jail.JailProfile, name: str) -> Path:
    return next(mount.path for mount in profile.mounts if mount.name == name)


def _runner_env(profile: jail.JailProfile, run_dir: Path, *, brief_id: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if profile.role == "author":
        if brief_id is None:
            brief_id = _mount_path(profile, "author_visible").parent.name
        workspace = run_dir / "export"
        workspace.mkdir(parents=True, exist_ok=True)
        env["AUTHORBENCH_WORKSPACE"] = str(workspace)
        env["AUTHORBENCH_AUTHOR_VISIBLE"] = str(_write_author_visible(profile, run_dir, brief_id))
    else:
        rubric_path = _mount_path(profile, "rubric")
        if not rubric_path.exists():
            rubric_path.parent.mkdir(parents=True, exist_ok=True)
            rubric_path.write_text(json.dumps(rubric.RUBRIC, sort_keys=True, indent=2), encoding="utf-8")
        env["AUTHORBENCH_EXPORT"] = str(_mount_path(profile, "export"))
        env["AUTHORBENCH_DRAFT"] = str(_mount_path(profile, "draft"))
        env["AUTHORBENCH_FACT_KEY"] = str(_mount_path(profile, "fact_key"))
        env["AUTHORBENCH_RUBRIC"] = str(rubric_path)
    return env


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iter_drafts(run_path: Path):
    drafts_root = run_path / "drafts"
    if not drafts_root.exists():
        return
    for seat_dir in sorted(drafts_root.iterdir()):
        if not seat_dir.is_dir():
            continue
        nested = sorted(
            brief_dir for brief_dir in seat_dir.iterdir()
            if brief_dir.is_dir() and (brief_dir / "raw.md").exists()
        )
        if nested:
            for brief_dir in nested:
                yield seat_dir.name, brief_dir.name, brief_dir / "raw.md"
        elif (seat_dir / "raw.md").exists():
            yield seat_dir.name, None, seat_dir / "raw.md"


def _default_canary_runner(*, role: str, surface: Path, **kwargs) -> None:
    subprocess.run(
        ["tools/authorbench/confinement/authorbench-canary.sh", role, str(surface)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _default_author_runner(*, role: str, prompt: str, **kwargs) -> str:
    profile = kwargs["profile"]
    run_dir = Path(kwargs["run_dir"])
    brief_id = _brief_id(kwargs["bundle"]) if isinstance(kwargs.get("bundle"), bundle.Bundle) else None
    result = subprocess.run(
        ["tools/authorbench/confinement/confined-authorbench.sh", role, "run", prompt],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_runner_env(profile, run_dir, brief_id=brief_id),
    )
    return result.stdout


def _default_judge_runner(panel, packet, **kwargs) -> dict[str, str]:
    profile = kwargs["profile"]
    run_dir = Path(kwargs["run_dir"])
    env = _runner_env(profile, run_dir)
    replies: dict[str, str] = {}
    prompt = json.dumps(packet, sort_keys=True)
    for seat in panel:
        result = subprocess.run(
            ["tools/authorbench/confinement/confined-authorbench.sh", "judge", "run", str(packet.get(str(seat), prompt))],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        replies[str(seat)] = result.stdout
    return replies


def author_turn(
    *,
    candidate,
    brief,
    bundle_obj=None,
    corpus_version="v1.0",
    seat_lineage=None,
    prior_exposure="none-known",
    override=None,
    run_dir=None,
    author_runner=None,
    canary_runner=None,
    export_dir=None,
) -> dict[str, Any]:
    run_path = _run_dir(run_dir)
    b = bundle_obj if isinstance(bundle_obj, bundle.Bundle) else _bundle_from(bundle_obj or brief, corpus_version)
    decision = ledger.check_burn(seat_lineage or candidate, brief, prior_exposure, override=override)
    if not decision.proceed:
        raise BurnRefused(decision.reason)
    brief_id = _brief_id(b)
    _copy_bundle(b, run_path)
    export_path = Path(export_dir) if export_dir is not None else run_path / "export"
    if not export_path.exists() and b.manifest.get("base_sha"):
        factkey.export_at(str(b.manifest["base_sha"]), export_path)
    profile = jail.author_profile(b, b.manifest.get("base_sha", "HEAD"))
    jail.assert_manifest(profile, "author")
    recorder = store.Recorder(run_path)
    recorder.write(
        {
            "event": "author_dispatch",
            "seat": str(candidate),
            "seat_lineage": str(seat_lineage or candidate),
            "brief_id": brief_id,
            "role": "author",
            "profile_ok": True,
            "prior_exposure": prior_exposure,
        }
    )
    (canary_runner or _default_canary_runner)(role="author", surface=b.path, profile=profile, run_dir=run_path)
    prompt = "\n\n".join([b.author_visible_subset()["brief.md"], *[v for k, v in b.author_visible_subset().items() if str(k).startswith("steer/")]])
    raw = (author_runner or _default_author_runner)(role="author", candidate=candidate, prompt=prompt, bundle=b, profile=profile, run_dir=run_path)
    raw = raw.stdout if hasattr(raw, "stdout") else str(raw)
    draft_dir = run_path / "drafts" / str(candidate) / brief_id
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "raw.md").write_text(raw, encoding="utf-8")
    legacy_draft = run_path / "drafts" / str(candidate) / "raw.md"
    if not legacy_draft.exists():
        legacy_draft.write_text(raw, encoding="utf-8")
    recorder.write(
        {
            "event": "author_draft",
            "seat": str(candidate),
            "brief_id": brief_id,
            "raw_sha256": _sha256_text(raw),
            "line_count": len(raw.splitlines()),
        }
    )
    return {"candidate": candidate, "brief": brief, "burn_decision": decision.reason, "run_dir": str(run_path)}


def judge_phase(run_id=None, *, panel=None, run_dir=None, judge_runner=None, canary_runner=None, author_identity_tokens=None, **kwargs) -> dict[str, Any]:
    run_path = _run_dir(run_dir, run_id)
    panel = list(panel or [])
    recorder = store.Recorder(run_path)
    out: dict[str, Any] = {"run_id": str(run_path), "panel": panel, "drafts": []}
    for seat, brief_id, raw_path in _iter_drafts(run_path):
        bundle_dir = _bundle_dir(run_path, brief_id) if brief_id is not None else run_path / "bundle"
        fk = factkey.load_factkey(bundle_dir / "fact_key.yaml")
        effective_brief = brief_id or fk.brief_id
        raw = raw_path.read_text(encoding="utf-8")
        staging_dir = run_path / "staging" / seat / effective_brief
        staged = normalize.stage_for_judges(raw, staging_dir, author_identity_tokens=author_identity_tokens or [seat])
        if not staged.staged:
            raise RuntimeError(f"draft for {seat} did not stage for judges")
        normalized = (staging_dir / "draft.md").read_text(encoding="utf-8")
        legacy_staging = run_path / "staging" / seat / "draft.md"
        if not legacy_staging.exists():
            legacy_staging.write_text(normalized, encoding="utf-8")
        recorder.write({"event": "blinding_scan", "seat": seat, "hits": [], "result": "quarantined" if staged.quarantined else "clean"})
        if staged.redaction_log:
            recorder.write(
                {
                    "event": "redaction_log",
                    "seat": seat,
                    "edits": [{"class": edit.token_class, "from": edit.from_text, "to": edit.to_text} for edit in staged.redaction_log],
                    "rescan": "clean",
                }
            )
        recorder.write(
            {
                "event": "normalized",
                "seat": seat,
                "brief_id": fk.brief_id,
                "normalized_sha256": _sha256_text(normalized),
                "normalizer_version": 1,
            }
        )
        r6 = rubric.r6_context(fk)
        recorder.write({"event": "factkey_r6_context", "seat": seat, "brief_id": fk.brief_id, "traps": [{"id": trap["id"], "precondition": trap["precondition"]} for trap in r6.traps]})
        packet = judge.judge_packet(
            {
                "export": run_path / "export",
                "draft": staging_dir / "draft.md",
                "fact_key": bundle_dir / "fact_key.yaml",
                "rubric": json.dumps(rubric.RUBRIC, sort_keys=True),
            }
        )
        rubric_path = run_path / "rubrics" / effective_brief / "rubric.yaml"
        rubric_path.parent.mkdir(parents=True, exist_ok=True)
        rubric_path.write_text(json.dumps(rubric.RUBRIC, sort_keys=True, indent=2), encoding="utf-8")
        role_profile = jail.judge_profile(run_path / "export", staging_dir / "draft.md", bundle_dir / "fact_key.yaml", rubric_path)
        jail.assert_manifest(role_profile, "judge")
        (canary_runner or _default_canary_runner)(role="judge", surface=staging_dir, profile=role_profile, run_dir=run_path)
        replies = (judge_runner or _default_judge_runner)(panel, packet, profile=role_profile, run_dir=run_path, draft_handle=seat, brief_id=effective_brief)
        if isinstance(replies, list):
            replies = {reply.seat: reply.raw for reply in replies}
        parsed_by_judge: dict[str, dict[str, Any]] = {}
        for judge_seat, raw_reply in dict(replies).items():
            parsed = judge.parse_anchors(judge.JudgeReply(str(judge_seat), str(raw_reply)))
            parsed_by_judge[str(judge_seat)] = {
                dim: {"score": value.score, "quote": value.quote} if isinstance(value, judge.Anchor) else {"error": value.reason}
                for dim, value in parsed.items()
            }
            recorder.write({"event": "judge_dispatch", "judge_seat": str(judge_seat), "draft_seat": seat, "brief_id": fk.brief_id, "role": "judge", "profile_ok": True})
            recorder.write({"event": "judge_anchor", "judge_seat": str(judge_seat), "draft_handle": seat, "brief_id": fk.brief_id, "anchors": parsed_by_judge[str(judge_seat)], "length_note": len(normalized.splitlines())})
        judge_dir = run_path / "judge" / seat
        judge_dir.mkdir(parents=True, exist_ok=True)
        (judge_dir / f"{effective_brief}.json").write_text(json.dumps(parsed_by_judge, sort_keys=True, indent=2), encoding="utf-8")
        legacy_judge = run_path / "judge" / f"{seat}.json"
        if not legacy_judge.exists():
            legacy_judge.write_text(json.dumps(parsed_by_judge, sort_keys=True, indent=2), encoding="utf-8")
        out["drafts"].append({"seat": seat, "brief_id": effective_brief})
    return out


def score_run(run_id=None, **kwargs) -> dict[str, Any]:
    run_path = _run_dir(kwargs.get("run_dir"), run_id)
    recorder = store.Recorder(run_path)
    mechanical = store.rederive_mechanical(run_path)
    scores: dict[str, Any] = {}
    for seat, events in mechanical.items():
        for event in events:
            recorder.write(event)
        grid: dict[str, str] = {}
        detail_counts: dict[str, dict[str, int]] = {}
        for r1_event in [event for event in events if event["event"] == "mechanical_r1"]:
            r1_cap = score.MET if r1_event["fabricated"] == 0 and r1_event["cited"] else score.PARTIAL if r1_event["cited"] else score.NOT_MET
            brief_id = r1_event["brief_id"]
            judge_path = run_path / "judge" / seat / f"{brief_id}.json"
            legacy_judge_path = run_path / "judge" / f"{seat}.json"
            parsed_by_judge = json.loads(judge_path.read_text(encoding="utf-8")) if judge_path.exists() else json.loads(legacy_judge_path.read_text(encoding="utf-8")) if legacy_judge_path.exists() else {}
            brief_grid: dict[str, str] = {}
            for dim in judge.DIMS:
                anchors = []
                for parsed in parsed_by_judge.values():
                    cell_data = parsed.get(dim, {})
                    if "score" in cell_data:
                        anchors.append(judge.Anchor(int(cell_data["score"]), str(cell_data.get("quote", ""))))
                    else:
                        anchors.append(judge.JUDGE_ERROR)
                verdict = score.cell(dim, anchors, {"r1_cap": r1_cap})
                brief_grid[dim] = str(verdict)
            grid[brief_id] = brief_grid
            detail_counts[brief_id] = {
                "verified": r1_event["true"],
                "checked": r1_event["cited"],
                "fabricated": r1_event["fabricated"],
            }
        scores[seat] = {
            "grid": grid,
            "detail": {
                "mechanical_counts": detail_counts if len(detail_counts) != 1 else next(iter(detail_counts.values()), {})
            },
        }
    (run_path / "scores.json").write_text(json.dumps(scores, sort_keys=True, indent=2), encoding="utf-8")
    return {"run_id": str(run_path), "seats": sorted(scores)}


def report_run(run_id=None, **kwargs) -> dict[str, Any]:
    run_path = _run_dir(kwargs.get("run_dir"), run_id)
    data = json.loads((run_path / "scores.json").read_text(encoding="utf-8"))
    out_dir = run_path / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    recorder = store.Recorder(run_path)
    rendered: list[str] = []
    for seat, payload in data.items():
        text = report.render_seat_report(seat, payload["grid"], payload["detail"])
        path = out_dir / f"report-{seat}.md"
        path.write_text(text, encoding="utf-8")
        recorder.write({"event": "report_rendered", "seat": seat, "path": str(path), "wall_ok": True})
        rendered.append(str(path))
    return {"run_id": str(run_path), "reports": rendered}


def run_bench(candidate, corpus, *, baseline=None, panel=None, run_dir=None, bundles=None, author_runner=None, judge_runner=None, canary_runner=None, export_dir=None) -> RunResult:
    run_path = _run_dir(run_dir)
    stages: list[str] = []
    bundle_paths = [Path(path) for path in bundles] if bundles is not None else sorted((Path("tools") / "authorbench" / "corpus" / str(corpus)).glob("*/bundle.yaml"))
    bundle_dirs = [path.parent if path.name == "bundle.yaml" else path for path in bundle_paths]
    freeze_check(Path("tools") / "authorbench" / "corpus" / str(corpus) if bundles is None else bundle_dirs[0])
    stages.append("freeze-check")
    for bpath in bundle_dirs:
        author_turn(candidate=candidate, brief=bpath.name, bundle_obj=bundle.load(bpath), prior_exposure="none-known", run_dir=run_path, author_runner=author_runner, canary_runner=canary_runner, export_dir=export_dir)
    stages.append("author")
    judge_phase(run_dir=run_path, panel=panel, judge_runner=judge_runner, canary_runner=canary_runner)
    stages.append("judge")
    score_run(run_dir=run_path)
    stages.append("score")
    report_run(run_dir=run_path)
    stages.append("report")
    return RunResult(candidate=str(candidate), corpus=str(corpus), baseline=baseline, run_dir=str(run_path), stages=stages)
