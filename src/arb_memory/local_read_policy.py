from __future__ import annotations

import os


LOCAL_MEMORY_ALLOW_CROSS_STORE = "ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE"


def local_read_dsn(env: dict[str, str] | os._Environ[str]) -> str:
    read_dsn = env.get("ARB_MEMORY_LOCAL_DSN", "")
    if not read_dsn:
        raise RuntimeError("ARB_MEMORY_LOCAL_DSN is missing/empty")

    writer_dsn = env.get("ARB_MEMORY_DSN", "")
    if not writer_dsn:
        return read_dsn
    if _store_fingerprint(read_dsn) == _store_fingerprint(writer_dsn):
        return read_dsn
    if env.get(LOCAL_MEMORY_ALLOW_CROSS_STORE) == "1":
        return read_dsn
    raise RuntimeError(
        "ARB_MEMORY_LOCAL_DSN store does not match ARB_MEMORY_DSN; set "
        f"{LOCAL_MEMORY_ALLOW_CROSS_STORE}=1 for a deliberate cross-store read"
    )


def _store_fingerprint(dsn: str) -> tuple[str, str, str]:
    try:
        from psycopg.conninfo import conninfo_to_dict

        parts = conninfo_to_dict(dsn)
    except Exception as exc:  # noqa: BLE001 - policy checks must fail closed
        raise RuntimeError(f"invalid ARB Memory DSN for local read policy: {exc}") from exc
    return (parts.get("host", ""), parts.get("port") or "5432", parts.get("dbname", ""))
