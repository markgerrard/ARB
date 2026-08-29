"""Offline tests for MuseRunner. No `muse` process is ever spawned.

Process management is exercised by injecting `_spawn`, so the whole file runs
with zero API spend. The live tier (spec §6, gates G1-G6) is separate.
"""
import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from arb_warm_orch.muse_runner import MuseConfig, MuseRunner


def cfg_in(d, **kw):
    return MuseConfig(cwd=Path(d), session_dir=Path(d) / "s", **kw)


class TestSessionIdentity(unittest.TestCase):
    """Design §3.1 -- the session-id inversion.

    runner.py:238-250 records that the Claude SDK hands back its session id at
    turn END, so an abandoned generator never advances the channel. We mint the
    UUID ourselves, so it is persisted at connect() before any process exists
    and continuity survives a crash, a kill, or an abandoned generator.
    """

    def test_connect_persists_session_id_before_any_process(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            asyncio.run(r.connect())
            sid = r.session_id
            self.assertIsNotNone(sid)
            self.assertEqual(len(sid), 36)
            self.assertEqual(sid, sid.lower())
            self.assertIsNone(r._proc)          # nothing was spawned
            self.assertTrue(r._session_id_path().exists())
            self.assertEqual(r._session_id_path().read_text().strip(), sid)

    def test_second_runner_reuses_the_persisted_id(self):
        with TemporaryDirectory() as d:
            a = MuseRunner(cfg_in(d))
            asyncio.run(a.connect())
            b = MuseRunner(cfg_in(d))
            asyncio.run(b.connect())
            self.assertEqual(a.session_id, b.session_id)

    def test_connect_is_idempotent(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            asyncio.run(r.connect())
            first = r.session_id
            asyncio.run(r.connect())
            self.assertEqual(r.session_id, first)



async def _drain(agen):
    return [e async for e in agen]


def _scripted_spawn(lines, record=None):
    """Stand-in for asyncio.create_subprocess_exec. Spawns nothing."""
    async def _spawn(argv, cwd):
        if record is not None:
            record.append((argv, cwd))

        class _Proc:
            returncode = 0

            def __init__(self):
                self.stdout = self._lines()

            async def _lines(self):
                for ln in lines:
                    yield ln.encode()

            async def wait(self):
                return 0

            def terminate(self):
                pass

        return _Proc()
    return _spawn


class TestArgv(unittest.TestCase):
    def test_argv_carries_the_required_flags(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d, model="muse-spark-1.2",
                                  reasoning_effort="minimal"))
            asyncio.run(r.connect())
            argv = r.build_argv(Path("/tmp/p.md"))
            self.assertEqual(argv[:2], ["muse", "exec"])
            self.assertIn("--json", argv)
            self.assertIn(r.session_id, argv)
            self.assertEqual(argv[argv.index("--session-id") + 1], r.session_id)
            self.assertEqual(argv[argv.index("--prompt-file") + 1], "/tmp/p.md")
            self.assertEqual(argv[argv.index("--model") + 1], "muse-spark-1.2")
            self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "minimal")
            self.assertEqual(argv[argv.index("--workspace") + 1], d)

    def test_argv_omits_unset_optionals(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            asyncio.run(r.connect())
            argv = r.build_argv(Path("/tmp/p.md"))
            self.assertNotIn("--model", argv)
            self.assertNotIn("--reasoning-effort", argv)

    def test_argv_never_widens_the_blast_radius(self):
        from arb_warm_orch.muse_runner import FORBIDDEN_FLAGS
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d, model="m", reasoning_effort="high"))
            asyncio.run(r.connect())
            argv = r.build_argv(Path("/tmp/p.md"))
            for banned in FORBIDDEN_FLAGS:
                self.assertNotIn(banned, argv)

    def test_build_argv_before_connect_is_an_error(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            with self.assertRaises(RuntimeError):
                r.build_argv(Path("/tmp/p.md"))


class TestStreamTurn(unittest.TestCase):
    LINES = [
        "muse: workspace root: /tmp/x",
        '{"payload_type":"run.output.delta","payload":{"text":"he"}}',
        '{"payload_type":"task.lifecycle.failed","payload":'
        '{"kind":"task_lifecycle","event":{"kind":"failed","reason":"noise"}}}',
        '{"payload_type":"run.output.delta","payload":{"text":"llo"}}',
        '{"payload_type":"run.terminal.completed","payload":'
        '{"terminal":"completed","text":"hello"}}',
    ]

    def test_stream_turn_maps_a_scripted_stdout(self):
        from arb_warm_orch.turn_events import TextDelta
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _scripted_spawn(self.LINES)
            got = asyncio.run(_drain(r.stream_turn("hi")))
            self.assertEqual([e.text for e in got if isinstance(e, TextDelta)],
                             ["he", "llo"])

    def test_turn_returns_joined_text_despite_failed_events(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _scripted_spawn(self.LINES)
            self.assertEqual(asyncio.run(r.turn("hi")), "hello")
            self.assertEqual(r.last_terminal, "completed")
            self.assertEqual(r.last_failures, ["noise"])

    def test_prompt_reaches_a_file_not_argv(self):
        recorded = []
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _scripted_spawn(self.LINES, record=recorded)
            asyncio.run(r.turn("some brief with 'quotes' and\nnewlines"))
            argv, _ = recorded[0]
            self.assertNotIn("some brief with 'quotes' and\nnewlines", argv)
            written = Path(argv[argv.index("--prompt-file") + 1]).read_text()
            self.assertEqual(written, "some brief with 'quotes' and\nnewlines")

    def test_proc_is_cleared_after_a_turn(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _scripted_spawn(self.LINES)
            asyncio.run(r.turn("hi"))
            self.assertIsNone(r._proc)


class TestInterruptAndSystemPrompt(unittest.TestCase):
    def test_interrupt_terminates_child_and_keeps_session_id(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            asyncio.run(r.connect())
            sid = r.session_id
            killed = []

            class _P:
                returncode = None

                def terminate(self):
                    killed.append(True)

                async def wait(self):
                    return -15

            r._proc = _P()
            asyncio.run(r.interrupt())
            self.assertEqual(killed, [True])
            self.assertEqual(r.session_id, sid)      # NOT rotated -- design 4
            self.assertIsNone(r._proc)

    def test_interrupt_with_no_child_is_a_noop(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            asyncio.run(r.connect())
            asyncio.run(r.interrupt())

    def test_session_id_survives_an_abandoned_generator(self):
        """The inversion in design 3.1, asserted rather than described."""
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _scripted_spawn(TestStreamTurn.LINES)

            async def abandon():
                agen = r.stream_turn("hi")
                await agen.__anext__()          # take ONE event, then walk away
                await agen.aclose()

            asyncio.run(abandon())
            reopened = MuseRunner(cfg_in(d))
            asyncio.run(reopened.connect())
            self.assertEqual(reopened.session_id, r.session_id)

    def test_apply_system_prompt_is_loud_not_a_silent_noop(self):
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            self.assertTrue(hasattr(r, "apply_system_prompt"))
            with self.assertRaises(NotImplementedError) as ctx:
                r.apply_system_prompt("you are a seat")
            self.assertIn("G2", str(ctx.exception))



class TestReplyIsolation(unittest.TestCase):
    """turn() must join TEXT ONLY -- tool traffic must not reach the channel.

    Added 2026-08-06 because the mutation sweep proved the existing turn() test
    could NOT catch a reply-leakage regression: its fixture contained only text
    events, so "join every event" and "join TextDelta only" produced identical
    output. A test that cannot distinguish the two is not testing the rule.
    """

    LINES_WITH_TOOLS = [
        "muse: workspace root: /tmp/x",
        '{"payload_type":"run.output.delta","payload":{"text":"before "}}',
        '{"payload_type":"task.lifecycle.proposed","payload":{"task_id":"t1",'
        '"kind":"task_lifecycle","event":{"kind":"proposed","task_id":"t1",'
        '"task_kind":"tool.bash"}}}',
        '{"payload_type":"task.lifecycle.scheduled","payload":{"task_id":"t1",'
        '"kind":"task_lifecycle","event":{"kind":"scheduled","task_id":"t1",'
        '"idempotency_key":"tool:call_x"}}}',
        '{"payload_type":"tool.result","payload":'
        '{"call_id":"call_x","correlation_facts":{"outcome":"success"}}}',
        '{"payload_type":"run.output.delta","payload":{"text":"after"}}',
        '{"payload_type":"run.terminal.completed","payload":'
        '{"terminal":"completed","text":"before after"}}',
    ]

    def test_tool_events_are_emitted_but_excluded_from_the_reply(self):
        from arb_warm_orch.turn_events import (
            TextDelta, ToolCallCompleted, ToolCallStarted)
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _scripted_spawn(self.LINES_WITH_TOOLS)
            events = asyncio.run(_drain(r.stream_turn("hi")))

            # the tool lifecycle DID surface as events...
            self.assertTrue(any(isinstance(e, ToolCallStarted) for e in events))
            self.assertTrue(any(isinstance(e, ToolCallCompleted) for e in events))
            self.assertEqual(len([e for e in events
                                  if isinstance(e, TextDelta)]), 2)

        # ...but turn() returns ONLY the text
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _scripted_spawn(self.LINES_WITH_TOOLS)
            self.assertEqual(asyncio.run(r.turn("hi")), "before after")



class _ReapingProc:
    """Fake child that records reaping. returncode stays None until wait()."""

    def __init__(self, lines):
        self._lines = lines
        self.returncode = None
        self.terminated = 0
        self.waited = 0
        self.stdout = self._gen()

    async def _gen(self):
        for ln in self._lines:
            yield ln.encode()

    def terminate(self):
        self.terminated += 1

    async def wait(self):
        self.waited += 1
        self.returncode = 0
        return 0


def _reaping_spawn(lines, holder):
    async def _spawn(argv, cwd):
        holder.append(_ReapingProc(lines))
        return holder[-1]
    return _spawn


class TestChildIsAlwaysReaped(unittest.TestCase):
    """Panel finding F1 (cold-opus, 2026-08-06), confirmed against source.

    `finally: self._proc = None` runs on GeneratorExit and cleared the handle
    WITHOUT terminating or waiting the child -- and `await proc.wait()` sat
    inside the try, so it was only reached on normal completion. interrupt()
    and disconnect() both early-return on `_proc is None`, so the orphan could
    never be reaped. It holds the session, and the next turn spawns a
    concurrent exec on the same --session-id (the unrun gate G4).

    The pre-existing abandoned-generator test could not catch this: it asserts
    only that the session id survives, which it does either way.
    """

    def test_abandoning_stream_turn_still_reaps_the_child(self):
        holder = []
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _reaping_spawn(TestStreamTurn.LINES, holder)

            async def abandon():
                agen = r.stream_turn("hi")
                await agen.__anext__()      # one event, then walk away
                await agen.aclose()

            asyncio.run(abandon())
            proc = holder[0]
            self.assertEqual(proc.terminated, 1, "child was never terminated")
            self.assertGreaterEqual(proc.waited, 1, "child was never awaited")
            self.assertIsNone(r._proc)

    def test_normal_completion_does_not_double_terminate(self):
        holder = []
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _reaping_spawn(TestStreamTurn.LINES, holder)
            asyncio.run(r.turn("hi"))
            proc = holder[0]
            self.assertEqual(proc.terminated, 0,
                             "a cleanly-finished child must not be signalled")
            self.assertGreaterEqual(proc.waited, 1)


class TestTurnLockIsAlwaysReleased(unittest.TestCase):
    """Panel finding P1 (second wave, 2026-08-07) -- reproduced by three seats.

    The F2 fix acquired `_turn_lock` at :191 but opened its `try` at :199, so
    `_write_prompt`, `build_argv` and `_spawn` sat in the gap between the
    acquire and the `finally` that releases it. Any raise there leaked the lock
    PERMANENTLY: every later turn on that runner blocks forever, and nothing
    logs. `acp_server` dispatches each `session/prompt` as a task, so one
    missing binary or bad cwd wedges the whole seat with no local recovery.

    Same defect class as F1 above -- a resource acquired outside the scope that
    releases it -- reintroduced BY the fix for F2. That is why these assert on
    the lock itself rather than on any observable side effect: the leak is
    silent, and a test that only checked the raise propagates would pass
    against the broken code.
    """

    def _runner_whose_spawn_fails(self, d):
        r = MuseRunner(cfg_in(d))

        async def _boom(argv, cwd):
            raise FileNotFoundError("muse binary missing")
        r._spawn = _boom
        return r

    def test_spawn_failure_releases_the_lock(self):
        with TemporaryDirectory() as d:
            r = self._runner_whose_spawn_fails(d)

            async def one_failing_turn():
                with self.assertRaises(FileNotFoundError):
                    async for _ in r.stream_turn("hi"):
                        pass
            asyncio.run(one_failing_turn())
            self.assertFalse(r._turn_lock.locked(),
                             "_turn_lock leaked after a spawn failure")

    def test_runner_still_usable_after_a_spawn_failure(self):
        """The consequence, not just the mechanism: a second turn must run.

        Asserting only `locked() is False` would still pass if a fix released
        the lock but left the runner wedged some other way.
        """
        holder = []
        with TemporaryDirectory() as d:
            r = self._runner_whose_spawn_fails(d)

            async def scenario():
                with self.assertRaises(FileNotFoundError):
                    async for _ in r.stream_turn("first"):
                        pass
                # The spawn now works; the second turn must complete normally
                # rather than block forever on the leaked lock.
                r._spawn = _reaping_spawn(TestStreamTurn.LINES, holder)
                return await asyncio.wait_for(r.turn("second"), timeout=5)

            text = asyncio.run(scenario())
            self.assertEqual(text, "hello")     # TestStreamTurn.LINES payload
            self.assertEqual(len(holder), 1, "second turn never spawned")

    def test_prompt_write_failure_releases_the_lock(self):
        """`_write_prompt` is the other pre-try step, and fails independently
        of `_spawn` -- a full disk or an unwritable session_dir reaches it."""
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))

            def _boom(_text):
                raise OSError("no space left on device")
            r._write_prompt = _boom

            async def one_failing_turn():
                with self.assertRaises(OSError):
                    async for _ in r.stream_turn("hi"):
                        pass
            asyncio.run(one_failing_turn())
            self.assertFalse(r._turn_lock.locked(),
                             "_turn_lock leaked after a prompt-write failure")



class _FullProc:
    """Fake child with stdout AND stderr, a settable exit code."""

    def __init__(self, lines, stderr_chunks=(), returncode=0):
        self._lines = lines
        self._stderr_chunks = list(stderr_chunks)
        self.stderr_read = 0
        self._rc = returncode
        self.returncode = None
        self.stdout = self._out()
        self.stderr = self._err()

    async def _out(self):
        for ln in self._lines:
            yield ln.encode()

    async def _err(self):
        for c in self._stderr_chunks:
            self.stderr_read += 1
            yield c.encode()

    def terminate(self):
        pass

    async def wait(self):
        self.returncode = self._rc
        return self._rc


def _full_spawn(holder, **kw):
    async def _spawn(argv, cwd):
        holder.append(_FullProc(TestStreamTurn.LINES, **kw))
        return holder[-1]
    return _spawn


class TestFailureIsLoud(unittest.TestCase):
    """Panel finding F4 (cold-opus, 2026-08-06), confirmed against source.

    The runner ignored the child's exit status and never consumed the terminal
    it recorded, so a failed turn returned "" and the ACP wire reported
    end_turn -- the silent-empty-success that grok_runner/pi_runner were
    changed to eliminate (GrokTurnFailed / PiTurnFailed are the precedent).
    """

    def test_nonzero_exit_raises_rather_than_returning_empty(self):
        from arb_warm_orch.muse_runner import MuseTurnFailed
        holder = []
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _full_spawn(holder, returncode=2)
            with self.assertRaises(MuseTurnFailed) as ctx:
                asyncio.run(r.turn("hi"))
            self.assertIn("2", str(ctx.exception))

    def test_missing_terminal_raises(self):
        """Asserts the SPECIFIC refusal, not merely that one happened.

        Since the P2 check landed (2026-08-07) this path is layered: a missing
        terminal is `None`, and `None != "completed"` means the P2 check would
        refuse it too. A bare `assertRaises(MuseTurnFailed)` therefore became
        UNABLE TO FAIL -- deleting the branch under test just let the next
        layer refuse, and the test stayed green. The mutation sweep caught it
        as NOT CAUGHT; nothing else would have.

        This is `docs/defect-classes/refusal-is-ambient-assert-the-code.md`.
        The two conditions are genuinely different diagnoses -- "the turn never
        finished" vs "the turn finished and reported failure" -- so the message
        is the thing worth pinning.
        """
        from arb_warm_orch.muse_runner import MuseTurnFailed
        holder = []
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            # no run.terminal.* line at all -> the turn never completed
            holder_lines = [l for l in TestStreamTurn.LINES
                            if "run.terminal" not in l]

            async def _spawn(argv, cwd):
                holder.append(_FullProc(holder_lines))
                return holder[-1]
            r._spawn = _spawn
            with self.assertRaises(MuseTurnFailed) as ctx:
                asyncio.run(r.turn("hi"))
            self.assertIn("no run.terminal.* event", str(ctx.exception),
                          "must be the missing-terminal refusal specifically, "
                          "not the P2 non-completed refusal standing in for it")

    def test_clean_turn_does_not_raise(self):
        holder = []
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _full_spawn(holder)
            self.assertEqual(asyncio.run(r.turn("hi")), "hello")

    def test_non_completed_terminal_raises_even_on_exit_zero(self):
        """Panel P2 (grok, second wave 2026-08-07).

        F4 raised on a non-zero exit and on a MISSING terminal, but not on a
        terminal that is PRESENT and not a success. `run.terminal.failed` with
        exit 0 therefore returned its partial text and the ACP wire reported an
        ordinary `end_turn` -- the same silent-success class F4 exists to kill.

        Only `completed` is attested: it is the sole terminal value in all five
        fixture occurrences and every test. So this fails CLOSED on anything
        else. If a future Muse build emits another success-shaped terminal,
        this test is where that shows up -- loudly, which is the point. A
        silent wrong answer is the failure mode being traded away.
        """
        from arb_warm_orch.muse_runner import MuseTurnFailed
        holder = []
        lines = [
            "muse: workspace root: /tmp/x",
            '{"payload_type":"run.output.delta","payload":{"text":"partial"}}',
            '{"payload_type":"run.terminal.failed","payload":'
            '{"terminal":"failed","text":"partial","reason":"provider gave up"}}',
        ]
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))

            async def _spawn(argv, cwd):
                holder.append(_FullProc(lines))       # returncode defaults to 0
                return holder[-1]
            r._spawn = _spawn
            with self.assertRaises(MuseTurnFailed) as ctx:
                asyncio.run(r.turn("hi"))
            self.assertIn("failed", str(ctx.exception),
                          "the raise must name the terminal it refused")

    def test_stderr_is_drained(self):
        """F4: stderr=PIPE with no reader deadlocks once the OS buffer fills.
        engines/_stdio.py:372 exists in this repo for exactly this reason."""
        holder = []
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))
            r._spawn = _full_spawn(holder, stderr_chunks=["noise\n"] * 50)
            asyncio.run(r.turn("hi"))
            self.assertEqual(holder[0].stderr_read, 50,
                             "stderr was never drained")


class TestTurnsAreSerialised(unittest.TestCase):
    """Panel finding F2 (cold-opus, 2026-08-06), confirmed against source.

    acp_server.py:142-145 dispatches every `session/prompt` as a TASK, and
    :85-89 documents that as deliberate. So concurrent turns on one
    --session-id are reachable by the consumer's design. G4 is not merely
    unmeasured, it was unenforced.
    """

    def test_concurrent_turns_never_overlap(self):
        live = {"now": 0, "max": 0}
        with TemporaryDirectory() as d:
            r = MuseRunner(cfg_in(d))

            async def _spawn(argv, cwd):
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])

                class _P:
                    returncode = None
                    def __init__(self):
                        self.stdout = self._o(); self.stderr = self._e()
                    async def _o(self):
                        for ln in TestStreamTurn.LINES:
                            await asyncio.sleep(0)
                            yield ln.encode()
                    async def _e(self):
                        if False:
                            yield b""
                    def terminate(self): pass
                    async def wait(self):
                        live["now"] -= 1
                        self.returncode = 0
                        return 0
                return _P()

            r._spawn = _spawn

            async def both():
                await asyncio.gather(r.turn("a"), r.turn("b"))

            asyncio.run(both())
            self.assertEqual(live["max"], 1,
                             f"{live['max']} concurrent execs on one session-id")


if __name__ == "__main__":
    unittest.main()
