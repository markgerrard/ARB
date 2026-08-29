from __future__ import annotations

import argparse
import inspect
from collections import deque
from dataclasses import replace
from datetime import datetime
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any, Mapping
import uuid
import json

from redis.exceptions import ResponseError

from . import claim_gate
from . import completion_gate
from .engines.agy_print import AgyPrintEngine
from .engines.agy_tmux import AgyTmuxEngine
# NOTE: AgentSdkEngine is imported lazily inside the `engine == "agent-sdk"` branch of
# build_engine() — importing it eagerly pulls in `claude_agent_sdk` (an undeclared, optional
# dependency), which breaks codex/agy/pi seats on hosts that don't have it installed.
from .engines.agent_sdk_models import resolve as resolve_agent_sdk_model
from .engines.agent_sdk_continuation import (
    ContinuationWorkspaceError,
    ContinuationWorkspaceLease,
    ContinuationWorkspaceStore,
)
from .engines.base import AgentEngine, EngineError, TurnResult, engine_init_timeout
from .engines.codex import CodexEngine, normalize_reasoning_effort
from .engines.cline_acp import ClineAcpEngine
from .engines.cursor_acp import CursorAcpEngine
from .engines.devin_acp import DevinAcpEngine
from .engines.dsh_acp import DshAcpEngine
from .engines.gemini_acp import GeminiAcpEngine
from .engines.grok_acp import GrokAcpEngine
from .engines.kimi_code_acp import KimiCodeAcpEngine
from .engines.omp_acp import OmpAcpEngine
from .engines.opencode_acp import OpencodeAcpEngine
from .engines.mini_agent_acp import MiniAgentAcpEngine
from .engines.openinterpreter import CellToolPlaneBroker, OpenInterpreterEngine
from .engines.pi_rpc import PiRpcEngine
from .engines.pi_sdk import PiSdkEngine
from .engine_pool import AffinityAmbiguousError, AffinityBusyError, AffinityMissError, EnginePool
from .readonly_gate import enforce_readonly_tool_surface
from .envelope import (
    TASK_REF_REQUIRED_ENV,
    Envelope,
    EnvelopeError,
    EnvelopeHeader,
    iso_now,
    make_notify,
    make_reply,
    task_ref_required,
)
from .panel_input import panel_input_lock_reason
from .protocol import build_task_prompt, parse_structured_reply
from .redis_io import IdentityOwnedError, OwnershipLostError, RedisCli, RedisConfig, read_env_file
from .stall_watch import BlindEpisode, StallWatch
from .worktree_lease import (
    WorktreeLeaseError,
    WorktreeLeaseLock,
    WorktreeLeaseRecord,
    WorktreeLeaseStore,
)

# Engines whose ONLY mid-turn progress source can go dark without any error
# signal (agy-print: the SQLite transcript poller). Their tasks start BLIND —
# stall detection stays silent until a real progress event proves the channel
# (AGY-2 design v2.1, blind-until-proven). agy-tmux has the same gap
# (transcript.jsonl tail) and is PARKED with a standup gate: no agy-tmux seat
# enters service until it is added here (docs/BACKLOG.md § AGY-4).
BLIND_UNTIL_PROGRESS = frozenset({"agy-print"})


def resolve_agy_conversations_root(env: dict[str, str]) -> Path | None:
    """BRIDGE_AGY_CONVERSATIONS_ROOT: env-file first, then process env (AGY-2 C)."""
    raw = env.get("BRIDGE_AGY_CONVERSATIONS_ROOT") or os.environ.get("BRIDGE_AGY_CONVERSATIONS_ROOT")
    return Path(raw) if raw else None


def blind_config_warning(*, engine: str, stall_after_secs: int, turn_timeout_max: int) -> str | None:
    """Warn when a blind-capable engine's wedge backstop is much weaker than the stall threshold.

    A blind agy wedge is invisible to stall detection by design; the turn
    timeout is its only bound. That is a deployment recommendation, not an
    invariant — surface the divergence at startup instead of relying on it.
    """
    if engine not in BLIND_UNTIL_PROGRESS or stall_after_secs <= 0:
        return None
    if turn_timeout_max <= stall_after_secs:
        return None
    return (
        f"[bridge] WARNING: engine {engine} tasks start BLIND (stall detection silent until "
        f"first real progress) and --turn-timeout-max {turn_timeout_max} > stall threshold "
        f"{stall_after_secs}: a blind wedge is bounded only by the turn timeout. "
        f"The largest grantable ceiling is --turn-timeout-max {turn_timeout_max}; watch "
        f"stall_unknown on the visibility plane."
    )
from .event_flusher import EventFlusher
from .transcript_flusher import TranscriptFlusher
from .visibility_tee import _live_fields, live_tee


# --env-file resolves in this order:
#   1. --env-file CLI flag
#   2. AGENT_ENV_FILE shell env (e.g. set by systemd Environment=)
#   3. ".env" in the current working directory (dotenv convention)
# --workdir resolves in this order:
#   1. --workdir CLI flag
#   2. AGENT_WORKDIR shell env
#   3. AGENT_WORKDIR= line inside the env file picked up above
#   4. Current working directory
# The previous legacy fallback to /srv/projects/project-c-dev caused the
# bridge to register and start with the wrong workdir on any host that wasn't
# the original project-c Linux box — codex didn't notice (it only cd's per
# turn), gemini-acp crashed at session/new because the directory didn't exist.
DEFAULT_ENV_FILE = Path(os.environ.get("AGENT_ENV_FILE", ".env"))
DEFAULT_WORKDIR = os.environ.get("AGENT_WORKDIR")  # None → fall through to env file / cwd in __init__
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
ROLE_PATTERN = re.compile(r"^[a-z0-9-]{1,16}$")
# Worktree names become a path segment under <workdir>/.claude/worktrees/ and are
# passed to `git worktree add`, so they must not enable path traversal or look
# like an option. Restrict hard.
WORKTREE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Trusted, closed-schema lease lifecycle only — starts no engine, produces no
# task diff. worktree_run is deliberately NOT here; it remains gate subject.
WORKTREE_LIFECYCLE_OPERATIONS = frozenset({"worktree_arm", "worktree_release"})
logger = logging.getLogger(__name__)


def default_agent_sdk_session_root() -> str:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return str(base / "agent-redis-bridge" / "agent-sdk-sessions")


class WorktreeError(Exception):
    """A dispatch's worktree spec was invalid or the worktree could not be created."""
ENGINE_TO_TOOL = {
    "codex": "codex",
    "cline-acp": "cline",
    "cursor-acp": "cursor",
    "devin-acp": "devin",
    # DeepSeek Harness over its ACP transport. Takes the "dsh" prefix rather
    # than "deepseek": the seat is the harness, and the model it boots with is a
    # per-seat property (DSH_ACP_MODEL), so a model-named prefix would promise a
    # routing distinction the seat does not make.
    "dsh-acp": "dsh",
    "gemini-acp": "gemini",
    "grok-acp": "grok",
    "kimi-code-acp": "kimi",
    "mini-agent-acp": "minimax",
    # oh-my-pi is a pi fork but a separate routable seat family, so it takes its
    # own tool prefix ("omp") — an omp-acp seat must not collide with pi-rpc /
    # pi-sdk seats on derived agent-ids.
    "omp-acp": "omp",
    "opencode-acp": "opencode",
    "pi-rpc": "pi",
    # pi-sdk uses a distinct tool prefix so pi-sdk seats coexist with pi-rpc
    # seats on the same bus without colliding on derived agent-ids
    # (pi-rpc-project-b-dev-qcn vs pi-sdk-project-b-dev-qcn). Both
    # engines drive pi under the hood, but they're separate routable seats.
    "pi-sdk": "pi-sdk",
    "agy-print": "agy",
    "agy-tmux": "agy-tmux",
    # The agent-sdk engine registers under the short tool/seat name "asdk" (so seats read
    # asdk-<project>-<workspace>-<role> in the roster, not the verbose agent-sdk-…). "asdk" is
    # also accepted as a --engine alias, normalized to "agent-sdk" after parse (see main()).
    "agent-sdk": "asdk",
    "asdk": "asdk",
    "openinterpreter": "interpreter",
}


def normalize_engine_name(engine: str) -> str:
    """Map the short ``asdk`` alias to the canonical ``agent-sdk`` engine name. Centralized so EVERY
    construction path normalizes (Bridge.__init__, build_engine), not just the CLI entrypoint — a
    caller reaching build_engine/Bridge with an un-normalized ``asdk`` would otherwise silently
    mis-route (every ``engine_name == "agent-sdk"`` gate would be False)."""
    return "agent-sdk" if engine == "asdk" else engine


def build_eval_record(*, run_id, task_id, seat_id, event, sent_at, data, orchestrator=None):
    """Extract-only eval record for eval:events, or None if run_id absent (mistake-prevention)."""
    if not run_id:
        return None
    from .eval_tee import EVAL_SCHEMA_VERSION, extract_eval_payload

    return {
        "run_id": run_id,
        "task_id": task_id,
        "seat_id": seat_id,
        "orchestrator": orchestrator or "",
        "event_type": event,
        "sent_at": sent_at,
        "schema_version": EVAL_SCHEMA_VERSION,
        "payload": json.dumps(extract_eval_payload(data), separators=(",", ":")),
    }


def resolve_eval_redis(env):
    """Resolve eval-Redis config: exported process env wins, the parsed .env file is the fallback.

    Mistake-prevention: a URL/DB/prefix present ONLY in the bridge's .env file must still arm the eval
    tee. The old os.environ-only read silently left the tee disarmed in that case.
    """
    url = os.environ.get("ARB_EVAL_REDIS_URL") or env.get("ARB_EVAL_REDIS_URL")
    db_raw = os.environ.get("ARB_EVAL_REDIS_DB") or env.get("ARB_EVAL_REDIS_DB") or "4"
    try:
        db = int(db_raw)
    except ValueError:
        raise ValueError(f"ARB_EVAL_REDIS_DB must be an integer, got {db_raw!r}") from None
    prefix = os.environ.get("ARB_EVAL_PREFIX") or env.get("ARB_EVAL_PREFIX") or ""
    return url, db, prefix


def resolve_audit_redis(env):
    """Resolve audit-bus Redis config: exported process env wins, parsed .env file is fallback.

    The audit bus URL carries its db (prod /5, dev /3). A URL present only in the bridge's .env must still
    arm vote emission (Slice-1 mistake-prevention lesson).

    ARB_AUDIT_REDIS_URL is the audit-emitter's own credential; it takes precedence, falling back to
    ARB_MEMORY_REDIS_URL so hosts that set only the historical shared var keep working unchanged. Setting
    ARB_AUDIT_REDIS_URL lets the long-lived audit-emitter flip buses independently of the short-lived FABA
    memory-writer that still reads ARB_MEMORY_REDIS_URL (see dispatch_authority.PUBLISH_CREDENTIAL_ENV)."""
    url = (
        os.environ.get("ARB_AUDIT_REDIS_URL")
        or env.get("ARB_AUDIT_REDIS_URL")
        or os.environ.get("ARB_MEMORY_REDIS_URL")
        or env.get("ARB_MEMORY_REDIS_URL")
    )
    prefix = (
        os.environ.get("ARB_AUDIT_PREFIX")
        or env.get("ARB_AUDIT_PREFIX")
        or os.environ.get("ARB_MEMORY_PREFIX")
        or env.get("ARB_MEMORY_PREFIX")
        or ""
    )
    return url, prefix


def resolve_trace_redis(env):
    """Resolve transcript trace Redis: process env wins, parsed .env file is fallback."""
    url = os.environ.get("ARB_TRACE_REDIS_URL") or env.get("ARB_TRACE_REDIS_URL")
    prefix = os.environ.get("ARB_TRACE_PREFIX") or env.get("ARB_TRACE_PREFIX") or ""
    return url, prefix


def resolve_live_redis(env):
    """Resolve visibility live Redis: process env wins, parsed .env file is fallback."""
    url = os.environ.get("ARB_LIVE_REDIS_URL") or env.get("ARB_LIVE_REDIS_URL")
    prefix = os.environ.get("ARB_LIVE_PREFIX") or env.get("ARB_LIVE_PREFIX")
    return url, prefix


class Bridge:
    # Class-level default so instances built via Bridge.__new__(Bridge) in tests
    # (bypassing __init__ entirely) still resolve live_redis safely via self.redis.
    _live_redis: Any | None = None
    _task_epoch: dict[str, int] = {}
    _task_turn_index: dict[str, int] = {}
    # Membership criterion: events that occur OUTSIDE any turn's execution (lifecycle
    # markers, administrative audits) — never stamped with turn_index. Turn-scoped
    # health events (stall_unknown, stall_detected, progress_channel) stay OUT of this
    # set deliberately: they can only fire while a turn is in flight (stall checks
    # iterate active_requests; progress_channel arrives from engine poll threads
    # mid-turn), and WHICH turn had the sick channel is the diagnostic payload.
    _OUT_OF_TURN_EVENTS = {"task_started", "task_continuing", "agent_sdk_subscription_audit"}
    stall_watch = StallWatch(after_secs=0)

    @staticmethod
    def is_scored_request(request: Any) -> bool:
        """Return whether a request belongs to the receipt-only bakeoff boundary."""

        run_id = getattr(request, "run_id", None)
        payload = getattr(request, "payload", {})
        if not run_id and isinstance(payload, dict):
            run_id = payload.get("run_id")
        return isinstance(run_id, str) and run_id.startswith("oi-pi-bakeoff-")

    @classmethod
    def completion_mode(cls, request: Any) -> str:
        return "receipt-only" if cls.is_scored_request(request) else "host-gated"

    @staticmethod
    def project_scored_completion(value: Any, broker: Any | None = None) -> dict[str, Any]:
        """Return only the controller-owned receipt projection for scored work.

        Engine completion metadata is not an attestation.  Until a controller receipt
        producer supplies the projection, the required closed fields describe an
        infrastructure failure and no host completion state is carried across.
        """

        provider = getattr(broker, "completion_projection", None)
        if callable(provider):
            try:
                projected = provider()
            except Exception:
                projected = None
            required = {
                "mode", "ref_namespace", "receipt_oids", "dirty", "seal_complete",
                "receipts_authenticated", "infrastructure_failure",
            }
            allowed = required | {"receipt_records", "status", "source_descriptor", "candidate_ref"}
            if isinstance(projected, dict) and required <= set(projected) and set(projected) <= allowed:
                if not ("imported_oids" in projected or "imported_graph_attested" in projected or "scorer_result" in projected):
                    return dict(projected)
        source = value if isinstance(value, dict) else {}
        failure = source.get("infrastructure_failure")
        return {
            "mode": "receipt-only",
            "ref_namespace": "cell-attempt",
            "receipt_oids": [],
            "dirty": False,
            "seal_complete": False,
            "receipts_authenticated": False,
            "infrastructure_failure": failure or "missing-controller-receipt-projection",
        }

    def _bind_scored_tool_plane(self, envelope: Envelope, worktree: Path) -> None:
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}

        scored_metadata = {
            "cell_id": payload.get("cell_id"),
            "attempt_id": payload.get("attempt_id"),
            "fixture_root_oid": payload.get("fixture_root_oid"),
            "allowed_paths": payload.get("allowed_paths"),
        }
        if not isinstance(scored_metadata["cell_id"], str) or not re.fullmatch(
            r"cell-[0-9a-f]{64}", scored_metadata["cell_id"]
        ):
            raise EngineError("scored bind requires controller cell identity")
        if not isinstance(scored_metadata["attempt_id"], str) or not re.fullmatch(
            r"attempt-[0-9a-f]{64}", scored_metadata["attempt_id"]
        ):
            raise EngineError("scored bind requires controller attempt identity")
        if not isinstance(scored_metadata["fixture_root_oid"], str) or not re.fullmatch(
            r"[0-9a-f]{40}", scored_metadata["fixture_root_oid"]
        ):
            raise EngineError("scored bind requires the controller fixture root OID")
        if not isinstance(scored_metadata["allowed_paths"], list) or not all(
            isinstance(path, str) and path for path in scored_metadata["allowed_paths"]
        ):
            raise EngineError("scored bind requires a closed task glob allowlist")

        def valid_gid(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and value > 0

        controller_gid = payload.get("tool_gid")
        if not valid_gid(controller_gid):
            raise EngineError("scored bind requires controller tool-plane GID on the envelope")
        endpoint = payload.get("tool_endpoint")
        if not isinstance(endpoint, str) or not os.path.isabs(endpoint):
            raise EngineError("scored bind requires the controller-owned tool-broker endpoint")
        if "tool_capability" in payload:
            raise EngineError("Git capability must not enter the scored control process")

        def local_runtime_gids() -> tuple[int, ...]:
            candidates: list[int] = []
            runtime = getattr(self.args, "cell_runtime", None) or getattr(self.args, "runtime", None)
            identities = getattr(runtime, "identities", None)
            for source in (runtime, identities):
                value = getattr(source, "tool_gid", None)
                if value is not None:
                    candidates.append(value)
            for source in (
                getattr(self.args, "tool_gid", None),
                getattr(self.args, "tool_plane_gid", None),
            ):
                if source is not None:
                    candidates.append(source)
            if any(not valid_gid(value) for value in candidates):
                raise EngineError("scored bind received an invalid provisioned tool-plane GID")
            if len(set(candidates)) > 1:
                raise EngineError("scored bind received conflicting tool-plane GIDs")
            return tuple(candidates)

        local_gids = local_runtime_gids()
        if any(value != controller_gid for value in local_gids):
            raise EngineError("scored bind received a local tool-plane GID that conflicts with the controller envelope")
        provisioned_gid = controller_gid
        self.args.scored_tool_gid = provisioned_gid
        identity = {
            "cell_id": scored_metadata["cell_id"],
            "attempt_id": scored_metadata["attempt_id"],
        }
        broker_factory = getattr(self.args, "scored_tool_broker_factory", CellToolPlaneBroker.from_endpoint)
        self.args.tool_broker = broker_factory(
            endpoint, socket_gid=provisioned_gid, identity=identity,
        )
        self.args._scored_tool_plane_bound = True
    def _ensure_task_maps(self) -> None:
        """v6 (codex r4 P2): the class-level `_task_epoch`/`_task_turn_index` defaults exist so
        Bridge.__new__ fixtures can READ safely. Any writer must call this first — writing through
        the class default would mutate shared state across every fixture in the process."""
        if "_task_epoch" not in self.__dict__:
            self._task_epoch = {}
        if "_task_turn_index" not in self.__dict__:
            self._task_turn_index = {}

    @property
    def live_redis(self):
        # Dynamic fallback to self.redis (not a snapshot taken at construction
        # time): tests reassign bridge.redis to a fake after construction, and
        # without this indirection _tee_live_event would keep talking to
        # whatever self.redis WAS at __init__ time instead of the fake.
        return self._live_redis if self._live_redis is not None else self.redis

    @live_redis.setter
    def live_redis(self, value):
        self._live_redis = value

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        turn_timeout = int(args.turn_timeout)
        turn_timeout_max = int(args.turn_timeout_max)
        if turn_timeout <= 0 or turn_timeout_max <= 0 or turn_timeout_max < turn_timeout:
            raise ValueError(
                "invalid turn timeout configuration: "
                f"--turn-timeout {turn_timeout}, --turn-timeout-max {turn_timeout_max}; "
                "both must be positive and turn-timeout-max must be >= turn-timeout"
            )
        lease_ttl = int(getattr(args, "worktree_lease_ttl", 7200))
        lease_ttl_max = int(getattr(args, "worktree_lease_ttl_max", 14400))
        max_armed = int(getattr(args, "max_armed_worktrees", 16))
        max_armed_per_sender = int(getattr(args, "max_armed_worktrees_per_sender", 4))
        if (
            lease_ttl < 7200 or lease_ttl_max < lease_ttl
            or max_armed < 1 or max_armed_per_sender < 1 or max_armed_per_sender > max_armed
        ):
            raise ValueError(
                "invalid worktree lease configuration: default TTL must be >=7200, "
                "max TTL must be >= default, and armed quotas must be positive with "
                "the per-sender quota <= the global quota"
            )
        self._task_epoch = {}
        self._task_turn_index = {}
        env_file = Path(args.env_file)
        env = read_env_file(env_file)

        # Resolve the pi tool surface through CLI/process-env > env-file. Without
        # this, BRIDGE_PI_TOOLS set in the env-file (the documented .env.pi-dev
        # read-only-seat shape) never reaches the engine (args.pi_tools stays None
        # -> pi falls back to the FULL toolset) NOR the read-only gate. Write the
        # effective value back onto args so build_engine() and the gate share one
        # source of truth. (tri-model review: codex+agy+opus.)
        self.args.pi_tools = getattr(args, "pi_tools", None) or env.get("BRIDGE_PI_TOOLS")
        self.args.agent_sdk_tools = (
            getattr(args, "agent_sdk_tools", None)
            or env.get("BRIDGE_AGENT_SDK_TOOLS")
            or os.environ.get("BRIDGE_AGENT_SDK_TOOLS")
        )
        self.args.agent_sdk_session_root = (
            getattr(args, "agent_sdk_session_root", None)
            or env.get("BRIDGE_AGENT_SDK_SESSION_ROOT")
            or os.environ.get("BRIDGE_AGENT_SDK_SESSION_ROOT")
        )
        self.args.worktree_lease_root = (
            getattr(args, "worktree_lease_root", None)
            or env.get("BRIDGE_WORKTREE_LEASE_ROOT")
            or os.environ.get("BRIDGE_WORKTREE_LEASE_ROOT")
            or self.args.agent_sdk_session_root
            or default_agent_sdk_session_root()
        )
        self.args.provider = (
            getattr(args, "provider", None)
            or env.get("BRIDGE_INTERPRETER_PROVIDER")
            or os.environ.get("BRIDGE_INTERPRETER_PROVIDER")
        )
        self.args.harness = (
            getattr(args, "harness", None)
            or env.get("BRIDGE_INTERPRETER_HARNESS")
            or os.environ.get("BRIDGE_INTERPRETER_HARNESS")
        )
        # The read-only gate opt-in marker, resolved env-file > process-env so a
        # seat can declare itself read-only in its own env-file (preferred) or via
        # the launchd plist's process env.
        self.required_readonly_tools = (
            env.get("ARB_REQUIRE_READONLY_TOOLS")
            or os.environ.get("ARB_REQUIRE_READONLY_TOOLS")
        )

        self.workspace = args.workspace or env.get("AGENT_WORKSPACE") or "dev"
        self.project = args.project or env.get("AGENT_PROJECT") or "project-c"
        self.args.engine = normalize_engine_name(args.engine)
        self.engine_name = self.args.engine
        if self.engine_name == "openinterpreter":
            self.args.model = (
                getattr(args, "model", None)
                or env.get("BRIDGE_INTERPRETER_MODEL")
                or os.environ.get("BRIDGE_INTERPRETER_MODEL")
            )
        self.tool = ENGINE_TO_TOOL[self.engine_name]
        if self.engine_name == "agent-sdk" and not args.role:
            args.role = resolve_agent_sdk_model(args.model or "minimax-m3").slug
        self.role = normalize_role(args.role)
        self.agent_id = args.agent_id or derive_agent_id(
            tool=self.tool,
            project=self.project,
            workspace=self.workspace,
            role=self.role,
        )
        # Slice 1c claim gate: default OFF. Credentials come only from the
        # supervisor process environment — never the app-repo --env-file.
        self.claim_gate_enabled = bool(getattr(args, "claim_gate", False))
        self.claim_resolver = None
        # Slice 1d-v: brief_hydrate readiness is one executed predicate shared by
        # construction (:491) and reassert_liveness (:926) registry advertisement.
        # seat-preflight does not yet call this predicate (residual). Never advertise
        # brief_hydrate=v1 without this proof. Dual-accept remains on; flags stay off.
        from .brief_hydrate_ready import prove_brief_hydrate_readiness

        stage_root = (
            os.environ.get("BRIDGE_BRIEF_STAGE_ROOT")
            or env.get("BRIDGE_BRIEF_STAGE_ROOT")
            or str(Path(tempfile.gettempdir()) / "bridge-brief-stage")
        )
        self.brief_stage_root = Path(stage_root)
        self.brief_hydrate_ready = prove_brief_hydrate_readiness(
            env={**os.environ, **env},
            runtime_root=self.brief_stage_root,
        )
        if self.claim_gate_enabled:
            reader_dsn = os.environ.get("ARB_GATE_READER_DSN")
            if not reader_dsn:
                raise RuntimeError(
                    "claim gate enabled but ARB_GATE_READER_DSN is missing; "
                    "refusing to serve"
                )
            from .claim_resolver import PsycopgClaimResolver
            from arb_memory.mcp.grants import GATE_READER_ROLE

            expected_role = os.environ.get("ARB_GATE_READER_ROLE", GATE_READER_ROLE)
            self.claim_resolver = PsycopgClaimResolver(
                reader_dsn, expected_role=expected_role
            )
        # Slice 1d-ii: server-side worktree lane (not caller-supplied). Defaults
        # gated; exact gated|exempt only.
        lane_raw = os.environ.get("BRIDGE_WORKTREE_LANE", "gated")
        if lane_raw not in {"gated", "exempt"}:
            raise RuntimeError(
                f"BRIDGE_WORKTREE_LANE must be exactly gated|exempt, got {lane_raw!r}"
            )
        self.worktree_lane = lane_raw
        # Lane writer: process-env secret only. Gate-on without DSN is fatal;
        # gate-off without DSN preserves FS-only seats until rollout; DSN present
        # (even with gate off) activates readiness + row writes for substrate proof.
        self.lane_writer = None
        writer_dsn = os.environ.get("ARB_GATE_LANE_WRITER_DSN")
        if self.claim_gate_enabled and not writer_dsn:
            raise RuntimeError(
                "claim gate enabled but ARB_GATE_LANE_WRITER_DSN is missing; "
                "refusing to serve"
            )
        if writer_dsn:
            from .lane_writer import PsycopgLaneWriter

            writer_role = os.environ.get("ARB_GATE_LANE_WRITER_ROLE")
            if not writer_role:
                raise RuntimeError(
                    "ARB_GATE_LANE_WRITER_DSN is set but ARB_GATE_LANE_WRITER_ROLE "
                    "is missing; refusing to construct lane writer"
                )
            self.lane_writer = PsycopgLaneWriter(
                writer_dsn,
                expected_role=writer_role,
                expected_consumer_id=self.agent_id,
                expected_lane=self.worktree_lane,
            )
        # A PID is host-local while agent_id ownership is bus-global. Mint one
        # unguessable identity per daemon boot and use it for every lease check.
        self.owner_token = uuid.uuid4().hex
        self.args._derived_agent_id = self.agent_id
        if self.engine_name == "agent-sdk":
            spec = resolve_agent_sdk_model(args.model or "minimax-m3")
            self.args._agent_sdk_key = os.environ.get(spec.key_env) or env.get(spec.key_env)
        self.usage_identity = args.usage_scope or self.agent_id
        workdir_value = args.workdir or env.get("AGENT_WORKDIR") or os.getcwd()
        self.workdir = Path(workdir_value).resolve()
        self.worktree_lease_store = WorktreeLeaseStore(Path(self.args.worktree_lease_root), self.agent_id)
        self.args._agent_sdk_primary_cwd = str(self.workdir)
        self.branch = git_branch(self.workdir)
        # Role-profile resolution: explicit CLI arg > env file / process env > none.
        # Pi engines consume the same value natively; other engines receive it by
        # bridge-side prompt wrapping on the first turn only.
        role_profile_file = (
            args.role_profile_file
            or env.get("BRIDGE_ROLE_PROFILE_FILE")
            or os.environ.get("BRIDGE_ROLE_PROFILE_FILE")
        )
        self.role_profile_path = str(Path(role_profile_file).expanduser()) if role_profile_file else None
        self.role_profile = load_role_profile(self.role_profile_path)
        self.args.role_profile_file = self.role_profile_path
        self.args._loaded_role_profile = self.role_profile
        self.args._role_profile_loaded = True
        # Completion gate: refuse to report an ok turn that left uncommitted edits.
        # Default ON; only ever acts on the unambiguous violation (see completion_gate).
        self.enforce_completion = bool(getattr(args, "enforce_completion", True))
        configured_claim_timeout = getattr(args, "identity_claim_timeout", None)
        self.args.identity_claim_timeout = max(
            1,
            int(configured_claim_timeout)
            if configured_claim_timeout is not None
            else int(args.heartbeat_ttl) + int(args.heartbeat_interval),
        )
        self.max_parallel = max(1, int(getattr(args, "max_parallel", 1)))
        # Drive-to-completion loop: re-prompt a continuation-capable engine in
        # its live worktree session until the task predicate passes, no progress
        # is made, or this budget trips. 0 disables the loop (single bounce).
        self.max_continuation_turns = max(0, int(getattr(args, "max_continuation_turns", 3)))
        stall_after_raw = env.get("BRIDGE_STALL_AFTER_SECS") or os.environ.get("BRIDGE_STALL_AFTER_SECS")
        if stall_after_raw is None:
            stall_after_raw = str(getattr(args, "stall_after_secs", 600))
        self.stall_watch = StallWatch(after_secs=int(stall_after_raw))
        agy_root = resolve_agy_conversations_root(env)
        if agy_root is not None:
            args.agy_conversations_root = agy_root
        config_warning = blind_config_warning(
            engine=str(getattr(args, "engine", "") or ""),
            stall_after_secs=self.stall_watch.after_secs,
            turn_timeout_max=int(getattr(args, "turn_timeout_max", 0) or 0),
        )
        if config_warning:
            logger.warning(config_warning)
        # Orchestrator-commit: once the dispatch's expected_artifacts are all
        # present but uncommitted, the bridge commits them itself (the model did
        # the hard part). Only ever fires when expected_artifacts is non-empty,
        # so non-artifact dispatches are unaffected.
        self.auto_commit = bool(getattr(args, "auto_commit", True))
        self.commit_message_from_model = bool(getattr(args, "commit_message_from_model", False))
        self.cancelled_tasks: set[str] = set()
        self.redis_config = RedisConfig.from_env_file(
            env_file,
            {
                "AGENT_REDIS_HOST": args.redis_host,
                "AGENT_REDIS_PORT": args.redis_port,
                "AGENT_REDIS_DB": args.redis_db,
                "AGENT_REDIS_PREFIX": args.redis_prefix,
            },
        )
        self.redis = RedisCli(self.redis_config)
        self._tee_drop_count = 0
        self._tee_marker_drop_count = 0
        self._tee_count_lock = threading.Lock()
        self._live_flusher = None
        self._live_thread = None
        self._eval_flusher = None
        self._eval_thread = None
        live_url, live_prefix = resolve_live_redis(env)
        self._live_prefix = live_prefix if live_prefix is not None else self.redis_config.prefix
        if live_url:
            import redis as _redis

            self.live_redis = _redis.from_url(
                live_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
                socket_keepalive=True,
                health_check_interval=30,
            )
            self._live_remote = True
            self._live_flusher = EventFlusher(
                self.live_redis,
                f"{self._live_prefix}events:live",
                maxsize=int(os.environ.get("ARB_LIVE_TEE_QMAX", "10000")),
                maxlen=self.args.max_task_events,
                on_drop=self._handle_async_tee_drop,
                on_marker_drop=self._handle_marker_drop,
            )
            self._live_thread = threading.Thread(target=self._live_flusher.run, daemon=True)
            self._live_thread.start()
        else:
            # No dedicated live-redis URL: track self.redis dynamically rather than
            # snapshotting a reference here. self.redis is reassigned by tests
            # (bridge.redis = fake); a snapshot would go stale and _tee_live_event
            # would keep talking to the real client the test meant to replace.
            self.live_redis = None
            self._live_remote = False
        eval_url, eval_db, eval_prefix = resolve_eval_redis(env)
        if eval_url:
            import redis as _redis

            self.eval_redis = _redis.from_url(
                eval_url,
                db=eval_db,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
                socket_keepalive=True,
                health_check_interval=30,
            )
            resolved_eval_db = int(self.eval_redis.connection_pool.connection_kwargs.get("db", 0))
            if resolved_eval_db != eval_db:
                raise ValueError(
                    f"ARB_EVAL_REDIS_DB mismatch: configured {eval_db}, "
                    f"Redis URL resolved to {resolved_eval_db}"
                )
            self._eval_stream = f"{eval_prefix}eval:events"
            self._eval_remote = True
            self._eval_flusher = EventFlusher(
                self.eval_redis,
                self._eval_stream,
                maxsize=int(os.environ.get("ARB_EVAL_TEE_QMAX", "10000")),
                on_drop=self._handle_async_tee_drop,
            )
            self._eval_thread = threading.Thread(target=self._eval_flusher.run, daemon=True)
            self._eval_thread.start()
        else:
            self.eval_redis = None
            self._eval_stream = None
            self._eval_remote = False
        trace_url, trace_prefix = resolve_trace_redis(env)
        if not live_url and (eval_url or trace_url):
            logger.warning(
                "ARB_LIVE_REDIS_URL is not set while remote eval/trace telemetry is configured; "
                "dropped markers fall back to synchronous local events:live XADD. "
                "Set ARB_LIVE_REDIS_URL too for nonblocking fleet visibility markers."
            )
        audit_url, audit_prefix = resolve_audit_redis(env)
        if audit_url:
            import redis as _redis
            self.audit_redis = _redis.from_url(
                audit_url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5,
            )
            self._audit_prefix = audit_prefix
        else:
            self.audit_redis = None
            self._audit_prefix = ""
        self.trace_redis = None
        self._trace_prefix = ""
        self._transcript_flusher = None
        self._transcript_thread = None
        self._transcript_enabled = False
        self.stop_event = threading.Event()
        self.reliable_inbox = True
        self.heartbeat_failures = 0
        self.registered_at = iso_now()
        self.seen_request_ids: deque[tuple[float, str]] = deque()
        sender_specs = (
            args.sender_policy
            or _split_sender_policy_env(env.get("AGENT_TRUSTED_SENDERS"))
            or _split_sender_policy_env(os.environ.get("AGENT_TRUSTED_SENDERS"))
            or []
        )
        self.sender_policies = self.parse_sender_policies(sender_specs)
        # B6 posture-injection seam (design §8 item 7): capture task-ref posture
        # once at construction. handle_raw passes this into Envelope.from_json as
        # ref_required=; no handling-time ambient os.environ reads on that path.
        # Precedence matches the file's other seams (e.g. BRIDGE_PI_TOOLS at
        # :420-430): explicit CLI/args > process env > env-file > default off.
        # Process env must beat env-file so a plist canary (=1) is not silently
        # reverted by deploy/.env.example's shipped BRIDGE_TASK_REF_REQUIRED=0.
        explicit_ref = getattr(args, "task_ref_required", None)
        if explicit_ref is not None:
            self.task_ref_required = bool(explicit_ref)
        elif TASK_REF_REQUIRED_ENV in os.environ:
            self.task_ref_required = task_ref_required()
        elif TASK_REF_REQUIRED_ENV in env:
            self.task_ref_required = task_ref_required(env=env)
        else:
            self.task_ref_required = False
        # CDX-1 D3: getattr defaults keep this safe on fabricated/partial args
        # (tests build Bridge objects without the full parser namespace).
        notice = codex_approval_path_notice(
            engine=normalize_engine_name(getattr(args, "engine", "") or ""),
            bypass=bool(getattr(args, "codex_bypass_approvals_and_sandbox", False)),
            sender_policies=self.sender_policies,
            unknown_sender_policy=getattr(args, "unknown_sender_policy", "reject"),
        )
        if notice is not None:
            getattr(logger, notice[0])(notice[1])
        if not self.sender_policies:
            logger.warning(
                "[bridge] WARNING: no sender policies configured. "
                "Bridge will reject all dispatches until AGENT_TRUSTED_SENDERS is set "
                "in the env file (preferred) or --sender-policy is passed on the CLI.",
            )
        # Fail-loud on the dangerous default combo for a PARALLEL orchestrator: with
        # notify_inbox=1 (the back-compat default) every task notify is LPUSHed into the
        # caller's :inbox — the same list the caller BLPOPs replies from. At max_parallel=1
        # that's a trickle, but under parallel load it floods the inbox (the "thousands of
        # notifies" failure mode). Route notifies to a separate list instead.
        if int(getattr(self.args, "max_parallel", 1)) > 1 and int(getattr(self.args, "notify_inbox", 1)) == 1:
            logger.warning(
                "[bridge] WARNING: max_parallel>1 with notify_inbox=1 routes every task "
                "notify into the caller's :inbox, which floods it under parallel load. "
                "Set BRIDGE_NOTIFY_INBOX=0 (or --notify-inbox 0) to route notifies to a "
                "separate :notify_inbox list for any parallel orchestrator.",
            )
        self.active_lock = threading.Lock()
        self.active_requests: dict[str, Envelope] = {}
        self.active_threads: dict[str, threading.Thread] = {}
        # Worktree-escape attribution guard: a base-checkout fingerprint can only
        # prove an escape when NO legitimate base-cwd task overlapped the window.
        # Count concurrent base-cwd turns, and bump a generation on every start so
        # a base turn that started AND finished inside the window is still seen.
        self.base_cwd_turns = 0
        self.base_cwd_turn_gen = 0
        # task_id -> the engine ACTUALLY running that task. For a worktree task this
        # is the fresh worktree engine, not the pooled slot engine, so steer/cancel
        # route to the engine running the turn (pool.get would return the slot token).
        self.task_engines: dict[str, AgentEngine] = {}
        # Per-task throttle map for streaming-response liveness HSET.
        # Keys are wiped in the per-task `finally` block so this never
        # grows past max_parallel entries.
        self._last_stream_heartbeat: dict[str, float] = {}
        # Per-task monotonic timestamp of the last events:live tee — drives the
        # turn-liveness heartbeat throttle so a quiet-but-active turn gets a
        # periodic `turn_heartbeat` while a chatty one does not. Wiped per-task.
        self._last_live_tee_ts: dict[str, float] = {}
        self._transcript_q: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=int(os.environ.get("ARB_TRANSCRIPT_QMAX", "10000"))
        )
        self._transcript_truncated = 0
        self._transcript_seq = 0
        capture_mode = os.environ.get("ARB_TRANSCRIPT_CAPTURE") or env.get("ARB_TRANSCRIPT_CAPTURE") or "on"
        if trace_url and capture_mode.lower() != "off":
            import redis as _redis

            self.trace_redis = _redis.from_url(
                trace_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
                socket_keepalive=True,
                health_check_interval=30,
            )
            self._trace_prefix = trace_prefix
            self._transcript_enabled = True
            self._transcript_flusher = TranscriptFlusher(self._transcript_q, self.trace_redis, self._trace_prefix)
            self._transcript_thread = threading.Thread(target=self._transcript_flusher.run, daemon=True)
            self._transcript_thread.start()
        self.pool: EnginePool[AgentEngine] = EnginePool(
            factory=lambda: build_engine(self.args, cwd=str(self.workdir)),
            max_size=max(1, int(getattr(self.args, "max_parallel", 1))),
        )
        resume_classes = {
            "codex": CodexEngine,
            "cline-acp": ClineAcpEngine,
            "cursor-acp": CursorAcpEngine,
            "devin-acp": DevinAcpEngine,
            "gemini-acp": GeminiAcpEngine,
            "grok-acp": GrokAcpEngine,
            "openinterpreter": OpenInterpreterEngine,
            "pi-rpc": PiRpcEngine,
            "pi-sdk": PiSdkEngine,
        }
        self.engine_supports_resume = (
            not bool(getattr(self.args, "agent_sdk_oneshot", False))
            if self.engine_name == "agent-sdk"
            else bool(getattr(resume_classes.get(self.engine_name), "supports_thread_resume", False))
        )

    def run(self) -> int:
        if not self.enforce_completion and not (
            self.args.self_test or self.args.once or self.args.dry_run
        ):
            raise RuntimeError(
                "--no-enforce-completion is restricted to diagnostic one-shot modes "
                "(--self-test, --once, or --dry-run); durable daemons must enforce completion"
            )
        install_signal_handlers(self.stop_event)
        # Pre-serve checks and registration share one cleanup boundary so a
        # readiness failure still closes the claim-reader connection.
        try:
            self.enforce_readonly_gate()
            self.enforce_claim_gate_ready()
            self.reconcile_worktree_leases()
            if not self.enforce_completion:
                logger.warning(
                    "[bridge-warning] completion-enforcement-disabled: unsafe diagnostic mode; "
                    "write-task success will not prove commit/artifact completion",
                )

            if self.args.self_test:
                self.register()
                self.self_test()
                return 0

            if self.engine_name == "agent-sdk":
                self.start_engine()
                self.register()
            else:
                self.register()
            heartbeat = threading.Thread(target=self.heartbeat_loop, daemon=True)
            heartbeat.start()
            if self.engine_name != "agent-sdk":
                self.start_engine()
            return self.inbox_loop()
        finally:
            self.cleanup()

    def enforce_claim_gate_ready(self) -> None:
        """Prove gate reader (when on) and lane-writer readiness before register().

        Writer readiness runs whenever a lane-writer DSN was constructed — including
        gate-off rollout — so the substrate is proved before any arm can write rows.
        """
        if self.claim_gate_enabled:
            assert self.claim_resolver is not None
            self.claim_resolver.assert_ready()
            logger.info(
                f"[claim-gate] {self.agent_id} reader ready; enforcement active"
            )
        if self.lane_writer is not None:
            self.lane_writer.assert_ready()
            logger.info(
                f"[claim-gate] {self.agent_id} lane writer ready "
                f"lane={self.worktree_lane}"
            )

    def enforce_readonly_gate(self) -> None:
        """Pre-serve gate for seats declared read-only via ARB_REQUIRE_READONLY_TOOLS.

        Refuses to register/serve unless the pi tool surface is a non-empty subset
        of the declared allowlist — closing the BRIDGE_PI_TOOLS fail-open
        (unset/empty -> pi falls back to the FULL toolset) fail-CLOSED. A violation
        raises, propagating to main() which prints [bridge-error] and exits 1
        (launchd KeepAlive crash-loops, visibly refusing to serve). Opt-in: seats
        that legitimately want full tools simply don't set the env var."""
        required = self.required_readonly_tools
        if not required:
            return
        enforce_readonly_tool_surface(
            engine=self.engine_name,
            pi_tools=getattr(self.args, "pi_tools", None),
            agent_sdk_tools=getattr(self.args, "agent_sdk_tools", None),
            required_csv=required,
        )
        logger.info(
            f"[readonly-gate] {self.agent_id} surface matches its SELF-DECLARED "
            f"read-only allowlist (<= {required}); serving. Declaration is "
            "seat-owned config, not store-backed posture (ARB-B14a).",
        )

    def register(self) -> None:
        deadline = time.monotonic() + self.args.identity_claim_timeout
        while True:
            try:
                self.reassert_liveness()
                break
            except IdentityOwnedError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"identity claim timed out after {self.args.identity_claim_timeout}s: {exc}"
                    ) from exc
                lease_wait = exc.ttl + 1 if exc.ttl >= 0 else self.args.heartbeat_interval
                wait_for = min(remaining, max(1, lease_wait))
                logger.warning(
                    f"[bridge-warning] identity-owned agent_id={self.agent_id} "
                    f"lease_ttl={exc.ttl}; retrying in {wait_for:.1f}s"
                )
                if self.stop_event.wait(wait_for):
                    raise RuntimeError("identity claim interrupted during shutdown") from exc
        logger.info(f"[bridge] {self.agent_id} online at {iso_now()} (pid={os.getpid()})")

    def reassert_liveness(self) -> None:
        # Advertise bus-and-gate-daemon-creds-v2 only after the selected engine
        # family's real-child scrub self-check succeeds. The check spawns a real
        # child via that family's production env path (not a predicate-only
        # simulation) — a family that never calls the scrub cannot advertise v2.
        from .engines._stdio import prove_env_scrub_capability

        env_scrub = prove_env_scrub_capability(engine_name=self.engine_name)
        # Slice 1d-v: re-run the same executed readiness predicate used at
        # construction. Advertisement is brief_hydrate=v1 only when that
        # predicate returns True — never a declarative empty-to-v1 flip.
        from .brief_hydrate_ready import prove_brief_hydrate_readiness

        self.brief_hydrate_ready = prove_brief_hydrate_readiness(
            env=os.environ,
            runtime_root=getattr(self, "brief_stage_root", None),
        )
        brief_hydrate = "v1" if self.brief_hydrate_ready else ""
        # worker_vantage is supervisor-owned.
        worker_vantage = (os.environ.get("BRIDGE_WORKER_VANTAGE") or "").strip()
        self.redis.register(
            agent_id=self.agent_id,
            tool=self.tool,
            project=self.project,
            workspace=self.workspace,
            branch=self.branch,
            path=str(self.workdir),
            registered_at=self.registered_at,
            pid=os.getpid(),
            owner_token=self.owner_token,
            ttl=self.args.heartbeat_ttl,
            env_scrub=env_scrub,
            worker_vantage=worker_vantage,
            task_wire="legacy-or-ref-v1",
            brief_hydrate=brief_hydrate,
            readonly_tools=(getattr(self, "required_readonly_tools", None) or ""),
        )

    def cleanup(self) -> None:
        self.stop_event.set()
        # Claim-reader / lane-writer close is best-effort and must not block
        # Redis ownership cleanup or engine shutdown. close() is idempotent.
        if self.claim_resolver is not None:
            try:
                self.claim_resolver.close()
            except Exception:
                pass
        if self.lane_writer is not None:
            try:
                self.lane_writer.close()
            except Exception:
                pass
        # Drain the transcript flusher before exit: it's a daemon thread (abandoned at
        # process exit), so a fast run could otherwise lose its in-flight coalesced
        # transcript. Best-effort + fail-soft (telemetry is never load-bearing).
        if self._transcript_flusher is not None:
            try:
                self._transcript_flusher.stop()
                thread = self._transcript_thread
                if thread is not None:
                    thread.join(timeout=2.0)
                # Only drain on this thread if the daemon actually EXITED — join(timeout)
                # does not guarantee that, and racing a live daemon would mutate _pending
                # concurrently. If it's still alive, leave the remainder (telemetry, non-load-bearing).
                if thread is None or not thread.is_alive():
                    self._transcript_flusher.flush_pending()
            except Exception:
                pass
        for flusher, thread in (
            (getattr(self, "_eval_flusher", None), getattr(self, "_eval_thread", None)),
            (getattr(self, "_live_flusher", None), getattr(self, "_live_thread", None)),
        ):
            if flusher is None:
                continue
            try:
                flusher.stop()
                if thread is not None:
                    thread.join(timeout=2.0)
                if thread is None or not thread.is_alive():
                    flusher.flush_pending()
            except Exception:
                pass
        self.redis.cleanup(self.agent_id, self.owner_token)
        self.pool.stop_all()

    def self_test(self) -> None:
        drained = 0
        while True:
            raw = self.redis.lpop(self.agent_id)
            if raw is None:
                break
            drained += 1
            logger.info(f"[inbox] {raw}")
            try:
                envelope = Envelope.from_json(raw)
            except EnvelopeError as exc:
                logger.error(f"[bridge-error] envelope-invalid {exc}")
                continue
            logger.info(f"[would-handle] {envelope.id}")
        logger.info(f"[bridge] self-test drained={drained}")

    def heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.args.heartbeat_interval):
            try:
                self.reassert_liveness()
                self.heartbeat_failures = 0
            except (IdentityOwnedError, OwnershipLostError) as exc:
                # A different boot instance owns the identity: this daemon is
                # deposed, not flaky. Stop now — the 3-strike ladder below is
                # for transient Redis errors and would let a deposed daemon
                # keep working as an impostor for up to three more intervals.
                logger.error(f"[bridge-error] heartbeat-ownership-lost {exc}")
                self.stop_event.set()
            except Exception as exc:
                self.heartbeat_failures += 1
                logger.error(f"[bridge-error] heartbeat-fail {exc}")
                if self.heartbeat_failures >= 3:
                    self.stop_event.set()
            # Turn-liveness heartbeat is fail-soft and kept OUT of the registry
            # heartbeat try above: a tee failure must not trip heartbeat_failures
            # (which kills the bridge at 3) — a stale roster row is not fatal.
            try:
                self._emit_turn_heartbeats(time.monotonic())
            except Exception:
                logger.exception("turn-liveness heartbeat emit failed")
            try:
                self._check_stalls(time.monotonic())
            except Exception:
                logger.exception("stall detection check failed")
            try:
                self.reconcile_worktree_leases()
            except Exception:
                logger.exception("worktree lease reconcile failed")

    def _emit_turn_heartbeats(self, now: float) -> None:
        """Tee a `turn_heartbeat` to events:live for any active turn that has gone
        quiet (no live event within heartbeat_interval), so a still-running but
        event-quiet seat (e.g. agy-print blocked on the model API) keeps a fresh
        last_event_ts in the visibility roster instead of reading stale. Gated on
        run_id exactly like every other live tee; chatty turns self-throttle."""
        interval = getattr(self.args, "heartbeat_interval", 10)
        with self.active_lock:
            active = list(self.active_requests.values())
            active_ids = set(self.active_requests)
        # Self-clean the throttle map: the per-task `finally` pops _last_live_tee_ts,
        # but a heartbeat/`_tee_live_event` write can re-insert a key after that pop
        # (the snapshot/finally race the review panel flagged). Dropping entries for
        # turns no longer active bounds that residue to a single tick instead of
        # leaking until restart — keeping the leak-free invariant without holding the
        # lock across a (possibly remote) tee.
        for stale_id in [k for k in self._last_live_tee_ts if k not in active_ids]:
            self._last_live_tee_ts.pop(stale_id, None)
        for envelope in active:
            task_id = envelope.id
            # Un-tagged seats are first-class in the roster (the live tee falls back run_id->task_id),
            # so keep them fresh too — otherwise they'd show then go stale after the interval despite
            # being alive (the agy-blocked-on-model staleness this heartbeat exists to prevent).
            run_id = getattr(envelope, "run_id", None) or task_id
            last = self._last_live_tee_ts.get(task_id, 0.0)
            if now - last < interval:
                continue
            self._tee_live_event(
                run_id=run_id,
                task_id=task_id,
                seat_id=self.agent_id,
                orchestrator=getattr(envelope, "sender", None),
                event_type="turn_heartbeat",
                sent_at=iso_now(),
                data={"alive": True, "kind": "turn_heartbeat"},
            )
            # Advance the throttle on the same clock the caller passed, so the
            # next tick stays quiet until a full interval elapses (deterministic
            # regardless of the monotonic stamp _tee_live_event just wrote).
            self._last_live_tee_ts[task_id] = now

    def start_engine(self) -> None:
        # Warm the pool by acquiring + releasing one engine so startup
        # failures surface eagerly, matching the previous single-engine
        # behaviour. Subsequent engines spawn lazily on demand up to
        # max_parallel.
        engine = self.pool.acquire("__warmup__")
        if engine is None:
            raise EngineError("engine pool refused warmup acquire")
        self.engine_supports_resume = bool(getattr(engine, "supports_thread_resume", False))
        self.pool.release("__warmup__")

    def _drain_control_lane(self) -> None:
        while True:
            try:
                ctl_raw = self.redis.lpop_control(self.agent_id)
            except Exception as exc:  # noqa: BLE001 - control lane must not crash the daemon
                if self.stop_event.is_set():
                    logger.info(f"[bridge] control drain interrupted by shutdown ({exc})")
                else:
                    logger.error(f"[bridge-error] control-fail {exc}")
                return
            if ctl_raw is None:
                return
            if not self._is_control_envelope(ctl_raw):
                logger.error("[bridge-error] control-lane-non-control dropped")
                continue
            try:
                self.handle_raw(ctl_raw)
            except Exception as exc:  # noqa: BLE001 - mirror the request lane's guard (audit CDX-2)
                logger.error(f"[bridge-error] control-handle-failed {exc}")

    def inbox_loop(self) -> int:
        handled = 0
        self.recover_processing_envelopes()
        while not self.stop_event.is_set():
            try:
                self.redis.consumer_heartbeat(
                    self.agent_id,
                    self.owner_token,
                    # Must survive the loop's one legitimate long block: a COLD
                    # pool.acquire runs engine.start() synchronously on this
                    # thread (handle_raw), which can take up to the engine init
                    # budget — a TTL of exactly heartbeat_ttl read as
                    # consumer=dead during a healthy cold start.
                    max(
                        self.args.heartbeat_ttl,
                        engine_init_timeout() + self.args.heartbeat_interval,
                        int(self.args.control_poll_timeout * 3),
                    ),
                )
            except OwnershipLostError as exc:
                logger.error(f"[bridge-error] consumer-ownership-lost {exc}")
                self.stop_event.set()
                break
            except Exception as exc:  # noqa: BLE001 - readiness signal is fail-soft on Redis blips
                logger.error(f"[bridge-error] consumer-heartbeat-fail {exc}")
            self._drain_control_lane()
            if self.stop_event.is_set():
                break

            if not self.pool.wait_for_capacity(self.args.control_poll_timeout, self.stop_event):
                continue
            raw, parked = self.pop_inbox(self.args.control_poll_timeout)
            if raw is None:
                continue

            if parked and self.stop_event.is_set():
                logger.info(
                    f"[bridge] shutdown with parked envelope id={self.envelope_id_for_log(raw)} "
                    "(will recover on restart)",
                )
                break

            should_count = False
            worker_owns_processing = False
            try:
                if len(raw.encode()) > self.args.max_message_bytes:
                    logger.error(f"[bridge-error] message-too-large bytes={len(raw.encode())}")
                    continue

                logger.info(f"[inbox] {raw}")
                try:
                    worker_owns_processing = self.handle_raw(raw, processing_raw=raw if parked else None)
                except Exception as exc:  # noqa: BLE001 - parked envelopes must be acknowledged after handler failure
                    logger.error(f"[bridge-error] inbox-handle-failed {exc}")
                should_count = True
            finally:
                if parked and not worker_owns_processing:
                    try:
                        self.redis.remove_processing(self.agent_id, raw, owner_token=self.owner_token)
                    except Exception as exc:  # noqa: BLE001 - keep daemon alive; startup recovery can clean leftovers
                        logger.error(f"[bridge-error] processing-remove-failed {exc}")

            if should_count:
                handled += 1
                if self.args.once and handled >= 1:
                    self.join_active_thread()
                    break

        return 0

    def pop_inbox(self, timeout: float | None = None) -> tuple[str | None, bool]:
        timeout = self.args.blpop_timeout if timeout is None else timeout
        if self.reliable_inbox:
            try:
                raw = self.redis.blmove_to_processing(self.agent_id, timeout)
                if raw is not None:
                    # The envelope has not been parsed yet, so its per-task ceiling is unknowable.
                    # Bound the whole task by the daemon max and continuation budget instead. Under
                    # the default seven-day events TTL this is belt-and-suspenders; the successful
                    # remove still deletes the claim promptly.
                    self.redis.claim_processing(
                        self.agent_id, raw, self.owner_token,
                        ttl=max(
                            self.args.events_ttl,
                            int(self.args.turn_timeout_max) * (1 + self.max_continuation_turns),
                        ),
                    )
                return raw, True
            except ResponseError as exc:
                if self.is_blmove_unsupported(exc):
                    self.warn_blmove_unsupported()
                    self.reliable_inbox = False
                    return None, False
                logger.error(f"[bridge-error] inbox-fail {exc}")
                return None, False
            except Exception as exc:
                if self.stop_event.is_set():
                    logger.info(f"[bridge] inbox interrupted by shutdown ({exc})")
                    return None, False
                logger.error(f"[bridge-error] inbox-fail {exc}")
                return None, False

        try:
            return self.redis.blpop(self.agent_id, timeout), False
        except Exception as exc:
            if self.stop_event.is_set():
                logger.info(f"[bridge] inbox interrupted by shutdown ({exc})")
                return None, False
            logger.error(f"[bridge-error] inbox-fail {exc}")
            return None, False

    def recover_processing_envelopes(self) -> None:
        if not self.reliable_inbox:
            return
        while True:
            try:
                raw = self.redis.peek_processing(self.agent_id)
                if raw is None:
                    return
                # The claim key is sha256(body), computed in Python and passed into Lua; Redis
                # Lua only has sha1, so the script cannot derive the key itself.
                recovered = self.redis.recover_validated(self.agent_id, raw)
            except ResponseError as exc:
                if self.is_blmove_unsupported(exc):
                    self.warn_blmove_unsupported()
                    self.reliable_inbox = False
                    return
                logger.error(f"[bridge-error] processing-recovery-failed {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - startup can still fall through to live inbox polling
                logger.error(f"[bridge-error] processing-recovery-failed {exc}")
                return
            if recovered == 1:
                logger.info(f"[bridge] recovered in-flight envelope id={self.envelope_id_for_log(raw)}")

    @staticmethod
    def is_blmove_unsupported(exc: ResponseError) -> bool:
        text = " ".join(str(exc).lower().split())
        unknown_command = "unknown command" in text or "unknown redis command" in text
        return unknown_command and ("blmove" in text or "lmove" in text)

    def warn_blmove_unsupported(self) -> None:
        logger.warning("[bridge-warning] blmove-unsupported falling back to blpop (at-most-once delivery)")

    @staticmethod
    def envelope_id_for_log(raw: str) -> str:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return "unknown"
        if not isinstance(payload, dict):
            return "unknown"
        value = payload.get("id")
        return value if isinstance(value, str) and value else "unknown"

    @staticmethod
    def _is_control_envelope(raw: str) -> bool:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        return payload.get("kind") in {"steer", "cancel"}

    def handle_raw(self, raw: str, *, processing_raw: str | None = None) -> bool:
        try:
            envelope = Envelope.from_json(
                raw, ref_required=getattr(self, "task_ref_required", None)
            )
        except EnvelopeError as exc:
            # Log line byte-identical: wave1-soak-scan greps this exact form.
            logger.error(f"[bridge-error] envelope-invalid {exc}")
            header = getattr(exc, "header", None)
            if header is not None:
                self.send_refusal_reply(header, str(exc))
            return False

        if envelope.recipient != self.agent_id:
            logger.error(f"[bridge-error] envelope-wrong-recipient {envelope.recipient}")
            return False

        if envelope.sender == self.agent_id:
            logger.info(f"[bridge] drop-self-message {envelope.id}")
            return False

        if envelope.kind in {"steer", "cancel"}:
            # Mid-turn control deliberately bypasses the claim gate. Named residual:
            # design §9.3a — an admitted turn is steerable by any non-rejected sender.
            self.handle_control(envelope)
            return False

        if envelope.kind != "request":
            logger.info(f"[bridge] ignored-{envelope.kind} {envelope.id}")
            return False

        policy = self.sender_policies.get(envelope.sender, self.args.unknown_sender_policy)
        timeout_error = self.validate_requested_turn_timeout(envelope, policy)
        if timeout_error is not None:
            logger.error(f"[bridge-error] turn-timeout-refused {timeout_error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=timeout_error))
            return False

        if policy == "reject":
            logger.error(f"[bridge-error] sender-rejected {envelope.sender}")
            self.send_reply(
                envelope,
                TurnResult(ok=False, result="", error=f"sender rejected: {envelope.sender}"),
            )
            return False

        operation = envelope.payload.get("operation")
        is_worktree_lifecycle = operation in WORKTREE_LIFECYCLE_OPERATIONS

        if self.claim_gate_enabled and not is_worktree_lifecycle:
            if self.claim_resolver is None:
                # Construction/readiness should make this impossible. Refuse rather
                # than make a missing dependency an admit path if a partial fixture
                # or future refactor violates it.
                outcome = claim_gate.GateOutcome(
                    code=claim_gate.STORE_UNREACHABLE,
                    gaps=[
                        "claim resolver is unavailable",
                        "operator action: restart the seat",
                    ],
                )
            else:
                evaluation = claim_gate.evaluate(
                    envelope,
                    seat_id=self.agent_id,
                    resolver=self.claim_resolver,
                )
                outcome = evaluation.outcome
                # Persist one-pass resolution audit through existing task-event surface.
                # Audit is metadata only — never an admission credential.
                try:
                    self.push_task_event(
                        envelope,
                        "gate_evaluation",
                        {
                            "task_id": envelope.id,
                            "decision": evaluation.audit.get("decision"),
                            "audit": evaluation.audit,
                            "brief_ref": evaluation.audit.get("brief_ref"),
                        },
                    )
                except Exception:  # noqa: BLE001 - audit emit must not change admission
                    logger.exception(
                        f"[bridge-warning] gate-audit-emit-failed {envelope.id}"
                    )
            if outcome is not None:
                logger.error(f"[bridge-error] {outcome.code} {outcome.gaps}")
                self.send_reply(
                    envelope,
                    TurnResult(ok=False, result="", error=outcome.as_error()),
                )
                return False

        # Lifecycle ops: a durable task result is replayed (not silently dropped)
        # so crash-after-result / redelivery returns the original pair. In-process
        # duplicate tracking still suppresses double-execution of non-lifecycle work.
        if operation in WORKTREE_LIFECYCLE_OPERATIONS:
            get_str = getattr(self.redis, "get_str", None)
            if callable(get_str):
                durable_raw = get_str(self.redis_config.task_result_key(envelope.id))
                if durable_raw is not None:
                    logger.info(f"[bridge] lifecycle-result-replay {envelope.id}")
                    self._replay_durable_lifecycle_result(envelope, durable_raw)
                    return False
        if self.is_duplicate(envelope.id):
            logger.info(f"[bridge] duplicate-request {envelope.id}")
            return False

        budget_error = self.check_usage_budget()
        if budget_error is not None:
            logger.error(f"[bridge-error] usage-budget {budget_error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=budget_error))
            return False

        if self.handle_worktree_operation(envelope, policy):
            return False

        scored_request = self.is_scored_request(envelope)
        try:
            worktree_spec = self.parse_worktree_spec(envelope)
            cell_root = self.parse_cell_root(envelope)
        except WorktreeError as exc:
            logger.error(f"[bridge-error] worktree-invalid {exc}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=f"worktree spec invalid: {exc}"))
            return False

        operation = envelope.payload.get("operation")
        lease_id = envelope.payload.get("worktree_lease")
        if lease_id is not None and operation != "worktree_run":
            error = "worktree-lease-requires-operation-worktree_run"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        if operation is not None and operation != "worktree_run":
            error = "worktree-operation-invalid"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        if operation == "worktree_run" and (not isinstance(lease_id, str) or not lease_id):
            error = "worktree-run-requires-lease"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        if lease_id is not None and worktree_spec is not None:
            error = "worktree-spec-and-lease-incompatible"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        if scored_request and lease_id is not None:
            error = "scored request rejects worktree leases"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False

        lease_record: WorktreeLeaseRecord | None = None
        lease_worktree: Path | None = None
        if lease_id is not None:
            try:
                lease_record = self.validate_worktree_lease(envelope, policy=policy)
                lease_worktree = self.worktree_path(lease_record.worktree_name)
            except WorktreeLeaseError as exc:
                error = str(exc)
                logger.error(f"[bridge-error] {error}")
                self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
                return False

        thread_id = self.request_thread_id(envelope)
        continuation_worktree: Path | None = None
        continuation_lease: ContinuationWorkspaceLease | None = None
        try:
            continuation_worktree = self.agent_sdk_continuation_worktree(
                envelope,
                policy=policy,
                thread_id=thread_id,
                worktree_spec=worktree_spec,
                lease_worktree=lease_worktree,
            )
        except ContinuationWorkspaceError as exc:
            error = str(exc)
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False

        error = self.agent_sdk_worktree_error(policy, worktree_spec, continuation_worktree or lease_worktree)
        if error is not None:
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False

        if thread_id is not None and not self.engine_supports_resume and (
            worktree_spec is not None or lease_worktree is not None
        ):
            error = "thread-affinity-worktree-incompatible"
            logger.error(f"[bridge-error] {error} thread={thread_id}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False

        if scored_request and (worktree_spec is not None or lease_worktree is not None):
            error = "scored request rejects ordinary bridge worktree_spec and worktree leases"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        if scored_request and cell_root is None:
            error = "scored request requires a controller-provisioned cell root"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        if scored_request and (
            envelope.payload.get("fresh_context") is not True
            or envelope.payload.get("reasoning_effort") != "medium"
        ):
            error = "scored request requires fresh_context=true and reasoning_effort=medium"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False

        reserved_scored = False
        try:
            if scored_request:
                if not self.pool.reserve(envelope.id):
                    engine = None
                else:
                    engine = None
                    reserved_scored = True
            else:
                engine = self.pool.acquire(
                    envelope.id,
                    thread_id=thread_id if thread_id is not None and not self.engine_supports_resume else None,
                )
        except AffinityMissError as exc:
            error = f"thread-affinity-miss thread={exc.thread_id}"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        except AffinityBusyError as exc:
            error = f"thread-affinity-busy task={exc.owning_task_id}"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        except AffinityAmbiguousError as exc:
            error = f"thread-affinity-ambiguous thread={exc.thread_id}"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        except EngineError as exc:
            error = f"engine-start-failed: {exc}"
            logger.error(f"[bridge-error] {error}")
            self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
            return False
        if engine is None and not reserved_scored:
            active_ids = self.pool.active_task_ids()
            busy_summary = ",".join(active_ids) if active_ids else "unknown"
            logger.error(f"[bridge-error] busy active_requests={busy_summary}")
            self.send_reply(
                envelope,
                TurnResult(
                    ok=False,
                    result="",
                    error=f"bridge busy with task {busy_summary}",
                ),
            )
            return False

        if continuation_worktree is not None and thread_id is not None:
            try:
                continuation_lease = self.agent_sdk_continuation_store().acquire(thread_id)
            except ContinuationWorkspaceError as exc:
                error = str(exc)
                logger.error(f"[bridge-error] {error}")
                self.pool.release(envelope.id)
                self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
                return False

        worktree_lease_lock: WorktreeLeaseLock | None = None
        if lease_record is not None:
            try:
                worktree_lease_lock = self.worktree_lease_store.acquire(lease_record.lease_id)
            except WorktreeLeaseError as exc:
                error = str(exc)
                logger.error(f"[bridge-error] {error}")
                if continuation_lease is not None:
                    continuation_lease.release()
                self.pool.release(envelope.id)
                self.send_reply(envelope, TurnResult(ok=False, result="", error=error))
                return False

        worker = threading.Thread(
            target=self.process_request,
            args=(envelope, policy, engine),
            kwargs={
                "worktree_spec": worktree_spec,
                "cell_root": cell_root,
                "continuation_worktree": continuation_worktree,
                "continuation_lease": continuation_lease,
                "worktree_lease_record": lease_record,
                "worktree_lease_lock": worktree_lease_lock,
                "processing_raw": processing_raw,
            },
            daemon=True,
        )
        with self.active_lock:
            self.active_requests[envelope.id] = envelope
            self.active_threads[envelope.id] = worker
        try:
            worker.start()
        except Exception:
            # The lease is normally released by process_request's finally, but
            # that only runs once the worker thread is actually started.
            if continuation_lease is not None:
                continuation_lease.release()
            if worktree_lease_lock is not None:
                worktree_lease_lock.release()
            with self.active_lock:
                self.active_requests.pop(envelope.id, None)
                self.active_threads.pop(envelope.id, None)
            self.pool.release(envelope.id)
            raise
        return processing_raw is not None

    def validate_requested_turn_timeout(self, envelope: Envelope, policy: str) -> str | None:
        if "turn_timeout" not in envelope.payload:
            return None
        value = envelope.payload["turn_timeout"]
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if policy != "trusted":
            return f"turn_timeout requires trusted sender policy; resolved policy={policy}"
        if type(value) is not int or value <= 0:
            return f"invalid turn_timeout value {rendered}; expected a positive integer"
        if value > self.args.turn_timeout_max:
            return (
                f"requested turn_timeout {value} exceeds seat --turn-timeout-max "
                f"{self.args.turn_timeout_max}"
            )
        return None

    def effective_task_turn_timeout(self, envelope: Envelope) -> int:
        value = envelope.payload.get("turn_timeout")
        return value if type(value) is int and value > 0 else self.args.turn_timeout

    @staticmethod
    def timeout_echo_fields(request: Envelope, *, served: bool = True) -> dict[str, Any]:
        if "turn_timeout" not in request.payload:
            return {}
        value = request.payload["turn_timeout"]
        fields = {"turn_timeout_requested": value}
        if served:
            fields["turn_timeout_served"] = value
        return fields

    def parse_worktree_spec(self, envelope: Envelope) -> dict[str, str] | None:
        """Validate and normalise an optional ``payload.worktree`` spec.

        Returns ``None`` when absent (the default — behaviour unchanged). Raises
        ``WorktreeError`` on a malformed spec so the dispatch is rejected before
        any engine work. ``name`` is strictly charset-checked (it becomes a path
        segment) and ``base_ref`` may not look like an option.
        """
        spec = envelope.payload.get("worktree")
        if spec is None:
            return None
        if not isinstance(spec, dict):
            raise WorktreeError("worktree must be an object")
        name = spec.get("name")
        if not isinstance(name, str) or ".." in name or not WORKTREE_NAME_PATTERN.match(name):
            raise WorktreeError(f"invalid worktree name: {name!r}")
        base_ref = spec.get("base_ref") or "HEAD"
        if not isinstance(base_ref, str) or not base_ref or base_ref.startswith("-") or any(c.isspace() for c in base_ref):
            raise WorktreeError(f"invalid base_ref: {base_ref!r}")
        cleanup = spec.get("cleanup", "keep")
        if cleanup not in ("keep", "auto"):
            raise WorktreeError(f"invalid cleanup policy: {cleanup!r} (want keep|auto)")
        return {"name": name, "base_ref": base_ref, "cleanup": cleanup}

    def parse_cell_root(self, envelope: Envelope) -> Path | None:
        """Validate a controller-provisioned scored cell root without resolving a new worktree."""

        value = envelope.payload.get("cell_root")
        if value is None:
            return None
        if not isinstance(value, str) or not os.path.isabs(value):
            raise WorktreeError("cell_root must be an absolute path")
        path = Path(value)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise WorktreeError("cell_root is unavailable") from exc
        mac_var_alias = path == Path("/var") or (path.is_relative_to(Path("/var")) and not path.is_symlink())
        if (path.is_symlink() and not mac_var_alias) or not path.is_dir():
            raise WorktreeError("cell_root must be a canonical directory")
        base_value = getattr(self.args, "scored_cell_root_base", None) or os.environ.get(
            "IMPLBENCH_CELL_ROOT_BASE", "/Users/Shared/arb-implbench"
        )
        try:
            base = Path(base_value).resolve(strict=False)
            resolved.relative_to(base)
        except (OSError, ValueError) as exc:
            raise WorktreeError("cell_root is outside the controller cell-root base") from exc
        return resolved

    def agent_sdk_worktree_error(
        self,
        policy: str,
        worktree_spec: dict[str, str] | None,
        continuation_worktree: Path | None,
    ) -> str | None:
        if self.engine_name != "agent-sdk":
            return None
        if getattr(self.args, "agent_sdk_oneshot", False):
            return None
        if policy != "trusted":
            return None
        if worktree_spec is not None or continuation_worktree is not None:
            return None
        return "agent-sdk trusted stateful requests require payload.worktree"

    def agent_sdk_continuation_store(self) -> ContinuationWorkspaceStore:
        root = Path(getattr(self.args, "agent_sdk_session_root", None) or default_agent_sdk_session_root())
        return ContinuationWorkspaceStore(root, self.agent_id)

    def agent_sdk_continuation_worktree(
        self,
        envelope: Envelope,
        *,
        policy: str,
        thread_id: str | None,
        worktree_spec: dict[str, str] | None,
        lease_worktree: Path | None = None,
    ) -> Path | None:
        """Resolve a trusted stateful SDK continuation to its recorded worktree.

        A continuation may not supply a worktree spec: accepting one would let a
        sender point a known thread at a different project key or turn a normal
        ``git worktree add`` collision into path reuse.  The bridge is the only
        writer of this mapping, after a successful persistent-worktree turn.
        """
        if (
            self.engine_name != "agent-sdk"
            or getattr(self.args, "agent_sdk_oneshot", False)
            or policy != "trusted"
            or thread_id is None
        ):
            return None
        if worktree_spec is not None:
            raise ContinuationWorkspaceError("continuation-worktree-must-be-omitted")
        record = self.agent_sdk_continuation_store().load(thread_id)
        if record is None:
            raise ContinuationWorkspaceError("continuation-worktree-unavailable")
        if record.sender != envelope.sender:
            raise ContinuationWorkspaceError("continuation-worktree-owner-mismatch")
        if WORKTREE_NAME_PATTERN.fullmatch(record.worktree_name) is None:
            raise ContinuationWorkspaceError("continuation-workspace-corrupt")
        path = self.worktree_path(record.worktree_name)
        if not self.is_registered_worktree(path):
            raise ContinuationWorkspaceError("continuation-worktree-unavailable")
        if lease_worktree is not None and path.resolve() != lease_worktree.resolve():
            raise ContinuationWorkspaceError("continuation-worktree-lease-mismatch")
        return path

    def is_registered_worktree(self, path: Path) -> bool:
        """Return whether ``path`` is a live worktree registered under this repo."""
        try:
            candidate = path.resolve(strict=True)
            root = (self.workdir / ".claude" / "worktrees").resolve(strict=True)
            candidate.relative_to(root)
        except (FileNotFoundError, ValueError):
            return False
        result = run_git_op(
            ["git", "-C", str(self.workdir), "worktree", "list", "--porcelain"],
        )
        if result.returncode != 0:
            return False
        return any(
            line.startswith("worktree ") and Path(line.removeprefix("worktree ")).resolve() == candidate
            for line in result.stdout.splitlines()
        )

    def record_agent_sdk_continuation_workspace(
        self,
        envelope: Envelope,
        result: TurnResult,
        *,
        worktree_spec: dict[str, str] | None,
        worktree_lease_record: WorktreeLeaseRecord | None = None,
    ) -> TurnResult:
        """Persist continuation routing only for successful kept worktrees."""
        if (
            not result.ok
            or self.engine_name != "agent-sdk"
            or getattr(self.args, "agent_sdk_oneshot", False)
            or (worktree_spec is None and worktree_lease_record is None)
            or (worktree_spec is not None and worktree_spec.get("cleanup") != "keep")
            or result.thread_id is None
        ):
            return result
        try:
            self.agent_sdk_continuation_store().record(
                thread_id=result.thread_id,
                sender=envelope.sender,
                worktree_name=(
                    worktree_lease_record.worktree_name
                    if worktree_lease_record is not None else worktree_spec["name"]
                ),
            )
        except ContinuationWorkspaceError as exc:
            logger.error(f"[bridge-error] continuation-workspace-record-failed {envelope.id} {exc}")
            return replace(result, ok=False, error=f"continuation-workspace-record-failed: {exc}")
        return result

    def worktree_path(self, name: str) -> Path:
        return self.workdir / ".claude" / "worktrees" / name

    def _prepare_exempt_worktree_or_raise(self, path: Path) -> None:
        """Configure push-less origin and prove denial for exempt arms.

        Gated lane is a no-op. Every exempt failure raises WorktreeLeaseError
        with the exact catalog code so the arm path removes the unleased
        worktree and never calls the row writer.
        """
        if self.worktree_lane != "exempt":
            return
        from .exempt_git import (
            ExemptGitError,
            prepare_exempt_worktree,
            supervisor_exempt_settings,
        )

        ssh_command, fingerprint, ledger_path = supervisor_exempt_settings()
        try:
            prepare_exempt_worktree(
                path,
                lane=self.worktree_lane,
                ssh_command=ssh_command,
                expected_fingerprint=fingerprint,
                ledger_path=ledger_path,
            )
        except ExemptGitError as exc:
            logger.error(
                "[exempt-git] arm refused code=%s path=%s detail=%s",
                exc.code,
                path,
                exc.detail,
            )
            raise WorktreeLeaseError(exc.code) from exc
        except Exception as exc:
            # Any non-catalog failure must still produce a coded arm reply —
            # never fall through to inbox-handle-failed silence (R7).
            logger.error(
                "[exempt-git] arm internal error path=%s err=%s",
                path,
                exc,
            )
            raise WorktreeLeaseError("exempt-prep-internal-error") from exc

    def _rev_parse_commit(self, ref: str) -> str | None:
        result = run_git_op(
            ["git", "-C", str(self.workdir), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def resolve_base_ref_oid(self, base_ref: str) -> str:
        """Resolve a worktree base ref to a commit OID, fetching once if needed.

        A seat workdir is a long-lived DEPLOYED clone that nothing routinely
        fetches. On 2026-08-08 a dispatch died with

            git worktree add failed: fatal: invalid reference: feat/muse-runner-spec

        The branch existed on origin. It did not exist locally, because the
        seat's clone had last fetched two days earlier, and `git worktree add`
        does not fall back to a remote-tracking ref for a bare branch name. So
        this is not about one branch: ANY dispatch naming a branch created since
        the seat's last fetch fails the same way.

        Order matters. A LOCAL ref wins outright, so this can never shadow a
        branch the seat is deliberately holding. Only when nothing resolves do we
        fetch -- so the common case pays no network cost -- and only then do we
        try the remote form.
        """
        oid = self._rev_parse_commit(base_ref)
        if oid:
            return oid

        logger.info("[worktree] base_ref %s unresolved; fetching origin", base_ref)
        run_git_op(
            ["git", "-C", str(self.workdir), "fetch", "--quiet", "origin"],
            timeout=300.0,
        )
        oid = self._rev_parse_commit(base_ref)
        if oid:
            return oid
        oid = self._rev_parse_commit(f"origin/{base_ref}")
        if oid:
            logger.info("[worktree] base_ref %s resolved via origin/%s", base_ref, base_ref)
            return oid

        # The old message was `fatal: invalid reference: <ref>`, which reads as
        # "you named the wrong branch" and sends the reader to the dispatcher.
        # Staleness is the likelier cause, so say what was tried.
        raise WorktreeError(
            f"base_ref {base_ref!r} does not resolve in {self.workdir}: tried it directly, "
            f"then `git fetch origin`, then origin/{base_ref}. The seat's clone is most "
            f"likely stale, or the branch was never pushed to origin."
        )

    def create_worktree(self, spec: dict[str, str]) -> Path:
        path = self.worktree_path(spec["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        # --detach: check out base_ref's commit in a DETACHED head. Avoids the
        # "branch already checked out" refusal when base_ref is (or defaults to)
        # a branch the main workdir holds. If the agent commits, the caller can
        # branch from the worktree afterwards.
        # Resolve to an OID first. Besides fixing the stale-clone case, it pins
        # creation to the commit we log: a moving branch cannot make the created
        # worktree differ from what this line reports.
        base_oid = self.resolve_base_ref_oid(spec["base_ref"])
        result = run_git_op(
            ["git", "-C", str(self.workdir), "worktree", "add", "--detach", str(path), base_oid],
            timeout=300.0,
        )
        if result.returncode != 0:
            raise WorktreeError(f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}")
        logger.info(f"[worktree] created {path} from {spec['base_ref']}")
        self._link_base_venv(path)
        return path

    def _link_base_venv(self, worktree: Path) -> None:
        """Mirror the base checkout's .venv into a fresh worktree, copy-on-write.

        A worktree checkout omits the (untracked) venv, so a seat running plain
        ``pytest`` there hits ModuleNotFoundError and silently degrades to
        source-tracing — losing the panel's execution-verification axis. The
        mirror is a REAL ``.venv`` directory (the conventional ignore pattern
        ``.venv/`` is directory-only and would not match a plain symlink, which
        would then bounce every turn off the completion gate) whose entries are
        symlinks into the base venv EXCEPT the ``lib*`` chain down to each
        site-packages, which is real directories so that:

        - editable-install hooks (any small .pth/.py whose text names the base
          workdir — setuptools/uv ``__editable__*`` files) are COPIED with the
          base path rewritten to the worktree, so the mirrored interpreter
          imports the WORKTREE's checkout of the base repo's own package, not
          the base copy (panel finding F2);
        - pip installs land in the worktree's real site-packages, not the
          shared base venv (F2 write side). Residual, documented: upgrading or
          uninstalling a SYMLINKED dist through the mirror can still reach into
          the base venv — the mirror is for running tests, not managing
          packages.

        Resolution honesty (F9): ``bin/python`` and every third-party package
        still RESOLVE TO THE BASE VENV through symlinks; only the base repo's
        own editable source is worktree-local. Gitignored-only, judged against
        the WORKTREE's own checked-out ignore rules (its base_ref, not the base
        tip — F4). POSIX symlink semantics assumed (F11); on failure the
        partial mirror is rolled back and the turn proceeds venv-less (F3) —
        degraded, not broken.
        """
        venv = self.workdir / ".venv"
        if not venv.is_dir():
            return
        target = worktree / ".venv"
        if target.exists() or target.is_symlink():
            return
        # Create the target dir FIRST so the directory-only ``.venv/`` pattern
        # can match, then ask the WORKTREE's git (its own checked-out
        # .gitignore) — not the base tip's.
        try:
            target.mkdir()
        except OSError as exc:
            logger.error(f"[bridge-error] worktree-venv-link-failed {worktree} {exc}")
            return
        ignored = subprocess.run(
            ["git", "-C", str(worktree), "check-ignore", "-q", ".venv"],
            capture_output=True,
            check=False,
        )
        if ignored.returncode == 1:
            target.rmdir()
            logger.info(f"[worktree] .venv not gitignored at {worktree}'s ref; not linking")
            return
        if ignored.returncode != 0:
            target.rmdir()
            stderr = ignored.stderr.decode(errors="replace").strip()
            logger.error(
                f"[bridge-error] worktree-venv-ignore-check-failed rc={ignored.returncode} {worktree} {stderr}"
            )
            return
        try:
            for entry in venv.iterdir():
                name = entry.name
                if name == "bin" and entry.is_dir() and not entry.is_symlink():
                    # A REAL bin dir with symlinked entries, like a normal venv:
                    # CPython finds pyvenv.cfg relative to the UNRESOLVED argv0
                    # only when the executable itself is the symlink — a
                    # symlinked bin DIRECTORY resolves the prefix back to the
                    # BASE venv and defeats the worktree-local site-packages.
                    (target / name).mkdir()
                    for script in entry.iterdir():
                        (target / name / script.name).symlink_to(script)
                elif name in ("lib", "lib64") and entry.is_dir() and not entry.is_symlink():
                    # Editable hooks store whatever path form pip saw; on macOS
                    # /var|/tmp|/etc alias /private/var|... — match both spellings.
                    workdir_forms = {str(self.workdir), str(self.workdir.resolve()), os.path.realpath(self.workdir)}
                    for form in list(workdir_forms):
                        if form.startswith("/private/"):
                            workdir_forms.add(form[len("/private"):])
                        elif form.startswith(("/var/", "/tmp/", "/etc/")):
                            workdir_forms.add("/private" + form)
                    self._mirror_venv_lib(entry, target / name, workdir_forms, str(worktree))
                elif entry.is_symlink():
                    # Preserve relative links (e.g. Linux lib64 -> lib) verbatim
                    # so they resolve inside the MIRROR, not back into the base.
                    (target / name).symlink_to(os.readlink(entry))
                else:
                    (target / name).symlink_to(entry)
        except OSError as exc:
            logger.error(f"[bridge-error] worktree-venv-link-failed {worktree} {exc}")
            shutil.rmtree(target, ignore_errors=True)
            return
        logger.info(f"[worktree] linked base .venv into {worktree} (copy-on-write editable hooks)")

    def _mirror_venv_lib(self, base_lib: Path, target_lib: Path, workdir_forms: set[str], worktree_s: str) -> None:
        """Real-dir mirror of lib/pythonX.Y/site-packages; symlink everything else."""
        target_lib.mkdir()
        for child in base_lib.iterdir():
            tchild = target_lib / child.name
            if child.is_dir() and not child.is_symlink():
                tchild.mkdir()
                for entry in child.iterdir():
                    if entry.name == "site-packages" and entry.is_dir() and not entry.is_symlink():
                        self._mirror_site_packages(entry, tchild / entry.name, workdir_forms, worktree_s)
                    else:
                        (tchild / entry.name).symlink_to(entry)
            else:
                tchild.symlink_to(child)

    # Editable hooks are small text files; anything bigger is not one.
    _EDITABLE_HOOK_MAX_BYTES = 1_000_000

    def _mirror_site_packages(self, base_sp: Path, target_sp: Path, workdir_forms: set[str], worktree_s: str) -> None:
        """Symlink site-packages entries; copy-and-rewrite base-workdir-referencing hooks."""
        target_sp.mkdir()
        for entry in base_sp.iterdir():
            tentry = target_sp / entry.name
            if (
                entry.is_file()
                and not entry.is_symlink()
                and entry.suffix in (".pth", ".py")
                and entry.stat().st_size <= self._EDITABLE_HOOK_MAX_BYTES
            ):
                try:
                    text = entry.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    tentry.symlink_to(entry)
                    continue
                # Single-pass alternation, longest form first: sequential
                # str.replace would re-scan its own output, and the worktree
                # path CONTAINS the base-workdir forms as substrings — a second
                # pass would double-rewrite into garbage (and set iteration
                # order made that a per-run coin flip).
                pattern = re.compile(
                    "|".join(re.escape(f) for f in sorted(workdir_forms, key=len, reverse=True))
                )
                rewritten = pattern.sub(worktree_s.replace("\\", r"\\"), text)
                if rewritten != text:
                    tentry.write_text(rewritten, encoding="utf-8")
                    continue
            tentry.symlink_to(entry)

    def remove_worktree(self, path: Path) -> None:
        result = run_git_op(
            ["git", "-C", str(self.workdir), "worktree", "remove", "--force", str(path)],
            timeout=300.0,
        )
        if result.returncode != 0:
            logger.error(f"[bridge-error] worktree-remove-failed {path} {result.stderr.strip()}")
        else:
            logger.info(f"[worktree] removed {path}")

    def canonical_repo_identity(self) -> str:
        result = run_git_op(
            ["git", "-C", str(self.workdir), "rev-parse", "--show-toplevel"],
        )
        if result.returncode != 0:
            raise WorktreeLeaseError("worktree-lease-repo-unavailable")
        return str(Path(result.stdout.strip()).resolve())

    def resolve_base_oid(self, base_ref: str) -> str:
        """Lease-path resolution. Delegates so the lease path gets the same
        fetch-and-fallback as the direct path; a stale clone failed here too,
        just with a coded error instead of git's message."""
        try:
            return self.resolve_base_ref_oid(base_ref)
        except WorktreeError as exc:
            logger.warning("[worktree] lease base_ref unresolved: %s", exc)
            raise WorktreeLeaseError("worktree-lease-base-ref-invalid") from exc

    def reconcile_worktree_leases(self) -> list[tuple[str, str]]:
        """Converge filesystem leases (and, when configured, lane-writer rows).

        Missing-row reclaim is safe only after the per-lease lock is acquired.
        Lock-busy means an arm/release is in-flight and is non-actionable for
        this pass — never evidence of an orphan. Heartbeat, startup, and
        operation-triggered reconciliation all share this one implementation.
        """
        records = self.worktree_lease_store.records()
        actions: list[tuple[str, str]] = []
        if records:
            repo_identity = (
                self.canonical_repo_identity()
                if any(record.state == "armed" for record in records)
                else ""
            )
            actions = self.worktree_lease_store.reconcile(
                repo_identity=repo_identity,
                is_live=lambda record: self.is_registered_worktree(
                    self.worktree_path(record.worktree_name)
                ),
                remove=self.reclaim_leased_worktree,
                tombstone_ttl=self.args.result_ttl,
            )
        if self.lane_writer is not None:
            actions.extend(self._reconcile_lane_writer_rows())
        for lease_id, action in actions:
            logger.info(f"[worktree-lease] reconcile lease={lease_id} action={action}")
        return actions

    def _reconcile_lane_writer_rows(self) -> list[tuple[str, str]]:
        """Two-record convergence for this consumer's lane-writer rows.

        Store unavailability propagates — never defaults lane. Destructive
        missing-row actions first acquire the per-lease lock; busy is skipped.
        """
        assert self.lane_writer is not None
        from .lane_writer import LaneStoreUnreachable

        try:
            rows = self.lane_writer.rows()
        except LaneStoreUnreachable:
            raise
        except Exception as exc:  # noqa: BLE001 - any store failure fails closed
            raise LaneStoreUnreachable(str(exc)) from exc

        row_by_id = {
            row["lease_id"]: row
            for row in rows
            if isinstance(row.get("lease_id"), str) and row["lease_id"]
        }
        records = self.worktree_lease_store.records()
        record_by_id = {record.lease_id: record for record in records}
        actions: list[tuple[str, str]] = []

        for record in records:
            row = row_by_id.get(record.lease_id)
            if record.state == "tombstoned":
                if row is None:
                    continue
                try:
                    lock = self.worktree_lease_store.acquire(record.lease_id)
                except WorktreeLeaseError as exc:
                    if str(exc) == "worktree-lease-busy":
                        continue
                    raise
                try:
                    if self.lane_writer.retire(record.lease_id):
                        actions.append((record.lease_id, "tombstone-row-retired"))
                finally:
                    lock.release()
                continue

            if record.state != "armed":
                continue

            # Armed FS + missing or mismatched row ⇒ reclaim under lock.
            mismatch_reason: str | None = None
            if row is None:
                mismatch_reason = "lane-row-missing"
            elif row.get("lane") != record.lane or row.get("armed_by") != self.agent_id:
                mismatch_reason = "lane-mismatch"
            if mismatch_reason is None:
                continue

            try:
                lock = self.worktree_lease_store.acquire(record.lease_id)
            except WorktreeLeaseError as exc:
                if str(exc) == "worktree-lease-busy":
                    # In-flight arm/release — not orphan evidence.
                    continue
                raise
            try:
                latest = self.worktree_lease_store.load(record.lease_id)
                if latest is None or latest.state != "armed":
                    continue
                # Re-check row under lock in case arm completed while we waited.
                try:
                    live_rows = {
                        r["lease_id"]: r
                        for r in self.lane_writer.rows()
                        if isinstance(r.get("lease_id"), str)
                    }
                except Exception as exc:  # noqa: BLE001
                    raise LaneStoreUnreachable(str(exc)) from exc
                live = live_rows.get(latest.lease_id)
                if live is not None and live.get("lane") == latest.lane and live.get(
                    "armed_by"
                ) == self.agent_id:
                    continue
                self.reclaim_leased_worktree(latest)
                self.worktree_lease_store.tombstone(latest, mismatch_reason)
                if live is not None:
                    try:
                        self.lane_writer.retire(latest.lease_id)
                    except Exception:
                        pass
                actions.append((latest.lease_id, mismatch_reason))
            finally:
                lock.release()

        # Row + no armed filesystem record ⇒ delete the orphan row.
        for lease_id, row in row_by_id.items():
            record = record_by_id.get(lease_id)
            if record is not None and record.state == "armed":
                continue
            if record is not None and record.state == "tombstoned":
                continue  # handled above
            try:
                lock = self.worktree_lease_store.acquire(lease_id)
            except WorktreeLeaseError as exc:
                if str(exc) == "worktree-lease-busy":
                    continue
                raise
            try:
                if self.lane_writer.retire(lease_id):
                    actions.append((lease_id, "orphan-row-retired"))
            finally:
                lock.release()

        return actions

    def reclaim_leased_worktree(self, record: WorktreeLeaseRecord) -> None:
        path = self.worktree_path(record.worktree_name)
        self.remove_worktree(path)
        if self.is_registered_worktree(path):
            raise WorktreeLeaseError("worktree-lease-reclaim-failed")

    def _operation_result(
        self, envelope: Envelope, result: TurnResult, *, fields: dict[str, Any] | None = None,
    ) -> None:
        extra = fields or {}
        summary = summarize(result.result or result.error or "")
        self.update_task_status(
            envelope, state="completed" if result.ok else "failed", phase="finished",
            last_summary=summary, ok=result.ok, error=result.error,
        )
        self.write_task_result(envelope, result, summary, extra_fields=extra)
        self.send_reply(envelope, result, extra_fields=extra)

    def _replay_durable_lifecycle_result(self, envelope: Envelope, durable_raw: str) -> None:
        """Re-send a previously durable lifecycle result without re-executing."""
        try:
            payload = json.loads(durable_raw)
        except json.JSONDecodeError:
            logger.error(f"[bridge-error] lifecycle-result-corrupt {envelope.id}")
            return
        if not isinstance(payload, dict):
            return
        ok = bool(payload.get("ok"))
        result = TurnResult(
            ok=ok,
            result=str(payload.get("result") or ""),
            error=payload.get("error") if not ok else None,
        )
        # Echo the original success fields (lease_id/path/…) so redelivery is
        # byte-stable from the caller's vantage.
        extra = {
            key: payload[key]
            for key in (
                "lease_id",
                "path",
                "expires_at",
                "base_oid",
                "thread_resume",
            )
            if key in payload
        }
        self.send_reply(envelope, result, extra_fields=extra or None)

    def _arm_success_fields(self, record: WorktreeLeaseRecord) -> dict[str, Any]:
        path = self.worktree_path(record.worktree_name)
        return {
            "lease_id": record.lease_id,
            "path": str(path.resolve()),
            "expires_at": record.expires_at,
            "base_oid": record.base_oid,
            # Advertise the seat's ACTUAL resume capability so drivers
            # pick bounce mode from ground truth instead of a pinned
            # per-family table (which wrongly forced stateless bounces
            # on non-oneshot asdk seats, e.g. asdk-bridge-dev-haiku45 —
            # panel-pisdk-rebaseline finding, 2026-07-23).
            "thread_resume": self.engine_supports_resume,
        }

    def _replay_arm_by_request_id(self, envelope: Envelope) -> bool:
        """If this envelope id already armed (or closed) a lease, replay it.

        Returns True when a replay reply was sent (caller must not mint again).
        """
        prior = self.worktree_lease_store.find_by_arm_request_id(envelope.id)
        if prior is None:
            return False
        if prior.state == "tombstoned":
            # Closed partial — never mint a replacement for the same request id.
            reason = prior.tombstone_reason or "worktree-lease-unavailable"
            # Lane-row failures (arm compensation, reconcile missing/mismatch) all
            # mean the two-record arm did not complete; surface the store-failed
            # refusal so redelivery is deterministic.
            if reason in {
                "lane-row-arm-failed",
                "lane-row-missing",
                "lane-mismatch",
            } or reason.startswith("lane-"):
                error = "worktree-lane-arm-store-failed"
            else:
                error = f"worktree-lease-unavailable:{reason}"
            self._operation_result(
                envelope, TurnResult(ok=False, result="", error=error)
            )
            return True
        if prior.state != "armed":
            return False
        # Valid FS record: when a lane writer is active, require a matching row.
        if self.lane_writer is not None:
            try:
                rows = self.lane_writer.rows()
            except Exception as exc:  # noqa: BLE001
                from .lane_writer import LaneStoreUnreachable

                raise LaneStoreUnreachable(str(exc)) from exc
            match = next(
                (
                    row
                    for row in rows
                    if row.get("lease_id") == prior.lease_id
                    and row.get("lane") == prior.lane
                    and row.get("armed_by") == self.agent_id
                ),
                None,
            )
            if match is None:
                # Partial without row — closed failure path, do not mint another.
                self._operation_result(
                    envelope,
                    TurnResult(
                        ok=False, result="", error="worktree-lane-arm-store-failed"
                    ),
                )
                return True
        if not self.is_registered_worktree(self.worktree_path(prior.worktree_name)):
            self._operation_result(
                envelope,
                TurnResult(ok=False, result="", error="worktree-lease-unavailable"),
            )
            return True
        fields = self._arm_success_fields(prior)
        self._operation_result(
            envelope, TurnResult(ok=True, result="worktree armed"), fields=fields
        )
        return True

    def _arm_lane_row_or_compensate(self, record: WorktreeLeaseRecord) -> None:
        """Write the lane-writer row; on failure reclaim + tombstone the FS record."""
        assert self.lane_writer is not None
        from .lane_writer import LaneStoreUnreachable

        try:
            row = self.lane_writer.arm(record.lease_id)
        except Exception as arm_exc:
            reclaim_err: Exception | None = None
            tombstone_err: Exception | None = None
            try:
                self.reclaim_leased_worktree(record)
            except Exception as exc:  # noqa: BLE001 - both failures compose
                reclaim_err = exc
            try:
                self.worktree_lease_store.tombstone(record, "lane-row-arm-failed")
            except Exception as exc:  # noqa: BLE001
                tombstone_err = exc
            if reclaim_err is not None or tombstone_err is not None:
                parts = [f"arm={arm_exc}"]
                if reclaim_err is not None:
                    parts.append(f"reclaim={reclaim_err}")
                if tombstone_err is not None:
                    parts.append(f"tombstone={tombstone_err}")
                raise WorktreeLeaseError(
                    "worktree-lane-arm-store-failed;compensation-failed:"
                    + ";".join(parts)
                ) from arm_exc
            raise WorktreeLeaseError("worktree-lane-arm-store-failed") from arm_exc
        # Function-created row must match bound consumer/lane.
        if (
            not isinstance(row, dict)
            or row.get("lease_id") != record.lease_id
            or row.get("armed_by") != self.agent_id
            or row.get("lane") != self.worktree_lane
        ):
            # Treat as arm-store failure and compensate the just-created record.
            try:
                self.lane_writer.retire(record.lease_id)
            except Exception:
                pass
            reclaim_err = None
            tombstone_err = None
            try:
                self.reclaim_leased_worktree(record)
            except Exception as exc:  # noqa: BLE001
                reclaim_err = exc
            try:
                self.worktree_lease_store.tombstone(record, "lane-row-arm-failed")
            except Exception as exc:  # noqa: BLE001
                tombstone_err = exc
            if reclaim_err or tombstone_err:
                raise WorktreeLeaseError(
                    "worktree-lane-arm-store-failed;compensation-failed:"
                    f"arm=row-identity-mismatch;reclaim={reclaim_err};"
                    f"tombstone={tombstone_err}"
                )
            raise WorktreeLeaseError("worktree-lane-arm-store-failed")

    def handle_worktree_operation(self, envelope: Envelope, policy: str) -> bool:
        from .lane_writer import LaneStoreUnreachable

        operation = envelope.payload.get("operation")
        if operation not in WORKTREE_LIFECYCLE_OPERATIONS:
            return False
        if policy != "trusted":
            self._operation_result(
                envelope, TurnResult(ok=False, result="", error="worktree lease operations require trusted sender policy")
            )
            return True
        if self.is_scored_request(envelope):
            self._operation_result(
                envelope, TurnResult(ok=False, result="", error="scored request rejects worktree leases")
            )
            return True
        try:
            self.reconcile_worktree_leases()
            if operation == "worktree_arm":
                unexpected = set(envelope.payload) - {"operation", "worktree", "lease_ttl", "run_id"}
                if unexpected:
                    raise WorktreeLeaseError("worktree-arm-invalid-schema")
                # Request-id replay before any mint: valid pair → same success;
                # tombstoned partial → closed refusal; never a second lease.
                if self._replay_arm_by_request_id(envelope):
                    return True
                spec = self.parse_worktree_spec(envelope)
                if spec is None or spec.get("cleanup") != "keep":
                    raise WorktreeLeaseError("worktree-arm-requires-keep-worktree")
                ttl = envelope.payload.get("lease_ttl", self.args.worktree_lease_ttl)
                if type(ttl) is not int or ttl <= 0 or ttl > self.args.worktree_lease_ttl_max:
                    raise WorktreeLeaseError("worktree-lease-ttl-invalid")
                total, per_sender = self.worktree_lease_store.active_counts()
                if total >= self.args.max_armed_worktrees:
                    raise WorktreeLeaseError("worktree-armed-global-quota")
                if per_sender.get(envelope.sender, 0) >= self.args.max_armed_worktrees_per_sender:
                    raise WorktreeLeaseError("worktree-armed-sender-quota")
                repo_identity = self.canonical_repo_identity()
                base_oid = self.resolve_base_oid(spec["base_ref"])
                # mint → acquire that lease-id's lock → worktree → FS record
                # (arm_request_id) → arm_lease_lane → durable status/result →
                # reply → unlock. Lock held across the whole sequence so the
                # heartbeat reconcile cannot treat an in-flight arm as an orphan.
                lease_id = self.worktree_lease_store.mint_lease_id()
                lock = self.worktree_lease_store.acquire(lease_id)
                try:
                    # Pin creation to the OID we record; a moving branch/ref cannot
                    # make the durable lease describe a different checkout.
                    path = self.create_worktree({**spec, "base_ref": base_oid})
                    try:
                        # Slice 1d-iii: after create_worktree, before the FS lease
                        # record is published — prove push-less origin for exempt.
                        # Failure removes the unleased worktree; row writer is never
                        # called. Gated lane is a no-op.
                        self._prepare_exempt_worktree_or_raise(path)
                        record = self.worktree_lease_store.create(
                            lease_id=lease_id,
                            sender=envelope.sender,
                            worktree_name=spec["name"],
                            repo_identity=repo_identity,
                            base_oid=base_oid,
                            ttl=ttl,
                            lane=self.worktree_lane,
                            arm_request_id=envelope.id,
                        )
                    except Exception:
                        self.remove_worktree(path)
                        raise
                    if self.lane_writer is not None:
                        # Compensation uses the just-created record, never an
                        # untrusted reloaded payload.
                        self._arm_lane_row_or_compensate(record)
                    fields = self._arm_success_fields(record)
                    self._operation_result(
                        envelope,
                        TurnResult(ok=True, result="worktree armed"),
                        fields=fields,
                    )
                finally:
                    lock.release()
                return True

            unexpected = set(envelope.payload) - {"operation", "worktree_lease", "run_id"}
            lease_id = envelope.payload.get("worktree_lease")
            if unexpected or not isinstance(lease_id, str) or not lease_id:
                raise WorktreeLeaseError("worktree-release-invalid-schema")
            # Lock held through reclaim → tombstone → bound retire → result/reply.
            lock = self.worktree_lease_store.acquire(lease_id)
            try:
                record = self.validate_worktree_lease(envelope, policy=policy, allow_expired=True)
                self.reclaim_leased_worktree(record)
                self.worktree_lease_store.tombstone(record, "released")
                if self.lane_writer is not None:
                    try:
                        retired = self.lane_writer.retire(lease_id)
                    except Exception as exc:
                        raise WorktreeLeaseError(
                            "worktree-lane-release-store-failed"
                        ) from exc
                    if not retired:
                        raise WorktreeLeaseError("worktree-lane-release-mismatch")
                self._operation_result(
                    envelope,
                    TurnResult(ok=True, result="worktree released"),
                    fields={"lease_id": lease_id},
                )
            finally:
                lock.release()
            return True
        except (WorktreeLeaseError, WorktreeError) as exc:
            self._operation_result(envelope, TurnResult(ok=False, result="", error=str(exc)))
            return True
        except LaneStoreUnreachable:
            # Store outage is RuntimeError, not WorktreeLeaseError — catch so the
            # sender gets a coded refusal rather than a silent inbox timeout.
            # Do NOT write a durable task result: the outage is transient, and
            # post-recovery redelivery must complete the arm rather than replay
            # a durable failure. Un-mark is_duplicate so the ~60s in-process
            # window does not silence the retry.
            self.send_reply(
                envelope,
                TurnResult(ok=False, result="", error="worktree-lane-store-unreachable"),
            )
            self.seen_request_ids = deque(
                (ts, rid) for ts, rid in self.seen_request_ids if rid != envelope.id
            )
            return True

    def validate_worktree_lease(
        self, envelope: Envelope, *, policy: str, allow_expired: bool = False,
    ) -> WorktreeLeaseRecord:
        lease_id = envelope.payload.get("worktree_lease")
        if not isinstance(lease_id, str) or not lease_id:
            raise WorktreeLeaseError("worktree-lease-invalid")
        if policy != "trusted":
            raise WorktreeLeaseError("worktree-lease-requires-trusted-sender")
        record = self.worktree_lease_store.load(lease_id)
        if record is None or record.state != "armed":
            raise WorktreeLeaseError("worktree-lease-unavailable")
        if record.sender != envelope.sender:
            raise WorktreeLeaseError("worktree-lease-owner-mismatch")
        if record.repo_identity != self.canonical_repo_identity():
            raise WorktreeLeaseError("worktree-lease-repo-mismatch")
        if WORKTREE_NAME_PATTERN.fullmatch(record.worktree_name) is None:
            raise WorktreeLeaseError("worktree-lease-corrupt")
        if not allow_expired and record.expires_at <= time.time():
            raise WorktreeLeaseError("worktree-lease-expired")
        if not self.is_registered_worktree(self.worktree_path(record.worktree_name)):
            raise WorktreeLeaseError("worktree-lease-unavailable")
        # Serve-path gate (worktree_run / mid-turn revalidation): when a lane
        # writer is configured the matching armed row is authoritative. A
        # half-armed FS record with no row must not be served. Release uses
        # allow_expired=True and skips this check so reclaim can still clean up.
        if self.lane_writer is not None and not allow_expired:
            from .lane_writer import LaneStoreUnreachable

            try:
                rows = self.lane_writer.rows()
            except LaneStoreUnreachable as exc:
                raise WorktreeLeaseError("worktree-lane-store-unreachable") from exc
            except Exception as exc:  # noqa: BLE001 - any store failure fails closed
                raise WorktreeLeaseError("worktree-lane-store-unreachable") from exc
            match = next(
                (
                    row
                    for row in rows
                    if row.get("lease_id") == record.lease_id
                    and row.get("lane") == record.lane
                    and row.get("armed_by") == self.agent_id
                ),
                None,
            )
            if match is None:
                raise WorktreeLeaseError("worktree-lane-row-missing")
        return record

    def _release_unstarted_request_resources(
        self,
        envelope: Envelope,
        *,
        continuation_lease: ContinuationWorkspaceLease | None,
        worktree_lease_lock: WorktreeLeaseLock | None,
        processing_raw: str | None,
    ) -> None:
        """Release resources when process_request rejects before its main lifecycle try."""
        if continuation_lease is not None:
            continuation_lease.release()
        if worktree_lease_lock is not None:
            worktree_lease_lock.release()
        self.pool.release(envelope.id)
        if processing_raw is not None:
            try:
                self.redis.remove_processing(self.agent_id, processing_raw, owner_token=self.owner_token)
            except Exception as exc:  # noqa: BLE001 - startup recovery can clean leftovers
                logger.error(f"[bridge-error] processing-remove-failed {exc}")
        with self.active_lock:
            self.active_requests.pop(envelope.id, None)
            self.active_threads.pop(envelope.id, None)
            self.task_engines.pop(envelope.id, None)

    def process_request(
        self,
        envelope: Envelope,
        policy: str,
        engine: AgentEngine | None = None,
        *,
        worktree_spec: dict[str, str] | None = None,
        cell_root: Path | None = None,
        continuation_worktree: Path | None = None,
        continuation_lease: ContinuationWorkspaceLease | None = None,
        worktree_lease_record: WorktreeLeaseRecord | None = None,
        worktree_lease_lock: WorktreeLeaseLock | None = None,
        processing_raw: str | None = None,
    ) -> None:
        started_at = time.monotonic()
        scored_request = self.is_scored_request(envelope)
        preflight_complete = False
        try:
            if scored_request and (worktree_spec is not None or worktree_lease_record is not None):
                self.send_reply(envelope, TurnResult(ok=False, result="", error="scored request rejects ordinary bridge worktree_spec and worktree leases"))
                return
            if scored_request and cell_root is None:
                self.send_reply(envelope, TurnResult(ok=False, result="", error="scored request requires a controller-provisioned cell root"))
                return
            if scored_request:
                try:
                    parsed_root = self.parse_cell_root(envelope)
                except WorktreeError as exc:
                    self.send_reply(envelope, TurnResult(ok=False, result="", error=f"cell root invalid: {exc}"))
                    return
                if parsed_root != cell_root:
                    self.send_reply(envelope, TurnResult(ok=False, result="", error="scored cell root was not controller-validated"))
                    return
            preflight_complete = True
        finally:
            if not preflight_complete:
                self._release_unstarted_request_resources(
                    envelope,
                    continuation_lease=continuation_lease,
                    worktree_lease_lock=worktree_lease_lock,
                    processing_raw=processing_raw,
                )
        worktree_path: Path | None = None
        worktree_engine: AgentEngine | None = None
        preserve_worktree = False  # set when the completion gate bounces a dirty turn
        counted_base_turn = False  # this task runs in the shared base cwd (see base_cwd_turns)
        try:
            # Setup writes are INSIDE the try: if any of these Redis calls throws,
            # the finally still releases the pool slot + the active maps (otherwise
            # a throwing status write before the try leaks the slot forever).
            self.record_request_started()
            self._ensure_task_maps()
            epoch = self.redis.incrby(
                self.redis_config.task_epoch_key(envelope.id), 1, ttl=self.args.events_ttl
            )
            self._task_epoch[envelope.id] = epoch
            logger.info(f"[turn-start] {envelope.id}")
            self.update_task_status(
                envelope,
                state="running",
                phase="starting",
                last_summary=f"Task accepted. {self.engine_name} turn starting.",
            )
            self._start_stall_watch(envelope)
            self.push_task_event(envelope, "task_started", {"task_id": envelope.id})
            self.send_milestone(envelope, "task_started", {"task_id": envelope.id})
            # Default path (no worktree_spec): run on the pooled engine, exactly
            # as before. With a worktree_spec, run on a FRESH single-use engine
            # whose cwd IS the worktree — the base checkout cannot be touched by
            # construction, so parallel file-mutating dispatches can't collide.
            task_engine = engine
            # Base snapshot must be captured BEFORE the worktree and its engine
            # exist: engine build/start hooks can write, and anything written
            # pre-snapshot is baked into the baseline and invisible to the
            # escape compare. Worktree-creation churn itself is excluded from
            # the fingerprint (WORKTREE_CONTAINER), so early capture is safe.
            if self.enforce_completion and not scored_request and (
                worktree_spec is not None or continuation_worktree is not None
                or worktree_lease_record is not None
            ):
                with self.active_lock:
                    base_busy_at_snapshot = self.base_cwd_turns
                    base_gen_at_snapshot = self.base_cwd_turn_gen
                base_snapshot_before = completion_gate.checkout_snapshot(self.workdir)
            else:
                base_busy_at_snapshot = 0
                base_gen_at_snapshot = 0
                base_snapshot_before = None
            try:
                if worktree_lease_record is not None:
                    # Security boundary: repeat every ownership/registration/TTL check
                    # while this turn owns the exclusive lease, before engine construction.
                    current = self.validate_worktree_lease(envelope, policy=policy)
                    if current != worktree_lease_record:
                        raise WorktreeLeaseError("worktree-lease-changed")
                if cell_root is not None:
                    worktree_path = cell_root
                    if scored_request:
                        self._bind_scored_tool_plane(envelope, worktree_path)
                    worktree_engine = build_engine(self.args, cwd=str(worktree_path))
                    starter = getattr(worktree_engine, "start", None)
                    if callable(starter):
                        starter()
                    task_engine = worktree_engine
                elif worktree_spec is not None:
                    worktree_path = self.create_worktree(worktree_spec)
                    if scored_request:
                        self._bind_scored_tool_plane(envelope, worktree_path)
                    worktree_engine = build_engine(self.args, cwd=str(worktree_path))
                    starter = getattr(worktree_engine, "start", None)
                    if callable(starter):
                        starter()
                    task_engine = worktree_engine
                elif continuation_worktree is not None:
                    worktree_path = continuation_worktree
                    if scored_request:
                        self._bind_scored_tool_plane(envelope, worktree_path)
                    worktree_engine = build_engine(self.args, cwd=str(worktree_path))
                    starter = getattr(worktree_engine, "start", None)
                    if callable(starter):
                        starter()
                    task_engine = worktree_engine
                elif worktree_lease_record is not None:
                    worktree_path = self.worktree_path(worktree_lease_record.worktree_name)
                    worktree_engine = build_engine(self.args, cwd=str(worktree_path))
                    starter = getattr(worktree_engine, "start", None)
                    if callable(starter):
                        starter()
                    task_engine = worktree_engine
                # Publish the real engine for this task so steer/cancel reach it
                # (must be set before the turn runs, since control can arrive mid-turn).
                with self.active_lock:
                    self.task_engines[envelope.id] = task_engine
                    if worktree_path is None:
                        counted_base_turn = True
                        self.base_cwd_turns += 1
                        self.base_cwd_turn_gen += 1
                result = self.fork_thread_if_requested(envelope, task_engine)
                if result is None:
                    result = self.resume_thread_if_requested(envelope, task_engine)
                if result is None:
                    self.apply_reasoning_effort_if_requested(envelope, task_engine, required=scored_request)
                    self.reset_context_if_requested(envelope, task_engine, required=scored_request)
                    affinity_setter = getattr(task_engine, "set_turn_thread_affinity", None)
                    if callable(affinity_setter):
                        affinity_setter(
                            self.request_thread_id(envelope) is not None
                            and not self.engine_supports_resume
                        )
                    # Completion gate: snapshot HEAD before the turn so a commit made
                    # DURING the turn is detectable. cwd is the worktree (isolated) or
                    # the shared default workdir.
                    active_cwd = worktree_path if worktree_path is not None else self.workdir
                    head_before = completion_gate.git_head(active_cwd) if self.enforce_completion and not scored_request else None
                    dirty_before = completion_gate.dirty_files(active_cwd) if self.enforce_completion and not scored_request else None
                    result = self.run_engine(envelope, policy=policy, engine=task_engine)
                    if scored_request:
                        result = replace(
                            result,
                            completion=self.project_scored_completion(
                                result.completion,
                                getattr(task_engine, "tool_broker", None),
                            ),
                        )
                    else:
                        result = self.drive_to_completion(
                            envelope, result, engine=task_engine, policy=policy,
                            active_cwd=active_cwd, head_before=head_before,
                            dirty_before=dirty_before, is_worktree=worktree_path is not None,
                        )
                        result = self.orchestrator_commit(
                            envelope, result, engine=task_engine, policy=policy,
                            active_cwd=active_cwd, head_before=head_before,
                            dirty_before=dirty_before, is_worktree=worktree_path is not None,
                        )
                        result, timeout_preserve = self.post_timeout_adopt(
                            envelope, result, active_cwd=active_cwd, head_before=head_before,
                            dirty_before=dirty_before, is_worktree=worktree_path is not None,
                        )
                        result, preserve_worktree = self.apply_completion_gate(
                            result, active_cwd=active_cwd, head_before=head_before,
                            dirty_before=dirty_before, is_worktree=worktree_path is not None,
                            expected=self._expected_artifacts(envelope),
                        )
                        preserve_worktree = preserve_worktree or timeout_preserve
                result = replace(result, thread_id=self.engine_thread_id(task_engine))
            except (WorktreeError, WorktreeLeaseError, EngineError) as exc:
                logger.error(f"[bridge-error] worktree-setup-failed {envelope.id} {exc}")
                result = TurnResult(ok=False, result="", error=f"worktree setup failed: {exc}")
            if (
                self.enforce_completion
                and not scored_request
                and worktree_path is not None
                and base_snapshot_before is not None
            ):
                # Runs on EVERY worktree-task exit — the happy path, failed
                # fork/resume routing, and engine-setup exceptions. A hook can
                # write to the base and THEN raise; the failure must not
                # swallow the escape.
                result, escape_preserve = self._verify_base_isolation(
                    envelope,
                    result,
                    base_snapshot_before=base_snapshot_before,
                    base_busy_at_snapshot=base_busy_at_snapshot,
                    base_gen_at_snapshot=base_gen_at_snapshot,
                )
                preserve_worktree = preserve_worktree or escape_preserve
                if (
                    worktree_lease_record is not None
                    and isinstance(result.completion, dict)
                    and result.completion.get("state") == "worktree_escape"
                ):
                    self.worktree_lease_store.tombstone(worktree_lease_record, "worktree_escape")
            # AFTER the isolation verdict, deliberately: the record helper
            # persists continuation routing only for result.ok turns, and an
            # escaped turn is only flipped to ok=False by the check above —
            # recording earlier leaked live continuation state for a rejected
            # escaped agent-sdk keep-worktree turn (r4 panel P1).
            if not scored_request:
                result = self.record_agent_sdk_continuation_workspace(
                    envelope, result, worktree_spec=worktree_spec,
                    worktree_lease_record=worktree_lease_record,
                )
            status = "ok" if result.ok else "error"
            summary = summarize(result.result or result.error or "")
            structured = self.parse_structured_for_request(envelope, result)
            logger.info(f"[turn-end] {envelope.id} {status} {summary}")
            self.push_task_event(
                envelope,
                "task_finished",
                {"task_id": envelope.id, "ok": result.ok, "summary": summary, "error": result.error},
            )
            self.update_task_status(
                envelope,
                state="completed" if result.ok else "failed",
                phase="finished",
                last_summary=summary,
                ok=result.ok,
                error=result.error,
            )
            self.write_task_result(envelope, result, summary, structured)
            self.send_milestone(
                envelope,
                "task_finished",
                {"task_id": envelope.id, "ok": result.ok, "summary": summary, "error": result.error},
            )
            self._emit_vote(envelope, result)  # BEFORE send_reply (see ordering note above)
            self.send_reply(envelope, result, structured, turn_started=True)
            self.record_turn_seconds(int(max(1, time.monotonic() - started_at)))
        finally:
            if continuation_lease is not None:
                continuation_lease.release()
            if hasattr(self, "_transcript_q"):
                self._capture(envelope, "turn_end", {})
            if worktree_engine is not None:
                stopper = getattr(worktree_engine, "stop", None)
                if callable(stopper):
                    try:
                        stopper()
                    except Exception as exc:  # noqa: BLE001 - teardown must not mask the turn result
                        logger.error(f"[bridge-error] worktree-engine-stop-failed {envelope.id} {exc}")
            if (
                worktree_path is not None
                and not scored_request
                and worktree_spec is not None
                and worktree_spec.get("cleanup") == "auto"
                and not preserve_worktree  # keep the dirty tree the gate flagged, so edits can be salvaged
            ):
                self.remove_worktree(worktree_path)
            if worktree_lease_record is not None and worktree_lease_lock is not None:
                try:
                    current = self.worktree_lease_store.load(worktree_lease_record.lease_id)
                    if current is not None and current.state == "armed" and current.expires_at <= time.time():
                        self.reclaim_leased_worktree(current)
                        self.worktree_lease_store.tombstone(current, "expired")
                finally:
                    worktree_lease_lock.release()
            self.pool.release(envelope.id)
            if scored_request:
                broker = getattr(self.args, "tool_broker", None)
                clearer = getattr(broker, "clear", None)
                if callable(clearer):
                    clearer()
                for name in (
                    "_scored_tool_plane_bound",
                    "tool_broker",
                    "scored_tool_gid",
                    "scored_git_service_factory",
                    "scored_receipt_chain_factory",
                    "scored_completion_provider",
                    "scored_cell_root",
                    "scored_cell_id",
                    "scored_attempt_id",
                ):
                    try:
                        delattr(self.args, name)
                    except AttributeError:
                        pass
            if processing_raw is not None:
                try:
                    self.redis.remove_processing(
                        self.agent_id, processing_raw, owner_token=self.owner_token
                    )
                except Exception as exc:  # noqa: BLE001 - startup recovery can clean leftovers
                    logger.error(f"[bridge-error] processing-remove-failed {exc}")
            with self.active_lock:
                self.active_requests.pop(envelope.id, None)
                self.active_threads.pop(envelope.id, None)
                self.task_engines.pop(envelope.id, None)
                if counted_base_turn:
                    self.base_cwd_turns -= 1
            self._task_epoch.pop(envelope.id, None)
            self._task_turn_index.pop(envelope.id, None)
            self.cancelled_tasks.discard(envelope.id)
            self._last_stream_heartbeat.pop(envelope.id, None)
            self._last_live_tee_ts.pop(envelope.id, None)
            self.stall_watch.end(envelope.id)
            self._clear_stall_status(envelope.id)

    def _verify_base_isolation(
        self,
        envelope: Envelope,
        result: TurnResult,
        *,
        base_snapshot_before: dict[str, Any],
        base_busy_at_snapshot: int,
        base_gen_at_snapshot: int,
    ) -> tuple[TurnResult, bool]:
        """Compare the base checkout against its pre-turn snapshot and fold the
        outcome into the result. Attributable escape → ok=False +
        completion.state=worktree_escape (an already-failed result keeps its
        original error); unattributable or unverifiable → reply-surface
        isolation marker with ok untouched. Returns (result, preserve_worktree)."""
        base_change = completion_gate.compare_checkout_snapshot(
            self.workdir,
            base_snapshot_before,
        )
        with self.active_lock:
            base_turn_overlapped = (
                base_busy_at_snapshot > 0
                or self.base_cwd_turns > 0
                or self.base_cwd_turn_gen != base_gen_at_snapshot
            )
        transient_changed = base_change.get("transient_changed", [])
        sentinel_changed = base_change.get("sentinel_changed", [])
        completion_info = dict(result.completion or {})
        if transient_changed:
            completion_info["isolation_transient_changed"] = transient_changed

        if sentinel_changed and base_turn_overlapped:
            logger.warning(
                f"[bridge-warning] worktree-isolation-unverifiable {envelope.id}: "
                "sentinel changed during turn but a base-cwd task overlapped; "
                "escape cannot be attributed"
            )
            return (
                replace(
                    result,
                    completion={
                        **completion_info,
                        "isolation": "unverifiable",
                        "isolation_reason": "sentinel_changed_with_overlap",
                        "sentinel_changed": sentinel_changed,
                    },
                ),
                False,
            )
        if sentinel_changed:
            escaped = [*base_change.get("new_dirty_files", []), *sentinel_changed]
            if base_change.get("head_after") != base_change.get("head_before"):
                escaped.append("<base HEAD changed>")
            escaped = list(dict.fromkeys(escaped))
            return (
                replace(
                    result,
                    ok=False,
                    error=result.error
                    or "worktree isolation could not be proven: base checkout changed during turn",
                    completion={
                        **completion_info,
                        **base_change,
                        "state": "worktree_escape",
                        "escaped_paths": escaped,
                        **(
                            {"isolation_transient_changed": transient_changed}
                            if transient_changed
                            else {}
                        ),
                    },
                ),
                True,
            )
        isolation_unverified: str | None = None
        if base_change["state"] == "base_checkout_changed" and base_turn_overlapped:
            # A legitimate base-cwd task ran during the window, so a changed
            # base fingerprint cannot be attributed to THIS worktree task.
            # Failing it here would punish an innocent worker for its
            # neighbour's writes.
            isolation_unverified = "base_changed_with_overlap"
            logger.warning(
                f"[bridge-warning] worktree-isolation-unverifiable {envelope.id}: "
                "base checkout changed during turn but a base-cwd task "
                "overlapped; escape cannot be attributed"
            )
        elif base_change["state"] == "fingerprint_unverifiable":
            isolation_unverified = "git_snapshot_error"
            logger.warning(
                f"[bridge-warning] worktree-isolation-unverifiable {envelope.id}: "
                "git snapshot errored; escape check skipped this turn"
            )
        elif base_change["state"] == "not_a_git_repo":
            # A worktree task's base IS a git repo by construction, so this
            # means the before/after probe itself failed — never a silent pass.
            isolation_unverified = "base_probe_failed"
            logger.warning(
                f"[bridge-warning] worktree-isolation-unverifiable {envelope.id}: "
                "base checkout probe failed; escape check skipped this turn"
            )
        if isolation_unverified is not None:
            # Truthful-signals contract: the degraded check must be visible on
            # the reply/result surface, not only in the daemon log. ok is
            # deliberately untouched.
            return (
                replace(
                    result,
                    completion={
                        **completion_info,
                        "isolation": "unverifiable",
                        "isolation_reason": isolation_unverified,
                    },
                ),
                False,
            )
        if base_change["state"] == "base_checkout_changed":
            escaped = base_change.get("new_dirty_files", [])
            if base_change.get("head_after") != base_change.get("head_before"):
                escaped = [*escaped, "<base HEAD changed>"]
            if not escaped:
                escaped = ["<content change to a pre-existing dirty path>"]
            return (
                replace(
                    result,
                    ok=False,
                    # A setup/routing failure that ALSO escaped keeps its own
                    # error (more diagnostic); the completion block carries the
                    # isolation verdict either way.
                    error=result.error
                    or "worktree isolation could not be proven: base checkout changed during turn",
                    completion={
                        **completion_info,
                        **base_change,
                        "state": "worktree_escape",
                        "escaped_paths": escaped,
                    },
                ),
                True,
            )
        if transient_changed:
            return replace(result, completion=completion_info), False
        return result, False

    def apply_completion_gate(
        self,
        result: TurnResult,
        *,
        active_cwd: Path,
        head_before: str | None,
        dirty_before: list[str] | None,
        is_worktree: bool,
        expected: list[str] | None = None,
    ) -> tuple[TurnResult, bool]:
        """Enforce the completion contract. Returns ``(result, preserve_worktree)``.

        Acts on an ``ok`` turn that either left a dirty tree OR failed to produce
        its contracted ``expected`` artifacts; failed turns, clean trees with no
        contract, non-git workdirs, and unattributable parallel default-workdir
        dispatches pass through unchanged. On a bounce the result is flipped to
        ``ok=False`` and the worktree is preserved so the edits can be salvaged.
        """
        if not self.enforce_completion or not result.ok:
            return result, False
        # orchestrator_commit already made the authoritative completion decision
        # (adopted the agent's commit or created the deterministic one) — don't
        # re-evaluate and clobber its committed_by audit field.
        if result.completion and result.completion.get("committed_by") is not None:
            return result, False
        # Parallel default-workdir dispatches share one tree — post-turn dirtiness
        # cannot be attributed to this task. Enforced completion needs a worktree.
        if not is_worktree and self.max_parallel > 1:
            return replace(result, completion=completion_gate.shared_cwd_unchecked(head_before)), False
        completion = completion_gate.evaluate(active_cwd, head_before, dirty_before)
        if completion["state"] in completion_gate.BOUNCE_STATES:
            dirty = ", ".join(completion["dirty_files"][:10])
            logger.info(
                f"[completion-gate] bounce {completion['state']} cwd={active_cwd} dirty=[{dirty}]",
            )
            bounced = replace(
                result,
                ok=False,
                error="incomplete: uncommitted changes, no commit (commit, or mark NO_COMMIT)",
                completion=completion,
            )
            return bounced, is_worktree
        # Contract enforcement (the no-op masquerade): a CLEAN tree whose expected
        # artifacts are missing means the work was never produced in this cwd — e.g.
        # the worker wrote outside the worktree (a no-Bash seat blind to its cwd) or
        # did nothing and claimed "done". The dirty-tree gate above cannot see this
        # (nothing is dirty), so without this an unmet contract passes vacuously
        # green. Fail loud and preserve the worktree for inspection.
        if expected:
            missing = completion_gate.missing_artifacts(active_cwd, expected)
            if missing:
                shown = ", ".join(missing[:10])
                logger.info(f"[completion-gate] bounce missing_artifacts cwd={active_cwd} missing=[{shown}]")
                bounced = replace(
                    result,
                    ok=False,
                    error=f"incomplete: expected artifacts missing: {shown}",
                    completion={**completion, "missing_artifacts": missing},
                )
                return bounced, is_worktree
        return replace(result, completion=completion), False

    def drive_to_completion(
        self,
        envelope: Envelope,
        result: TurnResult,
        *,
        engine: AgentEngine,
        policy: str,
        active_cwd: Path,
        head_before: str | None,
        dirty_before: list[str] | None,
        is_worktree: bool,
    ) -> TurnResult:
        """Re-prompt a continuation-capable engine in its live session until the
        task predicate passes, the model makes no progress, the budget trips, or
        a hard stop (cancel/refusal) occurs. Skipped for engines that can't
        continue, non-worktree parallel dispatches, and when the budget is 0."""
        if not self.enforce_completion or self.max_continuation_turns <= 0:
            return result
        if not getattr(engine, "supports_continuation", False):
            return result
        if not is_worktree and self.max_parallel > 1:
            return result  # shared cwd — dirtiness/artifacts can't be attributed
        expected = self._expected_artifacts(envelope)
        prev_progress: tuple[str | None, frozenset[str]] | None = None
        for attempt in range(1, self.max_continuation_turns + 1):
            if not result.ok or envelope.id in self.cancelled_tasks:
                return result
            if not self._continuable(result):
                return result
            if not getattr(engine, "is_healthy", lambda: True)():
                # The engine flagged its session as not reusable (e.g. agent-sdk abandoned a
                # background task whose later messages would be read by the next prompt).
                return result
            if not self._task_incomplete(active_cwd, head_before, dirty_before, expected):
                return result
            progress = (
                completion_gate.git_head(active_cwd),
                frozenset(completion_gate.dirty_files(active_cwd)),
            )
            if progress == prev_progress:
                logger.info(f"[completion-loop] no-progress; stopping {envelope.id} after {attempt - 1}")
                return result
            prev_progress = progress
            nudge = self._continuation_prompt(active_cwd, head_before, dirty_before, expected)
            logger.info(f"[completion-loop] continue {envelope.id} attempt={attempt}")
            self.update_task_status(
                envelope, state="running", phase="continuing", last_summary=f"continuation attempt {attempt}"
            )
            self.push_task_event(envelope, "task_continuing", {"task_id": envelope.id, "attempt": attempt})
            result = self.run_engine(envelope, policy=policy, engine=engine, task_override=nudge)
        return result

    @staticmethod
    def _continuable(result: TurnResult) -> bool:
        """A turn can be continued only if it didn't hard-stop. An ``end_turn``
        with zero tool calls AND empty output is the documented empty-completion
        masquerade (limit/refusal) — do not continue it."""
        if result.stop_reason in {"refusal", "cancelled", "failed", "error"}:
            return False
        if result.stop_reason == "end_turn" and result.tool_calls == 0 and not (result.result or "").strip():
            return False
        return True

    def _task_incomplete(
        self,
        active_cwd: Path,
        head_before: str | None,
        dirty_before: list[str] | None,
        expected: list[str],
    ) -> bool:
        # With an artifact contract, the loop drives MISSING artifacts (work the
        # model still must produce). The present-but-uncommitted case is NOT a
        # loop concern — orchestrator_commit handles committing finished work, so
        # we don't burn re-prompts nagging the model to commit.
        if expected:
            return bool(completion_gate.missing_artifacts(active_cwd, expected))
        # Gate-only fallback (no artifact contract): incomplete == uncommitted dirt.
        completion = completion_gate.evaluate(active_cwd, head_before, dirty_before)
        return completion["state"] in completion_gate.BOUNCE_STATES

    def _continuation_prompt(
        self,
        active_cwd: Path,
        head_before: str | None,
        dirty_before: list[str] | None,
        expected: list[str],
    ) -> str:
        if expected:
            # Artifact contract: drive only the missing files. Committing is the
            # orchestrator's job, so don't ask the model to commit.
            missing = completion_gate.missing_artifacts(active_cwd, expected)
            return (
                f"These expected files are still missing: {', '.join(missing)}. "
                "Create them now. Do not re-edit already-completed files, then stop."
            )
        # Gate-only fallback: the only signal is the dirty tree, so ask for a commit.
        completion = completion_gate.evaluate(active_cwd, head_before, dirty_before)
        shown = ", ".join(completion["dirty_files"][:10])
        return f"You left uncommitted changes in: {shown}. Commit them, then stop."

    @staticmethod
    def _expected_artifacts(envelope: Envelope) -> list[str]:
        raw = envelope.payload.get("expected_artifacts")
        if isinstance(raw, list):
            return [p for p in raw if isinstance(p, str)]
        return []

    def orchestrator_commit(
        self,
        envelope: Envelope,
        result: TurnResult,
        *,
        engine: AgentEngine,
        policy: str,
        active_cwd: Path,
        head_before: str | None,
        dirty_before: list[str] | None,
        is_worktree: bool,
    ) -> TurnResult:
        """Orchestrator is the source of truth for the commit. The agent MAY
        commit; this method inspects HEAD + tree and then adopts the agent's
        commit, creates the missing deterministic commit, or FAILS the run.
        Idempotent: never blindly creates a second commit. Worktree-only,
        gated on an expected_artifacts contract. Returns the (possibly bounced)
        result; the cases that fail set ok=False because the tree may be clean
        (so the completion gate alone would wrongly pass).

        States (start_sha = head_before):
          dirty_uncommitted   agent didn't commit  -> verify allowed, then commit
          dirty_after_commit  PARTIAL commit        -> FAIL (cleaner audit; no 2nd commit)
          committed_clean     agent committed       -> verify committed files allowed, ADOPT
          no_changes_clean    nothing changed       -> adopt (artifacts pre-existed)
        """
        if not self.auto_commit or not self.enforce_completion or not result.ok or not is_worktree:
            return result
        expected = self._expected_artifacts(envelope)
        if not expected or completion_gate.missing_artifacts(active_cwd, expected):
            return result  # no contract, or the task isn't done — leave to the gate/loop

        completion = completion_gate.evaluate(active_cwd, head_before, dirty_before)
        state = completion["state"]
        committed = self._committed_paths(active_cwd, head_before)  # files in commits since start
        allowed = self._allowed_set(envelope, expected)

        def fail(reason: str) -> TurnResult:
            logger.info(f"[orchestrator-commit] {envelope.id} FAIL: {reason}")
            return replace(result, ok=False, error=f"orchestrator-commit: {reason}",
                           completion={**completion, "committed_by": None, "reason": reason})

        # Case 4 (agent committed unexpected files) — applies whenever HEAD advanced.
        stray_committed = [f for f in committed if not self._path_allowed(f, allowed)]
        if stray_committed:
            return fail(f"agent committed files outside the allowed set: {', '.join(stray_committed[:10])}")

        if state == "dirty_after_commit":  # Case 3: partial commit
            return fail(f"partial commit — dirty tree after agent commit: {', '.join(completion['dirty_files'][:10])}")

        if state in ("committed_clean", "no_changes_clean"):  # Case 2: adopt the agent's commit
            self.push_task_event(envelope, "agent_committed", {"task_id": envelope.id, "head": completion["head_after"]})
            return replace(result, completion={**completion, "committed_by": "agent"})

        # state == dirty_uncommitted (Case 1): agent didn't commit.
        stray_dirty = [f for f in completion["dirty_files"] if not self._path_allowed(f, allowed)]
        if stray_dirty:  # Case 4 for uncommitted work
            return fail(f"uncommitted files outside the allowed set: {', '.join(stray_dirty[:10])}")

        message = self._commit_message(envelope, engine, policy)
        subprocess.run(["git", "-C", str(active_cwd), "add", "-A"], capture_output=True, text=True, check=False)
        commit = subprocess.run(
            ["git", "-C", str(active_cwd), "-c", "core.hooksPath=/dev/null", "commit", "-m", message,
             "--trailer", "Committed-by: agent-redis-bridge"],
            capture_output=True, text=True, check=False,
        )
        if commit.returncode != 0:
            return fail(f"git commit failed: {commit.stderr.strip()[:160]}")
        new = completion_gate.evaluate(active_cwd, head_before, dirty_before)
        if new["state"] != "committed_clean":  # commit must advance HEAD + leave a clean tree
            return fail(f"post-commit tree not clean: {new['state']}")
        logger.info(f"[orchestrator-commit] {envelope.id} committed: {message[:72]}")
        self.push_task_event(envelope, "orchestrator_committed",
                             {"task_id": envelope.id, "message": message, "head": new["head_after"]})
        return replace(result, completion={**new, "committed_by": "orchestrator"})

    @staticmethod
    def _is_engine_timeout(result: TurnResult) -> bool:
        """Identifies an engine-side turn timeout. All engines normalise the
        message via ``f"turn timed out after {timeout}s"`` (see engines/*.py),
        so this single substring check covers every engine."""
        return (not result.ok) and "turn timed out after" in (result.error or "")

    @staticmethod
    def _post_timeout_message(envelope: Envelope) -> str:
        """Commit message for post-timeout adoption. Skips the model-authored
        branch (the engine just timed out — re-calling it would re-timeout)."""
        provided = envelope.payload.get("commit_message")
        if isinstance(provided, str) and provided.strip():
            return provided.strip().splitlines()[0][:120]
        task = str(envelope.payload.get("task", "")).strip().splitlines()[0] if envelope.payload.get("task") else ""
        return f"chore: {task[:80]} (post-timeout adoption)" if task else "chore: orchestrator commit (post-timeout adoption)"

    def post_timeout_adopt(
        self,
        envelope: Envelope,
        result: TurnResult,
        *,
        active_cwd: Path,
        head_before: str | None,
        dirty_before: list[str] | None,
        is_worktree: bool,
    ) -> tuple[TurnResult, bool]:
        """Salvage path for engine-side turn timeouts when the worker's writes
        already satisfy the expected-artifact contract. The normal
        ``orchestrator_commit`` short-circuits on ``not result.ok``, so a turn
        that times out AFTER the worker wrote all expected files would lose
        the work — recovery required manual ``git add && git commit`` in the
        worktree, bypassing the allowed-path validation.

        Decision rules (narrower than the success path, so adoption can only
        happen with the same safety properties):

          missing_artifacts     any expected file absent       -> tag + preserve
          disallowed_committed  HEAD advanced w/ stray paths   -> tag + preserve
          partial_commit        dirty_after_commit (HEAD moved -> tag + preserve
                                AND tree still dirty)           (refuse 2nd commit)
          agent_committed       committed_clean, paths allowed -> tag (no commit)
          disallowed_dirty      dirty_uncommitted w/ strays    -> tag + preserve
          committed             dirty_uncommitted, all allowed -> commit, flip ok=True,
                                                                 preserve timeout_error

        Tags land inside ``result.completion`` as ``timeout_adoption`` with
        adjuncts: ``timeout_error`` (the original engine error),
        ``missing_artifacts`` / ``disallowed_paths`` / ``commit_error`` where
        applicable. Returns ``(result, preserve_worktree)``; preserve=True
        whenever there's salvageable state to inspect.
        """
        if not self.auto_commit or not self.enforce_completion or result.ok or not is_worktree:
            return result, False
        if not self._is_engine_timeout(result):
            return result, False
        expected = self._expected_artifacts(envelope)
        if not expected:
            return result, False

        timeout_error = result.error
        completion = completion_gate.evaluate(active_cwd, head_before, dirty_before)
        state = completion["state"]
        committed = self._committed_paths(active_cwd, head_before)
        allowed = self._allowed_set(envelope, expected)

        def tag(adoption: str, **extra: Any) -> dict:
            return {**completion, "committed_by": None, "timeout_adoption": adoption,
                    "timeout_error": timeout_error, **extra}

        def has_salvage() -> bool:
            return state != "no_changes_clean" or bool(committed)

        missing = completion_gate.missing_artifacts(active_cwd, expected)
        if missing:
            logger.info(f"[post-timeout-adopt] {envelope.id} missing artifacts: {', '.join(missing[:10])}")
            return replace(result, completion=tag("missing_artifacts", missing_artifacts=missing)), has_salvage()

        stray_committed = [f for f in committed if not self._path_allowed(f, allowed)]
        if stray_committed:
            logger.info(f"[post-timeout-adopt] {envelope.id} disallowed committed: {', '.join(stray_committed[:10])}")
            return replace(result, completion=tag("disallowed_committed", disallowed_paths=stray_committed)), True

        if state == "dirty_after_commit":
            logger.info(f"[post-timeout-adopt] {envelope.id} partial commit; refusing 2nd commit")
            return replace(result, completion=tag("partial_commit")), True

        if state in ("committed_clean", "no_changes_clean"):
            # All expected artifacts present + paths clean + no stray commits.
            # Adopt as agent_committed; leave ok=False because the engine still
            # signalled a timeout (caller can re-issue if they care about turn
            # liveness, but the artifact contract is satisfied).
            logger.info(f"[post-timeout-adopt] {envelope.id} agent_committed; adopting")
            self.push_task_event(envelope, "post_timeout_agent_committed",
                                 {"task_id": envelope.id, "head": completion["head_after"]})
            return replace(result, completion=tag("agent_committed", committed_by="agent")), False

        # state == dirty_uncommitted: all expected files present but uncommitted.
        stray_dirty = [f for f in completion["dirty_files"] if not self._path_allowed(f, allowed)]
        if stray_dirty:
            logger.info(f"[post-timeout-adopt] {envelope.id} disallowed dirty: {', '.join(stray_dirty[:10])}")
            return replace(result, completion=tag("disallowed_dirty", disallowed_paths=stray_dirty)), True

        # Happy path: commit the worker's writes ourselves.
        message = self._post_timeout_message(envelope)
        subprocess.run(["git", "-C", str(active_cwd), "add", "-A"], capture_output=True, text=True, check=False)
        commit = subprocess.run(
            ["git", "-C", str(active_cwd), "-c", "core.hooksPath=/dev/null", "commit", "-m", message,
             "--trailer", "Committed-by: agent-redis-bridge"],
            capture_output=True, text=True, check=False,
        )
        if commit.returncode != 0:
            err = commit.stderr.strip()[:160]
            logger.info(f"[post-timeout-adopt] {envelope.id} commit failed: {err}")
            return replace(result, completion=tag("commit_failed", commit_error=err)), True
        new = completion_gate.evaluate(active_cwd, head_before, dirty_before)
        if new["state"] != "committed_clean":
            logger.info(f"[post-timeout-adopt] {envelope.id} post-commit not clean: {new['state']}")
            return replace(result, completion=tag("commit_failed", post_state=new["state"])), True
        logger.info(f"[post-timeout-adopt] {envelope.id} adopted: {message[:72]}")
        self.push_task_event(envelope, "post_timeout_committed",
                             {"task_id": envelope.id, "message": message, "head": new["head_after"]})
        return replace(
            result,
            ok=True,
            result=result.result or "Timed out, but expected artifacts were adopted and committed.",
            error=None,
            completion={**new, "committed_by": "orchestrator",
                        "timeout_adoption": "committed", "timeout_error": timeout_error},
        ), False

    @staticmethod
    def _committed_paths(active_cwd: Path, head_before: str | None) -> list[str]:
        if head_before is None:
            return []
        res = subprocess.run(
            ["git", "-C", str(active_cwd), "diff", "--name-only", f"{head_before}..HEAD"],
            capture_output=True, text=True, check=False,
        )
        return [line for line in res.stdout.splitlines() if line]

    def _allowed_set(self, envelope: Envelope, expected: list[str]) -> tuple[set[str], list[str]]:
        """(exact allowed files, allowed path prefixes). Defaults to the expected
        artifacts; --allowed-path adds directory prefixes for files the dispatcher
        can't name exactly (e.g. timestamped migrations)."""
        raw = envelope.payload.get("allowed_paths")
        prefixes = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
        return set(expected), prefixes

    @staticmethod
    def _path_allowed(path: str, allowed: tuple[set[str], list[str]]) -> bool:
        exact, prefixes = allowed
        if path in exact:
            return True
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)

    def _commit_message(self, envelope: Envelope, engine: AgentEngine, policy: str) -> str:
        """A one-line commit message: the dispatch-provided message wins; else
        model-authored when enabled + supported; else derived from the task text."""
        provided = envelope.payload.get("commit_message")
        if isinstance(provided, str) and provided.strip():
            return provided.strip().splitlines()[0][:120]
        if (
            self.commit_message_from_model
            and getattr(engine, "supports_continuation", False)
            and getattr(engine, "is_healthy", lambda: True)()
        ):
            try:
                proposed = self.run_engine(
                    envelope, policy=policy, engine=engine,
                    task_override="Reply with ONLY a one-line conventional-commit message "
                    "(e.g. 'feat: add X') summarising the changes you just made. No other text, no backticks.",
                    task_turn=False,
                )
                line = (proposed.result or "").strip().splitlines()[0].strip().strip("`").strip()
                if 0 < len(line) <= 120:
                    return line
            except Exception as exc:  # noqa: BLE001 - message authoring must never break the commit
                logger.info(f"[orchestrator-commit] message-author failed: {exc}")
        task = str(envelope.payload.get("task", "")).strip().splitlines()[0] if envelope.payload.get("task") else ""
        return f"chore: {task[:80]}" if task else "chore: orchestrator commit"

    def handle_control(self, envelope: Envelope) -> None:
        policy = self.sender_policies.get(envelope.sender, self.args.unknown_sender_policy)
        if policy == "reject":
            logger.error(f"[bridge-error] sender-rejected {envelope.sender}")
            return

        with self.active_lock:
            if not self.active_requests:
                active = None
            else:
                target_task_id = envelope.payload.get("task_id")
                if isinstance(target_task_id, str) and target_task_id:
                    active = self.active_requests.get(target_task_id)
                elif len(self.active_requests) == 1:
                    active = next(iter(self.active_requests.values()))
                else:
                    # Ambiguous: multiple in flight and no task_id specified.
                    active_ids = list(self.active_requests.keys())
                    logger.error(f"[bridge-error] control-ambiguous {envelope.kind} active={active_ids}")
                    self.send_milestone(
                        envelope,
                        f"{envelope.kind}_rejected",
                        {"reason": "task_id required (multiple active)", "active_task_ids": active_ids},
                    )
                    return

        if active is None:
            target_task_id = envelope.payload.get("task_id")
            if isinstance(target_task_id, str) and target_task_id:
                logger.error(f"[bridge-error] control-wrong-task {target_task_id}")
                self.send_milestone(
                    envelope,
                    f"{envelope.kind}_rejected",
                    {"reason": "wrong task", "active_task_ids": self.pool.active_task_ids()},
                )
                return
            logger.error(f"[bridge-error] no-active-task {envelope.kind}")
            self.send_milestone(envelope, f"{envelope.kind}_rejected", {"reason": "no active task"})
            return

        # Certifying panel input is immutable for the lifetime of the task.
        # The UI also hides a cold composer, but the bridge must enforce the
        # boundary for ctl/RPUSH callers that bypass the UI.
        if envelope.kind in {"steer", "cancel"}:
            active_payload = getattr(active, "payload", None) or {}
            reason = panel_input_lock_reason(active_payload)
            if reason:
                event = f"{envelope.kind}_rejected"
                data = {"reason": reason, "task_id": active.id}
                logger.error(f"[bridge-error] {event.replace('_', '-')} {reason} task={active.id}")
                self.push_task_event(active, event, {"from": envelope.sender, **data})
                self.send_milestone(
                    envelope,
                    event,
                    data,
                )
                return

        # Route to the engine ACTUALLY running the task (the worktree engine for a
        # worktree task), falling back to the pooled engine. pool.get alone would
        # return the slot-token engine for worktree tasks and steer/cancel the wrong one.
        with self.active_lock:
            engine = self.task_engines.get(active.id)
        if engine is None:
            engine = self.pool.get(active.id)
        if engine is None:
            self.send_milestone(envelope, f"{envelope.kind}_rejected", {"reason": "engine unavailable"})
            return

        try:
            if envelope.kind == "steer":
                turn_id = engine.steer(envelope.payload["message"])
                self.push_task_event(active, "steer_sent", {"from": envelope.sender, "turn_id": turn_id})
                self.send_milestone(envelope, "steer_sent", {"task_id": active.id, "turn_id": turn_id})
            elif envelope.kind == "cancel":
                self.cancelled_tasks.add(active.id)  # so the drive-to-completion loop won't re-prompt
                turn_id = engine.interrupt()
                self.push_task_event(active, "cancel_sent", {"from": envelope.sender, "turn_id": turn_id})
                self.update_task_status(active, state="cancelling", phase="cancel", last_summary="Cancellation requested.")
                self.send_milestone(envelope, "cancel_sent", {"task_id": active.id, "turn_id": turn_id})
        except Exception as exc:  # noqa: BLE001 - a control failure must answer, never crash the daemon
            # Engines raise more than EngineError here (raw OSError from a dead
            # pipe pre-CDX-2-fix, SDK errors); the caller deserves <kind>_failed
            # either way (audit CDX-2).
            logger.error(f"[bridge-error] {envelope.kind}-failed {exc}")
            self.send_milestone(envelope, f"{envelope.kind}_failed", {"task_id": active.id, "error": str(exc)})

    def join_active_threads(self) -> None:
        with self.active_lock:
            threads = list(self.active_threads.values())
        for thread in threads:
            if thread.is_alive():
                thread.join()

    # Backwards-compat alias retained for callers/tests that target the
    # single-thread era. Joins every active worker.
    def join_active_thread(self) -> None:
        self.join_active_threads()

    def is_duplicate(self, request_id: str) -> bool:
        cutoff = time.monotonic() - 60
        while self.seen_request_ids and self.seen_request_ids[0][0] < cutoff:
            self.seen_request_ids.popleft()
        if any(item_id == request_id for _, item_id in self.seen_request_ids):
            return True
        self.seen_request_ids.append((time.monotonic(), request_id))
        return False

    def run_engine(
        self,
        envelope: Envelope,
        *,
        policy: str,
        engine: AgentEngine | None = None,
        task_override: str | None = None,
        task_turn: bool = True,
    ) -> TurnResult:
        if self.args.dry_run:
            time.sleep(1)
            return TurnResult(ok=True, result=f"DRY RUN - no {self.engine_name} call made")

        if engine is None:
            return TurnResult(ok=False, result="", error="engine is not running")
        active_engine = engine
        audit_context = getattr(active_engine, "set_turn_audit_context", None)
        if callable(audit_context):
            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            audit_context(
                orchestrator_identity=envelope.sender,
                orchestrator_model=payload.get("orchestrator_model"),
            )
        task = task_override if task_override is not None else envelope.payload["task"]
        # Slice 1d-v worker hydration: object ref requires executed readiness.
        # Never str(dict) into model input. Parse-only seats stay fail-closed.
        if task_override is None and isinstance(task, dict):
            if not getattr(self, "brief_hydrate_ready", False):
                return TurnResult(
                    ok=False, result="", error="brief_hydration_unavailable"
                )
            return self._run_engine_hydrated_ref(
                envelope,
                task=task,
                policy=policy,
                engine=active_engine,
                task_turn=task_turn,
            )
        if not isinstance(task, str):
            # Non-string non-dict is envelope-invalid; belt-and-braces if reached.
            return TurnResult(
                ok=False, result="", error="brief_hydration_unavailable"
            )
        prompt = build_task_prompt(
            task,
            system_prompt=self.role_profile_for_turn(active_engine, task_override=task_override),
            expect_structured=self.expects_structured(envelope),
        )

        try:
            return active_engine.run_turn_with_progress(
                prompt,
                timeout=(
                    self.effective_task_turn_timeout(envelope)
                    if task_turn else self.args.turn_timeout
                ),
                policy=policy,
                on_event=lambda event, data: self.handle_progress(envelope, event, data, policy=policy),
            )
        except EngineError as exc:
            return TurnResult(ok=False, result="", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - a lost reply is worse than a broad catch
            # Engines raise more than EngineError (claude_agent_sdk.ProcessError,
            # UnicodeDecodeError, FileNotFoundError, ...). Letting any of them
            # propagate kills the worker thread before send_reply/task_finished,
            # so the dispatch vanishes with zero signal (audit ASK-1/AGY-1).
            logger.exception(f"[bridge-error] engine-raise {envelope.id}")
            if hasattr(active_engine, "healthy"):
                active_engine.healthy = False
            return TurnResult(ok=False, result="", error=f"{type(exc).__name__}: {exc}")

    def _run_engine_hydrated_ref(
        self,
        envelope: Envelope,
        *,
        task: dict,
        policy: str,
        engine,
        task_turn: bool,
    ) -> TurnResult:
        """Pointer-only prompt + required worker receipt; never open staged body."""
        from .brief_hydrate_ready import (
            allocate_stage_dir,
            build_pointer_prompt,
            cleanup_stage_dir,
            parse_hydration_receipt,
        )
        from .brief_ref import BriefRefError, parse_brief_ref

        try:
            ref = parse_brief_ref(task)
        except BriefRefError:
            return TurnResult(ok=False, result="", error="brief_hydration_unavailable")

        runtime_root = getattr(self, "brief_stage_root", None)
        if runtime_root is None:
            runtime_root = Path(
                os.environ.get("BRIDGE_BRIEF_STAGE_ROOT")
                or (Path(tempfile.gettempdir()) / "bridge-brief-stage")
            )
        stage_dir = None
        try:
            # Stage allocation + pointer prompt must not escape as a dead thread
            # (audit ASK-1/AGY-1 class). Map failures to a named TurnResult code.
            try:
                stage_dir = allocate_stage_dir(runtime_root)
                # Constant filenames unrelated to artefact id.
                brief_path = stage_dir / "brief"
                receipt_path = stage_dir / "receipt"
                prompt = build_pointer_prompt(
                    artefact_id=ref.artefact_id,
                    version=ref.version,
                    output_path=brief_path,
                    receipt_path=receipt_path,
                )
            except OSError as exc:
                logger.exception(
                    f"[bridge-error] brief-hydrate stage {envelope.id}: {exc}"
                )
                return TurnResult(
                    ok=False, result="", error="brief_hydration_stage_failure"
                )
            except Exception as exc:  # noqa: BLE001 - lost reply worse than broad catch
                logger.exception(
                    f"[bridge-error] brief-hydrate stage {envelope.id}: {exc}"
                )
                return TurnResult(
                    ok=False, result="", error="brief_hydration_stage_failure"
                )
            try:
                result = engine.run_turn_with_progress(
                    prompt,
                    timeout=(
                        self.effective_task_turn_timeout(envelope)
                        if task_turn
                        else self.args.turn_timeout
                    ),
                    policy=policy,
                    on_event=lambda event, data: self.handle_progress(
                        envelope, event, data, policy=policy
                    ),
                )
            except EngineError as exc:
                return TurnResult(ok=False, result="", error=str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[bridge-error] engine-raise {envelope.id}")
                if hasattr(engine, "healthy"):
                    engine.healthy = False
                return TurnResult(
                    ok=False, result="", error=f"{type(exc).__name__}: {exc}"
                )

            # Receipt is required; a successful model reply cannot override missing/bad receipt.
            if not receipt_path.is_file():
                return TurnResult(
                    ok=False, result="", error="brief_hydration_receipt_missing"
                )
            try:
                receipt = parse_hydration_receipt(receipt_path)
            except (OSError, ValueError, json.JSONDecodeError):
                return TurnResult(
                    ok=False, result="", error="brief_hydration_receipt_invalid"
                )
            if (
                receipt.get("artefact_id") != ref.artefact_id
                or receipt.get("version") != ref.version
            ):
                return TurnResult(
                    ok=False, result="", error="brief_hydration_receipt_mismatch"
                )
            # Bridge may parse receipt metadata and remove the directory; never open body.
            # Attach receipt metadata for audit surfaces when result is a TurnResult.
            if result.ok:
                # Preserve engine result; hydration receipt is audit, not a credential.
                return result
            return result
        finally:
            if stage_dir is not None:
                if not cleanup_stage_dir(stage_dir):
                    logger.error(
                        "brief-hydrate stage cleanup failed for envelope %s; "
                        "stage dir may retain body: %s",
                        envelope.id,
                        stage_dir,
                    )

    @staticmethod
    def expects_structured(request: Envelope) -> bool:
        return request.payload.get("expect_structured") is True

    def role_profile_for_turn(self, engine: AgentEngine, *, task_override: str | None) -> str | None:
        if task_override is not None:
            return None
        if getattr(engine, "consumes_role_profile", False):
            return None
        return self.role_profile

    def wants_fresh_context(self, request: Envelope) -> bool:
        if (
            self.request_thread_id(request) is not None
            or self.request_fork_thread_id(request) is not None
        ) and "fresh_context" not in request.payload:
            return False
        if "fresh_context" not in request.payload:
            return bool(getattr(self.args, "fresh_context_default", False))
        return request.payload.get("fresh_context") is True

    @staticmethod
    def request_thread_id(request: Envelope) -> str | None:
        thread_id = request.payload.get("thread_id")
        if isinstance(thread_id, str) and thread_id.strip():
            return thread_id
        return None

    @staticmethod
    def request_fork_thread_id(request: Envelope) -> str | None:
        thread_id = request.payload.get("fork_from_thread_id")
        if isinstance(thread_id, str) and thread_id.strip():
            return thread_id
        return None

    def fork_thread_if_requested(self, request: Envelope, engine: AgentEngine | None) -> TurnResult | None:
        thread_id = self.request_fork_thread_id(request)
        if thread_id is None:
            return None
        fork_thread = getattr(engine, "fork_thread", None)
        if not callable(fork_thread):
            error = f"thread-fork-unsupported engine={self.engine_name}"
            logger.error(f"[bridge-error] {error}")
            return TurnResult(ok=False, result="", error=error)
        try:
            fork_thread(thread_id)
        except Exception as exc:  # noqa: BLE001 - fork failures are reported to the caller
            error = f"thread-fork-failed: {exc}"
            logger.error(f"[bridge-error] {error}")
            return TurnResult(ok=False, result="", error=error)
        return None

    def resume_thread_if_requested(self, request: Envelope, engine: AgentEngine | None) -> TurnResult | None:
        thread_id = self.request_thread_id(request)
        if thread_id is None:
            return None
        if not bool(getattr(engine, "supports_thread_resume", False)):
            if self.engine_thread_id(engine) == thread_id:
                return None
            return TurnResult(
                ok=False,
                result="",
                error=f"thread-continuation-unsupported engine={self.engine_name}",
            )
        resume_thread = getattr(engine, "resume_thread", None)
        if not callable(resume_thread):
            return TurnResult(
                ok=False, result="", error=f"thread-continuation-unsupported engine={self.engine_name}"
            )
        try:
            resume_thread(thread_id)
        except Exception as exc:  # noqa: BLE001 - resume failures are reported to the caller
            return TurnResult(ok=False, result="", error=f"thread-resume-failed: {exc}")
        return None

    def apply_reasoning_effort_if_requested(
        self, request: Envelope, engine: AgentEngine | None, *, required: bool = False
    ) -> None:
        raw = request.payload.get("reasoning_effort")
        absent = raw is None or (isinstance(raw, str) and not raw.strip())
        try:
            effort = None if absent else normalize_reasoning_effort(raw)
        except ValueError as exc:
            if required:
                raise EngineError(f"reasoning-effort-invalid: {exc}") from exc
            logger.warning(
                f"[bridge-warning] reasoning-effort-invalid engine={self.engine_name} "
                f"task_id={request.id} error={exc}",
            )
            return
        setter = getattr(engine, "set_turn_reasoning_effort", None)
        if not callable(setter):
            # Only warn when the caller actually asked for an effort the engine can't honor.
            if not absent:
                logger.warning(
                    f"[bridge-warning] reasoning-effort-unsupported engine={self.engine_name} "
                    f"task_id={request.id} effort={effort}",
                )
                if required:
                    raise EngineError(f"reasoning-effort-unsupported: {self.engine_name}")
            return
        # Always set (clearing to None when absent) so a prior dispatch's effort never leaks
        # into a later no-effort dispatch on a warm/pooled seat.
        try:
            setter(effort)
        except Exception as exc:  # noqa: BLE001 - effort is best-effort, never fail the turn
            if required:
                raise EngineError(f"reasoning-effort-apply-failed: {exc}") from exc
            logger.warning(
                f"[bridge-warning] reasoning-effort-apply-failed engine={self.engine_name} "
                f"task_id={request.id} error={exc}",
            )

    def reset_context_if_requested(
        self, request: Envelope, engine: AgentEngine | None, *, required: bool = False
    ) -> None:
        if not required and not self.wants_fresh_context(request):
            return
        reset_context = getattr(engine, "reset_context", None)
        if not callable(reset_context):
            logger.warning(
                f"[bridge-warning] fresh-context-unsupported engine={self.engine_name} task_id={request.id}",
            )
            if required:
                raise EngineError(f"fresh-context-unsupported: {self.engine_name}")
            return
        try:
            reset_context()
        except Exception as exc:  # noqa: BLE001 - fresh context is best-effort
            if required:
                raise EngineError(f"fresh-context-reset-failed: {exc}") from exc
            logger.warning(
                f"[bridge-warning] fresh-context-reset-failed engine={self.engine_name} "
                f"task_id={request.id} error={exc}",
            )

    @staticmethod
    def engine_thread_id(engine: AgentEngine | None) -> str | None:
        for attr in ("thread_id", "session_id"):
            value = getattr(engine, attr, None)
            if isinstance(value, str) and value:
                return value
        return None

    def parse_structured_for_request(self, request: Envelope, result: TurnResult) -> dict[str, Any] | None:
        if not self.expects_structured(request):
            return None
        if not (result.result or "").strip():
            return None
        parsed = parse_structured_reply(result.result or "")
        if parsed.error is not None:
            logger.warning(
                f"[bridge-warning] structured-reply-parse-failed task_id={request.id} error={parsed.error}",
            )
        return parsed.structured

    def send_reply(
        self,
        request: Envelope,
        result: TurnResult,
        structured: dict[str, Any] | None = None,
        *,
        turn_started: bool = False,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "result": result.result,
            "ok": result.ok,
            "error": result.error,
            "completion": result.completion,
            "thread_id": result.thread_id,
            "artifact_paths": [],
            **self.timeout_echo_fields(request, served=turn_started),
            **(extra_fields or {}),
        }
        if self.expects_structured(request):
            payload["structured"] = structured
        reply = make_reply(
            sender=self.agent_id,
            recipient=request.sender,
            branch=self.branch,
            in_reply_to=request.id,
            payload=payload,
        )
        self.redis.lpush(request.sender, reply.to_json())
        logger.info(f"[reply-sent] {reply.id} in_reply_to={request.id}")

    # Closed allowlist of EnvelopeError reasons that may mint a refusal reply
    # (design §2). Unknown reasons stay log-only — the allowlist is the sanitiser.
    REFUSAL_REPLY_REASONS = frozenset(
        {
            "invalid-payload-task-ref",
            "missing-payload-task",
            "invalid-payload-thread_id",
            "invalid-payload-fork_from_thread_id",
            "invalid-payload-claim_ref",
            "invalid-payload-lane",
            "contradictory-context",
            "invalid-run_id",
        }
    )

    @staticmethod
    def _refusal_task_type(task: Any) -> str:
        """Fixed-vocabulary Python type token for the refused payload.task (design §5)."""
        if task is None:
            return "null"
        if isinstance(task, bool):
            return "bool"
        if isinstance(task, int):
            return "int"
        if isinstance(task, float):
            return "float"
        if isinstance(task, str):
            return "str"
        if isinstance(task, dict):
            return "dict"
        if isinstance(task, list):
            return "list"
        return "other"

    def send_refusal_reply(self, header: EnvelopeHeader, reason: str) -> None:
        """Emit a kind=reply error for a parse-refused request (B6 design v4 core).

        Five-guard chain (design §6). Uses make_reply directly — never fabricates
        an Envelope that from_json would refuse. No send-once key (withdrawn, R-A).
        """
        # Guard 1: allowlist reason (⇒ header was valid when the reason fired).
        if reason not in self.REFUSAL_REPLY_REASONS:
            return
        # Guard 2: kind == "request" EXPLICIT — never inferred from the reason token.
        if header.kind != "request":
            return
        # Guard 3: addressed to this seat.
        if header.recipient != self.agent_id:
            return
        # Guard 4: not a self-message (self-reply loop).
        if header.sender == self.agent_id:
            return
        # Guard 5: EXPLICIT sender-policy roster entry that is not "reject".
        # Must NOT consult unknown_sender_policy (local target-set restriction).
        if header.sender not in self.sender_policies:
            return
        if self.sender_policies[header.sender] == "reject":
            return

        task = header.payload.get("task") if isinstance(header.payload, dict) else None
        payload: dict[str, Any] = {
            "result": "",
            "ok": False,
            "error": f"envelope-invalid: {reason}",
            "error_code": reason,
            "refused": "envelope-parse",
            "task_type": self._refusal_task_type(task),
            "task_ref_required": bool(getattr(self, "task_ref_required", False)),
            "completion": None,
            "thread_id": None,
            "artifact_paths": [],
        }
        # SPEC.md: replies to expect_structured requests carry structured; null on
        # parse failure. Same convention for parse-refusal replies (B6).
        if isinstance(header.payload, dict) and header.payload.get("expect_structured"):
            payload["structured"] = None

        reply = make_reply(
            sender=self.agent_id,
            recipient=header.sender,
            branch=self.branch,
            in_reply_to=header.id,
            payload=payload,
        )
        self.redis.lpush(header.sender, reply.to_json())
        logger.info(f"[reply-sent] {reply.id} in_reply_to={header.id}")

    def _next_transcript_seq(self) -> int:
        self._transcript_seq += 1
        return self._transcript_seq

    def _capture(self, request: Envelope, event: str, data: dict[str, Any]) -> None:
        if getattr(self, "_transcript_enabled", True) is False:
            return
        task_id = request.id
        kind = data.get("kind")
        if not isinstance(kind, str) or not kind:
            kind = event
        turn_id = data.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = task_id
        item_id = data.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            item_id = f"{turn_id or task_id}:{kind}"
        seq = data.get("seq")
        if not isinstance(seq, int):
            seq = self._next_transcript_seq()
        enriched = dict(data)
        enriched.update({"turn_id": turn_id, "item_id": item_id, "kind": kind, "seq": seq})
        payload = getattr(request, "payload", {})
        if not isinstance(payload, dict):
            payload = {}
        item = {
            "task_id": task_id,
            "run_id": getattr(request, "run_id", None) or payload.get("run_id"),
            "seat_id": self.agent_id,
            "orchestrator": payload.get("orchestrator") or getattr(request, "sender", None),
            "event": event,
            "data": enriched,
            "turn_id": turn_id,
            "item_id": item_id,
            "kind": kind,
            "seq": seq,
        }
        if event == "turn_end":
            # turn_end is the flusher's flush trigger and is enqueued from process_request's
            # `finally` — NOT the per-token hot path — so dropping it on a full queue would
            # silently lose the whole turn's transcript. Make space + enqueue it reliably
            # rather than dropping (a brief block here is off the per-token hot path).
            try:
                self._transcript_q.put_nowait(item)
            except queue.Full:
                try:
                    self._transcript_q.get_nowait()  # drop the oldest delta to make room
                    self._transcript_truncated += 1
                except queue.Empty:
                    pass
                try:
                    self._transcript_q.put(item, timeout=2.0)
                except queue.Full:
                    self._transcript_truncated += 1
            return
        try:
            self._transcript_q.put_nowait(item)
        except queue.Full:
            self._transcript_truncated += 1

    def _start_stall_watch(self, envelope: Envelope) -> None:
        # Structural blind-by-default (AGY-2 v2.1): armed from the bridge's own
        # engine name, before any engine event — cannot fail to arm, so even an
        # unenumerated dark state (wrong-but-existing conversations root) never
        # reads as a stall. Only a real progress event proves the channel.
        engine = getattr(getattr(self, "args", None), "engine", None)
        blind = engine in BLIND_UNTIL_PROGRESS
        self.stall_watch.start(envelope.id, now=time.monotonic(), blind=blind, blind_reason="unproven")

    def _handle_progress_channel(self, request: Envelope, data: dict[str, Any]) -> None:
        if data.get("state") != "dark":
            return
        reason = str(data.get("reason") or "unproven")
        clear_stale = self.stall_watch.mark_blind(request.id, reason, now=time.monotonic())
        if clear_stale:
            # No fired episode is active: remove any STALE markers so blind and
            # stalled never coexist. An active fired stalled_at is kept — going
            # dark must not silently retract an earned alarm (v2.1).
            self._clear_stall_status(request.id)

    def handle_progress(self, request: Envelope, event: str, data: dict[str, Any], *, policy: str) -> None:
        if event == "progress_channel":
            try:
                self._handle_progress_channel(request, data)
            except Exception as exc:  # noqa: BLE001 - runs inside engine poll threads (AGY-1 class)
                logger.warning(f"[bridge-error] progress-channel-failed {request.id}: {exc}")
        self._record_stall_progress(request, event, now=time.monotonic())
        try:
            self._emit_progress(request, event, data, policy=policy)
        except Exception as exc:  # noqa: BLE001 - observability degrades, never kills the turn
            # This runs inside engine callbacks (agy-print's poll thread, the
            # agent-sdk permission gate, ACP reader loops). A Redis/Valkey blip
            # here used to kill the poll thread or the whole turn (audit AGY-1).
            logger.warning(f"[bridge-error] progress-emit-failed {request.id} {event}: {exc}")

    def _emit_progress(self, request: Envelope, event: str, data: dict[str, Any], *, policy: str) -> None:
        # Per-token model_text deltas are observability noise that pre-DO-Valkey was
        # ~1ms hidden cost per call; over TLS-to-DO-Valkey each XADD+EXPIRE+HSET round-trip
        # is ~100ms. At hundreds of tokens per response that's minutes of self-inflicted
        # backpressure, and because it sits in the stdout-read hot path it throttles
        # codex App Server itself. Status HSET on command boundaries is fine; per-token
        # is not.
        #
        # To preserve a "still alive, still streaming" diagnostic without
        # the per-token cost, emit a single throttled HSET every 8s during
        # model_text streaming. For a normal 10-20s prose response that's
        # 1-3 heartbeats; for a stuck/slow one, callers can still tell
        # via the ticking `updated_at` that the turn is alive. The
        # `_last_stream_heartbeat` map is cleaned up in the per-task
        # `finally` block in handle_request.
        if event in {"model_text", "model_thinking"}:
            self._capture(request, event, data)
            # Same 8-sec throttled heartbeat for both response text and
            # extended-thinking output. Kimi-code-acp can emit hundreds of
            # `model_thinking` events before the first `model_text` delta;
            # without including it here, the status row would stay frozen
            # at "starting" for many minutes and the bridge would look
            # silently stalled even though the agent is actively reasoning.
            now = time.monotonic()
            phase = "thinking" if event == "model_thinking" else "responding"
            summary = "Extended thinking…" if event == "model_thinking" else "Streaming response…"
            if now - self._last_stream_heartbeat.get(request.id, 0.0) >= 8.0:
                self.update_task_status(
                    request,
                    state="running",
                    phase=phase,
                    last_summary=summary,
                )
                self._last_stream_heartbeat[request.id] = now
            return

        if event in {"command_started", "command_finished", "command_output"}:
            self._capture(request, event, data)

        self.push_task_event(request, event, data)

        if event == "command_started":
            command = data.get("command")
            logger.info(f"[turn-tool] {request.id} command")
            self.update_task_status(
                request,
                state="running",
                phase="command",
                last_summary=f"Command running: {summarize(str(command), 120)}",
            )
            self.send_milestone(request, "command_started", {"task_id": request.id, "command": command})
        elif event == "command_finished":
            self.update_task_status(
                request,
                state="running",
                phase="command",
                last_summary=f"Command finished with exit code {data.get('exit_code')}",
            )
            self.send_milestone(
                request,
                "command_finished",
                {
                    "task_id": request.id,
                    "command": data.get("command"),
                    "status": data.get("status"),
                    "exit_code": data.get("exit_code"),
                },
            )
        elif event == "turn_timeout":
            self.update_task_status(
                request,
                state="failed",
                phase="timeout",
                last_summary=f"Task timed out after {data.get('timeout')} seconds.",
            )
            self.send_milestone(request, "task_timeout", {"task_id": request.id, "timeout": data.get("timeout")})
        # NB: model_text is fully handled (8s-throttled) and `return`ed at the TOP
        # of this method. Do NOT add an `elif event == "model_text"` here — a
        # per-token status write is exactly the TLS-to-Valkey backpressure bug the
        # throttle was built to remove. (A dead duplicate of that branch lived here
        # and was removed 2026-05-31.)

    def push_task_event(self, request: Envelope, event: str, data: dict[str, Any]) -> None:
        epoch = getattr(self, "_task_epoch", {}).get(request.id)
        if epoch is not None:
            data.setdefault("attempt_epoch", epoch)
        self._stamp_turn_index(request.id, event, data)
        if event in {"command_started", "command_finished", "command_output"}:
            from .tool_call_id import canonical_tool_call_id
            cid = canonical_tool_call_id(data)
            if cid:
                data.setdefault("tool_call_id", cid)
        key = self.redis_config.task_events_key(request.id)
        sent_at = iso_now()
        fields = {
            "type": event,
            "task_id": request.id,
            "from": self.agent_id,
            "to": request.sender,
            "sent_at": sent_at,
            "data": json.dumps(data, separators=(",", ":")),
        }
        self.redis.xadd(key, fields, maxlen=self.args.max_task_events, ttl=self.args.events_ttl)
        self._tee_eval_event(request, event, sent_at, data)
        self._tee_live_event(
            run_id=getattr(request, "run_id", None),
            task_id=request.id,
            seat_id=self.agent_id,
            orchestrator=request.sender,
            event_type=event,
            sent_at=sent_at,
            data=data,
        )

    def _stamp_turn_index(self, task_id: str, event: str, data: dict[str, Any]) -> dict[str, Any]:
        self._ensure_task_maps()
        if event in self._OUT_OF_TURN_EVENTS:
            return data
        idx = self._task_turn_index.get(task_id, 0) or 1
        self._task_turn_index[task_id] = idx
        data.setdefault("turn_index", idx)
        if event == "turn_completed":
            self._task_turn_index[task_id] = idx + 1
        return data

    def _tee_eval_event(self, request, event, sent_at, data):
        if self.eval_redis is None:
            return
        record = build_eval_record(
            run_id=getattr(request, "run_id", None),
            task_id=request.id,
            seat_id=self.agent_id,
            event=event,
            sent_at=sent_at,
            data=data,
            orchestrator=getattr(request, "sender", None),
        )
        if record is None:
            return
        if getattr(self, "_eval_remote", False):
            flusher = getattr(self, "_eval_flusher", None)
            if flusher is None:
                self._handle_async_tee_drop(record, RuntimeError("eval flusher unavailable"))
                return
            if not flusher.enqueue(record):
                self._handle_async_tee_drop(record, queue.Full("eval tee queue full"))
            return
        try:
            # Healthy eval tee writes still cost one extra Redis round-trip per event.
            # If that proves too costly, move it behind a bounded background queue.
            self.eval_redis.xadd(self._eval_stream, record)
        except Exception:
            logger.exception("eval tee failed for task %s event %s", request.id, event)

    def _tee_live_event(self, *, run_id, task_id, seat_id, orchestrator, event_type, sent_at, data):
        # The roster (events:live) is the human-visibility plane and must show every seat, including
        # ad-hoc dispatches with no run_id. Fall back to task_id so the seat appears (keyed by its own
        # task) instead of being dropped — only run-tagged work showed before, hence "only the
        # orchestrator (run_id=session_id) in arb-watch". The eval/audit tee stays run_id-gated.
        if not run_id:
            run_id = task_id
        # Mark this turn as having just produced a live event, so the heartbeat
        # throttle skips it; a quiet turn's last-tee ages and the heartbeat fires.
        lt = getattr(self, "_last_live_tee_ts", None)
        if lt is not None:
            lt[task_id] = time.monotonic()
        if getattr(self, "_live_remote", False):
            fields = _live_fields(
                run_id=run_id,
                task_id=task_id,
                seat_id=seat_id,
                orchestrator=orchestrator,
                event_type=event_type,
                sent_at=sent_at,
                data=data,
            )
            flusher = getattr(self, "_live_flusher", None)
            if flusher is None:
                self._handle_async_tee_drop(fields, RuntimeError("live flusher unavailable"))
                return
            if not flusher.enqueue(fields):
                self._handle_async_tee_drop(fields, queue.Full("live tee queue full"))
            return
        try:
            live_redis = self.live_redis
            live_prefix = getattr(self, "_live_prefix", self.redis_config.prefix)
            live_tee(
                live_redis,
                live_prefix,
                run_id=run_id,
                task_id=task_id,
                seat_id=seat_id,
                orchestrator=orchestrator,
                event_type=event_type,
                sent_at=sent_at,
                data=data,
                maxlen=self.args.max_task_events,
                ttl=self.args.events_ttl,
            )
        except Exception:
            logger.exception("events:live tee failed for task %s event %s", task_id, event_type)

    def _handle_async_tee_drop(self, fields, exc) -> None:
        drop_count = self._increment_tee_counter("_tee_drop_count")
        logger.warning(
            "async tee dropped event: count=%s fields=%s",
            drop_count,
            fields,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if not self._emit_drop_marker(fields, drop_count):
            self._handle_marker_drop(fields, RuntimeError("drop marker queue full"))

    def _increment_tee_counter(self, attr: str) -> int:
        lock = getattr(self, "_tee_count_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._tee_count_lock = lock
        with lock:
            count = getattr(self, attr, 0) + 1
            setattr(self, attr, count)
            return count

    def _drop_marker_fields(self, fields, drop_count: int) -> dict[str, str]:
        agent_id = getattr(self, "agent_id", "")
        marker = {
            "run_id": str(fields.get("run_id") or ""),
            "task_id": str(fields.get("task_id") or ""),
            "seat_id": str(fields.get("seat_id") or agent_id or ""),
            "orchestrator": str(fields.get("orchestrator") or ""),
            "event_type": "dropped",
            "sent_at": iso_now(),
            "dropped_count": str(drop_count),
        }
        return marker

    def _emit_drop_marker(self, fields, drop_count: int) -> bool:
        marker = self._drop_marker_fields(fields, drop_count)
        if getattr(self, "_live_remote", False):
            flusher = getattr(self, "_live_flusher", None)
            if flusher is None:
                return False
            return flusher.enqueue(marker, marker=True)
        try:
            live_redis = self.live_redis
            live_prefix = getattr(self, "_live_prefix", self.redis_config.prefix)
            live_redis.xadd(
                f"{live_prefix}events:live",
                marker,
                maxlen=self.args.max_task_events,
                ttl=self.args.events_ttl,
            )
        except Exception:
            logger.exception("local events:live drop marker failed for fields=%s", fields)
            return False
        return True

    def _handle_marker_drop(self, fields, exc) -> None:
        marker_drop_count = self._increment_tee_counter("_tee_marker_drop_count")
        logger.warning(
            "async tee dropped marker could not be emitted: count=%s fields=%s",
            marker_drop_count,
            fields,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    def _emit_vote(self, envelope, result) -> None:
        """Transcribe a panel seat's terminal stance into a `vote` audit row (option A). Fail-soft.
        Guard (a) declared-panel only; (b) strict fenced parse; (c) no fabrication; (d) never crash."""
        if self.audit_redis is None:
            return
        if not getattr(envelope, "run_id", None):
            return
        payload = getattr(envelope, "payload", None) or {}
        if not payload.get("audit_vote_expected"):
            return
        try:
            if self._is_engine_timeout(result):
                stance = {"stance": "timed-out", "severity": "none", "refs": [], "note": "engine turn timeout"}
            else:
                from arb_memory.stance import parse_stance, StanceError
                try:
                    stance = parse_stance(result.result or "", require_fence=True)
                except StanceError as exc:
                    logger.warning("panel vote: no valid stance for %s run %s: %s",
                                   self.agent_id, envelope.run_id, exc)
                    return  # guard (c): fail-loud-no-fabricate (missing row -> reconcile gap)
            actor = "seat:" + self.agent_id
            from arb_memory.audit import AuditRun
            AuditRun(self.audit_redis, envelope.run_id, prefix=self._audit_prefix).emit(
                "seat:" + self.agent_id, "vote", {"actor": actor, **stance})
            self._tee_live_event(
                run_id=envelope.run_id,
                task_id=envelope.id,
                seat_id=self.agent_id,
                orchestrator=getattr(envelope, "sender", ""),
                event_type="vote",
                sent_at=iso_now(),
                data={"stance": stance.get("stance")},
            )
        except Exception:  # guard (d): a down/failed audit bus must never crash the worker turn
            logger.exception("panel vote emit failed for %s run %s", self.agent_id,
                             getattr(envelope, "run_id", "?"))

    def update_task_status(
        self,
        request: Envelope,
        *,
        state: str,
        phase: str,
        last_summary: str,
        ok: bool | None = None,
        error: str | None = None,
    ) -> None:
        key = self.redis_config.task_status_key(request.id)
        fields = {
            "task_id": request.id,
            "seat_id": self.agent_id,
            "state": state,
            "phase": phase,
            "last_summary": last_summary,
            "updated_at": iso_now(),
            **self.timeout_echo_fields(request),
        }
        if ok is not None:
            fields["ok"] = "true" if ok else "false"
        if error is not None:
            fields["error"] = error
        self.redis.hset_key(key, fields, ttl=self.args.status_ttl)
        if state in {"completed", "failed", "cancelled"}:
            self._clear_stall_status(request.id)

    def _clear_stall_status(self, task_id: str) -> None:
        """Clear both stall markers (stalled_at + progress_blind) — lifecycle parity (v2.1)."""
        if not hasattr(self, "redis_config") or not hasattr(self, "redis"):
            return
        key = self.redis_config.task_status_key(task_id)
        hdel = getattr(self.redis, "hdel_key", None)
        if callable(hdel):
            hdel(key, "stalled_at", "progress_blind")

    def _refresh_stalled_status_ttl(self, task_id: str) -> None:
        if not getattr(self.args, "status_ttl", None):
            return
        expire = getattr(self.redis, "expire", None)
        if not callable(expire):
            return
        expire(self.redis_config.task_status_key(task_id), self.args.status_ttl)

    def _record_stall_progress(self, request: Envelope, event: str, *, now: float) -> None:
        if self.stall_watch.progress(request.id, event, now=now) is not None:
            try:
                self._clear_stall_status(request.id)
            except Exception as exc:  # noqa: BLE001 - in-memory resume already recorded; the
                # Redis marker clear is observability and must degrade, not kill the
                # engine poll thread that delivered the resume (GLM panel P2, 2026-07-08).
                # A stale stalled_at may linger until terminal status clears it.
                logger.warning(f"[bridge-error] stall-resume-clear-failed {request.id}: {exc}")

    def _check_stalls(self, now: float) -> None:
        with self.active_lock:
            active = list(self.active_requests.values())
        for request in active:
            episode = self.stall_watch.check(request.id, now=now)
            if episode is None:
                if self.stall_watch.is_stalled(request.id) or self.stall_watch.is_blind_reported(request.id):
                    self._refresh_stalled_status_ttl(request.id)
                continue
            if isinstance(episode, BlindEpisode):
                # Unproven channel past threshold: honest unknown, never an alarm —
                # status field + task event only; NO notify, NO stalled_at, and the
                # go-client [stall] line stays silent (it keys on stalled_at).
                try:
                    status_key = self.redis_config.task_status_key(request.id)
                    self.redis.hset_key(status_key, {"progress_blind": episode.reason}, ttl=self.args.status_ttl)
                    self.push_task_event(
                        request,
                        "stall_unknown",
                        {
                            "task_id": request.id,
                            "reason": episode.reason,
                            "unproven_for_secs": episode.unproven_for_secs,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - GAP-1 parity: re-arm, never drop
                    self.stall_watch.unmark_blind_report(request.id)
                    logger.warning(f"[bridge-error] stall-unknown-emit-failed {request.id}: {exc}")
                continue
            stalled_at = iso_now()
            try:
                status_key = self.redis_config.task_status_key(request.id)
                self.redis.hset_key(status_key, {"stalled_at": stalled_at}, ttl=self.args.status_ttl)
                payload = {"task_id": request.id, "stalled_for_secs": episode.stalled_for_secs}
                self.push_task_event(request, "stall_detected", payload)
                self.send_milestone(
                    request,
                    "stall_detected",
                    {
                        "task_id": request.id,
                        "seat_id": self.agent_id,
                        # Fall back to task_id for ad-hoc (no-run_id) dispatches,
                        # matching _tee_live_event and the heartbeat emitter — this
                        # notify lands directly in the orchestrator's inbox (GO-2).
                        "run_id": getattr(request, "run_id", None) or request.id,
                        "stalled_for_secs": episode.stalled_for_secs,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - at-least-once beats silently-never
                # check() marked the task stalled before these emissions; without
                # the unmark a Redis blip here dropped the episode permanently —
                # no stalled_at, no event, no notify, and no retry (panel GAP-1).
                # Unmark so the next tick re-detects and re-emits. A partial
                # emission may then duplicate an event; detect-only consumers
                # tolerate duplicates, not silence.
                self.stall_watch.unmark(request.id)
                logger.warning(f"[bridge-error] stall-emit-failed {request.id}: {exc}")

    def write_task_result(
        self,
        request: Envelope,
        result: TurnResult,
        summary: str,
        structured: dict[str, Any] | None = None,
        *,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        key = self.redis_config.task_result_key(request.id)
        payload = {
            "task_id": request.id,
            "from": self.agent_id,
            "to": request.sender,
            "branch": self.branch,
            "ok": result.ok,
            "result": result.result,
            "error": result.error,
            "completion": result.completion,
            "thread_id": result.thread_id,
            "summary": summary,
            "artifact_paths": [],
            "updated_at": iso_now(),
            **self.timeout_echo_fields(request),
            **(extra_fields or {}),
        }
        if self.expects_structured(request):
            payload["structured"] = structured
        self.redis.set_key(key, json.dumps(payload, separators=(",", ":")), ttl=self.args.result_ttl)

    def write_announcement(self, announcement_id: str, detail: dict[str, Any]) -> str:
        key = self.redis_config.announcement_key(self.agent_id, announcement_id)
        payload = {
            "id": announcement_id,
            "from": self.agent_id,
            "branch": self.branch,
            "created_at": iso_now(),
            "detail": detail,
        }
        self.redis.set_key(key, json.dumps(payload, separators=(",", ":")), ttl=self.args.announcement_ttl)
        return key

    def check_usage_budget(self) -> str | None:
        # Thin adapter over the no-I/O decision core (usage_budget.py, ARB-B13):
        # this method owns the clock and the Redis reads; the branching does not.
        from .usage_budget import evaluate_usage_budget

        day = datetime.now().astimezone().strftime("%Y%m%d")
        return evaluate_usage_budget(
            request_limit=self.args.daily_request_limit,
            turn_seconds_limit=self.args.daily_turn_seconds_limit,
            read_requests=lambda: self.redis.get_int(
                self.redis_config.usage_key(self.usage_identity, day, "requests")
            ),
            read_turn_seconds=lambda: self.redis.get_int(
                self.redis_config.usage_key(self.usage_identity, day, "turn_seconds")
            ),
        )

    def record_request_started(self) -> None:
        if self.args.daily_request_limit <= 0:
            return
        day = datetime.now().astimezone().strftime("%Y%m%d")
        self.redis.incrby(self.redis_config.usage_key(self.usage_identity, day, "requests"), 1, ttl=36 * 60 * 60)

    def record_turn_seconds(self, seconds: int) -> None:
        if self.args.daily_turn_seconds_limit <= 0:
            return
        day = datetime.now().astimezone().strftime("%Y%m%d")
        self.redis.incrby(self.redis_config.usage_key(self.usage_identity, day, "turn_seconds"), seconds, ttl=36 * 60 * 60)

    def send_milestone(self, request: Envelope, event: str, data: dict[str, Any]) -> None:
        notify = make_notify(
            sender=self.agent_id,
            recipient=request.sender,
            branch=self.branch,
            event=event,
            data=data,
        )
        if int(getattr(self.args, "notify_inbox", 1)) == 0:
            # Notifies routed to a separate per-recipient list so orchestrator
            # BLPOP loops on :inbox stay reply-only (no churn through thousands
            # of activity-stream envelopes). Capped via LTRIM to bound growth.
            key = self.redis_config.notify_inbox_key(request.sender)
            push_key = getattr(self.redis, "lpush_key", None)
            if callable(push_key):
                push_key(key, notify.to_json(), trim=self.args.notify_inbox_maxlen)
            else:
                self.redis.lpush(request.sender, notify.to_json())
        else:
            self.redis.lpush(request.sender, notify.to_json())

    def parse_sender_policies(self, values: list[str]) -> dict[str, str]:
        policies: dict[str, str] = {}
        for value in values:
            if "=" not in value:
                raise ValueError(f"invalid sender policy: {value}")
            sender, policy = value.split("=", 1)
            if policy not in {"trusted", "human", "reject"}:
                raise ValueError(f"invalid sender policy value: {policy}")
            policies[sender] = policy
        return policies


def _split_sender_policy_env(value: str | None) -> list[str]:
    """Parse a comma-separated AGENT_TRUSTED_SENDERS value into a list of `agent_id=role` specs."""
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def _default_notify_inbox() -> int:
    raw = os.environ.get("BRIDGE_NOTIFY_INBOX")
    if raw is None:
        return 1
    try:
        value = int(raw)
    except ValueError:
        return 1
    return 0 if value == 0 else 1


def _default_max_parallel() -> int:
    raw = os.environ.get("BRIDGE_MAX_PARALLEL")
    if raw is None:
        return 1
    try:
        value = int(raw)
    except ValueError:
        return 1
    return value if value >= 1 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge agent engines to agent_scratch Redis.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    parser.add_argument("--workspace")
    parser.add_argument("--project")
    parser.add_argument("--engine", choices=sorted(ENGINE_TO_TOOL), default="codex")
    parser.add_argument(
        "--enforce-completion",
        dest="enforce_completion",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Bounce (ok=False) a turn that fails commit/artifact completion; default on. "
        "--no-enforce-completion is restricted to --self-test/--once/--dry-run diagnostics.",
    )
    parser.add_argument(
        "--claim-gate",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("BRIDGE_CLAIM_GATE", "0").lower() in {"1", "true", "yes"},
        help="Enforce the Postgres-backed dispatch claim gate (default off in Slice 1c).",
    )
    parser.add_argument(
        "--max-continuation-turns",
        dest="max_continuation_turns",
        type=int,
        default=int(os.environ.get("AGENT_MAX_CONTINUATION_TURNS", "3")),
        help="Drive-to-completion: max re-prompts of a continuation-capable engine "
        "to finish an incomplete task (0 disables; default 3).",
    )
    parser.add_argument(
        "--auto-commit",
        dest="auto_commit",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("AGENT_AUTO_COMMIT", "1") not in ("0", "false", "no"),
        help="Orchestrator-commit: once a dispatch's --expected-artifacts are all present "
        "but uncommitted in a worktree, commit them on the agent's behalf (default on).",
    )
    parser.add_argument(
        "--commit-message-from-model",
        dest="commit_message_from_model",
        action="store_true",
        default=os.environ.get("AGENT_COMMIT_MESSAGE_FROM_MODEL", "0") in ("1", "true", "yes"),
        help="Ask the (continuation-capable) engine to author the orchestrator-commit message.",
    )
    parser.add_argument(
        "--fresh-context-default",
        action="store_true",
        help="Reset engine conversation context for requests that omit payload.fresh_context.",
    )
    parser.add_argument(
        "--role-profile-file",
        help="Path to a role profile. Pi engines consume it natively; other engines receive it "
        "in the first-turn prompt.",
    )
    parser.add_argument("--role")
    parser.add_argument("--agent-id")
    parser.add_argument("--usage-scope")
    parser.add_argument("--redis-host")
    parser.add_argument("--redis-port")
    parser.add_argument("--redis-db")
    parser.add_argument("--redis-prefix")
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument("--harness", choices=["zcode", "kimi-cli"])
    parser.add_argument("--interpreter-bin", dest="interpreter_bin")
    parser.add_argument("--interpreter-sha256", dest="interpreter_sha256")
    parser.add_argument(
        "--cursor-fast",
        action="store_true",
        default=os.environ.get("BRIDGE_CURSOR_FAST", "0") in ("1", "true", "yes"),
        help="Enable Cursor ACP fast mode for this bridge seat. Default off.",
    )
    parser.add_argument("--pi-tools", default=os.environ.get("BRIDGE_PI_TOOLS"))
    parser.add_argument("--agent-sdk-tools", default=os.environ.get("BRIDGE_AGENT_SDK_TOOLS"))
    parser.add_argument(
        "--agent-sdk-session-root",
        default=os.environ.get("BRIDGE_AGENT_SDK_SESSION_ROOT"),
        help="Directory for agent-sdk SessionStore JSONL files.",
    )
    parser.add_argument(
        "--worktree-lease-root",
        default=os.environ.get("BRIDGE_WORKTREE_LEASE_ROOT"),
        help="Directory for durable bridge-minted worktree lease records.",
    )
    parser.add_argument(
        "--worktree-lease-ttl",
        type=int,
        default=int(os.environ.get("BRIDGE_WORKTREE_LEASE_TTL", "7200")),
        help="Default staged-worktree lease lifetime in seconds (default 7200).",
    )
    parser.add_argument(
        "--worktree-lease-ttl-max",
        type=int,
        default=int(os.environ.get("BRIDGE_WORKTREE_LEASE_TTL_MAX", "14400")),
        help="Largest arm-time lease lifetime this seat grants (default 14400).",
    )
    parser.add_argument(
        "--max-armed-worktrees",
        type=int,
        default=int(os.environ.get("BRIDGE_MAX_ARMED_WORKTREES", "16")),
        help="Maximum outstanding staged-worktree leases (default 16).",
    )
    parser.add_argument(
        "--max-armed-worktrees-per-sender",
        type=int,
        default=int(os.environ.get("BRIDGE_MAX_ARMED_WORKTREES_PER_SENDER", "4")),
        help="Maximum outstanding staged-worktree leases per sender (default 4).",
    )
    parser.add_argument(
        "--agent-sdk-oneshot",
        action="store_true",
        default=os.environ.get("BRIDGE_AGENT_SDK_ONESHOT", "0") in ("1", "true", "yes"),
        help="Use a one-shot agent-sdk engine instead of stateful continuation.",
    )
    parser.add_argument("--approval-policy", default="never")
    parser.add_argument("--sandbox", default="workspace-write")
    parser.add_argument(
        "--codex-bypass-approvals-and-sandbox",
        action="store_true",
        default=os.environ.get("CODEX_BYPASS_APPROVALS_AND_SANDBOX") == "1",
        help="Launch Codex App Server with --dangerously-bypass-approvals-and-sandbox.",
    )
    parser.add_argument("--heartbeat-ttl", type=int, default=60)
    parser.add_argument("--heartbeat-interval", type=int, default=30)
    parser.add_argument(
        "--identity-claim-timeout",
        type=int,
        default=(
            int(os.environ["AGENT_IDENTITY_CLAIM_TIMEOUT"])
            if "AGENT_IDENTITY_CLAIM_TIMEOUT" in os.environ
            else None
        ),
        help="Wait in-process for a stale foreign identity lease before failing "
        "(default heartbeat TTL + interval).",
    )
    parser.add_argument("--blpop-timeout", type=int, default=30)
    parser.add_argument("--control-poll-timeout", type=float, default=0.5)
    parser.add_argument("--turn-timeout", type=int, default=3600)
    parser.add_argument(
        "--turn-timeout-max",
        type=int,
        default=14400,
        help="Largest per-dispatch task-turn ceiling this seat will grant (default 14400).",
    )
    parser.add_argument("--max-message-bytes", type=int, default=131072)
    parser.add_argument("--max-task-events", type=int, default=500)
    parser.add_argument("--events-ttl", type=int, default=7 * 24 * 60 * 60)
    parser.add_argument("--status-ttl", type=int, default=7 * 24 * 60 * 60)
    parser.add_argument("--result-ttl", type=int, default=30 * 24 * 60 * 60)
    parser.add_argument("--announcement-ttl", type=int, default=30 * 24 * 60 * 60)
    parser.add_argument(
        "--stall-after-secs",
        type=int,
        default=int(os.environ.get("BRIDGE_STALL_AFTER_SECS", "600")),
        help="Detect-only stall threshold in seconds; 0 disables (env BRIDGE_STALL_AFTER_SECS, default 600)",
    )
    parser.add_argument("--daily-request-limit", type=int, default=0)
    parser.add_argument("--daily-turn-seconds-limit", type=int, default=0)
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=_default_max_parallel(),
        help="Maximum concurrent turns per bridge instance (default 1; env BRIDGE_MAX_PARALLEL)",
    )
    parser.add_argument(
        "--notify-inbox",
        type=int,
        choices=[0, 1],
        default=_default_notify_inbox(),
        help="Route notifies to caller :inbox (1, default — back-compat) or to a separate :notify_inbox list (0). Env: BRIDGE_NOTIFY_INBOX",
    )
    parser.add_argument(
        "--notify-inbox-maxlen",
        type=int,
        default=int(os.environ.get("BRIDGE_NOTIFY_INBOX_MAXLEN", "5000")),
        help="When --notify-inbox=0, cap the separate notify list via LTRIM to this many entries (default 5000)",
    )
    parser.add_argument("--unknown-sender-policy", choices=["reject", "human", "trusted"], default="reject")
    # B6: explicit CLI posture. default=None so "flag absent" is distinct from
    # --no-task-ref-required; Bridge.__init__ then falls through process env >
    # env-file > False. Without this flag the getattr(args, "task_ref_required")
    # branch was dead (no parser field created it).
    parser.add_argument(
        "--task-ref-required",
        dest="task_ref_required",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Require payload.task to be an exact BriefRef (default: BRIDGE_TASK_REF_REQUIRED "
        "from process env, then env-file, else dual-accept).",
    )
    # --sender-policy resolves in Bridge.__init__ with the same chain as workdir:
    #   1. --sender-policy CLI flag(s)
    #   2. AGENT_TRUSTED_SENDERS= line inside the env file (--env-file)
    #   3. AGENT_TRUSTED_SENDERS shell env (e.g. systemd Environment=)
    #   4. [] (no senders trusted — bridge rejects until configured)
    # Previously this fell back to ["claude-project-c-dev=trusted",
    # "claude-project-c-staging=trusted"] when AGENT_TRUSTED_SENDERS was unset
    # in the shell, even when --env-file populated it — same import-time
    # os.environ.get gotcha as the workdir bug fixed in 8e301e8.
    # Note: argparse `action="append"` with a non-None default *appends* to it
    # rather than replacing, so any default-list value would leak into CLI-set
    # runs. Using default=None and resolving in __init__ also fixes that.
    parser.add_argument("--sender-policy", action="append", default=None)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser


def codex_approval_path_notice(
    *,
    engine: str,
    bypass: bool,
    sender_policies: dict[str, str],
    unknown_sender_policy: str,
) -> tuple[str, str] | None:
    """CDX-1 design v1.2 D3: name the approval path's reachability at daemon start.

    Returns (level, message) or None for non-codex/bypass seats. WARNING when a
    `human`-policy sender can reach the engine (explicit roster entry OR the
    unknown-sender fallback); INFO for a compliant non-bypass seat. `reject`
    senders are refused at the envelope gate and can never produce an ask."""
    if engine != "codex" or bypass:
        return None
    human_reachable = "human" in sender_policies.values() or unknown_sender_policy == "human"
    if human_reachable:
        return (
            "warning",
            "[bridge] codex approval path is LIVE: non-bypass launch with a human-policy "
            "sender — non-trusted approval asks will be denied fail-closed "
            "(BRIDGE_APPROVAL_DENY_BUDGET per turn).",
        )
    return (
        "info",
        "[bridge] codex non-bypass launch; no human-policy sender configured — approval "
        "asks unreachable until one is added.",
    )


class ScoredBridgeControl:
    """One-attempt bridge control hosted in the external scored control process."""

    def __init__(
        self,
        *,
        config: Mapping[str, Any],
        tool_broker: CellToolPlaneBroker,
        provider_env: Mapping[str, str],
    ) -> None:
        self.config = dict(config)
        self.tool_broker = tool_broker
        self.provider_env = dict(provider_env)

    def probe_tool_plane(self) -> Mapping[str, Any]:
        value = self.tool_broker.handle_tool_request({"op": "status"})
        if not isinstance(value, Mapping):
            raise EngineError("scored bridge tool-plane probe is malformed")
        return value

    def run(self, task: str, *, timeout: int) -> dict[str, Any]:
        workdir = Path(str(self.config["workdir"]))
        if not workdir.is_absolute() or not workdir.is_dir() or workdir.is_symlink():
            raise EngineError("scored bridge workdir is not canonical")
        args = SimpleNamespace(
            engine=self.config["engine"], model=self.config["model"], provider=self.config["provider"],
            harness=self.config["harness"], interpreter_bin=self.config["interpreter_bin"],
            interpreter_sha256=self.config["interpreter_sha256"], tool_broker=self.tool_broker,
            _scored_tool_plane_bound=True, pi_tools="read,bash,edit,write",
            role_profile_file=None, _loaded_role_profile=None, _role_profile_loaded=True,
            _scored_provider_env=self.provider_env,
        )
        engine = build_engine(args, cwd=str(workdir))
        try:
            engine.start()
            result = engine.run_turn_with_progress(task, timeout=timeout, policy="trusted", on_event=None)
            structured = parse_structured_reply(result.result) if isinstance(result.result, str) else {}
            return {
                "status": "ok" if result.ok else "failed",
                "timed_out": False,
                "structured": structured if isinstance(structured, dict) else {},
                "completion": self.tool_broker.completion_projection(),
                "text": result.result,
            }
        finally:
            engine.stop()


def build_engine(args: argparse.Namespace, *, cwd: str) -> AgentEngine:
    args.engine = normalize_engine_name(args.engine)
    # ARB-B14b: a retired adapter must refuse HERE, not only in the bash entry
    # points — a daemon started with a dead engine otherwise boots and looks
    # live. The tier table is asserted against ENGINE_TO_TOOL and the adapter
    # directory by tests/test_engine_support_tiers.py.
    from .engines.support_tiers import RETIRED, tier_for

    if tier_for(args.engine) == RETIRED:
        raise EngineError(
            f"engine {args.engine!r} is RETIRED (engines/support_tiers.py); "
            "the wrapped CLI is dead — refusing to construct its adapter"
        )
    if args.engine == "codex":
        return CodexEngine(
            cwd=cwd,
            model=args.model or DEFAULT_CODEX_MODEL,
            approval_policy=args.approval_policy,
            sandbox=args.sandbox,
            bypass_approvals_and_sandbox=args.codex_bypass_approvals_and_sandbox,
        )
    if args.engine == "gemini-acp":
        return GeminiAcpEngine(cwd=cwd, model=args.model)
    if args.engine == "grok-acp":
        return GrokAcpEngine(cwd=cwd, model=args.model)
    if args.engine == "kimi-code-acp":
        return KimiCodeAcpEngine(cwd=cwd, model=args.model)
    if args.engine == "mini-agent-acp":
        return MiniAgentAcpEngine(cwd=cwd, model=args.model)
    if args.engine == "omp-acp":
        # omp reuses the pi-family `--pi-tools` allowlist and takes the role
        # profile as a spawn flag, so it loads the profile the same way pi-sdk
        # does (consumes_role_profile=True keeps the bridge from also
        # prepending it to the task text).
        append_system_prompt = getattr(args, "_loaded_role_profile", None)
        if (
            append_system_prompt is None
            and not getattr(args, "_role_profile_loaded", False)
            and getattr(args, "role_profile_file", None)
        ):
            append_system_prompt = load_role_profile(args.role_profile_file)
        return OmpAcpEngine(
            cwd=cwd,
            model=args.model,
            pi_tools=getattr(args, "pi_tools", None),
            append_system_prompt=append_system_prompt,
        )
    if args.engine == "opencode-acp":
        return OpencodeAcpEngine(cwd=cwd, model=args.model)
    if args.engine == "dsh-acp":
        return DshAcpEngine(cwd=cwd, model=args.model)
    if args.engine == "pi-rpc":
        if getattr(args, "_scored_tool_plane_bound", False):
            raise EngineError("scored Pi turns require a broker-aware Pi tool adapter")
        return PiRpcEngine(
            cwd=cwd,
            model=args.model,
            pi_tools=getattr(args, "pi_tools", None),
            role_profile_path=getattr(args, "role_profile_file", None),
        )
    if args.engine == "pi-sdk":
        append_system_prompt = getattr(args, "_loaded_role_profile", None)
        if (
            append_system_prompt is None
            and not getattr(args, "_role_profile_loaded", False)
            and getattr(args, "role_profile_file", None)
        ):
            append_system_prompt = load_role_profile(args.role_profile_file)
        scored = bool(getattr(args, "_scored_tool_plane_bound", False))
        tool_broker = getattr(args, "tool_broker", None) if scored else None
        if scored and (not isinstance(tool_broker, CellToolPlaneBroker) or not tool_broker.is_authenticated):
            raise EngineError("scored Pi SDK requires an authenticated cell broker")
        return PiSdkEngine(
            cwd=cwd,
            model=args.model,
            pi_tools=getattr(args, "pi_tools", None),
            append_system_prompt=append_system_prompt,
            tool_broker=tool_broker,
            scored=scored,
            process_env=getattr(args, "_scored_provider_env", None),
        )
    if args.engine == "cursor-acp":
        return CursorAcpEngine(cwd=cwd, model=args.model, fast=getattr(args, "cursor_fast", False))
    if args.engine == "cline-acp":
        return ClineAcpEngine(cwd=cwd, model=args.model)
    if args.engine == "devin-acp":
        return DevinAcpEngine(cwd=cwd, model=args.model)
    if args.engine == "agy-print":
        return AgyPrintEngine(
            cwd=cwd,
            model=args.model,
            conversations_root=getattr(args, "agy_conversations_root", None),
        )
    if args.engine == "agy-tmux":
        return AgyTmuxEngine(cwd=cwd, model=args.model)
    if args.engine == "openinterpreter":
        tool_broker = getattr(args, "tool_broker", None)
        # A scored OI turn must have a live cell RPC boundary before launch.
        if not isinstance(tool_broker, CellToolPlaneBroker) or not tool_broker.is_bound or (
            getattr(args, "_scored_tool_plane_bound", False) and not tool_broker.is_authenticated
        ):
            raise EngineError("Open Interpreter requires a bound scored cell tool-plane broker")
        return OpenInterpreterEngine(
            cwd=cwd,
            provider=getattr(args, "provider", None),
            model=(
                getattr(args, "model", None)
                or os.environ.get("BRIDGE_INTERPRETER_MODEL")
            ),
            harness=getattr(args, "harness", None),
            tool_broker=tool_broker,
            binary=getattr(args, "interpreter_bin", None),
            expected_sha256=getattr(args, "interpreter_sha256", None),
            process_env=getattr(args, "_scored_provider_env", None),
        )
    if args.engine == "agent-sdk":
        # Lazy import: only this engine needs claude_agent_sdk (optional, undeclared dep).
        from .engines.agent_sdk import AgentSdkEngine

        model = args.model or "minimax-m3"
        spec = resolve_agent_sdk_model(model)
        key_env = "CLAUDE_CODE_OAUTH_TOKEN" if spec.subscription else spec.key_env
        key = getattr(args, "_agent_sdk_key", None) or os.environ.get(key_env)
        if not key:
            raise EngineError(f"agent-sdk provider key missing: {key_env}")
        session_root = (
            getattr(args, "agent_sdk_session_root", None)
            or default_agent_sdk_session_root()
        )
        live_smoke = (
            Path(cwd).resolve() == Path(getattr(args, "_agent_sdk_primary_cwd", cwd)).resolve()
            and not getattr(args, "_agent_sdk_smoke_test_done", False)
        )
        if live_smoke:
            # Once per daemon: the smoke test is a real model turn, and with
            # retire-after-turn a fresh engine spawns per dispatch — without this
            # sentinel every dispatch would pay a model call at engine start.
            args._agent_sdk_smoke_test_done = True
        return AgentSdkEngine(
            cwd=cwd,
            model=model,
            tool_ceiling=getattr(args, "agent_sdk_tools", None),
            key=key,
            session_root=session_root,
            oneshot=bool(getattr(args, "agent_sdk_oneshot", False)),
            role_profile=getattr(args, "_loaded_role_profile", None),
            agent_id=getattr(args, "_derived_agent_id", None),
            bare=bool(getattr(args, "bare", False)),
            live_smoke_test=live_smoke,
        )
    raise ValueError(f"unknown engine: {args.engine}")


def normalize_role(role: str | None) -> str | None:
    if role is None or role == "":
        return None
    if ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("--role must contain only lowercase letters, digits, and hyphens, max length 16")
    return role


def load_role_profile(path: str | None) -> str | None:
    if path is None:
        return None
    profile_path = Path(path).expanduser()
    try:
        content = profile_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"[bridge-warning] role-profile-unavailable path={profile_path} error={exc}")
        return None
    stripped = content.strip()
    if not stripped:
        return None
    logger.info(f"[role-profile] loaded path={profile_path} chars={len(stripped)}")
    return stripped


def derive_agent_id(*, tool: str, project: str, workspace: str, role: str | None) -> str:
    agent_id = f"{tool}-{project}-{workspace}"
    if role is not None:
        return f"{agent_id}-{role}"
    return agent_id


def _configure_daemon_logging() -> None:
    """Route daemon diagnostics through logging with print-identical output.

    Historically the daemon's diagnostics were `print(..., flush=True)` to stdout
    (journald/launchd capture) while the 27 module loggers ran under Python's
    last-resort handler, silently dropping INFO. Message-only format to stdout
    keeps the byte-for-byte `[bridge-...]` line shape (timestamps come from
    journald), and INFO level surfaces what the prints used to carry. No-op if
    an embedding process already configured handlers.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    _configure_daemon_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    # `asdk` alias normalization is centralized in Bridge.__init__/build_engine (normalize_engine_name).
    try:
        return Bridge(args).run()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.exception(f"[bridge-error] {exc!r}")
        return 1


# Bounded ceiling for daemon-side git subprocesses. A hung git child
# (external-volume getcwd wedge, 2026-07-22: fable5 arm + glm/opus48 startup)
# froze whole seat inbox loops; a bounded op degrades to a failed operation
# the caller already handles via returncode.
GIT_OP_TIMEOUT = 60.0


def run_git_op(cmd: list[str], *, timeout: float = GIT_OP_TIMEOUT, **kwargs: Any) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    try:
        return subprocess.run(cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        logger.error(f"[bridge-error] git-op-timeout after {timeout:g}s: {' '.join(cmd[:6])}")
        return subprocess.CompletedProcess(
            args=cmd, returncode=124, stdout="",
            stderr=f"git operation timed out after {timeout:g}s",
        )


def git_branch(path: Path) -> str:
    result = run_git_op(
        ["git", "-C", str(path), "branch", "--show-current"],
    )
    branch = result.stdout.strip()
    return branch or "unknown"


def install_signal_handlers(stop_event: threading.Event) -> None:
    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def summarize(value: str, limit: int = 160) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."
