"""Engine adapter support tiers (ARB-B14b).

Twenty-odd adapters track third-party CLIs on other people's release cadences;
without a maintained tier table a dead adapter looks exactly like a live one.
This table is asserted against both the engine registry (``ENGINE_TO_TOOL``)
and the adapter directory by ``tests/test_engine_support_tiers.py``, so adding
an adapter without classifying it — or retiring a CLI without recording it —
is a red test, not a memory exercise.

Tiers (the ARB-B14 filing's vocabulary):

- ``certifying`` — the engine backs seats eligible for certify quorums per the
  routing docs (`docs/agent-role-routing.md`, `skills/using-agent-bridge` panel
  section): codex (the anchor certifier), agy-print (Gemini-family quorum
  seat), pi-sdk (pi-GLM, promoted 2026-07-10). This documents the CURRENT
  routing-doc calibration — per-round panel composition remains owner-set and
  is not delegated to this table.
- ``experimental`` — live and dispatchable, but non-certifying: adjunct
  reviewer engines whose severity labels are advisory (grok-acp, devin-acp,
  kimi-code-acp, mini-agent-acp, pi-rpc), explicitly-experimental implementors
  (cursor-acp), newly-landed adapters with no deny-proof behind them yet
  (omp-acp, opencode-acp, added 2026-08-02; dsh-acp, added 2026-08-17 with one
  live turn behind it and no deny-proof), and harness variants (agy-tmux,
  agent-sdk, openinterpreter).
- ``retired`` — the wrapped CLI is dead; ``build_engine`` refuses to construct
  the adapter (the operator entry points already reject it by name). gemini-acp
  since 2026-07-03 (Google retired the CLI; memory ``gemini-cli-deprecated``).

Keys are canonical engine names (post ``normalize_engine_name``); pure aliases
(``asdk``) are not listed.
"""

from __future__ import annotations

CERTIFYING = "certifying"
EXPERIMENTAL = "experimental"
RETIRED = "retired"

VALID_TIERS = frozenset({CERTIFYING, EXPERIMENTAL, RETIRED})

SUPPORT_TIERS: dict[str, str] = {
    "codex": CERTIFYING,
    "agy-print": CERTIFYING,
    "pi-sdk": CERTIFYING,
    "cline-acp": EXPERIMENTAL,
    "cursor-acp": EXPERIMENTAL,
    "devin-acp": EXPERIMENTAL,
    "dsh-acp": EXPERIMENTAL,
    "grok-acp": EXPERIMENTAL,
    "kimi-code-acp": EXPERIMENTAL,
    "mini-agent-acp": EXPERIMENTAL,
    "omp-acp": EXPERIMENTAL,
    "opencode-acp": EXPERIMENTAL,
    "pi-rpc": EXPERIMENTAL,
    "agy-tmux": EXPERIMENTAL,
    "agent-sdk": EXPERIMENTAL,
    "openinterpreter": EXPERIMENTAL,
    "gemini-acp": RETIRED,
}


def tier_for(engine: str) -> str | None:
    """Tier for a canonical engine name; None for unknown engines."""
    return SUPPORT_TIERS.get(engine)
