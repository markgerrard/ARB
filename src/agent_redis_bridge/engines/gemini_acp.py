"""DEPRECATED (2026-07-03): the gemini-acp engine is non-functional.

Google deprecated the `gemini` CLI and it stopped working, so this engine — which drives
`gemini --acp` as a subprocess — can no longer run a real turn. It is retained ONLY because its
unit tests exercise the ACP protocol handshake logic (against a fake subprocess), not the dead CLI.
Do NOT stand up a gemini-acp seat: it is removed from the launcher's known-engines list and the
`agent-dispatch` / `agent-bridge-ping` operator entry points reject it with a deprecation error.
See CHANGELOG and memory `gemini-cli-deprecated`.

Shim since 2026-08-29. The ACP client this class used to define is now
:class:`agent_redis_bridge.engines.generic_acp.GenericAcpEngine`, because it is
the live base of omp-acp, opencode-acp, kimi-code-acp, mini-agent-acp and
dsh-acp — a deprecated adapter's name on a shared base is how an omp seat ended
up reporting itself as "Gemini". Everything left here is gemini's own: the
`gemini --acp` command line and the engine's identity strings.

This module keeps exporting ``GeminiAcpEngine`` and ``normalize_session_update``
at their old paths, so `bridge.py`, `engines/__init__.py` and every existing
import continue to work unchanged. The deprecation itself is unchanged and is
enforced elsewhere — `engines/support_tiers.py` lists `gemini-acp` as RETIRED
(so `bridge.build_engine` refuses it) and `scripts/agent-dispatch` refuses it by
name.
"""

from __future__ import annotations

from .generic_acp import GenericAcpEngine, normalize_session_update

__all__ = ["GeminiAcpEngine", "normalize_session_update"]


class GeminiAcpEngine(GenericAcpEngine):
    """`gemini --acp` over stdio. Deprecated; see the module docstring.

    A pure identity + command-line specialisation of
    :class:`~agent_redis_bridge.engines.generic_acp.GenericAcpEngine`: it must
    not override any transport or turn-loop method, and
    `tests/test_generic_acp_shim.py` asserts exactly that.
    """

    engine_label = "gemini"
    display_name = "Gemini"
    default_command = "gemini"

    def command_args(self) -> list[str]:
        return [self.command, "--acp"]
