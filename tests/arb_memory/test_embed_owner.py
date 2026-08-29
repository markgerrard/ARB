import ast
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from arb_memory.embed import EMBED_DIM


def test_embed_dim(fake_embed):
    vec = fake_embed("hello")
    assert len(vec) == EMBED_DIM
    assert all(isinstance(x, float) for x in vec)


def test_single_embedding_owner_drift_guard():
    """Structural committed-code guard, not a deployment single-writer proof."""
    src = Path(__file__).parents[2] / "src"
    owner = src / "arb_memory" / "embed.py"
    offenders = []
    endpoint_offenders = []

    for path in src.rglob("*.py"):
        if path == owner:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "openai" or alias.name.startswith("openai.") for alias in node.names):
                    offenders.append(str(path.relative_to(src)))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "openai" or node.module.startswith("openai."):
                    offenders.append(str(path.relative_to(src)))

        text = path.read_text(encoding="utf-8")
        if "api.openai.com" in text or "/embeddings" in text:
            endpoint_offenders.append(str(path.relative_to(src)))

    assert offenders == []
    assert endpoint_offenders == []
