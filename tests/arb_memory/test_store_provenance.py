from arb_memory import store


def test_artefact_persists_source_and_author(conn_factory, fake_embed):
    conn = conn_factory()
    store.write_artefact_and_hints(
        conn,
        artefact={
            "artefact_id": "art-prov1",
            "content": "hello",
            "mime": "text/plain",
            "source": "mcp",
            "author": "cid-abc",
        },
    )
    row = store.fetch_artefact(conn, "art-prov1", 1)
    assert row["source"] == "mcp"
    assert row["author"] == "cid-abc"


def test_artefact_defaults_source_author(conn_factory):
    conn = conn_factory()
    store.upsert_artefact(conn, "art-prov2", content="x", mime="text/plain")
    row = store.fetch_artefact(conn, "art-prov2", 1)
    assert row["source"] == "seat"
    assert row["author"] == "unknown"
