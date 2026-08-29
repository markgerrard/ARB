from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


STAGES = {"design", "spec", "plan"}


@dataclass(frozen=True)
class BundleManifest:
    id: str
    path: Path
    data: dict[str, Any]


@dataclass(frozen=True)
class Bundle:
    path: Path
    manifest: dict[str, Any]

    def author_visible_subset(self) -> dict[str, Any]:
        return author_visible_subset(self)

    def judge_inputs(self) -> dict[str, Any]:
        data = self.author_visible_subset()
        data["fact_key.yaml"] = (self.path / "fact_key.yaml").read_text(encoding="utf-8")
        return data


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_base_sha(*, base_sha: str | None, artefact_commit: str | None, cwd: Path) -> str:
    if bool(base_sha) == bool(artefact_commit):
        raise ValueError("exactly one of base_sha or artefact_commit is required")
    if base_sha:
        return base_sha
    result = subprocess.run(
        ["git", "rev-parse", f"{artefact_commit}^"],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _destination(src: Path, corpus_version: str, brief_id: str) -> Path:
    repo_corpus = Path.cwd() / "tools" / "authorbench" / "corpus"
    try:
        src.resolve().relative_to(Path.cwd().resolve())
        in_repo = True
    except ValueError:
        in_repo = False
    root = repo_corpus if in_repo and repo_corpus.exists() else src.parent / "corpus"
    return root / corpus_version / brief_id


def freeze(
    src,
    *,
    corpus_version: str,
    base_sha: str | None = None,
    artefact_commit: str | None = None,
) -> BundleManifest:
    src = Path(src)
    meta_path = src / "bundle.yaml"
    if not meta_path.exists():
        raise FileNotFoundError(meta_path)
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    brief_id = meta["id"]
    stage = meta["stage"]
    if stage not in STAGES:
        raise ValueError(f"invalid stage: {stage}")
    resolved_base = _resolve_base_sha(base_sha=base_sha, artefact_commit=artefact_commit, cwd=Path.cwd())
    dest = _destination(src, corpus_version, brief_id)
    if (dest / "bundle.yaml").exists():
        raise FileExistsError(f"frozen bundle already exists: {dest}")
    dest.mkdir(parents=True, exist_ok=False)

    shutil.copy2(src / "brief.md", dest / "brief.md")
    shutil.copy2(src / "fact_key.yaml", dest / "fact_key.yaml")
    steer_dest = dest / "steer"
    steer_dest.mkdir()
    steer_blocks: list[str] = []
    for steer in sorted((src / "steer").glob("*")):
        if steer.is_file():
            shutil.copy2(steer, steer_dest / steer.name)
            steer_blocks.append(steer.stem)

    manifest = {
        "id": brief_id,
        "stage": stage,
        "corpus_version": corpus_version,
        "source": meta["source"],
        "provenance": meta["provenance"],
        "base_sha": resolved_base,
        "length_budget": int(meta["length_budget"]),
        "normalizer_version": int(meta.get("normalizer_version", 1)),
        "factkey_version": int(meta.get("factkey_version", 1)),
        "outcome_globs": list(meta["outcome_globs"]),
        "steer_blocks": steer_blocks,
        "hashes": {
            "brief_md": _sha256(dest / "brief.md"),
            "fact_key_yaml": _sha256(dest / "fact_key.yaml"),
            "steer": {path.stem: _sha256(path) for path in sorted(steer_dest.glob("*")) if path.is_file()},
        },
    }
    (dest / "bundle.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return BundleManifest(id=brief_id, path=dest, data=manifest)


def load(bundle_dir) -> Bundle:
    path = Path(bundle_dir)
    manifest = yaml.safe_load((path / "bundle.yaml").read_text(encoding="utf-8")) or {}
    return Bundle(path=path, manifest=manifest)


def author_visible_subset(bundle: Bundle) -> dict[str, Any]:
    subset: dict[str, Any] = {
        "brief.md": (bundle.path / "brief.md").read_text(encoding="utf-8"),
        "length_budget": bundle.manifest["length_budget"],
    }
    for path in sorted((bundle.path / "steer").glob("*")):
        if path.is_file():
            subset[f"steer/{path.name}"] = path.read_text(encoding="utf-8")
    return subset


def bundle_hashes(bundle: Bundle) -> dict[str, Any]:
    return dict(bundle.manifest["hashes"])
