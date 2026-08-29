from __future__ import annotations

from pathlib import Path

import pytest

from arb_memory.run import load_pointed_env_file


def test_loader_populates_environ_from_pointed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "mcp.env"
    env_file.write_text(
        "ARB_MEMORY_LOCAL_DSN=postgresql://reader:p=a?ss@host:25060/db?sslmode=require\n"
        "OPENAI_API_KEY=sk-loader-test\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ARB_MEMORY_LOCAL_ENV_FILE", str(env_file))
    monkeypatch.delenv("ARB_MEMORY_LOCAL_DSN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    load_pointed_env_file()

    import os

    # value split on FIRST '=' only — DSNs legitimately contain '=' in query params
    assert os.environ["ARB_MEMORY_LOCAL_DSN"] == "postgresql://reader:p=a?ss@host:25060/db?sslmode=require"
    assert os.environ["OPENAI_API_KEY"] == "sk-loader-test"


def test_loader_noop_when_pointer_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARB_MEMORY_LOCAL_ENV_FILE", raising=False)
    load_pointed_env_file()  # must not raise


def test_loader_fails_loud_when_pointed_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_ENV_FILE", str(tmp_path / "absent.env"))
    with pytest.raises(RuntimeError, match="ARB_MEMORY_LOCAL_ENV_FILE"):
        load_pointed_env_file()


def test_env_file_writer_uses_per_process_tmp_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two daemons bootstrapping in the same second race os.replace on a shared tmp
    # name (the codex-bridge-dev-luna standup crash, 2026-07-09) — the tmp path must
    # be unique per process so concurrent writers never replace each other's tmp.
    import os

    from agent_redis_bridge import local_memory_mcp

    monkeypatch.setenv("HOME", str(tmp_path))
    replaced_sources: list[str] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        replaced_sources.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(local_memory_mcp.os, "replace", recording_replace)
    local_memory_mcp.write_local_memory_mcp_env_file({"K": "v"})

    assert replaced_sources, "writer must go through an atomic replace"
    assert str(os.getpid()) in Path(replaced_sources[0]).name
