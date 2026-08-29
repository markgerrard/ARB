from agent_redis_bridge.claude_tail.identity import cold_identity, parse_marker, warm_identity


def test_warm_identity():
    i = warm_identity("sess-1", "bridge", "dev")
    assert i.task_id == "sess-1"
    assert i.run_id == "sess-1"
    assert i.seat_id == "claude-bridge-dev"
    assert i.orchestrator == "claude-bridge-dev"


def test_cold_identity_with_marker():
    i = cold_identity("agentX", "sess-1", "[ARB_RUN:run-9 ARB_SEAT:cold-opus-1 ARB_ORCH:claude-otherproj-dev]")
    assert i.task_id == "agentX"
    assert i.run_id == "run-9"
    assert i.seat_id == "cold-opus-1"
    assert i.orchestrator == "claude-otherproj-dev"


def test_cold_identity_two_field_marker_has_blank_orchestrator():
    i = cold_identity("agentX", "sess-1", "[ARB_RUN:run-9 ARB_SEAT:cold-opus-1]")
    assert i.task_id == "agentX"
    assert i.run_id == "run-9"
    assert i.seat_id == "cold-opus-1"
    assert i.orchestrator == ""


def test_cold_marker_absent_falls_back_to_session_runid():
    i = cold_identity("agentX", "sess-1", "no marker here")
    assert i.task_id == "agentX"
    assert i.run_id == "sess-1"
    assert i.seat_id == "cold-opus-agentX"
    assert i.orchestrator == ""


def test_cold_marker_absent_uses_project_workspace_seat_id_when_given():
    # Bridge-seat parity (codex-bridge-dev, agy-bridge-dev): when the daemon knows its own
    # project/workspace, a markerless cold seat gets readable naming instead of a raw agent-id
    # GUID -- full parity, no agent_id suffix at all. Never run_id (that's the run-id-label fix's
    # job) -- only seat_id.
    #
    # ACCEPTED RISK (explicit, not an oversight -- codex flagged this as P1 during review, an
    # 8-char-slice version shipped briefly to close it, then was deliberately reverted here on
    # user direction): dedupSeatRuns() in tools/arb-watch-go/model.go collapses roster rows
    # sharing an identical (seat_id, run_id) PAIR -- distinct run_ids never collide regardless of
    # seat_id (see its own doc comment). Two cold-Opus seats sharing this project/workspace only
    # collide if a caller ALSO tags both with the same run_id (e.g. one shared panel label) --
    # confirmed accepted because in practice only one cold-Opus reviewer runs per panel round;
    # multiple reviewers come from distinct bridge seats (codex/agy-print), never multiple
    # concurrent cold-Opus instances. If that usage pattern ever changes, this is the first place
    # to revisit.
    i = cold_identity("agentX", "sess-1", "no marker here", project="bridge", workspace="dev")
    assert i.task_id == "agentX"
    assert i.run_id == "sess-1"
    assert i.seat_id == "cold-opus-bridge-dev"
    assert i.orchestrator == ""


def test_cold_marker_absent_falls_back_to_guid_when_project_or_workspace_missing():
    # Partial config (e.g. only one of the two env vars set) must not produce a malformed
    # seat_id like "cold-opus-bridge-" -- fall back to the collision-safe GUID default.
    assert cold_identity("agentX", "sess-1", "no marker here", project="bridge").seat_id == "cold-opus-agentX"
    assert cold_identity("agentX", "sess-1", "no marker here", workspace="dev").seat_id == "cold-opus-agentX"


def test_cold_marker_present_ignores_project_workspace():
    # A marker's own ARB_SEAT still wins outright -- project/workspace only apply to the
    # no-marker fallback, never override an explicit marker (unchanged existing convention).
    i = cold_identity(
        "agentX", "sess-1", "[ARB_RUN:run-9 ARB_SEAT:cold-opus-1]", project="bridge", workspace="dev"
    )
    assert i.seat_id == "cold-opus-1"


def test_marker_only_from_first_message_is_callers_job():
    assert parse_marker("[ARB_RUN:r ARB_SEAT:s] do the review") == {"run_id": "r", "seat_id": "s", "orchestrator": ""}


def test_parse_marker_with_orchestrator():
    assert parse_marker("[ARB_RUN:r ARB_SEAT:s ARB_ORCH:claude-otherproj-dev] do the review") == {
        "run_id": "r",
        "seat_id": "s",
        "orchestrator": "claude-otherproj-dev",
    }


def test_parse_marker_absent_returns_none():
    assert parse_marker("no marker here") is None


def test_parse_marker_can_appear_after_prefix_text():
    assert parse_marker("please review [ARB_RUN:run-9 ARB_SEAT:cold-opus-1]") == {
        "run_id": "run-9",
        "seat_id": "cold-opus-1",
        "orchestrator": "",
    }


def test_malformed_markers_return_none():
    assert parse_marker("[ARB_SEAT:cold-opus-1 ARB_RUN:run-9]") is None
    assert parse_marker("[ARB_RUN:run-9 ARB_SEAT:cold-opus-1") is None
    assert parse_marker("[ARB_RUN:run-9 ARB_ORCH:claude-otherproj-dev]") is None
