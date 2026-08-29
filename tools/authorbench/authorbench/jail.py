from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .bundle import Bundle


AUTHOR_MOUNTS = {"workspace", "author_visible", "home", "config"}
JUDGE_MOUNTS = {"export", "draft", "fact_key", "rubric", "home", "config"}


class ManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mount:
    name: str
    path: Path
    read_only: bool = True
    files: dict[str, Any] | None = None


@dataclass(frozen=True)
class JailProfile:
    role: str
    mounts: tuple[Mount, ...]
    network: str
    arb_memory_enabled: bool
    tmpfs_home: bool
    auth_read_only: bool
    forbidden_token_classes: tuple[str, ...]

    def with_mount_path(self, name: str, path) -> "JailProfile":
        replacement = tuple(
            replace(mount, path=Path(path)) if mount.name == name else mount
            for mount in self.mounts
        )
        return replace(self, mounts=replacement)


@dataclass(frozen=True)
class TokenSet:
    role: str
    outcome_tokens: tuple[str, ...]
    author_identity_tokens: tuple[str, ...]


def author_profile(bundle: Bundle, base_sha) -> JailProfile:
    subset = bundle.author_visible_subset()
    outcome_globs = tuple(bundle.manifest.get("outcome_globs") or ())
    return JailProfile(
        role="author",
        mounts=(
            Mount("workspace", Path(f"git-archive:{base_sha}")),
            Mount("author_visible", bundle.path / "author-visible", files=subset),
            Mount("home", Path("tmpfs:/home/author"), read_only=False),
            Mount("config", Path("codex-config-ro")),
        ),
        network="model-api-only",
        arb_memory_enabled=False,
        tmpfs_home=True,
        auth_read_only=True,
        forbidden_token_classes=("outcome-token", "author-identity-token"),
    )


def judge_profile(export, draft, factkey, rubric) -> JailProfile:
    return JailProfile(
        role="judge",
        mounts=(
            Mount("export", Path(export)),
            Mount("draft", Path(draft)),
            Mount("fact_key", Path(factkey)),
            Mount("rubric", Path(rubric)),
            Mount("home", Path("tmpfs:/home/judge"), read_only=False),
            Mount("config", Path("codex-config-ro")),
        ),
        network="model-api-only",
        arb_memory_enabled=False,
        tmpfs_home=True,
        auth_read_only=True,
        forbidden_token_classes=("author-identity-token",),
    )


def canary_tokens(role: str, run: dict[str, Any]) -> TokenSet:
    identity = tuple(str(item) for item in run.get("author_identity_tokens", ()) if item)
    if not identity:
        identity = ("AUTHOR_IDENTITY_TOKEN",)
    outcomes = tuple(str(item) for item in run.get("outcome_globs", ()) if item)
    if role == "author":
        catches = tuple(
            str(trap.get("historical_catch"))
            for trap in run.get("traps", ())
            if trap.get("historical_catch")
        )
        return TokenSet(role=role, outcome_tokens=outcomes + catches, author_identity_tokens=identity)
    if role == "judge":
        return TokenSet(role=role, outcome_tokens=(), author_identity_tokens=identity)
    raise ManifestError(f"unknown role: {role}")


def _factkey_like(path: Path) -> bool:
    if path.name == "fact_key.yaml":
        return True
    if path.suffix not in {".yaml", ".yml"}:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "facts:" in text and "traps:" in text and "brief_id:" in text


def _assert_no_author_key(profile: JailProfile) -> None:
    for mount in profile.mounts:
        if mount.name != "author_visible":
            continue
        for key in mount.files or {}:
            if "fact_key" in key or "factkey" in key:
                raise ManifestError("author-visible files include fact_key")
        if mount.path.exists():
            for path in mount.path.rglob("*"):
                if path.is_file() and _factkey_like(path):
                    raise ManifestError(f"author-visible mount includes fact-key schema: {path}")


def assert_manifest(profile: JailProfile, role: str) -> None:
    if profile.role != role:
        raise ManifestError(f"profile role {profile.role} does not match {role}")
    expected = AUTHOR_MOUNTS if role == "author" else JUDGE_MOUNTS if role == "judge" else None
    if expected is None:
        raise ManifestError(f"unknown role: {role}")
    names = {mount.name for mount in profile.mounts}
    if names != expected:
        raise ManifestError(f"{role} mounts {sorted(names)} != {sorted(expected)}")
    if any("agy-home" in str(mount.path) for mount in profile.mounts):
        raise ManifestError("persistent agy-home volume is forbidden")
    if profile.arb_memory_enabled:
        raise ManifestError("ARB Memory MCP is forbidden in authorbench jail")
    if profile.network != "model-api-only":
        raise ManifestError("network must be limited to model-api-only")
    if not profile.tmpfs_home or not profile.auth_read_only:
        raise ManifestError("profile requires tmpfs HOME and read-only auth")
    if role == "author":
        _assert_no_author_key(profile)
