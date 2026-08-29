"""dsh ACP engine — DeepSeek Harness's `dsh-acp-demo` runtime over stdio.

DeepSeek Harness (`dsh`, github.com/deepseek-ai/deepseek-harness) is a
plugin-composed agent harness. Its ACP transport is built on the official
`@agentclientprotocol/sdk` (0.25.1, `packages/acp/acp/package.json`), so it lands
on the same `GenericAcpEngine` base as kimi/opencode rather than needing a new
transport. dsh also ships a bespoke newline-JSON-RPC runtime; we deliberately do
not use it (design note:
`docs/superpowers/specs/2026-08-17-dsh-acp-engine-design.md`).

Differences from the base, each established by probing the runtime on 2026-08-17
with the exact frames this base sends — not inferred:

- **Command shape.** dsh is a node script plus a config flag, not a CLI with an
  `--acp` switch, so the whole argv is replaced:
  `node <DSH_ACP_BIN> --config <DSH_ACP_CORDIS>`.

- **`session/set_model` is NOT implemented** — the runtime answers
  `-32601 Method not found`. The base calls it from `start_session` whenever a
  model is set, so that call is dropped here. The model is instead a property of
  the composition the runtime boots with (see "Model is a seat property" below).

- **`session/set_mode` is NOT implemented** — same `-32601`. The base sends it
  before EVERY turn (`generic_acp.py`, `set_session_mode_for_policy`), so left
  inherited this engine would fail every turn at the mode call, before the model
  was ever reached. It is overridden to a no-op.

  Consequence worth stating plainly: ARB's `trusted` vs `human` sender policy
  normally maps onto session mode, and here it CANNOT. Both policies get whatever
  posture `DSH_PERMISSION_MODE` gave the runtime at spawn. Sender policy governs
  only who may dispatch at all. **A dsh seat is not equivalent to a trusted codex
  seat and must not be described as one.**

- **`mcpServers` must be empty.** dsh's ACP server hard-rejects a non-empty list
  with `invalidParams` (`packages/acp/acp/src/index.ts:435`) — NOT `-32601`, so
  it is a separate failure from the two omitted methods. `start_session`
  therefore refuses rather than forwarding whatever `local_memory_mcp_servers()`
  returns. An earlier revision of this docstring called that payload merely
  "untested"; a reviewing seat read the harness source and found it is a hard
  reject, which is why the code now enforces what the prose used to hedge.

- `initialize`, `session/new` and `session/prompt` are inherited unchanged
  (modulo the `mcpServers` assertion); all three were verified against the same
  runtime.

`supports_thread_resume` is False. dsh may implement `session/load`, but it was
not probed, and declaring resume on an untested method is the class of unverified
claim this engine's docstring exists to avoid.

**This engine has no tool-allowlist surface.** Like opencode-acp, there is no
`--tools` flag; dsh's read-only posture is composition-based
(`DSH_PERMISSION_MODE` selects the sandbox policy and the approval plugin's
`ask`/`never`). It therefore cannot be certified read-only by `readonly_gate.py`
and is deliberately absent from its allowlist-engine set.

**Model is a seat property, not a dispatch parameter.** Because `set_model` does
not exist on the wire, the model is whatever the cordis composition boots with.
ARB's own composition (`configs/dsh/acp-agent.cordis.yml`) reads it from
`DSH_ACP_MODEL`, so the seat's plist is the single source of truth. `--model` is
accepted only to ASSERT agreement with that variable: a disagreement raises here
rather than silently serving a different model than the dispatch recorded, which
would make every downstream claim about "which model reviewed this" false.

Credentials reach the runtime through the ordinary child environment
(`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`); `scrub_env_dict` removes only bus and
gate-daemon credentials, so provider keys survive. Note that neither `initialize`
nor `session/new` contacts the gateway — a seat can ping healthy and handshake
cleanly with a dead credential, and only fail on the first prompt.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable

from ._acp import TurnPolicyPermissionMixin
from .base import EngineError
from .generic_acp import GenericAcpEngine

DEFAULT_MODEL = "deepseek-v4-pro"

# The package ARB's composition mounts as its `acp-agent` plugin. Used as the
# resolution probe: if THIS is reachable from the config's directory, cordis can
# resolve the composition; a directory merely NAMED node_modules proves nothing.
PROBE_PACKAGE = "dsh-acp-demo"

# Variables the composition consults for its session-persistence root, in the
# order it consults them. Named here because the ACP composition reads
# DSH_SNAPSHOT_SESSIONS_ROOT while the jsonrpc one reads DSH_SESSION_ROOT — the
# obvious guess is silently wrong on one of them.
PERSISTENCE_ENV_VARS = ("DSH_SESSION_ROOT", "DSH_SNAPSHOT_SESSIONS_ROOT")


class DshAcpEngine(TurnPolicyPermissionMixin, GenericAcpEngine):
    supports_thread_resume = False
    engine_label = "dsh"
    display_name = "dsh"

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        command: str | None = None,
        bin_path: str | None = None,
        cordis_config: str | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        node = command or os.environ.get("DSH_RUNTIME_NODE") or "node"
        self.bin_path = bin_path or os.environ.get("DSH_ACP_BIN") or ""
        self.cordis_config = cordis_config or os.environ.get("DSH_ACP_CORDIS") or ""
        if not self.bin_path:
            raise EngineError(
                "dsh-acp: DSH_ACP_BIN is unset; point it at the dsh-acp-demo "
                "runtime entry (packages/examples/acp-demo/lib/bin.js in a built "
                "deepseek-harness checkout)"
            )
        if not self.cordis_config:
            raise EngineError(
                "dsh-acp: DSH_ACP_CORDIS is unset; point it at the cordis "
                "composition the runtime should boot (ARB ships "
                "configs/dsh/acp-agent.cordis.yml)"
            )
        for label, path in (("DSH_ACP_BIN", self.bin_path), ("DSH_ACP_CORDIS", self.cordis_config)):
            if not os.path.exists(path):
                raise EngineError(f"dsh-acp: {label}={path!r} does not exist")

        self._assert_plugins_resolvable(self.cordis_config)
        self._assert_model_agrees(model)
        self._assert_persistence_is_outside(cwd)
        self._assert_sandbox_root_matches_session_cwd(cwd)

        super().__init__(
            cwd=cwd,
            model=model,
            command=node,
            popen_factory=popen_factory,
        )

    @staticmethod
    def _assert_plugins_resolvable(cordis_config: str) -> None:
        """Refuse a composition whose plugin specifiers cannot resolve.

        cordis names plugins as bare specifiers (`@deepseek-ai/dsh-hooks-codex`),
        and the runtime resolves them with Node's ESM algorithm rooted at the
        CONFIG FILE's directory — not the bin's, not the cwd. A config sitting
        outside any tree with a `node_modules` therefore dies at boot with
        ERR_MODULE_NOT_FOUND, which reaches the bridge as a 60s handshake
        timeout followed by a broken pipe: no usable diagnosis at all.

        Verified 2026-08-17 by pointing DSH_ACP_CORDIS at ARB's own copy of the
        composition before the node_modules symlink existed.

        The check looks for a NAMED PACKAGE, not merely a directory called
        `node_modules`. A bare existence check passes on an empty directory and
        on the wrong module tree, so it would certify exactly the layout it
        exists to reject and hand the operator back the opaque timeout
        (panel-dsh-acp-20260817T2200Z-e4cc59: codex P2, cold-Opus P1-2, grok
        P1-2 — all three seats, independently).
        """
        probe = os.path.join("@deepseek-ai", PROBE_PACKAGE)
        d = os.path.dirname(os.path.abspath(cordis_config))
        seen: list[str] = []
        while True:
            candidate = os.path.join(d, "node_modules")
            if os.path.isdir(candidate):
                seen.append(candidate)
                if os.path.exists(os.path.join(candidate, probe)):
                    return
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        found = f" Found node_modules without it at: {', '.join(seen)}." if seen else ""
        raise EngineError(
            f"dsh-acp: cannot resolve @deepseek-ai/{PROBE_PACKAGE} from "
            f"{cordis_config!r}.{found} cordis resolves bare plugin specifiers "
            "from the CONFIG's directory upward, so the composition must sit in "
            "a tree that can see the harness's plugin packages. In a pnpm "
            "workspace those are linked under the workspace package that "
            "DECLARES them, which is examples/, NOT the harness root: "
            "`ln -s <deepseek-harness>/examples/node_modules "
            "configs/dsh/node_modules`. Without this the runtime dies at boot "
            "with ERR_MODULE_NOT_FOUND and the bridge sees only a handshake "
            "timeout."
        )

    @staticmethod
    def _assert_model_agrees(model: str | None) -> None:
        """Refuse a --model the runtime cannot honour.

        `session/set_model` is `-32601` on this runtime, so a `--model` that
        disagrees with the composition's `DSH_ACP_MODEL` would be silently
        ignored and the seat would serve a different model than the dispatch
        recorded. Refusing is the whole point: a silent substitution corrupts the
        provenance of everything the seat produces.

        `or` is deliberately NOT used to apply the default. The composition uses
        JavaScript's `??`, which is nullish-based: an EMPTY `DSH_ACP_MODEL`
        survives it and boots the runtime with an empty model, while Python's
        falsy-based `or` would substitute the default and report an agreement
        that does not exist. The two operators differ on exactly one input, so
        that input is rejected outright (panel finding: codex, cold-Opus P1-4,
        grok P2-2 — three seats).
        """
        raw = os.environ.get("DSH_ACP_MODEL")
        if raw is not None and raw.strip() == "":
            raise EngineError(
                "dsh-acp: DSH_ACP_MODEL is set but empty. The composition "
                "resolves it with JavaScript's `??`, which treats an empty "
                "string as a real value, so the runtime would boot with an "
                "empty model rather than falling back to "
                f"{DEFAULT_MODEL!r}. Unset it or give it a model name."
            )
        if model is None:
            return
        seat_model = raw if raw is not None else DEFAULT_MODEL
        if model != seat_model:
            raise EngineError(
                f"dsh-acp: --model {model!r} disagrees with DSH_ACP_MODEL "
                f"{seat_model!r}. dsh does not implement session/set_model "
                "(-32601), so the model is fixed by the composition at spawn and "
                "--model cannot change it. Set DSH_ACP_MODEL in the seat's plist "
                "to the model you want, or pass a matching --model."
            )

    @staticmethod
    def _assert_persistence_is_outside(cwd: str) -> None:
        """Refuse a seat whose session log would land in its own working tree.

        The composition's fallback persistence root is `./.sessions`, relative
        to the runtime's cwd — which for a bridge seat is the repo. The runtime
        then writes `session.jsonl.zstd` (and a derived `session-query.db`) into
        the working tree, and the completion gate bounces the turn
        `dirty_after_commit`: a turn that fully succeeded reports ok=false.

        Observed on the first live gate run, 2026-08-17. It was fixed by setting
        the env var on one seat, which left the NEXT conforming deployment free
        to hit it again — the fix lived in host state, not in the contract. All
        three reviewing seats filed it (codex P1, cold-Opus P1-1, grok P2-3).
        """
        root = ""
        for var in PERSISTENCE_ENV_VARS:
            value = (os.environ.get(var) or "").strip()
            if value:
                root = value
                break
        if not root:
            raise EngineError(
                "dsh-acp: no session-persistence root is set. Set one of "
                f"{' / '.join(PERSISTENCE_ENV_VARS)} to a path OUTSIDE the "
                "seat's workdir. Unset, the composition falls back to "
                "'./.sessions' inside the working tree, and every turn is "
                "bounced dirty_after_commit by the completion gate even when "
                "the turn itself succeeded."
            )
        resolved = os.path.abspath(os.path.expanduser(root))
        workdir = os.path.abspath(cwd)
        if resolved == workdir or resolved.startswith(workdir + os.sep):
            raise EngineError(
                f"dsh-acp: session-persistence root {resolved!r} is inside the "
                f"seat's workdir {workdir!r}. Every turn would dirty the working "
                "tree and be bounced by the completion gate. Point it somewhere "
                "outside the repo (e.g. ~/.local/state/arb/dsh-sessions)."
            )

    @staticmethod
    def _assert_sandbox_root_matches_session_cwd(cwd: str) -> None:
        """Refuse a seat whose sandbox would be rooted at the wrong tree.

        ARB's composition pins the sandbox policy's `workspaceRoot` and the
        filesystem sandbox's `cwd` to `process.cwd()` — the node child's working
        directory. The ACP base spawns that child WITHOUT a `cwd=` argument, so
        it inherits the bridge daemon's cwd, while the session's cwd is the
        separate `--workdir`-derived value sent in `session/new`.

        Those are two independently-set knobs (launchd `WorkingDirectory` and
        `--workdir`). They coincided during the live gate, which is exactly why
        it passed and why it proves nothing about the case where they differ. If
        they diverge, the sandbox is rooted at one tree while the agent works in
        another — and the design note's claim that tools are "confined to the
        session cwd" would be false. Asserting it makes the claim true by
        enforcement rather than by coincidence (cold-Opus P1-5).
        """
        daemon_cwd = os.path.abspath(os.getcwd())
        session_cwd = os.path.abspath(cwd)
        if daemon_cwd != session_cwd:
            raise EngineError(
                f"dsh-acp: the daemon's cwd {daemon_cwd!r} differs from the "
                f"session cwd {session_cwd!r}. dsh roots its sandbox at the "
                "runtime process's cwd, but the session works in the second "
                "path, so the sandbox would fence the wrong tree. Set the "
                "seat's launchd WorkingDirectory equal to its --workdir."
            )

    def command_args(self) -> list[str]:
        return [self.command, self.bin_path, "--config", self.cordis_config]

    def start_session(self) -> str:
        """session/new only — the base's set_model call is `-32601` here.

        `mcpServers` is asserted empty rather than forwarded blindly. dsh's ACP
        server hard-rejects a non-empty list:

            packages/acp/acp/src/index.ts:435
            if (params.mcpServers.length > 0)
                throw invalidParams('mcpServers is not supported')

        That is `invalidParams`, not `-32601`, so it is not covered by the two
        method omissions above. `local_memory_mcp_servers()` returns `[]` only
        while no local-memory MCP is configured on the host; the moment one is,
        EVERY dsh session would fail at `session/new`. Refusing here names the
        cause instead of surfacing an upstream schema error (grok P2-1).
        """
        from agent_redis_bridge.local_memory_mcp import local_memory_mcp_servers

        mcp_servers = local_memory_mcp_servers()
        if mcp_servers:
            raise EngineError(
                "dsh-acp: this host configures a local-memory MCP server, and "
                "dsh's ACP transport rejects a non-empty mcpServers list "
                "(packages/acp/acp/src/index.ts:435, invalidParams "
                "'mcpServers is not supported'). A dsh seat cannot carry the "
                "local-memory MCP; unset ARB_MEMORY_LOCAL_MCP for this seat, or "
                "use an engine that supports MCP servers."
            )

        response = self.request(
            "session/new",
            {"cwd": self.cwd, "mcpServers": mcp_servers},
            timeout=30,
        )
        session_id = response.get("sessionId")
        if not isinstance(session_id, str):
            raise EngineError("session/new did not return sessionId")
        self.session_id = session_id
        return self.session_id

    def set_session_mode_for_policy(self, policy: str) -> None:
        """No-op: dsh answers `session/set_mode` with `-32601 Method not found`.

        Posture is fixed at spawn by DSH_PERMISSION_MODE, so there is nothing to
        select per turn. Deliberately does NOT raise — the base calls this before
        every turn, and raising would make the engine unusable rather than
        correctly limited.
        """
        if self.session_id is None:
            raise EngineError("ACP session not started")
        return None
