from __future__ import annotations

from pathlib import Path

from implbench.harness import provenance
from implbench.harness.provenance import Provenance, collect


def test_model_declared_from_config_not_prose(monkeypatch, tmp_path: Path) -> None:
    """RED: a lying reply must not affect model_declared."""
    monkeypatch.setattr(provenance, "_seat_model", lambda seat: "config-model")
    monkeypatch.setattr(provenance, "_run", lambda args, cwd=None: "version-or-sha")
    prov = collect("seat-a", "codex", tmp_path, corpus_version="corp", reply_text="I am other-model")
    assert prov.model_declared == "config-model"


def test_harness_version_is_bridge_head_sha(monkeypatch, tmp_path: Path) -> None:
    """RED: harness_version must be git rev-parse HEAD of the bridge repo."""
    calls: list[tuple[str, ...]] = []

    def fake_run(args, cwd=None):
        calls.append(tuple(args))
        return "abc123"

    monkeypatch.setattr(provenance, "_seat_model", lambda seat: "model")
    monkeypatch.setattr(provenance, "_run", fake_run)
    assert collect("seat-a", "codex", tmp_path, corpus_version="corp").harness_version == "abc123"
    assert ("git", "rev-parse", "HEAD") in calls


def test_engine_version_readback_present(monkeypatch, tmp_path: Path) -> None:
    """RED: engine --version output must be threaded into provenance."""
    monkeypatch.setattr(provenance, "_seat_model", lambda seat: "model")
    monkeypatch.setattr(provenance, "_run", lambda args, cwd=None: "codex 1.2.3" if "--version" in args else "sha")
    assert collect("seat-a", "codex", tmp_path, corpus_version="corp").engine_version == "codex 1.2.3"


def test_corpus_version_threaded(monkeypatch, tmp_path: Path) -> None:
    """RED: caller-supplied corpus_version must be preserved."""
    monkeypatch.setattr(provenance, "_seat_model", lambda seat: "model")
    monkeypatch.setattr(provenance, "_run", lambda args, cwd=None: "x")
    assert collect("seat-a", "codex", tmp_path, corpus_version="corpus-sha").corpus_version == "corpus-sha"
