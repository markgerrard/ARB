from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .tasks import Task


class ForgeHostUnsupported(RuntimeError):
    pass


_PINNED_ENV = {
    "GIT_AUTHOR_NAME": "implbench",
    "GIT_AUTHOR_EMAIL": "implbench@localhost",
    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "implbench",
    "GIT_COMMITTER_EMAIL": "implbench@localhost",
    "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
}


def _git(repo: Path, *args: str, input: str | None = None, env: dict[str, str] | None = None) -> str:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input,
        text=True,
        capture_output=True,
        check=False,
        env=full_env,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f"git {' '.join(args)} failed")
    return res.stdout.strip()


def materialize(task: Task, repo: str | Path) -> str:
    if _is_forge_host(repo):
        raise ForgeHostUnsupported(str(repo))
    repo_path = Path(repo)
    tree_root = _task_root(task, repo_path) / "tree"
    if not tree_root.exists():
        raise FileNotFoundError(tree_root)
    modes = _read_modes(tree_root / "tree.mode")
    tree_sha = _write_tree(repo_path, tree_root, tree_root, modes)
    return _git(
        repo_path,
        "commit-tree",
        tree_sha,
        input=f"implbench fixture {task.task_id}\n",
        env=_PINNED_ENV,
    )


def _write_tree(repo: Path, root: Path, current: Path, modes: dict[str, str]) -> str:
    entries: list[str] = []
    for child in sorted(current.iterdir(), key=lambda p: p.name):
        if child.name == "tree.mode":
            continue
        rel = child.relative_to(root).as_posix()
        if child.is_dir():
            sha = _write_tree(repo, root, child, modes)
            entries.append(f"040000 tree {sha}\t{child.name}")
        elif child.is_file():
            sha = _git(repo, "hash-object", "-w", str(child))
            mode = modes.get(rel, "100644")
            entries.append(f"{mode} blob {sha}\t{child.name}")
    return _git(repo, "mktree", input="\n".join(entries) + ("\n" if entries else ""))


def _read_modes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    modes: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        mode, rel = line.split(maxsplit=1)
        modes[rel.strip()] = mode.strip()
    return modes


def _task_root(task: Task, repo: Path) -> Path:
    manifest = getattr(task, "_manifest_path", None)
    if manifest is not None:
        return Path(manifest).parent
    # Tests and live code pass tasks loaded from bench/implbench/fixtures/<task>/task.yaml.
    cwd = Path.cwd()
    candidates = [
        cwd / "bench" / "implbench" / "fixtures" / task.task_id,
        repo / "bench" / "implbench" / "fixtures" / task.task_id,
    ]
    for root in candidates:
        if root.exists():
            return root
    # Fallback for temp corpora: locate by task_id under the current process temp tree inputs.
    matches = list(cwd.rglob(f"fixtures/{task.task_id}/task.yaml"))
    if matches:
        return matches[0].parent
    raise FileNotFoundError(f"fixture tree for {task.task_id}")


def _is_forge_host(repo: str | Path) -> bool:
    text = str(repo)
    return "://" in text or bool(re.match(r"^[^/\s]+:[^/].*", text))
