from agent_redis_bridge.claude_tail.watchdog import LAST_GASP, Watchdog


class Harness:
    def __init__(self, start=1000.0):
        self.now = start
        self.writes = []
        self.exits = []

    def time(self):
        return self.now

    def write(self, fd, data):
        self.writes.append((fd, data))

    def exit(self, code):
        self.exits.append(code)


def _watchdog(h, threshold=300.0, interval=1.0):
    return Watchdog(threshold, interval, time_func=h.time, write_func=h.write, exit_func=h.exit)


def test_no_fire_below_threshold():
    h = Harness()
    wd = _watchdog(h)
    h.now += 299.0
    wd.check()
    assert h.exits == []


def test_fires_past_threshold_with_raw_write_then_exit_86():
    h = Harness()
    wd = _watchdog(h)
    h.now += 301.0
    wd.check()
    assert h.exits == [86]
    assert h.writes == [(2, LAST_GASP)]


def test_no_fire_in_first_window_with_zero_ticks():
    # init pins last_tick to construction time (spec §B, panel r3 grok):
    # a fresh daemon must not immediately-fire before its first tick.
    h = Harness()
    wd = _watchdog(h)
    h.now += 100.0
    wd.check()
    assert h.exits == []


def test_mark_tick_resets_the_clock():
    h = Harness()
    wd = _watchdog(h)
    h.now += 250.0
    wd.mark_tick()
    h.now += 250.0
    wd.check()
    assert h.exits == []


def test_effective_threshold_floor_vs_long_interval():
    # interval 360s with configured 300s must NOT false-fire a healthy
    # sleeping daemon (spec §B, panel r3 agy): floor = 3*interval + 60.
    h = Harness()
    wd = Watchdog(300.0, 360.0, time_func=h.time, write_func=h.write, exit_func=h.exit)
    assert wd.effective_threshold == 3 * 360.0 + 60.0
    h.now += 1000.0
    wd.check()
    assert h.exits == []
    h.now += 200.0  # total 1200 > 1140
    wd.check()
    assert h.exits == [86]


def test_exit_runs_even_if_write_raises():
    # EBADF on a detached stderr must not kill the watchdog before exit
    # (spec §B, panel r2 agy).
    h = Harness()

    def bad_write(fd, data):
        raise OSError(9, "EBADF")

    wd = Watchdog(300.0, 1.0, time_func=h.time, write_func=bad_write, exit_func=h.exit)
    h.now += 301.0
    wd.check()
    assert h.exits == [86]


def test_check_never_touches_logging(monkeypatch):
    # The last gasp must not enter the logging framework (spec §B, panel r1
    # cold-Opus: a main thread hung holding a handler lock would deadlock us).
    import logging

    def boom(*args, **kwargs):
        raise AssertionError("watchdog called into logging")

    for name in ("info", "warning", "error", "exception", "critical", "debug", "log"):
        monkeypatch.setattr(logging.Logger, name, boom)
    h = Harness()
    wd = _watchdog(h)
    h.now += 301.0
    wd.check()
    assert h.exits == [86]
