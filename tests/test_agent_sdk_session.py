import asyncio
import tempfile
import unittest
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.session_resume import materialize_resume_session
from claude_agent_sdk._internal.session_store_validation import _store_implements
from claude_agent_sdk._internal.sessions import project_key_for_directory
from claude_agent_sdk.types import SessionStore

from agent_redis_bridge.engines.agent_sdk_session import (
    FileSessionStore,
    ScrubbedSessionStore,
    scrub,
)


KEY = {"project_key": "proj", "session_id": "sess1"}
RESUME_SESSION_ID = "11111111-1111-4111-8111-111111111111"


class AppendLoadOnlyStore:
    async def append(self, key, entries):
        pass

    async def load(self, key):
        return None


class ProtocolDefaultStore(SessionStore):
    async def append(self, key, entries):
        pass

    async def load(self, key):
        return None


class SessionStoreTest(unittest.TestCase):
    def test_scrub_redacts_value_and_var(self):
        self.assertNotIn("sk-x", scrub("k=sk-x", ["sk-x"], []))
        self.assertNotIn("AGENT_SDK_GLM_KEY", scrub("$AGENT_SDK_GLM_KEY", [], ["AGENT_SDK_GLM_KEY"]))

    def test_scrub_redacts_lane_writer_dsn_and_var_name(self):
        dsn = "postgresql://lw-secret@db/arb_memory"
        text = f"connecting with {dsn} via ARB_GATE_LANE_WRITER_DSN"
        cleaned = scrub(
            text,
            [dsn],
            ["ARB_GATE_LANE_WRITER_DSN", "ARB_GATE_LANE_WRITER_ROLE"],
        )
        self.assertNotIn("lw-secret", cleaned)
        self.assertNotIn(dsn, cleaned)
        self.assertNotIn("ARB_GATE_LANE_WRITER_DSN", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_scrubbed_store_scrubs_on_append(self):
        seen = []

        class Fake:
            async def append(self, key, entries):
                seen.extend(entries)

            async def load(self, key):
                return None

        store = ScrubbedSessionStore(Fake(), secrets=["sk-leak"], var_names=[])
        asyncio.run(store.append(KEY, [{"text": "ran with sk-leak in args"}]))
        self.assertNotIn("sk-leak", str(seen[-1]))

    def test_scrubbed_store_does_not_advertise_missing_optional_methods(self):
        wrapped = ScrubbedSessionStore(AppendLoadOnlyStore(), secrets=[], var_names=[])

        self.assertFalse(_store_implements(wrapped, "list_subkeys"))
        self.assertFalse(_store_implements(wrapped, "list_sessions"))
        self.assertFalse(hasattr(wrapped, "list_subkeys"))

    def test_scrubbed_store_does_not_expose_protocol_default_optional_methods(self):
        wrapped = ScrubbedSessionStore(ProtocolDefaultStore(), secrets=[], var_names=[])

        self.assertFalse(_store_implements(wrapped, "list_subkeys"))
        self.assertFalse(hasattr(wrapped, "list_subkeys"))

    def test_file_store_roundtrip_namespaced(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                store = FileSessionStore(Path(temp_dir), "agent-sdk-x-dev-m3")
                await store.append(KEY, [{"a": 1}])
                self.assertEqual(await store.load(KEY), [{"a": 1}])
                self.assertTrue((Path(temp_dir) / "agent-sdk-x-dev-m3").is_dir())

        asyncio.run(scenario())

    def test_file_store_supports_subpath(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                store = FileSessionStore(Path(temp_dir), "agent")
                key = {"project_key": "proj", "session_id": "sid", "subpath": "subagents/agent-1"}
                await store.append(key, [{"a": 1}])
                self.assertEqual(await store.load(key), [{"a": 1}])

        asyncio.run(scenario())

    def test_file_store_lists_sessions_subkeys_and_deletes(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                store = FileSessionStore(Path(temp_dir), "agent")
                main = {"project_key": "proj", "session_id": "sid"}
                first_sub = {**main, "subpath": "subagents/agent-1"}
                second_sub = {**main, "subpath": "subagents/nested/agent-2"}

                self.assertEqual(await store.list_subkeys(main), [])
                await store.append(main, [{"type": "main"}])
                await store.append(first_sub, [{"type": "sub-1"}])
                await store.append(second_sub, [{"type": "sub-2"}])

                self.assertCountEqual(
                    await store.list_subkeys(main),
                    ["subagents/agent-1", "subagents/nested/agent-2"],
                )
                sessions = await store.list_sessions("proj")
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0]["session_id"], "sid")
                self.assertIsInstance(sessions[0]["mtime"], int)
                self.assertGreater(sessions[0]["mtime"], 0)

                await store.delete(first_sub)
                self.assertIsNone(await store.load(first_sub))
                self.assertEqual(await store.load(second_sub), [{"type": "sub-2"}])
                self.assertEqual(await store.load(main), [{"type": "main"}])

                await store.delete(first_sub)
                await store.delete(main)
                self.assertIsNone(await store.load(main))
                self.assertIsNone(await store.load(second_sub))
                self.assertEqual(await store.list_subkeys(main), [])
                self.assertEqual(await store.list_sessions("proj"), [])

        asyncio.run(scenario())

    def test_real_store_materializes_sdk_resume_with_subkeys(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "sessions"
                cwd = Path(temp_dir) / "work"
                cwd.mkdir()
                project_key = project_key_for_directory(cwd)
                inner = FileSessionStore(root, "agent")
                store = ScrubbedSessionStore(inner, secrets=["secret-token"], var_names=[])
                main = {"project_key": project_key, "session_id": RESUME_SESSION_ID}
                sub = {**main, "subpath": "subagents/agent-1"}

                self.assertTrue(_store_implements(store, "list_subkeys"))
                self.assertTrue(_store_implements(store, "list_sessions"))
                await store.append(main, [{"type": "user", "message": {"content": "hi secret-token"}}])
                await store.append(sub, [{"type": "assistant", "message": {"content": "sub"}}])

                materialized = await materialize_resume_session(
                    ClaudeAgentOptions(
                        cwd=cwd,
                        session_store=store,
                        resume=RESUME_SESSION_ID,
                        env={"ANTHROPIC_API_KEY": "test-key"},
                    )
                )
                self.assertIsNotNone(materialized)
                assert materialized is not None
                try:
                    project_dir = materialized.config_dir / "projects" / project_key
                    self.assertTrue((project_dir / f"{RESUME_SESSION_ID}.jsonl").is_file())
                    self.assertTrue(
                        (project_dir / RESUME_SESSION_ID / "subagents" / "agent-1.jsonl").is_file()
                    )
                finally:
                    await materialized.cleanup()

        asyncio.run(scenario())
