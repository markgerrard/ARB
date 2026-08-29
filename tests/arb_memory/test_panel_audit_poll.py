from arb_memory.panel_audit import _poll_until_stable

class FakeClock:
    def __init__(self): self.t = 0.0
    def now(self): return self.t
    def sleep(self, s): self.t += s

def test_stabilizes_when_count_steady_and_lag_zero():
    clk = FakeClock()
    counts = iter([1, 2, 3, 3])           # stable on the 3==3 read
    lags = iter([{"pending":0,"lag":0}] * 4)
    assert _poll_until_stable(lambda: next(counts), lambda: next(lags),
                              timeout_s=30, interval_s=0.25, sleep=clk.sleep, now=clk.now) is True

def test_incomplete_when_lag_never_clears():
    clk = FakeClock()
    assert _poll_until_stable(lambda: 5, lambda: {"pending":3,"lag":1},
                              timeout_s=1, interval_s=0.25, sleep=clk.sleep, now=clk.now) is False

def test_incomplete_when_count_keeps_growing():
    clk = FakeClock()
    n = iter(range(1, 100))
    assert _poll_until_stable(lambda: next(n), lambda: {"pending":0,"lag":0},
                              timeout_s=1, interval_s=0.25, sleep=clk.sleep, now=clk.now) is False
