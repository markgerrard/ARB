import math
import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("openai")

from arb_memory.embed import embed


def _cosine_distance(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 1.0 - (dot / (na * nb))


@pytest.mark.smoke
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="no OPENAI_API_KEY")
def test_real_embed_dim_and_semantic_nearness():
    hello = embed("hello")
    assert len(hello) == 1536

    anchor = embed("semantic memory database")
    related = embed("vector store for remembered documents")
    unrelated = embed("ripe bananas on a kitchen counter")
    assert _cosine_distance(anchor, related) < _cosine_distance(anchor, unrelated)
