import anyio

from textual.widgets import DataTable

from arb_memory.watch.app import WatchApp


def test_watch_app_ingests_seats_and_populates_timeline(monkeypatch):
    async def fake_stream(url, token, last_id=None):
        if url.endswith("/sse/orchestrator/orch-1"):
            yield {
                "id": "1-0",
                "event": "seat_appear",
                "data": {
                    "task_id": "t1",
                    "seat_id": "codex",
                    "orchestrator": "orch-1",
                    "run_id": "run-1",
                    "state": "running",
                    "last_event": "task_started",
                    "last_event_ts": "2026-06-25T10:00:00+00:00",
                },
            }
            yield {
                "id": "2-0",
                "event": "seat_appear",
                "data": {
                    "task_id": "t2",
                    "seat_id": "agy",
                    "orchestrator": "orch-1",
                    "run_id": "run-1",
                    "state": "done",
                    "last_event": "task_finished",
                    "last_event_ts": "2026-06-25T10:01:00+00:00",
                },
            }
            return

        if url.endswith("/sse/seat/t1"):
            yield {
                "id": "backfill-1",
                "event": "backfill",
                "data": {
                    "source": "eval",
                    "task_id": "t1",
                    "seat_id": "codex",
                    "event_type": "task_started",
                    "ts": "2026-06-25T10:00:00+00:00",
                },
            }

    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", fake_stream)

    async def scenario():
        app = WatchApp(base_url="http://visibility", token="token-1", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#seats", DataTable)
            assert table.row_count == 2
            assert set(app.seats) == {"t1", "t2"}

            app.select_seat("t1")
            await pilot.pause()
            assert app.timeline_lines == []

    anyio.run(scenario)


def test_selecting_seat_does_not_cancel_orchestrator_stream(monkeypatch):
    first_sent = anyio.Event()
    allow_second = anyio.Event()

    async def fake_stream(url, token, last_id=None):
        if url.endswith("/sse/orchestrator/orch-1"):
            yield {
                "id": "1-0",
                "event": "seat_appear",
                "data": {
                    "task_id": "t1",
                    "seat_id": "codex",
                    "orchestrator": "orch-1",
                    "run_id": "run-1",
                    "state": "running",
                    "last_event": "task_started",
                    "last_event_ts": "2026-06-25T10:00:00+00:00",
                },
            }
            first_sent.set()
            await allow_second.wait()
            yield {
                "id": "2-0",
                "event": "seat_appear",
                "data": {
                    "task_id": "t2",
                    "seat_id": "agy",
                    "orchestrator": "orch-1",
                    "run_id": "run-1",
                    "state": "running",
                    "last_event": "task_started",
                    "last_event_ts": "2026-06-25T10:01:00+00:00",
                },
            }
            await anyio.sleep_forever()

        if url.endswith("/sse/seat/t1"):
            yield {
                "id": "backfill-1",
                "event": "backfill",
                "data": {
                    "source": "eval",
                    "task_id": "t1",
                    "seat_id": "codex",
                    "event_type": "task_started",
                    "ts": "2026-06-25T10:00:00+00:00",
                },
            }
            await anyio.sleep_forever()

    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", fake_stream)

    async def scenario():
        app = WatchApp(base_url="http://visibility", token="token-1", orchestrator="orch-1")
        async with app.run_test() as pilot:
            with anyio.fail_after(2):
                await first_sent.wait()
            await pilot.pause()
            app.select_seat("t1")
            await pilot.pause()

            allow_second.set()
            with anyio.fail_after(2):
                while "t2" not in app.seats:
                    await pilot.pause()

            table = app.query_one("#seats", DataTable)
            assert table.row_count == 2
            assert set(app.seats) == {"t1", "t2"}
            assert app.timeline_lines == []

    anyio.run(scenario)


def test_main_threads_no_mouse_flag_to_run(monkeypatch):
    """--no-mouse must reach App.run(mouse=False); default keeps mouse capture on."""
    from arb_memory.watch import app as watch_app

    captured = {}
    monkeypatch.setattr(watch_app.WatchApp, "run", lambda self, **kw: captured.update(kw))

    watch_app.main(["--base-url", "http://v", "--token", "t", "--no-mouse"])
    assert captured == {"mouse": False}

    captured.clear()
    watch_app.main(["--base-url", "http://v", "--token", "t"])
    assert captured == {"mouse": True}


def test_copy_transcript_action_copies_full_timeline(monkeypatch):
    """Pressing `c` copies the selected seat's full transcript via copy_to_clipboard."""
    async def fake_stream(url, token, last_id=None):
        if url.endswith("/sse/orchestrator/orch-1"):
            yield {
                "id": "1-0", "event": "seat_appear",
                "data": {"task_id": "t1", "seat_id": "codex", "orchestrator": "orch-1",
                         "run_id": "run-1", "state": "running", "last_event": "task_started",
                         "last_event_ts": "2026-06-25T10:00:00+00:00"},
            }
            return
        if url.endswith("/sse/seat/t1"):
            for i, kind in enumerate(("model_text", "command_started"), start=1):
                yield {"id": f"backfill-{i}", "event": "backfill",
                       "data": {"source": "transcript", "task_id": "t1", "seat_id": "codex",
                                "kind": kind, "tool_name": "bash" if kind == "command_started" else "",
                                "content": f"line {i}", "ts": f"2026-06-25T10:00:0{i}+00:00"}}

    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", fake_stream)

    async def scenario():
        app = WatchApp(base_url="http://visibility", token="token-1", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            copied = {}
            monkeypatch.setattr(app, "copy_to_clipboard", lambda text: copied.setdefault("text", text))
            app.select_seat("t1")
            await pilot.pause()
            assert len(app.timeline_lines) == 2  # transcript backfilled
            await pilot.press("c")
            await pilot.pause()
            assert copied["text"] == "\n\n".join(app.timeline_lines)

    anyio.run(scenario)


def test_copy_transcript_with_no_selection_is_safe(monkeypatch):
    """`c` with nothing selected warns and does not call copy_to_clipboard."""
    async def fake_stream(url, token, last_id=None):
        if False:
            yield {}
        return

    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", fake_stream)

    async def scenario():
        app = WatchApp(base_url="http://visibility", token="token-1", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            called = {"n": 0}
            monkeypatch.setattr(app, "copy_to_clipboard", lambda text: called.__setitem__("n", called["n"] + 1))
            await pilot.press("c")
            await pilot.pause()
            assert called["n"] == 0  # nothing selected -> no clipboard write

    anyio.run(scenario)


def test_toggle_timestamps_rerenders_without_leading_ts(monkeypatch):
    """`t` strips the leading timestamp from every line and toggles back."""
    async def fake_stream(url, token, last_id=None):
        if url.endswith("/sse/orchestrator/orch-1"):
            yield {"id": "1-0", "event": "seat_appear",
                   "data": {"task_id": "t1", "seat_id": "codex", "orchestrator": "orch-1",
                            "run_id": "run-1", "state": "running", "last_event": "task_started",
                            "last_event_ts": "2026-06-25T10:00:00+00:00"}}
            return
        if url.endswith("/sse/seat/t1"):
            yield {"id": "backfill-1", "event": "backfill",
                   "data": {"source": "transcript", "task_id": "t1", "seat_id": "codex",
                            "kind": "model_text", "content": "hello world",
                            "ts": "2026-06-25T10:00:01+00:00"}}

    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", fake_stream)

    async def scenario():
        app = WatchApp(base_url="http://visibility", token="token-1", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.select_seat("t1")
            await pilot.pause()
            assert not app.timeline_lines[0].startswith("2026-06-25")  # ts OFF by default
            assert "hello world" in app.timeline_lines[0]

            await pilot.press("t")  # timestamps on
            await pilot.pause()
            assert app.show_timestamps is True
            assert app.timeline_lines[0].startswith("2026-06-25T10:00:01+00:00")  # ts shown
            assert "hello world" in app.timeline_lines[0]

            await pilot.press("t")  # back off
            await pilot.pause()
            assert app.show_timestamps is False
            assert not app.timeline_lines[0].startswith("2026-06-25")

    anyio.run(scenario)


def _orch_then_transcript(*events):
    async def fake_stream(url, token, last_id=None):
        if url.endswith("/sse/orchestrator/orch-1"):
            yield {"id": "1-0", "event": "seat_appear",
                   "data": {"task_id": "t1", "seat_id": "codex", "orchestrator": "orch-1",
                            "run_id": "run-1", "state": "running", "last_event": "task_started",
                            "last_event_ts": "2026-06-25T10:00:00+00:00"}}
            return
        if url.endswith("/sse/seat/t1"):
            for i, data in enumerate(events, start=1):
                yield {"id": f"backfill-{i}", "event": "backfill", "data": data}
    return fake_stream


def test_styling_uses_bullet_and_branch_markers(monkeypatch):
    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", _orch_then_transcript(
        {"source": "transcript", "task_id": "t1", "kind": "model_text", "content": "hi", "ts": "2026-06-25T10:00:01+00:00"},
        {"source": "transcript", "task_id": "t1", "kind": "command_output", "content": "out", "ts": "2026-06-25T10:00:02+00:00"},
    ))

    async def scenario():
        app = WatchApp(base_url="http://v", token="t", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.select_seat("t1")
            await pilot.pause()
            assert "⏺ hi" in app.timeline_lines[0]       # model action marker
            assert "⎿ out" in app.timeline_lines[1]       # tool output branch marker

    anyio.run(scenario)


def test_toggle_labels_adds_source_kind(monkeypatch):
    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", _orch_then_transcript(
        {"source": "transcript", "task_id": "t1", "kind": "model_text", "content": "hi", "ts": "2026-06-25T10:00:01+00:00"},
    ))

    async def scenario():
        app = WatchApp(base_url="http://v", token="t", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.select_seat("t1")
            await pilot.pause()
            assert "transcript model_text" not in app.timeline_lines[0]  # labels off by default
            await pilot.press("l")
            await pilot.pause()
            assert app.show_labels is True
            assert "transcript model_text" in app.timeline_lines[0]      # labels revealed

    anyio.run(scenario)


def test_toggle_fullscreen_hides_seat_pane(monkeypatch):
    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", _orch_then_transcript())

    async def scenario():
        app = WatchApp(base_url="http://v", token="t", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable as DT
            seats = app.query_one("#seats", DT)
            assert seats.display is True
            await pilot.press("f")
            await pilot.pause()
            assert app.fullscreen is True and seats.display is False
            await pilot.press("f")
            await pilot.pause()
            assert app.fullscreen is False and seats.display is True

    anyio.run(scenario)


def test_eval_command_lifecycle_noise_is_filtered(monkeypatch):
    """`· command_started/finished` eval echoes are dropped; transcript + task lifecycle stay."""
    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", _orch_then_transcript(
        {"source": "eval", "event_type": "task_started"},                          # keep (bookend)
        {"source": "eval", "event_type": "command_started"},                        # DROP (noise)
        {"source": "eval", "event_type": "command_finished"},                       # DROP (noise)
        {"source": "transcript", "kind": "command_started", "tool_name": "ls"},     # keep (real ⏺)
        {"source": "eval", "event_type": "task_finished"},                          # keep (bookend)
    ))

    async def scenario():
        app = WatchApp(base_url="http://v", token="t", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.select_seat("t1")
            await pilot.pause()
            joined = "\n".join(app.timeline_lines)
            assert "command_started" not in joined.replace("⏺ ls", "")  # no eval command echo
            assert "command_finished" not in joined
            assert "task_started" not in joined and "task_finished" not in joined  # eval dropped
            assert "⏺ ls" in joined  # the real transcript command kept
            assert len(app.timeline_lines) == 1  # transcript-only

    anyio.run(scenario)


def test_format_command_strips_shell_wrapper():
    from arb_memory.watch.app import _format_command
    assert _format_command("/bin/zsh -lc 'python3 fibonacci.py'") == "Bash(python3 fibonacci.py)"
    assert _format_command('/bin/bash -lc "git status --short"') == "Bash(git status --short)"
    assert _format_command("python3 fibonacci.py") == "python3 fibonacci.py"  # no wrapper, as-is
    assert _format_command("Read") == "Read"  # non-shell tool unchanged


def test_turn_lifecycle_is_filtered_too(monkeypatch):
    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", _orch_then_transcript(
        {"source": "eval", "event_type": "task_started"},
        {"source": "eval", "event_type": "turn_started"},     # DROP
        {"source": "eval", "event_type": "turn_completed"},   # DROP
        {"source": "eval", "event_type": "task_finished"},
    ))

    async def scenario():
        app = WatchApp(base_url="http://v", token="t", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.select_seat("t1")
            await pilot.pause()
            joined = "\n".join(app.timeline_lines)
            assert "turn_started" not in joined and "turn_completed" not in joined
            assert "task_started" not in joined and "task_finished" not in joined
            assert app.timeline_lines == []  # all eval lifecycle suppressed

    anyio.run(scenario)


def test_menu_lists_orchestrators_and_drills_into_seats(monkeypatch):
    """No --orchestrator: root shows fleet orchestrators; selecting one drills to its seats; m returns."""
    import httpx as _httpx

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"orchestrators": ["orch-1", "orch-2"]}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None): return FakeResp()

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)

    async def fake_stream(url, token, last_id=None):
        if url.endswith("/sse/orchestrator/orch-1"):
            yield {"id": "1-0", "event": "seat_appear",
                   "data": {"task_id": "t1", "seat_id": "codex", "orchestrator": "orch-1",
                            "run_id": "r", "state": "running", "last_event": "task_started",
                            "last_event_ts": "2026-06-25T10:00:00+00:00"}}
            return

    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", fake_stream)

    async def scenario():
        app = WatchApp(base_url="http://v", token="t")  # NO orchestrator -> root menu
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable as DT
            table = app.query_one("#seats", DT)
            assert app.view_mode == "orchestrators"
            assert {r.value for r in table.rows} == {"orch-1", "orch-2"}  # fleet listed

            app.enter_seats_view("orch-1")  # drill in (as a row-select would)
            await pilot.pause()
            assert app.view_mode == "seats" and app.orchestrator == "orch-1"
            assert "t1" in app.seats  # its seats now stream in

            await pilot.press("m")  # back to root
            await pilot.pause()
            assert app.view_mode == "orchestrators" and app.orchestrator is None
            assert {r.value for r in app.query_one("#seats", DT).rows} == {"orch-1", "orch-2"}

    anyio.run(scenario)


def test_age_text_escalates_for_running_seats_only():
    from datetime import datetime, timezone, timedelta
    from arb_memory.watch.app import _age_text

    now = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)

    def style(age_s, state):
        ts = (now - timedelta(seconds=age_s)).isoformat()
        t = _age_text({"last_event_ts": ts, "state": state}, now=now)
        return t.spans[0].style if t.spans else str(t.style)

    assert style(5, "running") == "dim"       # fresh
    assert style(45, "running") == "yellow"    # amber (>=30s)
    assert style(120, "running") == "bold red" # red (>=90s)
    assert style(300, "done") == "dim"         # terminal -> informational, no alarm
    # missing ts -> placeholder, no crash
    assert _age_text({"state": "running"}, now=now).plain == "—"


def test_seat_header_shows_seat_agent_run_on_select(monkeypatch):
    """Selecting a seat fills the detail-pane header with name · agent · run · state."""
    monkeypatch.setattr("arb_memory.watch.app.sse_client.stream", _orch_then_transcript(
        {"source": "transcript", "task_id": "t1", "kind": "model_text", "content": "hi"},
    ))

    async def scenario():
        app = WatchApp(base_url="http://v", token="t", orchestrator="orch-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            # seat t1 comes from the shared orchestrator fixture (seat_id "codex", run "r")
            assert app._header_plain == ""  # nothing selected yet
            app.select_seat("t1")
            await pilot.pause()
            assert "codex" in app._header_plain   # seat name + agent (both "codex")
            assert "run" in app._header_plain     # run label

    anyio.run(scenario)


def test_seat_header_text_and_agent_derivation():
    from datetime import datetime, timezone, timedelta
    from arb_memory.watch.app import _seat_header_text, _agent_of

    assert _agent_of("codex-ff-demo") == "codex"
    assert _agent_of("agy-bridge-dev") == "agy"
    assert _agent_of("pi-glm") == "pi"

    now = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)
    seat = {"seat_id": "codex-ff-demo", "run_id": "r1", "state": "running",
            "last_event_ts": (now - timedelta(seconds=3)).isoformat()}
    h = _seat_header_text(seat, now=now).plain
    assert h.startswith("codex-ff-demo  ·  codex")  # name · agent
    assert "run r1" in h and "running" in h and "3s" in h
    assert "·  gpt" not in h                          # no model when absent
    h2 = _seat_header_text(dict(seat, model="gpt-5.5"), now=now).plain
    assert "gpt-5.5" in h2                            # model shown when present
