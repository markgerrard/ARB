from agent_redis_bridge.stall_watch import BlindEpisode, StallEpisode, StallWatch


def test_heartbeat_does_not_count_as_progress_but_tool_and_model_events_do():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0)

    assert watch.progress("task-1", "turn_heartbeat", now=590.0) is None
    assert watch.check("task-1", now=661.0).stalled_for_secs == 661

    assert watch.progress("task-1", "command_output", now=700.0) is not None
    assert watch.check("task-1", now=1299.0) is None

    assert watch.progress("task-1", "model_text", now=1299.0) is None
    assert watch.check("task-1", now=1898.0) is None
    assert watch.check("task-1", now=1900.0).stalled_for_secs == 601


def test_episode_emits_once_until_progress_rearms():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=10.0)

    first = watch.check("task-1", now=611.0)
    second = watch.check("task-1", now=700.0)

    assert first is not None
    assert first.stalled_for_secs == 601
    assert second is None

    resumed = watch.progress("task-1", "tool_output", now=701.0)
    assert resumed is not None
    assert watch.check("task-1", now=1300.0) is None
    restalled = watch.check("task-1", now=1302.0)
    assert restalled is not None
    assert restalled.stalled_for_secs == 601


def test_progress_between_gap_check_and_mark_prevents_false_stall():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0)

    episode = watch.check("task-1", now=601.0, before_mark=lambda: watch.progress("task-1", "command_output", now=601.0))

    assert episode is None
    assert watch.check("task-1", now=1201.0) is None
    restalled = watch.check("task-1", now=1202.0)
    assert restalled is not None
    assert restalled.stalled_for_secs == 601


def test_disabled_watch_never_emits():
    watch = StallWatch(after_secs=0)
    watch.start("task-1", now=0.0)

    assert watch.check("task-1", now=9999.0) is None
    assert watch.progress("task-1", "command_started", now=9999.0) is None


# --- blind-until-proven (AGY-2 design v2.1) ---


def test_blind_task_reports_blind_episode_once_never_a_stall():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0, blind=True, blind_reason="unproven")

    episode = watch.check("task-1", now=700.0)
    assert isinstance(episode, BlindEpisode)
    assert episode.reason == "unproven"
    assert episode.unproven_for_secs == 700

    assert watch.check("task-1", now=800.0) is None  # latched, once per blind episode


def test_first_progress_clears_blind_and_enables_normal_detection():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0, blind=True, blind_reason="unproven")

    resumed = watch.progress("task-1", "model_text", now=10.0)
    assert resumed is not None  # bridge clears progress_blind on this signal
    assert not watch.is_blind("task-1")

    assert watch.check("task-1", now=300.0) is None
    later = watch.check("task-1", now=611.0)
    assert isinstance(later, StallEpisode)  # proven-live channel gone quiet: real episode


def test_reblind_starts_new_blind_episode_without_resetting_progress_clock():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0, blind=True, blind_reason="unproven")

    watch.progress("task-1", "model_text", now=10.0)
    clear_stale = watch.mark_blind("task-1", "tracker-disabled", now=500.0)
    assert clear_stale is True  # no active episode -> safe to clear stale marker

    episode = watch.check("task-1", now=620.0)
    assert isinstance(episode, BlindEpisode)
    assert episode.reason == "tracker-disabled"
    assert episode.unproven_for_secs == 610  # measured from last real progress, not mark_blind


def test_mark_blind_during_active_stall_episode_keeps_alarm_and_stays_quiet():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0)

    fired = watch.check("task-1", now=700.0)
    assert isinstance(fired, StallEpisode)

    clear_stale = watch.mark_blind("task-1", "tracker-disabled", now=710.0)
    assert clear_stale is False  # active fired episode: do NOT retract stalled_at
    assert watch.is_stalled("task-1")
    assert watch.check("task-1", now=1400.0) is None  # no blind report on top of a live alarm


def test_mark_blind_when_already_blind_updates_reason_without_new_episode():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0, blind=True, blind_reason="unproven")

    first = watch.check("task-1", now=700.0)
    assert isinstance(first, BlindEpisode)

    watch.mark_blind("task-1", "tracker-disabled", now=710.0)
    assert watch.check("task-1", now=800.0) is None  # same blind episode, no re-report


def test_unmark_blind_report_rearms_after_emission_failure():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0, blind=True, blind_reason="unproven")

    assert isinstance(watch.check("task-1", now=700.0), BlindEpisode)
    watch.unmark_blind_report("task-1")
    retried = watch.check("task-1", now=701.0)
    assert isinstance(retried, BlindEpisode)


def test_progress_between_blind_gap_check_and_mark_prevents_stale_blind_report():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0, blind=True, blind_reason="unproven")

    episode = watch.check(
        "task-1",
        now=601.0,
        before_mark=lambda: watch.progress("task-1", "command_output", now=601.0),
    )

    assert episode is None  # proof of light raced the report: no blind report
    assert not watch.is_blind("task-1")


def test_disabled_watch_never_reports_blind():
    watch = StallWatch(after_secs=0)
    watch.start("task-1", now=0.0, blind=True, blind_reason="unproven")

    assert watch.check("task-1", now=9999.0) is None
    assert watch.mark_blind("task-1", "tracker-disabled", now=9999.0) is False


def test_turn_continued_keepalive_resets_the_stall_clock():
    watch = StallWatch(after_secs=600)
    watch.start("task-1", now=0.0)
    watch.progress("task-1", "turn_continued", now=500.0)
    assert watch.check("task-1", now=1000.0) is None
    # Without the keepalive the same silence trips the watch.
    watch.start("task-2", now=0.0)
    assert watch.check("task-2", now=1000.0) is not None
