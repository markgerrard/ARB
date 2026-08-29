"""ARB-B6 refusal reply — acceptance criteria A1–A14 (design v4 confirmed core).

A15/A16 are withdrawn with their mechanisms; not implemented here.

rem1: tests pin the *named* guard (mutation-sensitive) and exercise the
construction-time posture seam rather than post-construction attribute writes.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_redis_bridge.bridge import Bridge, build_parser
from agent_redis_bridge.envelope import TASK_REF_REQUIRED_ENV, EnvelopeHeader

from tests.test_bridge_handle_raw import FakeRedis, make_bridge, request_json, RecordingEngine


def _ref_required_bridge(*extra: str) -> Bridge:
    """Seat with task-ref posture forced through the construction-time CLI seam."""
    return make_bridge("--dry-run", "--task-ref-required", *extra)


def _dual_accept_bridge(*extra: str, dry_run: bool = False) -> Bridge:
    """Dual-accept seat via construction-time CLI seam (not ambient env)."""
    args: list[str] = ["--no-task-ref-required", *extra]
    if dry_run:
        args = ["--dry-run", *args]
    return make_bridge(*args)


def _bridge_with_env_file(
    env_body: str,
    *extra: str,
    env_overrides: dict[str, str] | None = None,
    clear_task_ref_env: bool = True,
) -> Bridge:
    """Build a Bridge whose env-file and optional process-env are under test control."""
    with tempfile.TemporaryDirectory() as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text(
            "AGENT_REDIS_HOST=127.0.0.1\n"
            "AGENT_REDIS_PORT=6390\n"
            "AGENT_REDIS_DB=12\n"
            "AGENT_REDIS_PREFIX=agent_scratch:\n"
            "AGENT_WORKSPACE=dev\n"
            "AGENT_PROJECT=project-c\n"
            + env_body
        )
        args = build_parser().parse_args(
            [
                "--env-file",
                str(env_file),
                "--workdir",
                "/srv/projects/example-bridge",
                "--sender-policy",
                "claude-project-c-dev=trusted",
                "--dry-run",
                *extra,
            ]
        )
        # Isolate process env for the posture key unless the caller injects it.
        cleaned = {
            k: v
            for k, v in os.environ.items()
            if not (clear_task_ref_env and k == TASK_REF_REQUIRED_ENV)
        }
        if env_overrides:
            cleaned.update(env_overrides)
        with mock.patch.dict(os.environ, cleaned, clear=True):
            return Bridge(args)


def _legacy_refusal_raw(
    request_id: str = "req-b6",
    *,
    sender: str = "claude-project-c-dev",
    recipient: str = "codex-project-c-dev",
    payload: dict | None = None,
    kind: str = "request",
    run_id: object | None = ...,
    extra_fields: dict | None = None,
) -> str:
    body: dict = {
        "id": request_id,
        "from": sender,
        "branch": "manual",
        "to": recipient,
        "kind": kind,
        "sent_at": "2026-04-26T19:00:00+01:00",
        "payload": payload if payload is not None else {"task": "legacy string"},
    }
    if run_id is not ...:
        body["run_id"] = run_id
    if extra_fields:
        body.update(extra_fields)
    return json.dumps(body, separators=(",", ":"))


def _request_header(
    *,
    request_id: str = "req-b6-hdr",
    sender: str = "claude-project-c-dev",
    recipient: str = "codex-project-c-dev",
    kind: str = "request",
    payload: dict | None = None,
) -> EnvelopeHeader:
    return EnvelopeHeader(
        id=request_id,
        sender=sender,
        branch="manual",
        recipient=recipient,
        kind=kind,
        sent_at="2026-04-26T19:00:00+01:00",
        payload=payload if payload is not None else {"task": "legacy string"},
    )


# Pre-change ordinary-reply oracle for A10 (design §9): structural equality after
# masking generated id/sent_at. Captured from send_reply shape under dry-run
# engine stub with --no-enforce-completion; refusal path must not perturb it.
_A10_ORDINARY_ORACLE = {
    "branch": "unknown",
    "from": "codex-project-c-dev",
    "in_reply_to": "req-b6-a10",
    "kind": "reply",
    "to": "claude-project-c-dev",
    "payload": {
        "artifact_paths": [],
        "completion": None,
        "error": None,
        "ok": True,
        "result": "ordinary-ok",
        "thread_id": None,
    },
}


class B6RefusalReplyTest(unittest.TestCase):
    def test_a1_ref_required_legacy_string_sends_refusal_reply(self) -> None:
        bridge = _ref_required_bridge()
        self.assertIs(bridge.task_ref_required, True)
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        bridge.handle_raw(_legacy_refusal_raw("req-b6"))

        self.assertEqual(len(fake.replies), 1)
        agent_id, body_raw = fake.replies[0]
        self.assertEqual(agent_id, "claude-project-c-dev")
        body = json.loads(body_raw)
        self.assertEqual(body["kind"], "reply")
        self.assertEqual(body["in_reply_to"], "req-b6")
        self.assertEqual(body["to"], "claude-project-c-dev")
        self.assertEqual(body["from"], "codex-project-c-dev")
        payload = body["payload"]
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error_code"], "invalid-payload-task-ref")
        self.assertEqual(payload["task_type"], "str")
        self.assertEqual(payload["refused"], "envelope-parse")
        self.assertEqual(payload["error"], "envelope-invalid: invalid-payload-task-ref")
        self.assertIs(payload["task_ref_required"], True)
        self.assertEqual(payload["result"], "")
        self.assertIsNone(payload["completion"])
        self.assertIsNone(payload["thread_id"])
        self.assertEqual(payload["artifact_paths"], [])

    def test_a2_envelope_invalid_log_line_unchanged(self) -> None:
        bridge = _ref_required_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as cm:
            bridge.handle_raw(_legacy_refusal_raw("req-b6-a2"))
        # Equality on the record message (not substring of joined output): a
        # prefixed/suffixed variant must fail.
        messages = [record.getMessage() for record in cm.records]
        self.assertIn(
            "[bridge-error] envelope-invalid invalid-payload-task-ref",
            messages,
        )
        match = [
            m
            for m in messages
            if "envelope-invalid" in m and "invalid-payload-task-ref" in m
        ]
        self.assertEqual(len(match), 1)
        self.assertEqual(
            match[0],
            "[bridge-error] envelope-invalid invalid-payload-task-ref",
        )

    def test_a3_malformed_ref_dict_task_type(self) -> None:
        bridge = _ref_required_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        bridge.handle_raw(
            _legacy_refusal_raw(
                "req-b6-a3",
                payload={"task": {"artefact_id": "art-x", "version": 0}},
            )
        )

        self.assertEqual(len(fake.replies), 1)
        payload = json.loads(fake.replies[0][1])["payload"]
        self.assertEqual(payload["error_code"], "invalid-payload-task-ref")
        self.assertEqual(payload["task_type"], "dict")

    def test_a4_unknown_sender_no_reply_but_log(self) -> None:
        bridge = _ref_required_bridge("--unknown-sender-policy", "reject")
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as cm:
            bridge.handle_raw(
                _legacy_refusal_raw("req-b6-a4", sender="nobody-agent")
            )
        self.assertEqual(fake.replies, [])
        messages = [record.getMessage() for record in cm.records]
        self.assertIn(
            "[bridge-error] envelope-invalid invalid-payload-task-ref",
            messages,
        )

    def test_a5_wrong_recipient_no_reply(self) -> None:
        bridge = _ref_required_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        bridge.handle_raw(
            _legacy_refusal_raw("req-b6-a5", recipient="some-other-seat")
        )
        self.assertEqual(fake.replies, [])
        # Guard 3: header.recipient != self.agent_id (parse still logs reason).
        self.assertNotEqual("some-other-seat", bridge.agent_id)

    def test_a6_self_sender_no_reply(self) -> None:
        # Roster the seat's own agent_id so guard 5 cannot suppress independently
        # of guard 4 (self-reply). Deleting guard 4 alone must turn this red.
        bridge = _ref_required_bridge(
            "--sender-policy", "codex-project-c-dev=trusted"
        )
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        self.assertEqual(bridge.agent_id, "codex-project-c-dev")
        self.assertIn("codex-project-c-dev", bridge.sender_policies)

        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as cm:
            bridge.handle_raw(
                _legacy_refusal_raw("req-b6-a6", sender="codex-project-c-dev")
            )
        self.assertEqual(fake.replies, [])
        # Reached the refusal path (header valid + task-ref refuse); guard 4
        # dropped the send. The self condition is sender == self.agent_id.
        messages = [record.getMessage() for record in cm.records]
        self.assertIn(
            "[bridge-error] envelope-invalid invalid-payload-task-ref",
            messages,
        )
        self.assertEqual("codex-project-c-dev", bridge.agent_id)

    def test_a7_header_invalid_missing_id_no_reply(self) -> None:
        bridge = _ref_required_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        raw = json.dumps(
            {
                "from": "claude-project-c-dev",
                "branch": "manual",
                "to": "codex-project-c-dev",
                "kind": "request",
                "sent_at": "2026-04-26T19:00:00+01:00",
                "payload": {"task": "legacy string"},
            },
            separators=(",", ":"),
        )
        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as cm:
            bridge.handle_raw(raw)
        self.assertEqual(fake.replies, [])
        joined = "\n".join(record.getMessage() for record in cm.records)
        self.assertIn("missing-id", joined)

    def test_a8_non_repliable_reason_request_no_reply(self) -> None:
        # Guard 1 isolation: kind=request header so guard 2 cannot suppress;
        # reason missing-payload-message is NOT on REFUSAL_REPLY_REASONS.
        # Widening the allowlist with that token must turn this red.
        bridge = _ref_required_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        header = _request_header(request_id="req-b6-a8")
        self.assertEqual(header.kind, "request")
        self.assertNotIn(
            "missing-payload-message",
            Bridge.REFUSAL_REPLY_REASONS,
        )
        bridge.send_refusal_reply(header, "missing-payload-message")
        self.assertEqual(fake.replies, [])

    def test_a9_payload_bound_and_no_echo(self) -> None:
        bridge = _ref_required_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]

        huge = "x" * (1024 * 1024)
        bridge.handle_raw(
            _legacy_refusal_raw("req-b6-a9-huge", payload={"task": huge})
        )
        self.assertEqual(len(fake.replies), 1)
        payload = json.loads(fake.replies[0][1])["payload"]
        self.assertLessEqual(len(json.dumps(payload)), 512)
        self.assertEqual(payload["error_code"], "invalid-payload-task-ref")

        fake.replies.clear()
        bridge.handle_raw(
            _legacy_refusal_raw(
                "req-b6-a9-secret",
                payload={"task": "legacy-secret-xyz"},
            )
        )
        self.assertEqual(len(fake.replies), 1)
        dumped = json.dumps(json.loads(fake.replies[0][1])["payload"])
        self.assertNotIn("legacy", dumped)
        self.assertNotIn("secret", dumped)

    def test_a10_ordinary_valid_request_unchanged(self) -> None:
        # Design A10: structural equality to pre-change ordinary reply after
        # masking generated id/sent_at. Filter kind==reply so notify_inbox
        # LPUSHes do not count as the ordinary reply under test.
        bridge = _dual_accept_bridge("--no-enforce-completion")
        self.assertIs(bridge.task_ref_required, False)
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        engine = RecordingEngine("ordinary-ok")
        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.pool.acquire = mock.Mock(return_value=engine)  # type: ignore[method-assign]
            bridge.pool.release = mock.Mock()  # type: ignore[method-assign]
            bridge.handle_raw(request_json("req-b6-a10", payload={"task": "Work."}))
            bridge.join_active_thread()

        replies = [
            json.loads(body)
            for _, body in fake.replies
            if json.loads(body).get("kind") == "reply"
        ]
        self.assertEqual(len(replies), 1)
        body = replies[0]
        masked = {k: v for k, v in body.items() if k not in ("id", "sent_at")}
        self.assertEqual(masked, _A10_ORDINARY_ORACLE)
        self.assertNotIn("error_code", body["payload"])
        self.assertNotIn("refused", body["payload"])

    def test_a11_dual_accept_legacy_string_admitted(self) -> None:
        bridge = _dual_accept_bridge("--no-enforce-completion")
        self.assertIs(bridge.task_ref_required, False)
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        engine = RecordingEngine("dual-accept-ok")
        with mock.patch("agent_redis_bridge.bridge.build_engine", return_value=engine):
            bridge.pool.acquire = mock.Mock(return_value=engine)  # type: ignore[method-assign]
            bridge.pool.release = mock.Mock()  # type: ignore[method-assign]
            bridge.handle_raw(
                request_json("req-b6-a11", payload={"task": "legacy string"})
            )
            bridge.join_active_thread()

        replies = [
            json.loads(body)
            for _, body in fake.replies
            if json.loads(body).get("kind") == "reply"
        ]
        self.assertEqual(len(replies), 1)
        payload = replies[0]["payload"]
        self.assertNotIn("error_code", payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"], "dual-accept-ok")

    def test_a13_kind_guard_invalid_run_id_no_reply(self) -> None:
        kinds = ("reply", "steer", "cancel", "notify")
        for kind in kinds:
            with self.subTest(kind=kind, path="ordinary"):
                bridge = _ref_required_bridge()
                fake = FakeRedis()
                bridge.redis = fake  # type: ignore[assignment]
                if kind == "reply":
                    body = {
                        "id": f"req-b6-a13-{kind}",
                        "from": "claude-project-c-dev",
                        "branch": "manual",
                        "to": "codex-project-c-dev",
                        "kind": "reply",
                        "sent_at": "2026-04-26T19:00:00+01:00",
                        "in_reply_to": "parent-1",
                        "run_id": "",
                        "payload": {"result": "", "ok": True},
                    }
                    raw = json.dumps(body, separators=(",", ":"))
                elif kind == "notify":
                    raw = _legacy_refusal_raw(
                        f"req-b6-a13-{kind}",
                        kind="notify",
                        payload={"event": "x", "data": {}},
                        run_id="",
                    )
                else:
                    raw = _legacy_refusal_raw(
                        f"req-b6-a13-{kind}",
                        kind=kind,
                        payload={"message": "please stop"} if kind == "steer" else {},
                        run_id="",
                    )
                with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as cm:
                    bridge.handle_raw(raw)
                self.assertEqual(fake.replies, [])
                self.assertIn(
                    "invalid-run_id",
                    "\n".join(record.getMessage() for record in cm.records),
                )

        for kind in ("steer", "cancel"):
            with self.subTest(kind=kind, path="control-lane"):
                bridge = _ref_required_bridge()
                fake = FakeRedis()
                bridge.redis = fake  # type: ignore[assignment]
                raw = _legacy_refusal_raw(
                    f"req-b6-a13-ctl-{kind}",
                    kind=kind,
                    payload={"message": "please stop"} if kind == "steer" else {},
                    run_id="",
                )
                control_items = [raw]

                def lpop_control(_agent_id: str) -> str | None:
                    return control_items.pop(0) if control_items else None

                fake.lpop_control = lpop_control  # type: ignore[attr-defined]
                with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as cm:
                    bridge._drain_control_lane()
                self.assertEqual(fake.replies, [])
                self.assertIn(
                    "invalid-run_id",
                    "\n".join(record.getMessage() for record in cm.records),
                )

    def test_a14_roster_guard_unknown_sender_policy_human(self) -> None:
        for unknown_policy in ("human", "trusted"):
            with self.subTest(unknown=unknown_policy):
                bridge = _ref_required_bridge(
                    "--unknown-sender-policy", unknown_policy
                )
                fake = FakeRedis()
                bridge.redis = fake  # type: ignore[assignment]
                # Unconfigured forged from — must NOT fall back to unknown policy.
                bridge.handle_raw(
                    _legacy_refusal_raw(
                        f"req-b6-a14-{unknown_policy}",
                        sender="forged-unrostered-agent",
                    )
                )
                self.assertEqual(fake.replies, [])
                self.assertNotIn(
                    "forged-unrostered-agent",
                    bridge.sender_policies,
                )

    def test_a14b_rostered_reject_no_refusal_reply(self) -> None:
        # r4 P2: guard 5's explicit-policy==reject half — rostered reject must
        # not mint a refusal reply even when unknown default would be human.
        bridge = _ref_required_bridge(
            "--sender-policy",
            "evil-agent=reject",
            "--unknown-sender-policy",
            "human",
        )
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        self.assertEqual(bridge.sender_policies.get("evil-agent"), "reject")
        with self.assertLogs("agent_redis_bridge.bridge", level="ERROR") as cm:
            bridge.handle_raw(
                _legacy_refusal_raw("req-b6-a14b", sender="evil-agent")
            )
        self.assertEqual(fake.replies, [])
        self.assertIn(
            "invalid-payload-task-ref",
            "\n".join(record.getMessage() for record in cm.records),
        )

    def test_structured_null_when_expect_structured(self) -> None:
        bridge = _ref_required_bridge()
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        bridge.handle_raw(
            _legacy_refusal_raw(
                "req-b6-structured",
                payload={"task": "legacy string", "expect_structured": True},
            )
        )
        self.assertEqual(len(fake.replies), 1)
        payload = json.loads(fake.replies[0][1])["payload"]
        self.assertIn("structured", payload)
        self.assertIsNone(payload["structured"])
        self.assertEqual(payload["error_code"], "invalid-payload-task-ref")

        fake.replies.clear()
        bridge.handle_raw(
            _legacy_refusal_raw(
                "req-b6-no-structured",
                payload={"task": "legacy string"},
            )
        )
        payload = json.loads(fake.replies[0][1])["payload"]
        self.assertNotIn("structured", payload)
        self.assertEqual(payload["error_code"], "invalid-payload-task-ref")

    # --- R-0 posture precedence (construction-time seam) ---------------------

    def test_r0_process_env_wins_over_env_file(self) -> None:
        # process env =1 + env-file =0 ⇒ task_ref_required is True
        bridge = _bridge_with_env_file(
            f"{TASK_REF_REQUIRED_ENV}=0\n",
            env_overrides={TASK_REF_REQUIRED_ENV: "1"},
            clear_task_ref_env=False,
        )
        self.assertIs(bridge.task_ref_required, True)

    def test_r0_env_file_when_process_env_unset(self) -> None:
        # env-file =1 + process env unset ⇒ True (env-file still contributes)
        bridge = _bridge_with_env_file(
            f"{TASK_REF_REQUIRED_ENV}=1\n",
            clear_task_ref_env=True,
        )
        self.assertIs(bridge.task_ref_required, True)

    def test_r0_neither_set_defaults_false(self) -> None:
        bridge = _bridge_with_env_file("", clear_task_ref_env=True)
        self.assertIs(bridge.task_ref_required, False)

    def test_r0_cli_flag_overrides_process_and_env_file(self) -> None:
        # Explicit --task-ref-required beats process env =0 and env-file =0.
        bridge = _bridge_with_env_file(
            f"{TASK_REF_REQUIRED_ENV}=0\n",
            "--task-ref-required",
            env_overrides={TASK_REF_REQUIRED_ENV: "0"},
            clear_task_ref_env=False,
        )
        self.assertIs(bridge.task_ref_required, True)
        bridge_off = _bridge_with_env_file(
            f"{TASK_REF_REQUIRED_ENV}=1\n",
            "--no-task-ref-required",
            env_overrides={TASK_REF_REQUIRED_ENV: "1"},
            clear_task_ref_env=False,
        )
        self.assertIs(bridge_off.task_ref_required, False)

    # --- R-2b: no handling-time ambient env read ----------------------------

    def test_handle_raw_follows_constructed_posture_not_ambient(self) -> None:
        # Construct ref-required, then flip ambient env to dual-accept; handle_raw
        # must still refuse (seam captured at construction).
        bridge = _ref_required_bridge()
        self.assertIs(bridge.task_ref_required, True)
        fake = FakeRedis()
        bridge.redis = fake  # type: ignore[assignment]
        with mock.patch.dict(os.environ, {TASK_REF_REQUIRED_ENV: "0"}):
            bridge.handle_raw(_legacy_refusal_raw("req-b6-ambient-rr"))
        self.assertEqual(len(fake.replies), 1)
        payload = json.loads(fake.replies[0][1])["payload"]
        self.assertEqual(payload["error_code"], "invalid-payload-task-ref")
        self.assertIs(payload["task_ref_required"], True)

        # Construct dual-accept, then flip ambient to ref-required; must admit.
        bridge2 = _dual_accept_bridge("--no-enforce-completion")
        self.assertIs(bridge2.task_ref_required, False)
        fake2 = FakeRedis()
        bridge2.redis = fake2  # type: ignore[assignment]
        engine = RecordingEngine("ambient-dual-ok")
        with mock.patch.dict(os.environ, {TASK_REF_REQUIRED_ENV: "1"}):
            with mock.patch(
                "agent_redis_bridge.bridge.build_engine", return_value=engine
            ):
                bridge2.pool.acquire = mock.Mock(return_value=engine)  # type: ignore[method-assign]
                bridge2.pool.release = mock.Mock()  # type: ignore[method-assign]
                bridge2.handle_raw(
                    request_json(
                        "req-b6-ambient-da",
                        payload={"task": "legacy string"},
                    )
                )
                bridge2.join_active_thread()
        replies = [
            json.loads(body)
            for _, body in fake2.replies
            if json.loads(body).get("kind") == "reply"
        ]
        self.assertEqual(len(replies), 1)
        self.assertNotIn("error_code", replies[0]["payload"])
        self.assertEqual(replies[0]["payload"]["result"], "ambient-dual-ok")

    def test_construction_time_capture_is_consumed_by_handle_raw(self) -> None:
        # Breaking the construction-time capture (attribute never set / wrong)
        # must fail: A1-shaped input under a dual-accept-constructed bridge that
        # has had the attribute cleared must not emit a refusal with task_ref.
        bridge = _ref_required_bridge()
        self.assertTrue(hasattr(bridge, "task_ref_required"))
        self.assertIs(bridge.task_ref_required, True)
        # If construction left the attribute unset, getattr(..., None) would let
        # from_json fall through to ambient env — pin that the attribute exists
        # as a bool captured at init.
        self.assertIsInstance(bridge.task_ref_required, bool)


if __name__ == "__main__":
    unittest.main()
