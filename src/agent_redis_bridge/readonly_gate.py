"""Startup read-only gate for seats with explicit tool allowlists.

A judgment-oracle / reviewer seat is read-only by *tool absence* — its pi tool
surface is an allowlist (``--pi-tools read,grep,find,ls``) with no write/edit/
bash tool. But that posture has a verified fail-open: if ``BRIDGE_PI_TOOLS`` is
unset (or parses empty), pi falls back to its DEFAULT full toolset
(read/bash/edit/write). A seat meant to be read-only would then serve
write-capable, silently.

This gate closes that hole at the root and *fail-closed*: when a seat declares
itself read-only (``ARB_REQUIRE_READONLY_TOOLS=<csv>``), the bridge refuses to
register/serve unless the effective pi tool surface is non-empty AND a subset of
the declared allowlist. A misconfigured seat crashes at startup (visible, e.g.
launchd KeepAlive crash-loop) instead of quietly accepting writes.

Surface-check semantics intentionally mirror ``tools/seat_deny_proof`` (the
post-launch, behaviour-level deny-proof). This is the pre-serve counterpart:
the deny-proof proves read-only by *attempting writes against a live seat*; this
gate refuses to *start* a seat whose declared surface could not pass it. Only
engines exposing a tool ALLOWLIST surface can be certified this way — the
pi-family ``--pi-tools`` engines (pi-sdk, pi-rpc, and the oh-my-pi fork
``omp-acp``) plus ``agent-sdk``.

NOT every read-only-capable engine qualifies. ``opencode-acp`` is read-only via
an ACP session MODE (``plan``) rather than a tool allowlist, and a mode is not a
surface this module can check — so it is deliberately excluded and its seats
refuse the gate. Mode-based posture is a case for the ``seat_posture_v``
migration named below, not for widening this module.

HONEST LIMIT (ARB-B14a; the flaw ``claim_gate.py`` and bus-side gate spec §9.3
name). The DECLARATION this gate enforces is seat-owned config
(``ARB_REQUIRE_READONLY_TOOLS`` in the seat's own env file / plist): a seat can
stop being "read-only" by deleting one line it owns, and this gate cannot tell
"never declared" from "quietly de-declared". What it defends is therefore
MISCONFIGURATION of a declared-read-only seat (the BRIDGE_PI_TOOLS fail-open),
not sabotage of the declaration itself. Two mitigations exist, neither making
this gate an authority: the declaration is published to the seat registry at
registration (``readonly_tools`` field) so drift is centrally visible, and the
CLASS fix — posture as a fact the store returns (``seat_posture_v``) — is the
claim gate's design, to which any future posture question should be migrated
rather than extending this module.
"""

from __future__ import annotations

# Engines whose read-only surface is a `--pi-tools` / BRIDGE_PI_TOOLS allowlist.
# omp-acp is oh-my-pi, a pi FORK, and reuses the same allowlist surface on its
# ACP transport — verified behaviourally 2026-08-02 with a control/limited pair:
# the control run wrote a file, the `--tools read,grep` run could not and
# reported only read/grep available.
_PI_ENGINES = ("pi-sdk", "pi-rpc", "omp-acp")
_ALLOWLIST_ENGINES = (*_PI_ENGINES, "agent-sdk")

# What each pi-family engine falls back to when the allowlist is empty. Named in
# the refusal so an operator sees what an unset surface would actually have
# served — omp's default set is far wider than pi's, and "full toolset" alone
# understates it.
_FULL_SURFACE = {
    "pi-sdk": "read/bash/edit/write",
    "pi-rpc": "read/bash/edit/write",
    "omp-acp": (
        "29 built-ins including bash/write/edit and the host- and "
        "network-reaching browser/computer/github/web_search"
    ),
}


class ReadonlyGateError(Exception):
    """Raised when a read-only-declared seat's tool surface is not certifiably
    read-only. Propagates to ``main()`` which prints ``[bridge-error]`` and exits
    non-zero — the seat refuses to serve."""


def _parse_tools(csv: str | None) -> set[str]:
    return {t.strip() for t in (csv or "").split(",") if t.strip()}


def enforce_readonly_tool_surface(
    *,
    engine: str,
    pi_tools: str | None,
    required_csv: str,
    agent_sdk_tools: str | None = None,
) -> None:
    """Refuse to serve unless the engine tool surface is a non-empty read-only subset.

    Raises :class:`ReadonlyGateError` (fail-closed) on:
      - an engine with no allowlist surface to certify;
      - an empty/unset/degenerate surface (the fail-open: pi would fall back to
        the FULL toolset; agent-sdk would have no auditable ceiling);
      - any tool outside the required allowlist (write/edit/bash, etc.).
    """
    required = _parse_tools(required_csv)

    if engine not in _ALLOWLIST_ENGINES:
        raise ReadonlyGateError(
            f"ARB_REQUIRE_READONLY_TOOLS is set but engine {engine!r} has no "
            "allowlist surface to certify; only "
            f"{'/'.join(_ALLOWLIST_ENGINES)} seats can be gated this way. "
            "Refusing to serve."
        )

    surface_value = agent_sdk_tools if engine == "agent-sdk" else pi_tools
    surface_name = "BRIDGE_AGENT_SDK_TOOLS" if engine == "agent-sdk" else "BRIDGE_PI_TOOLS"
    flag_name = "--agent-sdk-tools" if engine == "agent-sdk" else "--pi-tools"
    surface = _parse_tools(surface_value)
    if not surface:
        if engine in _PI_ENGINES:
            suffix = (
                f" An empty surface means {engine} falls back to its FULL "
                f"toolset ({_FULL_SURFACE[engine]})."
            )
        else:
            suffix = ""
        raise ReadonlyGateError(
            f"read-only seat requires an explicit non-empty {flag_name} / "
            f"{surface_name} (expected a subset of {sorted(required)}); got "
            f"{surface_value!r}.{suffix} Refusing to serve."
        )

    offending = surface - required
    if offending:
        raise ReadonlyGateError(
            f"read-only seat tool surface {sorted(surface)} exceeds the required "
            f"allowlist {sorted(required)}; offending tools: {sorted(offending)}. "
            "Refusing to serve."
        )
