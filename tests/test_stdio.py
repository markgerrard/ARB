import io
import threading
import time
import unittest

from agent_redis_bridge.engines._stdio import start_stderr_drain


class FakeProc:
    def __init__(self, stderr) -> None:
        self.stderr = stderr


class StderrDrainTest(unittest.TestCase):
    def setUp(self) -> None:
        # Capture the forwarder's output so high-volume tests don't spam stderr.
        import agent_redis_bridge.engines._stdio as mod
        self._mod = mod
        self._orig_stderr = mod.sys.stderr
        mod.sys.stderr = io.StringIO()

    def tearDown(self) -> None:
        self._mod.sys.stderr = self._orig_stderr

    def _drain_and_wait(self, stream) -> threading.Thread:
        t = start_stderr_drain(FakeProc(stream), "test")
        t.join(timeout=2)
        return t

    def test_drains_text_stream_to_eof(self) -> None:
        # A finite text stream should be fully consumed and the thread exit.
        stream = io.StringIO("warning one\nwarning two\n")
        t = self._drain_and_wait(stream)
        self.assertFalse(t.is_alive())

    def test_drains_bytes_stream_to_eof(self) -> None:
        # pi_rpc spawns in bytes mode; the helper must handle bytes lines too.
        stream = io.BytesIO(b"byte warning\n\xff\xfe bad utf8\n")
        t = self._drain_and_wait(stream)
        self.assertFalse(t.is_alive())

    def test_none_stderr_is_noop(self) -> None:
        t = start_stderr_drain(FakeProc(None), "test")
        t.join(timeout=2)
        self.assertFalse(t.is_alive())

    def test_large_volume_does_not_block(self) -> None:
        # The whole point: a high-volume stderr must be drained, not buffered.
        # 1 MB of lines far exceeds any OS pipe buffer; the drain must finish.
        big = ("x" * 200 + "\n") * 5000
        stream = io.StringIO(big)
        start = time.monotonic()
        t = self._drain_and_wait(stream)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - start, 2)

    def test_reader_unblocked_by_stuck_forward_sink(self) -> None:
        # codex round-2 P1: a single-thread drain that synchronously flushed to a
        # backpressured sys.stderr would shift the deadlock one pipe downstream.
        # With the reader/forwarder split, the child-stderr reader must finish even
        # when the bridge's own stderr sink blocks forever.
        import agent_redis_bridge.engines._stdio as mod

        class BlockingSink:
            def write(self, *_a) -> int:
                time.sleep(60)
                return 0

            def flush(self) -> None:
                pass

        big = ("y" * 200 + "\n") * 5000
        stream = io.StringIO(big)
        orig = mod.sys.stderr
        mod.sys.stderr = BlockingSink()
        try:
            start = time.monotonic()
            reader = start_stderr_drain(FakeProc(stream), "test", max_buffer=10)
            reader.join(timeout=2)
            self.assertFalse(reader.is_alive())
            self.assertLess(time.monotonic() - start, 2)
        finally:
            mod.sys.stderr = orig


if __name__ == "__main__":
    unittest.main()
