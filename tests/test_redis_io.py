from pathlib import Path
import tempfile
import unittest

from agent_redis_bridge.redis_io import RedisCli, RedisConfig, read_env_file


class FakeRedisClient:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    def eval(self, script: str, numkeys: int, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if numkeys == 3:
            # cleanup: atomic compare-and-delete of status/consumer/registry
            if self.strings.get(keys[0]) == argv[0]:
                self.strings.pop(keys[0], None)
            if self.strings.get(keys[1]) == argv[1]:
                self.strings.pop(keys[1], None)
            if self.hashes.get(keys[2], {}).get("owner_token") == argv[2]:
                self.hashes.pop(keys[2], None)
            return 1
        key = keys[0]
        current = self.strings.get(key)
        owner = argv[0]
        if "if not current" in script:
            if current is None:
                self.strings[key] = owner
                return 1
            if current == owner:
                return 1
            return current
        if numkeys == 2:
            if current == owner:
                self.strings[keys[1]] = argv[1]
                return 1
            return current or ""
        if current == owner:
            return 1
        return current or ""

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    def ttl(self, key: str) -> int:
        return 60 if key in self.strings else -2

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.strings.pop(key, None)
            self.hashes.pop(key, None)


class RedisIoTest(unittest.TestCase):
    def test_register_refuses_live_duplicate_agent_id(self) -> None:
        cli, client, config = make_cli()
        client.strings[config.status_key("codex-project-c-dev")] = "alive:owner-b"

        with self.assertRaisesRegex(RuntimeError, "already owned.*owner-b"):
            cli.register(
                agent_id="codex-project-c-dev", tool="codex", project="project-c", workspace="dev",
                branch="dev", path="/tmp/repo", registered_at="now", pid=123,
                owner_token="owner-a", ttl=60,
            )

        self.assertNotIn(config.registry_key("codex-project-c-dev"), client.hashes)

    def test_register_same_pid_reasserts_registry(self) -> None:
        cli, client, config = make_cli()
        client.strings[config.status_key("codex-project-c-dev")] = "alive:owner-a"

        cli.register(
            agent_id="codex-project-c-dev", tool="codex", project="project-c", workspace="dev",
            branch="dev", path="/tmp/repo", registered_at="now", pid=123,
            owner_token="owner-a", ttl=60,
        )

        self.assertEqual(client.hashes[config.registry_key("codex-project-c-dev")]["pid"], "123")

    def test_same_pid_with_different_boot_token_is_refused(self) -> None:
        cli, client, config = make_cli()
        client.strings[config.status_key("codex-project-c-dev")] = "alive:owner-a"

        with self.assertRaisesRegex(RuntimeError, "already owned"):
            cli.register(
                agent_id="codex-project-c-dev", tool="codex", project="project-c", workspace="dev",
                branch="dev", path="/other-host/repo", registered_at="later", pid=123,
                owner_token="owner-b", ttl=60,
            )

        self.assertNotIn(config.registry_key("codex-project-c-dev"), client.hashes)

    def test_prefix_keeps_single_colon(self) -> None:
        config = RedisConfig(host="127.0.0.1", port="6390", db="12", prefix="agent_scratch:")

        self.assertEqual(config.registry_key("codex-project-c-dev"), "agent_scratch:registry:codex-project-c-dev")
        self.assertEqual(config.status_key("codex-project-c-dev"), "agent_scratch:agent:codex-project-c-dev:status")
        self.assertEqual(config.consumer_key("codex-project-c-dev"), "agent_scratch:agent:codex-project-c-dev:consumer")
        self.assertEqual(config.inbox_key("codex-project-c-dev"), "agent_scratch:agent:codex-project-c-dev:inbox")
        self.assertEqual(config.task_events_key("task-1"), "agent_scratch:task:task-1:events")
        self.assertEqual(config.task_status_key("task-1"), "agent_scratch:task:task-1:status")
        self.assertEqual(config.task_result_key("task-1"), "agent_scratch:task:task-1:result")
        self.assertEqual(
            config.announcement_key("codex-project-c-dev", "ann-1"),
            "agent_scratch:announcement:codex-project-c-dev:ann-1",
        )

    def test_read_env_file_strips_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text('AGENT_REDIS_HOST="127.0.0.1"\nAGENT_REDIS_PREFIX=agent_scratch:\n')

            values = read_env_file(env_file)

        self.assertEqual(values["AGENT_REDIS_HOST"], "127.0.0.1")
        self.assertEqual(values["AGENT_REDIS_PREFIX"], "agent_scratch:")

    def test_consumer_heartbeat_is_separate_from_process_liveness(self) -> None:
        cli, client, config = make_cli()

        client.strings[config.status_key("codex-project-c-dev")] = "alive:owner-a"
        cli.consumer_heartbeat("codex-project-c-dev", "owner-a", 60)

        self.assertEqual(client.strings[config.consumer_key("codex-project-c-dev")], "consuming:owner-a")

    def test_consumer_heartbeat_refuses_lost_owner(self) -> None:
        cli, client, config = make_cli()
        client.strings[config.status_key("codex-project-c-dev")] = "alive:owner-b"

        with self.assertRaisesRegex(RuntimeError, "ownership lost"):
            cli.consumer_heartbeat("codex-project-c-dev", "owner-a", 60)

        self.assertNotIn(config.consumer_key("codex-project-c-dev"), client.strings)

    def test_cleanup_with_matching_pid_deletes_status_and_registry(self) -> None:
        cli, client, config = make_cli()
        client.strings[config.status_key("codex-project-c-dev")] = "alive:owner-a"
        client.strings[config.consumer_key("codex-project-c-dev")] = "consuming:owner-a"
        client.hashes[config.registry_key("codex-project-c-dev")] = {
            "pid": "123", "owner_token": "owner-a", "tool": "codex",
        }

        cli.cleanup("codex-project-c-dev", "owner-a")

        self.assertNotIn(config.status_key("codex-project-c-dev"), client.strings)
        self.assertNotIn(config.registry_key("codex-project-c-dev"), client.hashes)

    def test_cleanup_with_mismatched_pid_deletes_neither_key(self) -> None:
        cli, client, config = make_cli()
        client.strings[config.status_key("codex-project-c-dev")] = "alive:owner-b"
        client.hashes[config.registry_key("codex-project-c-dev")] = {
            "pid": "456", "owner_token": "owner-b", "tool": "codex",
        }

        cli.cleanup("codex-project-c-dev", "owner-a")

        self.assertEqual(client.strings[config.status_key("codex-project-c-dev")], "alive:owner-b")
        self.assertEqual(client.hashes[config.registry_key("codex-project-c-dev")]["pid"], "456")

    def test_cleanup_with_missing_registry_preserves_successor_status(self) -> None:
        cli, client, config = make_cli()
        client.strings[config.status_key("codex-project-c-dev")] = "alive:owner-b"

        cli.cleanup("codex-project-c-dev", "owner-a")

        self.assertEqual(client.strings[config.status_key("codex-project-c-dev")], "alive:owner-b")
        self.assertNotIn(config.registry_key("codex-project-c-dev"), client.hashes)

    def test_register_writes_wire_vantage_and_generation(self) -> None:
        """Slice 1d-iv: registry freeze fields for the dispatch authority."""
        cli, client, config = make_cli()

        cli.register(
            agent_id="codex-project-c-dev",
            tool="codex",
            project="project-c",
            workspace="dev",
            branch="dev",
            path="/tmp/repo",
            registered_at="now",
            pid=123,
            owner_token="owner-a",
            ttl=60,
            worker_vantage="mac-host-dev",
            task_wire="legacy-or-ref-v1",
            brief_hydrate="",
            registration_generation="gen-42",
        )

        fields = client.hashes[config.registry_key("codex-project-c-dev")]
        self.assertEqual(fields["worker_vantage"], "mac-host-dev")
        self.assertEqual(fields["task_wire"], "legacy-or-ref-v1")
        self.assertEqual(fields["brief_hydrate"], "")
        self.assertEqual(fields["registration_generation"], "gen-42")
        self.assertEqual(fields["owner_token"], "owner-a")

    def test_register_defaults_generation_to_owner_token(self) -> None:
        cli, client, config = make_cli()

        cli.register(
            agent_id="codex-project-c-dev",
            tool="codex",
            project="project-c",
            workspace="dev",
            branch="dev",
            path="/tmp/repo",
            registered_at="now",
            pid=123,
            owner_token="owner-a",
            ttl=60,
            worker_vantage="mac-host-dev",
        )

        fields = client.hashes[config.registry_key("codex-project-c-dev")]
        self.assertEqual(fields["registration_generation"], "owner-a")
        self.assertEqual(fields["worker_vantage"], "mac-host-dev")
        # Absent hydration advertisement is the Stage 1d-iv default.
        self.assertEqual(fields.get("brief_hydrate", ""), "")


    def test_register_stores_brief_hydrate_v1_when_provided(self) -> None:
        """Registry stores the executed-readiness advertisement when the seat sets it."""
        cli, client, config = make_cli()
        cli.register(
            agent_id="codex-project-c-dev",
            tool="codex",
            project="project-c",
            workspace="dev",
            branch="dev",
            path="/tmp/repo",
            registered_at="now",
            pid=123,
            owner_token="owner-a",
            ttl=60,
            worker_vantage="mac-host-dev",
            task_wire="legacy-or-ref-v1",
            brief_hydrate="v1",
            registration_generation="gen-42",
        )
        fields = client.hashes[config.registry_key("codex-project-c-dev")]
        self.assertEqual(fields["brief_hydrate"], "v1")
        self.assertEqual(fields["task_wire"], "legacy-or-ref-v1")


def make_cli() -> tuple[RedisCli, FakeRedisClient, RedisConfig]:
    config = RedisConfig(host="127.0.0.1", port="6390", db="12", prefix="agent_scratch:")
    cli = RedisCli(config)
    client = FakeRedisClient()
    cli.client = client  # type: ignore[assignment]
    return cli, client, config


if __name__ == "__main__":
    unittest.main()
