from dataclasses import replace

import pytest

from arb_memory.mcp import tools as tools_mod
from arb_memory.mcp.tools import MemoryTools
from tests.arb_memory.test_mcp_tools import _settings


def _graph_tools(monkeypatch, settings=None, **graph_stubs):
    mt = MemoryTools(settings or _settings(), conn_factory=lambda: object(),
                     embed=lambda t: [0.0] * 1536)
    for name, fn in graph_stubs.items():
        monkeypatch.setattr(tools_mod.graph, name, fn)
    return mt


@pytest.mark.anyio
async def test_connector_memory_related_preserves_none_sentinel(monkeypatch):
    seen = {}

    def fake_related(conn, artefact_id, version, *, k, threshold, subject_hints):
        seen.update(version=version, subject_hints=subject_hints)
        return [("other", 2, 0.1)]

    mt = _graph_tools(monkeypatch, artefact_exists=lambda c, a, v=None: True,
                      related_artefacts=fake_related)
    out = await mt.memory_related("subject", access_token="tok-a")
    assert seen["version"] is None and seen["subject_hints"] == "live"
    assert out == [{"artefact_id": "other", "version": 2, "distance": 0.1}]


@pytest.mark.anyio
async def test_connector_memory_related_explicit_version_as_written(monkeypatch):
    seen = {}

    def fake_related(conn, artefact_id, version, *, k, threshold, subject_hints):
        seen.update(version=version, subject_hints=subject_hints)
        return []

    mt = _graph_tools(monkeypatch, artefact_exists=lambda c, a, v=None: True,
                      related_artefacts=fake_related)
    await mt.memory_related("subject", version=1, access_token="tok-a")
    assert seen["version"] == 1 and seen["subject_hints"] == "as_written"


@pytest.mark.anyio
async def test_connector_param_and_not_found_contracts(monkeypatch):
    mt = _graph_tools(monkeypatch, artefact_exists=lambda c, a, v=None: False,
                      related_artefacts=lambda *a, **k: [])
    with pytest.raises(ValueError):
        await mt.memory_related("s", k=0, access_token="tok-a")
    with pytest.raises(ValueError, match="artefact not found"):
        await mt.memory_related("ghost", access_token="tok-a")
    with pytest.raises(ValueError, match="artefact not found"):
        await mt.memory_references("ghost", access_token="tok-a")


@pytest.mark.anyio
async def test_connector_memory_references_resolves_none_via_latest(monkeypatch):
    seen = {}

    def fake_refs(conn, artefact_id, version):
        seen["version"] = version
        return {"references": [], "referenced_by": []}

    mt = _graph_tools(monkeypatch, artefact_exists=lambda c, a, v=None: True,
                      references=fake_refs, latest_version=lambda c, a: 7)
    await mt.memory_references("subject", access_token="tok-a")
    assert seen["version"] == 7
    await mt.memory_references("subject", version=3, access_token="tok-a")
    assert seen["version"] == 3


@pytest.mark.anyio
async def test_connector_graph_bucket_per_token_shared_across_tools_and_not_search(monkeypatch):
    settings = replace(_settings(), graph_rate_per_min=2)
    mt = _graph_tools(monkeypatch, settings,
                      artefact_exists=lambda c, a, v=None: True,
                      related_artefacts=lambda *a, **k: [],
                      references=lambda c, a, v: {"references": [], "referenced_by": []},
                      latest_version=lambda c, a: 1)
    await mt.memory_related("s", access_token="tok-a")
    await mt.memory_references("s", access_token="tok-a")     # SAME bucket as related
    with pytest.raises(ValueError, match="graph rate limit"):
        await mt.memory_related("s", access_token="tok-a")
    await mt.memory_related("s", access_token="tok-b")        # different token unaffected
    assert mt._search_hits == {}                              # search bucket never touched
