from arb_memory import run


def test_run_main_dispatches_mcp(monkeypatch):
    calls = []

    monkeypatch.setattr(run, "run_mcp", lambda: calls.append("mcp"))

    assert run.main(["mcp"]) == 0
    assert calls == ["mcp"]


def test_run_main_dispatches_memory_and_audit(monkeypatch):
    calls = []

    monkeypatch.setattr(run, "run_memory", lambda: calls.append("memory"))
    monkeypatch.setattr(run, "run_audit", lambda: calls.append("audit"))

    assert run.main(["memory"]) == 0
    assert run.main(["audit"]) == 0
    assert calls == ["memory", "audit"]


def test_eval_service_choice_accepted(monkeypatch):
    import arb_memory.run as run

    called = {}
    monkeypatch.setattr(run, "run_eval", lambda: called.setdefault("eval", True))
    # main dispatches to run_eval without falling through to mcp
    run.main(["eval"])
    assert called.get("eval") is True


def test_eval_purge_service_choice_accepted(monkeypatch):
    import arb_memory.run as run

    called = {}
    monkeypatch.setattr(run, "run_eval_purge", lambda: called.setdefault("eval-purge", True))
    run.main(["eval-purge"])
    assert called.get("eval-purge") is True
