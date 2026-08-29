from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key: str
    prefix: str = "agent-files/"
    presign_ttl: int = 900
    inline_put_max: int = 262144
    inline_get_max: int = 5_242_880
    inline_get_image_max: int = 3_670_016
    list_max: int = 1000
    local_root: str | None = None
    read_rate_per_min: int = 60
    write_rate_per_min: int = 30


_REQUIRED = (
    "ARB_FILES_ENDPOINT",
    "ARB_FILES_REGION",
    "ARB_FILES_BUCKET",
    "ARB_FILES_ACCESS_KEY",
    "ARB_FILES_SECRET_KEY",
)


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    value = env.get(key)
    return int(value) if value else default


def load_settings(env: Mapping[str, str]) -> Settings:
    missing = [key for key in _REQUIRED if not env.get(key)]
    if missing:
        raise ValueError(f"ARB Files config missing: {', '.join(missing)}")

    prefix = env.get("ARB_FILES_PREFIX", "agent-files/")
    if not prefix.endswith("/"):
        prefix += "/"

    return Settings(
        endpoint=env["ARB_FILES_ENDPOINT"],
        region=env["ARB_FILES_REGION"],
        bucket=env["ARB_FILES_BUCKET"],
        access_key=env["ARB_FILES_ACCESS_KEY"],
        secret_key=env["ARB_FILES_SECRET_KEY"],
        prefix=prefix,
        presign_ttl=_int(env, "ARB_FILES_PRESIGN_TTL", 900),
        inline_put_max=_int(env, "ARB_FILES_INLINE_PUT_MAX", 262144),
        inline_get_max=_int(env, "ARB_FILES_INLINE_GET_MAX", 5_242_880),
        inline_get_image_max=_int(env, "ARB_FILES_INLINE_GET_IMAGE_MAX", 3_670_016),
        list_max=_int(env, "ARB_FILES_LIST_MAX", 1000),
        local_root=env.get("ARB_FILES_LOCAL_ROOT") or env.get("AGENT_WORKDIR"),
        read_rate_per_min=_int(env, "ARB_FILES_READ_RATE_PER_MIN", 60),
        write_rate_per_min=_int(env, "ARB_FILES_WRITE_RATE_PER_MIN", 30),
    )
