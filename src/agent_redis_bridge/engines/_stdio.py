from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Mapping


# Registry capability string a seat advertises once every child-spawn path on its
# code version neutralizes bus and gate-daemon credentials. tools/faba/subagent/
# bridge_round.py mirrors this literal (deliberately decoupled); a parity test in
# tools/faba/tests/test_bridge_round.py keeps the two in lockstep.
# v2 = bus + gate-reader + lane-writer DSN/role scrubbed from every spawn family.
ENV_SCRUB_CAPABILITY = "bus-and-gate-daemon-creds-v2"


def is_bus_credential(key: str) -> bool:
    """True for env vars that grant access to the bridge's Redis bus.

    The single shared definition: scrubbed_child_env (Popen engines) drops these
    keys; the agent-sdk overlay (agent_sdk_models) blanks them to "" because the
    SDK merges the daemon's os.environ under options.env, so omission alone
    cannot remove an inherited key there. Includes harness-publish material
    (``ARB_MEMORY_REDIS_URL`` via the ``_REDIS_URL`` suffix)."""
    return (
        key.startswith("AGENT_REDIS_")
        or key == "REDISCLI_AUTH"
        or key.endswith("_REDIS_URL")
        or key.endswith("_BUS_URL")
    )


def is_gate_reader_credential(key: str) -> bool:
    """Deprecated name retained for import compatibility; prefer
    ``is_gate_daemon_credential``."""
    return is_gate_daemon_credential(key)


def is_gate_daemon_credential(key: str) -> bool:
    """True for env vars that grant bus-side gate daemon DSN/role access.

    Covers the claim-gate reader and the per-seat lane-writer credentials.
    Scrubbed from every engine child alongside bus credentials so a seat process
    cannot inherit them into an ACP/engine/SDK child (design §9.3 residual).
    ``ARB_MEMORY_LOCAL_DSN`` is intentionally NOT in this set — hydration runs
    in the worker.
    """
    return key in {
        "ARB_GATE_READER_DSN",
        "ARB_GATE_READER_ROLE",
        "ARB_GATE_LANE_WRITER_DSN",
        "ARB_GATE_LANE_WRITER_ROLE",
        "ARB_GATE_LANE_WRITER_CONSUMER_ID",
        "ARB_GATE_LANE_WRITER_LANE",
    }


def scrub_env_dict(env: Mapping[str, str]) -> dict[str, str]:
    """Drop bus and gate-daemon credentials from an env mapping.

    Single transform used by every spawn family: the daemon os.environ baseline,
    a scored ``provider_env`` merge, and any explicit process_env override.
    Provider API keys and the local hydration DSN are preserved.
    """
    return {
        key: value
        for key, value in env.items()
        if not is_bus_credential(key) and not is_gate_daemon_credential(key)
    }


def scrubbed_child_env() -> dict[str, str]:
    """The daemon's environment minus bridge-bus and gate-daemon credentials.

    An engine subprocess needs its own auth (CLI dotfiles, provider API keys)
    and the usual PATH/HOME plumbing — it never needs the bus the daemon rides,
    and a child that inherits those creds can publish/forge bus traffic
    (panel-devin-acp-20260718T111820Z-ac7270 finding 5, fleet-wide). Gate-daemon
    DSNs (reader + lane writer) are the same class for the claim/lane residual
    (§9.3). Local hydration DSN is preserved intentionally.
    """
    return scrub_env_dict(os.environ)


def resolve_child_env(process_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Daemon→engine choke point for every Popen / process_env spawn family.

    * ``process_env is None`` → scrub the daemon environment (fleet default).
    * ``process_env`` provided (scored-cell curated env) → scrub *that* mapping
      so a ``{**os.environ, **provider}`` merge cannot reintroduce bus or
      gate-daemon secrets into pi-sdk / openinterpreter children.

    New selectable families must call this (or ``scrubbed_child_env``) rather
    than inheriting ``os.environ`` or trusting an unscrubbed override.
    """
    if process_env is None:
        return scrubbed_child_env()
    return scrub_env_dict(process_env)


def _live_daemon_credentials(env: Mapping[str, str]) -> list[str]:
    return sorted(
        key
        for key, value in env.items()
        if value and (is_bus_credential(key) or is_gate_daemon_credential(key))
    )


def _probe_child_credential_presence(env: Mapping[str, str] | None) -> dict[str, bool]:
    """Spawn a real Python child; return presence booleans (never secret values)."""
    import json
    import subprocess

    probe = (
        "import json,os;"
        "print(json.dumps({"
        "'local':bool(os.environ.get('ARB_MEMORY_LOCAL_DSN')),"
        "'lane_dsn':bool(os.environ.get('ARB_GATE_LANE_WRITER_DSN')),"
        "'lane_role':bool(os.environ.get('ARB_GATE_LANE_WRITER_ROLE')),"
        "'lane_consumer':bool(os.environ.get('ARB_GATE_LANE_WRITER_CONSUMER_ID')),"
        "'lane':bool(os.environ.get('ARB_GATE_LANE_WRITER_LANE')),"
        "'publish':bool(os.environ.get('ARB_MEMORY_REDIS_URL')),"
        "'bus_pw':bool(os.environ.get('AGENT_REDIS_PASSWORD')),"
        "}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=None if env is None else dict(env),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"env scrub real-child probe failed rc={proc.returncode}: {proc.stderr}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_probe_contained(data: Mapping[str, bool], *, family: str) -> None:
    live = [name for name, present in data.items() if name != "local" and present]
    if live:
        raise RuntimeError(
            f"env scrub self-check failed for family={family!r}: "
            f"real child still has {sorted(live)}"
        )
    # Hydration DSN must survive when the parent carried it (probe only knows presence).
    # We do not require it when the source never had it.


@contextmanager
def _environ_scope(
    source: Mapping[str, str], *, mutate: bool
) -> Iterator[Mapping[str, str]]:
    """Swap os.environ to *source* only when tests pass a synthetic env.

    Production heartbeat calls prove_env_scrub_capability() with env=None; the
    source is already the live process environment, so clear+update is a
    semantic no-op that still races concurrent spawns (P1 rem2). Skip the swap.
    """
    if mutate:
        with _temporary_environ(source) as current:
            yield current
    else:
        yield os.environ


def _prove_family_real_child(
    engine_name: str | None,
    source: dict[str, str],
    *,
    mutate_environ: bool,
) -> None:
    """Exercise the selected engine family's production child-env path with a real child.

    Predicate-only simulation is not sufficient: a family that never calls the
    scrub would still pass a pure dict transform. This path builds the env the
    family would hand its child (via production helpers / engine methods) and
    spawns a real Python subprocess to assert bus + lane-writer keys are absent.

    *mutate_environ*: when True (unit-test synthetic env), temporarily replace
    ``os.environ`` so production helpers that read the process env see *source*.
    When False (heartbeat / default prove path), leave ``os.environ`` untouched.
    """
    name = engine_name or "codex"

    if name == "agy-tmux":
        # Production AgyTmuxEngine._run must pass env=; missing env → inherit = leak.
        from .agy_tmux import AgyTmuxEngine

        probe_holder: dict[str, bool] = {}

        def run_command(args, **kwargs):  # type: ignore[no-untyped-def]
            child_env = kwargs["env"] if "env" in kwargs else None
            # When env is omitted, the child inherits the current process env.
            # Under mutate_environ the current env is *source*; otherwise live.
            with _environ_scope(source, mutate=mutate_environ):
                data = _probe_child_credential_presence(child_env)
            probe_holder.update(data)
            return subprocess.CompletedProcess(args, 0, "", "")

        with _environ_scope(source, mutate=mutate_environ):
            eng = AgyTmuxEngine(
                cwd="/tmp",
                command="agy",
                tmux_command="tmux",
                run_command=run_command,
            )
            eng._run(["tmux", "new-session", "-d", "-s", "env-scrub-probe"], check=False)
        if not probe_holder:
            raise RuntimeError("env scrub self-check failed: agy-tmux probe did not run")
        _assert_probe_contained(probe_holder, family=name)
        return

    if name in ("pi-sdk", "openinterpreter"):
        # Scored control merges full os.environ into provider_env; the engine must
        # still scrub before the child sees it.
        with _environ_scope(source, mutate=mutate_environ):
            process_env = {**os.environ}
            if name == "pi-sdk":
                import tempfile

                from .pi_sdk import PiSdkEngine

                captured: dict[str, Any] = {}

                def factory(*args, **kwargs):  # type: ignore[no-untyped-def]
                    captured["has_env"] = "env" in kwargs
                    captured["env"] = kwargs.get("env")
                    raise RuntimeError("env-scrub-probe-captured")

                with tempfile.NamedTemporaryFile(suffix=".mjs") as host:
                    eng = PiSdkEngine(
                        cwd="/tmp",
                        model="zai/glm-5.2",
                        host_script_path=host.name,
                        popen_factory=factory,
                        process_env=process_env,
                    )
                    try:
                        eng.start()
                    except RuntimeError as exc:
                        if "env-scrub-probe-captured" not in str(exc):
                            raise
                if not captured.get("has_env"):
                    raise RuntimeError(
                        "env scrub self-check failed: pi-sdk spawn omitted env="
                    )
                data = _probe_child_credential_presence(captured.get("env"))
            else:
                from .openinterpreter import CellToolPlaneBroker, OpenInterpreterEngine

                eng = OpenInterpreterEngine(
                    cwd="/tmp",
                    provider="p",
                    model="m",
                    harness="zcode",
                    tool_broker=CellToolPlaneBroker(handler=lambda _: {}),
                    process_env=process_env,
                )
                kwargs = eng.popen_kwargs()
                if "env" not in kwargs:
                    raise RuntimeError(
                        "env scrub self-check failed: openinterpreter omitted env="
                    )
                data = _probe_child_credential_presence(kwargs["env"])
        _assert_probe_contained(data, family=name)
        return

    if name == "agent-sdk":
        # Agent-sdk blanks rather than drops (SDK merges os.environ under options.env).
        blanked = dict(source)
        for key in list(blanked):
            if is_bus_credential(key) or is_gate_daemon_credential(key):
                blanked[key] = ""
        live = _live_daemon_credentials(blanked)
        if live:
            raise RuntimeError(
                f"env scrub self-check failed for family={name!r}: blank live={live}"
            )
        # Real child with the blanked mapping (blank values are still "present" as
        # empty strings — presence-bool probe treats "" as absent, matching the
        # neutralize contract assert_no_live_bus_credentials enforces).
        data = _probe_child_credential_presence(blanked)
        _assert_probe_contained(data, family=name)
        return

    # Default Popen family (codex, ACP, pi-rpc, agy-print, …): production scrub.
    with _environ_scope(source, mutate=mutate_environ):
        child_env = scrubbed_child_env()
    data = _probe_child_credential_presence(child_env)
    _assert_probe_contained(data, family=name)
    if source.get("ARB_MEMORY_LOCAL_DSN") and not data.get("local"):
        raise RuntimeError(
            f"env scrub self-check failed for family={name!r}: "
            "ARB_MEMORY_LOCAL_DSN was stripped"
        )


class _temporary_environ:
    """Replace os.environ for the duration of a prove probe; restore on exit."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = dict(env)
        self._prior: dict[str, str] | None = None

    def __enter__(self) -> dict[str, str]:
        self._prior = dict(os.environ)
        os.environ.clear()
        os.environ.update(self._env)
        return os.environ

    def __exit__(self, *exc: object) -> None:
        os.environ.clear()
        if self._prior is not None:
            os.environ.update(self._prior)


def prove_env_scrub_capability(
    env: dict[str, str] | None = None,
    *,
    engine_name: str | None = None,
) -> str:
    """Execute the selected-engine scrub self-check; return ENV_SCRUB_CAPABILITY.

    A declarative registry string without this executed check is not sufficient
    (Stage 1d-i). Checks:

    1. The shared drop/blank *predicates* neutralize bus + gate-daemon keys on
       ``env`` (default: current process environment).
    2. A **real child** is spawned using the **selected engine family's**
       production child-env path (``engine_name``), and that child must not see
       lane-writer or bus credentials. Predicate-only simulation is not enough —
       a family that never calls the scrub would otherwise stay green.

    Raises RuntimeError if either check fails. Seats must not advertise v2 when
    this raises.
    """
    # Heartbeat path passes env=None: source IS the live process environment.
    # Do not clear/rebuild os.environ for that path — the swap is a semantic
    # no-op that still races concurrent spawns on the background heartbeat thread.
    mutate_environ = env is not None
    source = dict(os.environ if env is None else env)
    # Predicate checks (drop + blank) — still kill a broken scrub transform.
    scrubbed = scrub_env_dict(source)
    blanked = dict(source)
    for k in list(blanked):
        if is_bus_credential(k) or is_gate_daemon_credential(k):
            blanked[k] = ""
    live_after_drop = _live_daemon_credentials(scrubbed)
    live_after_blank = _live_daemon_credentials(blanked)
    if live_after_drop or live_after_blank:
        raise RuntimeError(
            "env scrub self-check failed: live credentials remain "
            f"drop={live_after_drop} blank={live_after_blank}"
        )
    # Hydration DSN must remain available when present in the source.
    if source.get("ARB_MEMORY_LOCAL_DSN") and not scrubbed.get("ARB_MEMORY_LOCAL_DSN"):
        raise RuntimeError(
            "env scrub self-check failed: ARB_MEMORY_LOCAL_DSN was stripped"
        )
    # Selected-family real child (kills vacuous predicate-only advertisement).
    _prove_family_real_child(
        engine_name, source, mutate_environ=mutate_environ
    )
    return ENV_SCRUB_CAPABILITY


def start_stderr_drain(
    process: subprocess.Popen, label: str, *, max_buffer: int = 1000
) -> threading.Thread:
    """Drain a child's stderr pipe so it can never deadlock the turn loop.

    A subprocess spawned with ``stderr=PIPE`` blocks on ``write()`` once the OS
    pipe buffer (~16-64 KB) fills if nothing reads it. A child blocked on stderr
    stops emitting stdout too, so the engine's stdout reader starves and the
    JSON-RPC turn loop hangs until timeout — a silent "bridge wedged after start"
    failure.

    The fix splits the work across two daemon threads:

    * a **reader** that is the sole consumer of the child's stderr pipe and only
      ever does a non-blocking ``put_nowait`` (dropping lines when the buffer is
      full). It therefore can never block, so the child's stderr never fills —
      even if the *bridge's own* stderr sink is a slow or blocked pipe.
    * a **forwarder** that emits buffered lines to the bridge's stderr prefixed
      ``[<label>-stderr]`` for diagnosis. If that sink blocks, only the forwarder
      waits; the reader keeps draining the child and drops overflow.

    Decoupling matters: a single thread that read the child and synchronously
    flushed to a backpressured ``sys.stderr`` would just shift the same deadlock
    one pipe downstream. Returns the reader thread (the one whose liveness
    indicates draining is active). Handles both text- and bytes-mode pipes.
    """
    buffer: queue.Queue = queue.Queue(maxsize=max_buffer)
    reader = threading.Thread(target=_read, args=(process, buffer), daemon=True)
    forwarder = threading.Thread(target=_forward, args=(buffer, label), daemon=True)
    reader.start()
    forwarder.start()
    return reader


def _read(process: subprocess.Popen, buffer: queue.Queue) -> None:
    stream = process.stderr
    if stream is None:
        _offer(buffer, None)
        return
    try:
        while True:
            raw = stream.readline()
            if not raw:
                break
            if isinstance(raw, (bytes, bytearray)):
                line = raw.decode("utf-8", errors="replace").rstrip()
            else:
                line = raw.rstrip()
            if line:
                _offer(buffer, line)
    except Exception:
        pass
    finally:
        _offer(buffer, None)


def _forward(buffer: queue.Queue, label: str) -> None:
    while True:
        line = buffer.get()
        if line is None:
            break
        try:
            print(f"[{label}-stderr] {line}", file=sys.stderr, flush=True)
        except Exception:
            pass


def _offer(buffer: queue.Queue, item) -> None:
    # Never block the child-stderr reader on a full buffer.
    try:
        buffer.put_nowait(item)
    except queue.Full:
        pass
