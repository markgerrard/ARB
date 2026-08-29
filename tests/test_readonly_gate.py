"""Startup read-only gate — closes the BRIDGE_PI_TOOLS fail-open.

A pi seat declared read-only (ARB_REQUIRE_READONLY_TOOLS set) must refuse to
serve unless its effective tool surface is non-empty AND a subset of the
required allowlist. The named threat: drop BRIDGE_PI_TOOLS and pi-sdk silently
falls back to the FULL toolset (write/edit/bash). The gate makes that
fail-CLOSED (refuse to start) instead of fail-open (serve write-capable).

Shares surface-check semantics with tools/seat_deny_proof (the post-launch
deny-proof); this is the pre-serve counterpart.
"""

import unittest

from agent_redis_bridge.readonly_gate import (
    ReadonlyGateError,
    enforce_readonly_tool_surface,
)

ALLOWED = "read,grep,find,ls"

# omp is a pi FORK but NOT pi's tool vocabulary: it has no `find` and no `ls`
# (`omp --tools read,grep,find,ls` exits non-zero with "Unknown tool in --tools:
# ls"). Certifying an omp seat against pi's four therefore describes a seat that
# cannot start — the gate only checks subset membership, never startability, so
# the fixture went green while naming an impossible config. `glob` is omp's
# equivalent, and `read` handles directories. Panel finding, run
# panel-omp-opencode-arc-20260803T125825Z-570c21.
ALLOWED_OMP = "read,grep,glob"


class ReadonlyGateTests(unittest.TestCase):
    def _ok(self, pi_tools: str | None, engine: str = "pi-sdk", required: str = ALLOWED) -> None:
        # Should NOT raise.
        enforce_readonly_tool_surface(
            engine=engine, pi_tools=pi_tools, required_csv=required
        )

    def _refused(self, pi_tools: str | None, engine: str = "pi-sdk", required: str = ALLOWED) -> str:
        with self.assertRaises(ReadonlyGateError) as ctx:
            enforce_readonly_tool_surface(
                engine=engine, pi_tools=pi_tools, required_csv=required
            )
        return str(ctx.exception)

    # --- the fail-open the panel verified: var dropped entirely ---
    def test_none_pi_tools_refuses(self):
        msg = self._refused(None)
        self.assertIn("full", msg.lower())

    def test_empty_string_refuses(self):
        self._refused("")

    def test_degenerate_comma_refuses(self):
        # "," parses to no tools -> would silently fall back to full tools.
        self._refused(",")

    def test_whitespace_only_refuses(self):
        self._refused("   ")

    # --- exact / subset allowlists pass ---
    def test_exact_allowlist_passes(self):
        self._ok("read,grep,find,ls")

    def test_subset_passes(self):
        self._ok("read")

    def test_whitespace_tolerated(self):
        self._ok("read, grep , find,ls")

    # --- write-capable surfaces refuse ---
    def test_write_tool_refuses(self):
        msg = self._refused("read,write")
        self.assertIn("write", msg.lower())

    def test_bash_tool_refuses(self):
        self._refused("read,grep,find,ls,bash")

    def test_edit_tool_refuses(self):
        self._refused("read,edit")

    # --- engine without a pi-tools surface can't be certified this way ---
    def test_non_pi_engine_refuses(self):
        msg = self._refused("read,grep,find,ls", engine="codex")
        self.assertIn("codex", msg.lower())

    def test_pi_rpc_engine_supported(self):
        self._ok("read,grep,find,ls", engine="pi-rpc")

    # --- omp-acp: a pi FORK sharing the same --pi-tools allowlist surface,
    # but NOT pi's tool vocabulary (see ALLOWED_OMP) ---
    def test_omp_acp_engine_supported(self):
        self._ok(ALLOWED_OMP, engine="omp-acp", required=ALLOWED_OMP)

    def test_omp_acp_pi_vocabulary_is_not_omps_vocabulary(self):
        # Regression guard for the fixture defect this file used to carry: a
        # surface certified against pi's four names tools omp does not have, so
        # the "certified" seat could never actually start. Keep the gate and the
        # engine's real vocabulary in the same test file so they cannot drift.
        self.assertNotIn("find", ALLOWED_OMP)
        self.assertNotIn("ls", ALLOWED_OMP.split(","))

    def test_omp_acp_write_surface_refuses(self):
        msg = self._refused("read,write", engine="omp-acp", required=ALLOWED_OMP)
        self.assertIn("write", msg.lower())

    def test_omp_acp_unset_surface_names_its_own_full_toolset(self):
        # The fail-open is worse for omp than for pi: an empty surface falls
        # back to 29 built-ins including browser/computer/github. The refusal
        # must say so rather than reciting pi's four-tool default.
        msg = self._refused(None, engine="omp-acp")
        self.assertIn("full", msg.lower())
        self.assertIn("29", msg)
        self.assertIn("browser", msg.lower())

    def test_pi_engine_refusal_does_not_claim_omps_toolset(self):
        # Guards the inverse mistake: the per-engine note must stay per-engine.
        msg = self._refused(None, engine="pi-sdk")
        self.assertIn("read/bash/edit/write", msg)
        self.assertNotIn("browser", msg.lower())

    # --- opencode-acp is deliberately NOT certifiable here ---
    def test_opencode_acp_refuses_no_allowlist_surface(self):
        # opencode's read-only posture is MODE-based (ACP `plan`), not an
        # allowlist. This gate checks surfaces, so it must refuse rather than
        # appear to certify a posture it cannot see.
        msg = self._refused("read,grep,find,ls", engine="opencode-acp")
        self.assertIn("opencode-acp", msg)
        self.assertIn("no allowlist surface", msg.lower())


class ReadonlyDeclarationPublishedTest(unittest.TestCase):
    """ARB-B14a: the SELF-DECLARED posture must reach the registry.

    The declaration is seat-owned config, so the gate cannot be an authority —
    but publishing it makes silent de-declaration a centrally observable drift.
    This pins the field so dropping it is a red test, not an unnoticed loss.
    """

    def _register_with(self, required, monkey_readiness=False, tmp=None):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock

        from agent_redis_bridge.bridge import Bridge

        registered: dict = {}

        class FakeRedis:
            def register(self, **kwargs):
                registered.update(kwargs)

        tmpdir = Path(tempfile.mkdtemp(prefix="readonly-reg-"))
        bridge = object.__new__(Bridge)
        bridge.args = SimpleNamespace(dry_run=False, turn_timeout=30, heartbeat_ttl=60)
        bridge.engine_name = "codex"
        bridge.agent_id = "codex-test"
        bridge.tool = "codex"
        bridge.project = "bridge"
        bridge.workspace = "ws"
        bridge.branch = "dev"
        bridge.workdir = tmpdir
        bridge.registered_at = "2026-07-31T00:00:00+00:00"
        bridge.owner_token = "tok"
        bridge.brief_stage_root = tmpdir / "stage"
        bridge.brief_stage_root.mkdir()
        bridge.redis = FakeRedis()
        bridge.required_readonly_tools = required
        with mock.patch(
            "agent_redis_bridge.engines._stdio.prove_env_scrub_capability",
            lambda **k: "",
        ), mock.patch(
            "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
            lambda **k: False,
        ):
            Bridge.reassert_liveness(bridge)
        return registered

    def test_declared_allowlist_is_published_verbatim(self):
        registered = self._register_with("read,grep,find,ls")
        self.assertEqual(registered.get("readonly_tools"), "read,grep,find,ls")

    def test_undeclared_seat_publishes_empty(self):
        registered = self._register_with(None)
        self.assertEqual(registered.get("readonly_tools"), "")


if __name__ == "__main__":
    unittest.main()
