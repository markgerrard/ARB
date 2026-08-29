import unittest

from agent_redis_bridge.envelope import Envelope, EnvelopeError, make_notify, make_reply


def valid_request() -> str:
    return (
        '{"id":"req-1","from":"claude-project-c-dev","branch":"dev",'
        '"to":"codex-project-c-dev","kind":"request","sent_at":"2026-04-26T18:00:00+01:00",'
        '"payload":{"task":"run git status"}}'
    )


class EnvelopeTest(unittest.TestCase):
    def test_parses_valid_request(self) -> None:
        envelope = Envelope.from_json(valid_request())

        self.assertEqual(envelope.id, "req-1")
        self.assertEqual(envelope.sender, "claude-project-c-dev")
        self.assertEqual(envelope.payload["task"], "run git status")

    def test_request_requires_task(self) -> None:
        raw = valid_request().replace('"task":"run git status"', '"note":"missing"')

        with self.assertRaisesRegex(EnvelopeError, "missing-payload-task"):
            Envelope.from_json(raw)

    def test_request_rejects_non_string_thread_id(self) -> None:
        raw = valid_request().replace(
            '"payload":{"task":"run git status"}',
            '"payload":{"task":"run git status","thread_id":123}',
        )

        with self.assertRaisesRegex(EnvelopeError, "invalid-payload-thread_id"):
            Envelope.from_json(raw)

    def test_request_rejects_empty_thread_id(self) -> None:
        raw = valid_request().replace(
            '"payload":{"task":"run git status"}',
            '"payload":{"task":"run git status","thread_id":"   "}',
        )

        with self.assertRaisesRegex(EnvelopeError, "invalid-payload-thread_id"):
            Envelope.from_json(raw)

    def test_request_rejects_thread_id_with_fresh_context_true(self) -> None:
        raw = valid_request().replace(
            '"payload":{"task":"run git status"}',
            '"payload":{"task":"run git status","thread_id":"thread-1","fresh_context":true}',
        )

        with self.assertRaisesRegex(EnvelopeError, "contradictory-context"):
            Envelope.from_json(raw)

    def test_request_accepts_thread_id_with_fresh_context_false(self) -> None:
        raw = valid_request().replace(
            '"payload":{"task":"run git status"}',
            '"payload":{"task":"run git status","thread_id":"thread-1","fresh_context":false}',
        )

        envelope = Envelope.from_json(raw)

        self.assertEqual(envelope.payload["thread_id"], "thread-1")

    def test_request_accepts_fork_from_thread_id(self) -> None:
        raw = valid_request().replace(
            '"payload":{"task":"run git status"}',
            '"payload":{"task":"run git status","fork_from_thread_id":"thread-base"}',
        )

        envelope = Envelope.from_json(raw)

        self.assertEqual(envelope.payload["fork_from_thread_id"], "thread-base")

    def test_request_rejects_invalid_fork_from_thread_id(self) -> None:
        for value in ("123", '"   "'):
            raw = valid_request().replace(
                '"payload":{"task":"run git status"}',
                f'"payload":{{"task":"run git status","fork_from_thread_id":{value}}}',
            )

            with self.assertRaisesRegex(EnvelopeError, "invalid-payload-fork_from_thread_id"):
                Envelope.from_json(raw)

    def test_request_rejects_thread_id_with_fork_from_thread_id(self) -> None:
        raw = valid_request().replace(
            '"payload":{"task":"run git status"}',
            '"payload":{"task":"run git status","thread_id":"thread-1","fork_from_thread_id":"thread-base"}',
        )

        with self.assertRaisesRegex(EnvelopeError, "contradictory-context"):
            Envelope.from_json(raw)

    def test_request_rejects_fork_from_thread_id_with_fresh_context_true(self) -> None:
        raw = valid_request().replace(
            '"payload":{"task":"run git status"}',
            '"payload":{"task":"run git status","fork_from_thread_id":"thread-base","fresh_context":true}',
        )

        with self.assertRaisesRegex(EnvelopeError, "contradictory-context"):
            Envelope.from_json(raw)

    def test_request_accepts_fork_from_thread_id_with_fresh_context_false(self) -> None:
        raw = valid_request().replace(
            '"payload":{"task":"run git status"}',
            '"payload":{"task":"run git status","fork_from_thread_id":"thread-base","fresh_context":false}',
        )

        envelope = Envelope.from_json(raw)

        self.assertEqual(envelope.payload["fork_from_thread_id"], "thread-base")

    def test_reply_requires_in_reply_to(self) -> None:
        raw = valid_request().replace('"kind":"request"', '"kind":"reply"')

        with self.assertRaisesRegex(EnvelopeError, "missing-in_reply_to"):
            Envelope.from_json(raw)

    def test_make_reply_sets_in_reply_to(self) -> None:
        reply = make_reply(
            sender="codex-project-c-dev",
            recipient="claude-project-c-dev",
            branch="dev",
            in_reply_to="req-1",
            payload={"result": "ok", "ok": True},
        )

        self.assertEqual(reply.kind, "reply")
        self.assertEqual(reply.in_reply_to, "req-1")
        self.assertIs(Envelope.from_json(reply.to_json()).payload["ok"], True)

    def test_make_notify_sets_event_payload(self) -> None:
        notify = make_notify(
            sender="codex-project-c-dev",
            recipient="claude-project-c-dev",
            branch="dev",
            event="task_started",
            data={"task_id": "req-1"},
        )

        self.assertEqual(notify.kind, "notify")
        self.assertEqual(notify.payload["event"], "task_started")
        self.assertEqual(notify.payload["data"]["task_id"], "req-1")

    def test_parses_steer_message(self) -> None:
        raw = (
            '{"id":"steer-1","from":"human-codexctl","branch":"manual",'
            '"to":"codex-project-c-dev","kind":"steer","sent_at":"2026-04-26T18:00:00+01:00",'
            '"payload":{"task_id":"req-1","message":"tighten the patch"}}'
        )

        envelope = Envelope.from_json(raw)

        self.assertEqual(envelope.kind, "steer")
        self.assertEqual(envelope.payload["message"], "tighten the patch")

    def test_steer_requires_message(self) -> None:
        raw = (
            '{"id":"steer-1","from":"human-codexctl","branch":"manual",'
            '"to":"codex-project-c-dev","kind":"steer","sent_at":"2026-04-26T18:00:00+01:00",'
            '"payload":{"task_id":"req-1"}}'
        )

        with self.assertRaisesRegex(EnvelopeError, "missing-payload-message"):
            Envelope.from_json(raw)


if __name__ == "__main__":
    unittest.main()
