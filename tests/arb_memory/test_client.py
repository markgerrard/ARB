import json
import importlib

client = importlib.import_module("arb_memory.client")


class FakeRedis:
    pass


def test_write_emits_bus_intent(monkeypatch):
    calls = []
    redis = FakeRedis()

    monkeypatch.setattr(client, "_redis_client", lambda: redis)
    monkeypatch.setattr(
        client.bus,
        "memory_write",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "U1",
    )

    assert (
        client.write(
            "remember this",
            artefact_id="a.md",
            artefact_content="body",
            repo_pointer="src/a.py:1",
            source="seat-test",
            author="codex",
        )
        == "U1"
    )

    assert calls == [
        (
            (redis,),
            {
                "artefact": {"artefact_id": "a.md", "content": "body"},
                "hints": [{"text": "remember this", "repo_pointer": "src/a.py:1"}],
                "source": "seat-test",
                "author": "codex",
            },
        )
    ]


def test_query_returns_bus_hits(monkeypatch):
    redis = FakeRedis()
    hits = [{"text": "hit"}]

    monkeypatch.setattr(client, "_redis_client", lambda: redis)
    monkeypatch.setattr(client.bus, "memory_query", lambda *args, **kwargs: hits)

    assert client.query("needle", k=3, timeout_s=4) == hits


def test_query_falls_back_to_grep_on_none_in_production(monkeypatch, tmp_path):
    redis = FakeRedis()
    planted = "arb-memory-planted-token"
    source = tmp_path / "source.txt"
    source.write_text(f"contains {planted}\n", encoding="utf-8")

    monkeypatch.setattr(client, "_redis_client", lambda: redis)
    monkeypatch.setattr(client.bus, "memory_query", lambda *args, **kwargs: None)

    assert client.query(planted, repo_root=tmp_path) == [
        {
            "source": "grep-fallback",
            "path": "source.txt",
            "line": 1,
            "text": f"contains {planted}",
        }
    ]


def test_transport_only_none_does_not_grep(monkeypatch, tmp_path):
    redis = FakeRedis()
    (tmp_path / "source.txt").write_text("needle\n", encoding="utf-8")

    monkeypatch.setattr(client, "_redis_client", lambda: redis)
    monkeypatch.setattr(client.bus, "memory_query", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        client,
        "_grep_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("grep called")),
    )

    assert client.query("needle", transport_only=True, repo_root=tmp_path) is None


def test_query_returns_bus_hits_in_both_modes(monkeypatch, tmp_path):
    redis = FakeRedis()
    hits = [{"source": "bus", "text": "needle"}]

    monkeypatch.setattr(client, "_redis_client", lambda: redis)
    monkeypatch.setattr(client.bus, "memory_query", lambda *args, **kwargs: hits)
    monkeypatch.setattr(
        client,
        "_grep_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("grep called")),
    )

    assert client.query("needle", repo_root=tmp_path) is hits
    assert client.query("needle", transport_only=True, repo_root=tmp_path) is hits


def test_main_write_prints_ulid(monkeypatch, capsys):
    monkeypatch.setattr(client, "write", lambda *args, **kwargs: "U2")

    assert client.main(["write", "--text", "hello"]) == 0
    assert capsys.readouterr().out == "U2\n"


def test_main_query_prints_json(monkeypatch, capsys):
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: [{"text": "hello"}])

    assert client.main(["query", "--q", "hello"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"text": "hello"}]


def test_main_query_accepts_transport_only(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: calls.append((args, kwargs)) or None)

    assert client.main(["query", "--q", "hello", "--transport-only"]) == 0
    assert calls == [(("hello",), {"k": 8, "timeout_s": 10, "transport_only": True, "repo_root": "."})]
