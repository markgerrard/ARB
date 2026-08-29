"""Two-plane coordination canary harness.

Built for the findings in codex-arbmem-prod's adversarial review of the
cohort-affinity amendment (2026-08-12, ARB Files
``handoffs/cohort-affinity-amendment-adversarial-review-codex-prod-20260812.md``,
sha256 9c60b6e2...). Its P1-5 says the amendment's evidence base is
category-mismatched: the Mini precedent and the n=4 enumeration are AUDIT-plane
evidence, and nothing in them exercises a DB12 COORDINATION cutover. These
canaries are that missing evidence.

WHY REAL SERVERS AND NOT THE FAKE-REDIS SHIM the sibling watcher tests use:
every finding under test is about behaviour a shim defines away. "Two watchers
on two independent buses have no shared atomic claim" cannot be observed against
a fake whose atomicity is whatever the fake implements, and "the same envelope id
present on both buses" needs two genuinely independent servers. So each canary
runs against two real Valkey-compatible instances in containers.

SAFETY FENCE. A coordination canary is a failure-INJECTION harness: it strands
envelopes, kills consumers mid-flight, and pushes forged senders. Pointed at the
production bus it would be indistinguishable from an attack, and this workstream
has already lost a trimmed production stream and a deleted trust root to a probe
that executed the destructive capability it was probing for. ``_assert_local``
below refuses any endpoint that is not loopback, and it is called on every plane
handout rather than once at construction.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

IMAGE = "redis:7-alpine"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _assert_local(host: str) -> None:
    """Refuse any non-loopback endpoint. Called per plane handout, not once."""
    if host not in LOOPBACK:
        raise RuntimeError(
            f"coordination canaries refuse a non-loopback endpoint: {host!r}. "
            "These tests inject failures (stranding, forged senders, mid-flight "
            "kills) and must never reach a shared or production bus."
        )


def _docker_available() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker binary not found"
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True, timeout=60,
    )
    if probe.returncode != 0:
        return False, f"docker daemon unreachable: {probe.stderr.strip()[:200]}"
    images = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True, timeout=60,
    )
    if IMAGE not in images.stdout:
        return False, f"{IMAGE} not cached locally (canaries do not pull over the network)"
    return True, ""


@dataclass
class Plane:
    """One bus. Test-side assertions use redis-py; the system under test is
    driven through the real scripts, which shell out to redis-cli."""

    name: str
    host: str
    port: int
    container: str
    db: str = "12"
    prefix: str = "agent_scratch:"
    _client: object | None = field(default=None, repr=False)

    @property
    def client(self):
        import redis  # imported lazily so collection works without the dep

        _assert_local(self.host)
        if self._client is None:
            self._client = redis.Redis(
                host=self.host, port=self.port, db=int(self.db), decode_responses=True
            )
        return self._client

    def inbox_key(self, agent_id: str) -> str:
        return f"{self.prefix}agent:{agent_id}:inbox"

    def processing_key(self, agent_id: str) -> str:
        return f"{self.inbox_key(agent_id)}:processing"

    def status_key(self, agent_id: str) -> str:
        return f"{self.prefix}agent:{agent_id}:status"

    def depth(self, agent_id: str) -> int:
        return int(self.client.llen(self.inbox_key(agent_id)))

    def processing_depth(self, agent_id: str) -> int:
        return int(self.client.llen(self.processing_key(agent_id)))

    def send(self, envelope: dict) -> str:
        """LPUSH, because that is what a real coordination sender does.

        Not a detail. ``docs/claude-peer-coordination.md:176`` sends with LPUSH,
        and the reliable watcher consumes ``BLMOVE inbox processing RIGHT LEFT``
        (agent-inbox-watcher-reliable:318) — head-push + tail-pop, i.e. FIFO.
        An earlier draft of this harness used RPUSH and produced LIFO ordering,
        which read as a system finding until the instrument was checked. The
        operational BLPOP watcher pops the HEAD, so it pairs with LPUSH as LIFO;
        see test_canary_ordering.py, which pins both.
        """
        blob = json.dumps(envelope, separators=(",", ":"))
        self.client.lpush(self.inbox_key(envelope["to"]), blob)
        return envelope["id"]

    def script_env(self, agent_id: str, inbox_dir: Path, **extra) -> dict:
        """Environment for the REAL watcher scripts against this plane."""
        _assert_local(self.host)
        env = {
            "AGENT_ID": agent_id,
            "AGENT_REDIS_HOST": self.host,
            "AGENT_REDIS_PORT": str(self.port),
            "AGENT_REDIS_DB": self.db,
            "AGENT_REDIS_PREFIX": self.prefix,
            "AGENT_REDIS_USER": "",          # no ACL user on the local instance
            "AGENT_REDIS_PASSWORD": "",
            "AGENT_REDIS_TLS": "0",
            "AGENT_BRIDGE_INBOX_DIR": str(inbox_dir),
            "PATH": _script_path(),
        }
        env.update({k: str(v) for k, v in extra.items()})
        return env


_BASE_SCRIPT_PATH = "/usr/bin:/bin:/usr/local/bin"


def _script_path() -> str:
    """PATH for the spawned watcher scripts: the hermetic base, plus wherever
    `redis-cli` actually lives on THIS host.

    The base list is deliberately small — these tests spawn real scripts and a
    fat inherited PATH makes it impossible to say which binary they resolved.
    But the list was written on a host where Homebrew installs to
    /usr/local/bin. On Apple Silicon it installs to /opt/homebrew/bin, so
    `redis-cli` fell off the end of the world: the watcher died with
    FileNotFoundError AFTER announcing itself, and all 16 canaries failed on an
    assertion about envelopes rather than saying "redis-cli is not on the PATH
    I built". Green on Linux and Intel, red on Apple Silicon — the same
    authored-here-runs-there class these canaries exist to catch.

    Resolving the real location instead of hardcoding a second prefix keeps the
    fix true on hosts nobody has tried yet.
    """
    found = shutil.which("redis-cli")
    if found is None:
        raise RuntimeError(
            "redis-cli is not on PATH, so the spawned watcher scripts cannot "
            "reach the canary plane. Install it (brew install redis) or put it "
            "on PATH before running the coordination canaries."
        )
    extra = str(Path(found).parent)
    if extra in _BASE_SCRIPT_PATH.split(":"):
        return _BASE_SCRIPT_PATH
    return f"{_BASE_SCRIPT_PATH}:{extra}"


def _start_plane(name: str) -> Plane:
    container = f"arb-canary-{name}-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", container,
            "-p", "127.0.0.1::6379", IMAGE,
            # No persistence: a canary plane must start empty every time, and
            # an inherited RDB would silently seed the "stale backlog" canaries
            # with someone else's leftovers.
            "redis-server", "--save", "", "--appendonly", "no",
        ],
        capture_output=True, text=True, timeout=120,
    )
    if run.returncode != 0:
        raise RuntimeError(f"failed to start canary plane {name}: {run.stderr.strip()}")
    port_out = subprocess.run(
        ["docker", "port", container, "6379/tcp"],
        capture_output=True, text=True, timeout=60,
    )
    port = int(port_out.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
    plane = Plane(name=name, host="127.0.0.1", port=port, container=container)

    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if plane.client.ping():
                return plane
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"canary plane {name} never answered PING on {port}")


def _stop_plane(plane: Plane) -> None:
    subprocess.run(["docker", "rm", "-f", plane.container],
                   capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="session")
def _planes():
    ok, reason = _docker_available()
    if not ok:
        pytest.skip(f"coordination canaries need two local buses: {reason}")
    try:
        import redis  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("redis-py not installed")

    a = _start_plane("plane-a")
    try:
        b = _start_plane("plane-b")
    except Exception:
        _stop_plane(a)
        raise
    try:
        yield a, b
    finally:
        _stop_plane(a)
        _stop_plane(b)


@pytest.fixture
def planes(_planes):
    """Two independent buses, flushed between canaries.

    Flush is per-test rather than per-session: several canaries deliberately
    strand keys on a plane, and a leaked strand would make the NEXT canary's
    "nothing was left behind" assertion pass for the wrong reason.
    """
    a, b = _planes
    a.client.flushdb()
    b.client.flushdb()
    return a, b


@pytest.fixture
def inbox_dir(tmp_path) -> Path:
    d = tmp_path / "agent-bridge-inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d
