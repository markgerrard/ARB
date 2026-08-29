import unittest
from pathlib import Path

from agent_redis_bridge.engines.agent_sdk_models import MODELS, resolve, isolated_env, subscription_env


class ModelsTest(unittest.TestCase):
    def test_model_registry_uses_short_slugs(self):
        self.assertEqual(
            set(MODELS),
            {"minimax-m3", "kimi", "glm-5.2", "opus-4.8", "opus-5", "fable-5", "sonnet-5", "haiku-4.5"},
        )
        for spec in MODELS.values():
            self.assertRegex(spec.slug, r"^[a-z0-9-]{1,16}$")

    def test_model_sets_are_explicitly_split_by_auth_lane(self):
        vendor = {name for name, spec in MODELS.items() if not spec.subscription}
        subscription = {name for name, spec in MODELS.items() if spec.subscription}

        self.assertEqual(vendor, {"minimax-m3", "kimi", "glm-5.2"})
        self.assertEqual(subscription, {"opus-4.8", "opus-5", "fable-5", "sonnet-5", "haiku-4.5"})

    def test_subscription_models_use_claude_code_account_auth(self):
        self.assertTrue(resolve("opus-4.8").subscription)
        self.assertTrue(resolve("sonnet-5").subscription)
        self.assertTrue(resolve("haiku-4.5").subscription)
        self.assertFalse(resolve("minimax-m3").subscription)
        self.assertEqual(resolve("opus-4.8").key_env, "CLAUDE_CODE_OAUTH_TOKEN")
        self.assertTrue(resolve("opus-4.8").reviewer)
        # Opus 5 is a certifying subscription seat on the same auth lane as 4.8.
        self.assertTrue(resolve("opus-5").subscription)
        self.assertTrue(resolve("opus-5").reviewer)
        self.assertEqual(resolve("opus-5").key_env, "CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(resolve("opus-5").model_id, "claude-opus-5")

    def test_fable_is_a_subscription_implementor_not_a_certifier(self):
        fable = resolve("fable-5")

        self.assertEqual(fable.slug, "fable5")
        self.assertEqual(fable.model_id, "claude-fable-5")
        self.assertEqual(fable.key_env, "CLAUDE_CODE_OAUTH_TOKEN")
        self.assertTrue(fable.subscription)
        self.assertFalse(fable.reviewer)

    def test_glm_uses_auth_token_and_lane_env(self):
        glm = resolve("glm-5.2")
        self.assertEqual(glm.auth_style, "auth-token")
        self.assertIn("ANTHROPIC_DEFAULT_SONNET_MODEL", glm.lane_env)

    def test_isolated_env_sets_selected_and_neutralizes_sdk_merge_leaks(self):
        spec = resolve("minimax-m3")
        polluted = {
            "PATH": "/x",
            "ANTHROPIC_AUTH_TOKEN": "leak",
            "ANTHROPIC_CUSTOM_X": "custom-leak",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "wrong",
            "AGENT_SDK_OTHER": "other-leak",
            "AGENT_SDK_KIMI_KEY": "leak2",
            "ANTHROPIC_BASE_URL": "old",
        }
        config_dir = Path("/tmp/agent-sdk-test-config")
        overlay = isolated_env(spec, "K123", base=polluted, config_dir=config_dir)
        child_env = {**polluted, **overlay}

        self.assertEqual(child_env["ANTHROPIC_BASE_URL"], "https://api.minimax.io/anthropic")
        self.assertEqual(child_env["ANTHROPIC_API_KEY"], "K123")
        self.assertEqual(child_env["CLAUDE_CONFIG_DIR"], str(config_dir))
        self.assertEqual(child_env["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(overlay["ANTHROPIC_CUSTOM_X"], "")
        self.assertEqual(child_env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "")
        self.assertEqual(overlay["AGENT_SDK_OTHER"], "")
        self.assertEqual(child_env["AGENT_SDK_KIMI_KEY"], "")
        self.assertEqual(child_env["PATH"], "/x")

    def test_subscription_env_zeroes_shadow_keys_by_prefix_and_preserves_oauth_config(self):
        polluted = {
            "PATH": "/x",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
            "CLAUDE_CONFIG_DIR": "/tmp/current-claude",
            "ANTHROPIC_API_KEY": "vendor-key",
            "ANTHROPIC_AUTH_TOKEN": "vendor-token",
            "ANTHROPIC_SHADOW_NEW": "new-leak",
            "AGENT_SDK_MINIMAX_KEY": "minimax-leak",
            "AGENT_SDK_UNLISTED": "future-leak",
        }

        overlay = subscription_env(base=polluted)
        child_env = {**polluted, **overlay}

        self.assertEqual(child_env["CLAUDE_CODE_OAUTH_TOKEN"], "oauth-token")
        self.assertEqual(child_env["CLAUDE_CONFIG_DIR"], "/tmp/current-claude")
        self.assertEqual(child_env["PATH"], "/x")
        self.assertEqual(child_env["ANTHROPIC_API_KEY"], "")
        self.assertEqual(child_env["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(child_env["ANTHROPIC_SHADOW_NEW"], "")
        self.assertEqual(child_env["AGENT_SDK_MINIMAX_KEY"], "")
        self.assertEqual(child_env["AGENT_SDK_UNLISTED"], "")

    def test_subscription_env_can_override_config_dir_per_seat(self):
        overlay = subscription_env(
            base={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token", "CLAUDE_CONFIG_DIR": "/tmp/shared"},
            config_dir=Path("/tmp/seat-specific"),
        )

        self.assertEqual(overlay["CLAUDE_CONFIG_DIR"], "/tmp/seat-specific")


class BusCredentialOverlayTest(unittest.TestCase):
    """The SDK merges the daemon's os.environ under options.env, so bus
    credentials must be explicitly overwritten to "" in the overlay — the
    agent-sdk equivalent of scrubbed_child_env for Popen engines."""

    # Includes gate-reader keys: the SDK merges the daemon's os.environ *under*
    # options.env, so only explicit blanking removes an inherited DSN. Omission
    # here is the arm that actually leaks (P1-A, rem2).
    BUS_POLLUTION = {
        "AGENT_REDIS_HOST": "bus.example",
        "AGENT_REDIS_PASSWORD": "hunter2",
        "REDISCLI_AUTH": "hunter2",
        "ARB_MEMORY_REDIS_URL": "rediss://:secret@bus:6379/9",
        "ARB_BRIDGE_BUS_URL": "rediss://:secret@bus:6379/7",
        "ARB_GATE_READER_DSN": "postgresql://reader@db/arb_memory",
        "ARB_GATE_READER_ROLE": "arb_gate_reader",
        "ARB_GATE_LANE_WRITER_DSN": "postgresql://lw@db/arb_memory",
        "ARB_GATE_LANE_WRITER_ROLE": "arb_gate_lw_seat_a",
        "ARB_GATE_LANE_WRITER_CONSUMER_ID": "consumer-a",
        "ARB_GATE_LANE_WRITER_LANE": "gated",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://local@db/arb_memory",
    }

    def test_isolated_env_neutralizes_bus_credentials(self):
        spec = resolve("minimax-m3")
        polluted = {"PATH": "/x", **self.BUS_POLLUTION}
        overlay = isolated_env(spec, "K123", base=polluted, config_dir=Path("/tmp/cfg"))
        child_env = {**polluted, **overlay}

        for name, value in self.BUS_POLLUTION.items():
            if name == "ARB_MEMORY_LOCAL_DSN":
                # Hydration intentionally remains available to the worker.
                self.assertEqual(child_env[name], value, name)
            else:
                self.assertEqual(child_env[name], "", name)
        self.assertEqual(child_env["PATH"], "/x")
        self.assertEqual(child_env["ANTHROPIC_API_KEY"], "K123")

    def test_subscription_env_neutralizes_bus_credentials_and_keeps_oauth(self):
        polluted = {
            "PATH": "/x",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
            **self.BUS_POLLUTION,
        }
        overlay = subscription_env(base=polluted)
        child_env = {**polluted, **overlay}

        for name, value in self.BUS_POLLUTION.items():
            if name == "ARB_MEMORY_LOCAL_DSN":
                self.assertEqual(child_env[name], value, name)
            else:
                self.assertEqual(child_env[name], "", name)
        self.assertEqual(child_env["CLAUDE_CODE_OAUTH_TOKEN"], "oauth-token")
        self.assertEqual(child_env["PATH"], "/x")

    def test_keyed_and_subscription_overlays_in_real_subprocesses(self):
        """Stage 1d-i: real Python subprocesses for both SDK overlay families."""
        import json
        import subprocess
        import sys

        probe = (
            "import json,os;"
            "print(json.dumps({"
            "'local':bool(os.environ.get('ARB_MEMORY_LOCAL_DSN')),"
            "'lane':bool(os.environ.get('ARB_GATE_LANE_WRITER_DSN')),"
            "'publish':bool(os.environ.get('ARB_MEMORY_REDIS_URL')),"
            "}))"
        )
        for builder in ("isolated", "subscription"):
            polluted = {"PATH": "/x", **self.BUS_POLLUTION}
            if builder == "isolated":
                overlay = isolated_env(
                    resolve("minimax-m3"),
                    "K123",
                    base=polluted,
                    config_dir=Path("/tmp/cfg"),
                )
            else:
                polluted["CLAUDE_CODE_OAUTH_TOKEN"] = "oauth-token"
                overlay = subscription_env(base=polluted)
            final = {**polluted, **overlay}
            # Final-env assertion (spawn gate) must pass.
            from agent_redis_bridge.engines.agent_sdk import assert_no_live_bus_credentials

            assert_no_live_bus_credentials(final)
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                env=final,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(data["local"], builder)
            self.assertFalse(data["lane"], builder)
            self.assertFalse(data["publish"], builder)
