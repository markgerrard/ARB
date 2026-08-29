import ast
from pathlib import Path

from arb_memory import client


def test_client_has_no_store_psycopg_or_embed_imports():
    path = Path(__file__).parents[2] / "src" / "arb_memory" / "client.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"psycopg", "store", "embed"} or alias.name.endswith(".store"):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if (
                node.module in {"psycopg", "store", "embed", "arb_memory.store", "arb_memory.embed"}
                or node.module.endswith(".store")
                or node.module.endswith(".embed")
            ):
                offenders.append(node.module)

    assert offenders == []


def test_client_write_path_only_uses_bus_memory_write():
    path = Path(__file__).parents[2] / "src" / "arb_memory" / "client.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    write_fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "write")
    bus_calls = []
    forbidden_calls = []

    for node in ast.walk(write_fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "bus":
                bus_calls.append(node.func.attr)
            if node.func.attr in {"write_artefact_and_hints", "handle_write_intent"}:
                forbidden_calls.append(node.func.attr)

    assert bus_calls == ["memory_write"]
    assert forbidden_calls == []


def test_write_does_not_call_store_writer(monkeypatch):
    import arb_memory.store as store

    monkeypatch.setattr(client, "_redis_client", lambda: object())
    monkeypatch.setattr(client.bus, "memory_write", lambda *args, **kwargs: "U3")
    monkeypatch.setattr(
        store,
        "write_artefact_and_hints",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("store write called")),
    )

    assert client.write("remember this") == "U3"
