# PF3 Containment (macOS slice) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a working, testable FABA SDK harness on macOS whose worker can reach only a destination-pinned loopback provider broker, cannot obtain Memory credentials, and is fully reaped before controller-owned publication.

**Architecture:** Keep `faba_launch.py` as the privileged controller and run the Claude Agent SDK in a separate, isolated Python exec image under a dedicated per-round UID and an outer macOS Seatbelt profile. The controller alone owns provider and Redis credentials; it materialises the real round artefacts, starts a destination-pinned credential broker, launches a secret-free worker, proves PGID and UID teardown, then mints the attestation required by `publish_and_gate`.

**Tech Stack:** Python 3.14, `claude_agent_sdk` 0.2.107, Redis/`arb_memory` bus, macOS Seatbelt (`sandbox-exec`), a narrowly scoped C setuid UID helper, launchd, pytest

## Global Constraints

- This plan is the macOS-first slice only. The managed-Linux mount namespace/container and cgroup path is a separate follow-up plan.
- Pin and runtime-assert `claude_agent_sdk==0.2.107`; every SDK or Claude CLI upgrade must rerun the live additive-preapproval and credential-harvest probes.
- No engine launch is possible until its destination-pinned loopback credential broker passes provider traffic and cannot forward, CONNECT, or tunnel to either TCP bus fixture.
- The worker environment contains neither `ANTHROPIC_AUTH_TOKEN` nor `ANTHROPIC_API_KEY`; its only auth-adjacent value is `ANTHROPIC_BASE_URL=http://localhost:{broker_port}`.
- The outer `sandbox-exec` profile is the network authority: deny network by default and allow only the exact broker host/port. `SandboxSettings.deniedDomains` is orthogonal WebFetch policy.
- A live macOS probe against remote TCP Redis, separately bound localhost TCP Redis, and any configured Unix-socket Redis path is a hard launch gate. Missing fixtures, missing executables, an unseen escape attempt, or an unsupported Seatbelt filter is failure, never a skip or pass.
- The worker and controller are separate exec images, never a fork that retains controller memory. Launch with `start_new_session=True`, `close_fds=True`, and only fds 0/1/2 plus the declared result fd.
- Rebuild the child environment from a positive allow-list. Reject controller startup if the initial environment contains any bus/provider credential (`ARB_MEMORY_*`, credential-bearing `AGENT_REDIS_*`, provider API key/token, or generic token/API-key key); remove proxy, `CLAUDE_CODE_*`, `OTEL_*`, and `SSH_AUTH_SOCK` keys while constructing the child environment.
- Effective SDK options are exactly: `permission_mode="default"`, `allowed_tools=[]`, `setting_sources=[]`, `hooks={}`, `agents=None`, `strict_mcp_config=True`, the exact FABA callback, no unsandboxed commands, no ambient MCP servers, and the pinned socket/Mach sandbox policy.
- The first slice removes all three local Memory read MCP tools. `ARB_MEMORY_LOCAL_MCP` or a manifest declaring those tools fails closed; their possible return requires a separate read-only ACL/database design and proof.
- Tests build the real brief and payload through production `build_brief` and `materialise_workspace`; they never inject handwritten prompt, decision-record, or SDK options content.
- The generated readable source view contains tracked working-tree bytes plus only HEAD-reachable Git objects, and omits env files, reflogs, stash, other-worktree metadata, alternates, submodule metadata, and untracked files.
- The worker may write only beneath the canonical round-workspace realpath. It cannot write the source view, shared `/private/var/folders`, `/tmp`, or `/private/tmp`; Seatbelt remains authoritative when callback path checks are raced.
- Irreversible actions remain recommendations in the decision record; the callback denies git commit, push, merge, rebase, tag, mutating checkout/switch, remote mutation, unknown tools, mutation MCP, WebFetch, WebSearch, and Task.
- `deny process-info` is certification evidence only after its fleet canary passes; inability to run the canary fails launch.
- Every SDK success, SDK error, timeout, and signal path performs TERM, bounded grace, KILL, wait, then dedicated-UID `pgrep -U`/`pkill -U` census. Any startup or teardown proof failure exits non-zero with no Redis client creation and no publication.
- `publish_and_gate` requires a controller-owned `TeardownAttestation`; validation, receipt-key DEL, publish, and poll cannot be reached before attestation.
- Receipt gating retains the current `artefact_id` binding. Receipt-envelope ULID echo/binding and store fetch-by-id digest verification are future hardenings and are not tasks in this plan.
- `round-contract.md` remains the shared instruction surface and receives no containment rules. The prototype subagent execution path is not claimed PF3-contained by this SDK-harness plan.

---

## File Structure

### Create

- `tools/faba/provider_broker.py` — destination-pinned Anthropic reverse proxy that injects the controller-held credential and exposes no generic proxy method.
- `tools/faba/source_view.py` — builds and verifies the secret-omitting, read-only source view from tracked working bytes and HEAD-reachable objects.
- `tools/faba/macos_boundary.py` — renders the canonical Seatbelt profile, launches boundary canaries, and returns machine-readable proof.
- `tools/faba/macos/faba_uid_helper.c` — minimal setuid entry point for a dedicated numeric UID, worker exec, UID census, and UID-wide reap.
- `tools/faba/macos/install_uid_helper.sh` — fixed-path compile/install recipe for `/usr/local/libexec/faba-uid-helper` with root ownership and mode `4755`.
- `tools/faba/faba_sdk_policy.py` — manifest v2 parser, callback predicates, and fail-closed SDK permission result adapter.
- `tools/faba/faba_worker.py` — secret-free SDK worker entry point and startup option/deny proofs.
- `tools/faba/faba_worker_runtime.py` — constructs the isolated worker zipapp without the repository `src/` tree.
- `tools/faba/faba_containment.py` — controller-side environment gate, worker launch/journal state machine, PGID/UID teardown, and attestation type.
- `tools/faba/faba_cleanup.py` — launchd/next-launch stale-journal recovery using the UID helper.
- `tools/faba/macos/com.openai.faba-cleanup.plist` — launchd ownership for crash cleanup.
- `tools/faba/tests/probe_pf3.py` — one non-skipping executable runner for broker, additive-preapproval, credential-harvest, and descendant-reseed certification cases.
- `tools/faba/tests/test_provider_broker.py` — broker routing, credential non-disclosure, and tunnel-denial tests.
- `tools/faba/tests/test_source_view.py` — source omission, Git metadata, scanner control, and read-only view tests.
- `tools/faba/tests/test_macos_boundary.py` — profile rendering and live network/filesystem/process-info gate tests.
- `tools/faba/tests/test_uid_helper.py` — helper validation, dedicated-UID execution, census, and cleanup tests.
- `tools/faba/tests/test_faba_sdk_policy.py` — manifest and callback decision table tests.
- `tools/faba/tests/test_faba_worker.py` — exact options, real payload, startup proof, environment, fd, and import-isolation tests.
- `tools/faba/tests/test_faba_containment.py` — controller lifecycle, teardown ordering, crash recovery, and attested publication tests.
- `src/agent_redis_bridge/agent_sdk_baseline.py` — dependency-light shared home for the three normative ask-path option literals.

### Modify

- `pyproject.toml:20-24` — pin the optional Agent SDK dependency to exactly 0.2.107.
- `src/agent_redis_bridge/engines/agent_sdk_mediation.py:1-52` — import the three normative ask-path literals from the small shared baseline module.
- `tools/faba/manifest.json:1-25` — migrate to manifest version 2, `permission_mode="default"`, and remove local Memory MCP tools.
- `tools/faba/faba_launch.py:52-411` — extract production materialisation, use the controller/worker state machine, and require teardown attestation for publication.
- `tools/faba/tests/test_faba_harness.py:1-225` — exercise real `build_brief`/`materialise_workspace` composition and the initial-environment gate.
- `tools/faba/tests/test_faba_schema.py:99-230` — pass a real attestation and assert it precedes Redis client creation and DEL.
- `tools/faba/README.md:1-83` — document the macOS boundary, root-owned helper installation, launchd cleanup, hard probes, and named future hardenings.

### Explicitly unchanged

- `src/arb_memory/bus.py` and `src/arb_memory/store.py` — no ULID receipt echo or fetch-by-id work belongs to this slice.
- `tools/faba/round-contract.md` — containment is enforced by callback and kernel boundaries, not duplicated as agent prose.
- `tools/faba/subagent/` — remains a separate prototype execution path and is not certified by this plan.

---

### Task 1: Pin the shared SDK baseline and migrate the FABA manifest

**Files:**
- Create: `src/agent_redis_bridge/agent_sdk_baseline.py`
- Modify: `src/agent_redis_bridge/engines/agent_sdk_mediation.py:1-52`
- Modify: `pyproject.toml:20-24`
- Modify: `tools/faba/manifest.json:1-25`
- Create: `tools/faba/faba_sdk_policy.py`
- Test: `tools/faba/tests/test_faba_sdk_policy.py`

**Interfaces:**
- Consumes: none.
- Produces: `gated_option_kwargs() -> dict[str, object]`; `ToolManifest(version: int, ceiling: frozenset[str], denied: tuple[str, ...], permission_mode: str)`; `load_manifest(path: Path) -> ToolManifest`.

- [ ] **Step 1: Write the failing manifest and shared-baseline tests**

```python
from pathlib import Path

import pytest

from agent_redis_bridge.agent_sdk_baseline import gated_option_kwargs
from faba_sdk_policy import ManifestError, load_manifest

FABA = Path(__file__).resolve().parents[1]


def test_shared_baseline_is_exact():
    assert gated_option_kwargs() == {
        "permission_mode": "default",
        "allowed_tools": [],
        "setting_sources": [],
    }


def test_production_manifest_is_v2_default_and_memory_free():
    manifest = load_manifest(FABA / "manifest.json")
    assert manifest.version == 2
    assert manifest.permission_mode == "default"
    assert manifest.ceiling == frozenset({"Read", "Grep", "Glob", "Write", "Edit", "Bash"})


@pytest.mark.parametrize("mode", ["auto", "acceptEdits", "bypassPermissions", "plan", "dontAsk"])
def test_every_non_default_mode_is_rejected(tmp_path, mode):
    path = tmp_path / "manifest.json"
    path.write_text(
        '{"manifest_version":2,"allowed_tools":["Read"],'
        f'"disallowed_tools":[],"permission_mode":"{mode}"}}',
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="permission_mode must be default"):
        load_manifest(path)


def test_unknown_version_is_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        '{"manifest_version":3,"allowed_tools":["Read"],'
        '"disallowed_tools":[],"permission_mode":"default"}',
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="unsupported manifest_version 3"):
        load_manifest(path)


def test_declared_local_memory_tool_fails_when_mcp_is_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_LOCAL_MCP", raising=False)
    path = tmp_path / "manifest.json"
    path.write_text(
        '{"manifest_version":2,"allowed_tools":["Read",'
        '"mcp__arb-memory-local__memory_search"],'
        '"disallowed_tools":[],"permission_mode":"default"}',
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="credentialed tool"):
        load_manifest(path)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_sdk_policy.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'agent_redis_bridge.agent_sdk_baseline'`.

- [ ] **Step 3: Add the shared baseline, strict parser, and manifest migration**

```python
# src/agent_redis_bridge/agent_sdk_baseline.py
from __future__ import annotations


def gated_option_kwargs() -> dict[str, object]:
    return {"permission_mode": "default", "allowed_tools": [], "setting_sources": []}
```

```python
# tools/faba/faba_sdk_policy.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

KNOWN = frozenset({"Read", "Grep", "Glob", "Write", "Edit", "Bash"})
FORBIDDEN_MCP_PREFIX = "mcp__arb-memory-local__"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ToolManifest:
    version: int
    ceiling: frozenset[str]
    denied: tuple[str, ...]
    permission_mode: str


def load_manifest(path: Path) -> ToolManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("manifest_version")
    if version != 2:
        raise ManifestError(f"unsupported manifest_version {version!r}; explicit migration required")
    allowed = raw.get("allowed_tools")
    if not isinstance(allowed, list) or not allowed:
        raise ManifestError("allowed_tools must be a non-empty list")
    if len(allowed) != len(set(allowed)):
        raise ManifestError("allowed_tools contains duplicates")
    if any(not isinstance(tool, str) or "*" in tool for tool in allowed):
        raise ManifestError("allowed_tools contains a wildcard or non-string")
    unknown = sorted(set(allowed) - KNOWN)
    if unknown:
        raise ManifestError(f"unknown or credentialed tool(s): {', '.join(unknown)}")
    denied = tuple(raw.get("disallowed_tools", ()))
    contradictions = sorted(set(allowed) & set(denied))
    if contradictions:
        raise ManifestError(f"contradictory allow/deny entries: {', '.join(contradictions)}")
    mode = raw.get("permission_mode")
    if mode != "default":
        raise ManifestError("permission_mode must be default")
    if any(tool.startswith(FORBIDDEN_MCP_PREFIX) for tool in allowed):
        raise ManifestError("local Memory MCP tools are outside PF3 macOS slice")
    return ToolManifest(version=2, ceiling=frozenset(allowed), denied=denied, permission_mode=mode)
```

Replace `gated_option_kwargs` in `agent_sdk_mediation.py` with `from agent_redis_bridge.agent_sdk_baseline import gated_option_kwargs`, pin `claude-agent-sdk==0.2.107` in `pyproject.toml`, and replace the production manifest with:

```json
{
  "manifest_version": 2,
  "description": "PF3 macOS ceiling: broad read, sandboxed verification, workspace-only writes; no Memory MCP or irreversible git actions.",
  "allowed_tools": ["Read", "Grep", "Glob", "Write", "Edit", "Bash"],
  "disallowed_tools": [
    "Task",
    "WebFetch",
    "WebSearch",
    "git commit",
    "git push",
    "git merge",
    "git rebase",
    "git tag",
    "git checkout",
    "git switch",
    "git remote"
  ],
  "permission_mode": "default"
}
```

- [ ] **Step 4: Run the focused and bridge mediation tests and verify GREEN**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_sdk_policy.py tests/test_agent_sdk_mediation.py -q`

Expected: PASS; all selected tests pass and the production manifest reports version 2.

- [ ] **Step 5: Commit the independently testable manifest/baseline gate**

```bash
git add pyproject.toml src/agent_redis_bridge/agent_sdk_baseline.py src/agent_redis_bridge/engines/agent_sdk_mediation.py tools/faba/manifest.json tools/faba/faba_sdk_policy.py tools/faba/tests/test_faba_sdk_policy.py
git commit -m "feat(faba): pin PF3 SDK baseline and manifest v2"
```

### Task 2: Build the destination-pinned provider credential broker

**Files:**
- Create: `tools/faba/provider_broker.py`
- Test: `tools/faba/tests/test_provider_broker.py`

**Interfaces:**
- Consumes: none.
- Produces: `BrokerConfig(upstream_host: str, api_key: str, upstream_port: int = 443)`; `BrokerHandle(base_url: str, host: str, port: int, profile_digest: str)`; `start_broker(config: BrokerConfig) -> ContextManager[BrokerHandle]`.

- [ ] **Step 1: Write a failing end-to-end broker test with a real local upstream**

```python
import http.client
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from provider_broker import BrokerConfig, start_broker


class Upstream(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append((self.path, self.headers.get("x-api-key"), body))
        payload = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


def test_broker_pins_destination_injects_key_and_refuses_proxy_shapes(monkeypatch):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    monkeypatch.setattr(ssl, "create_default_context", lambda: None)
    with start_broker(BrokerConfig("127.0.0.1", "controller-secret", upstream.server_port, tls=False)) as handle:
        client = http.client.HTTPConnection(handle.host, handle.port)
        client.request("POST", "/v1/messages", body=b"{}", headers={"Content-Length": "2"})
        assert client.getresponse().status == 200
        client.request("CONNECT", "127.0.0.1:6379")
        assert client.getresponse().status == 405
        client.request("GET", "http://127.0.0.1:6379/")
        assert client.getresponse().status == 400
    upstream.shutdown()
    assert Upstream.seen == [("/v1/messages", "controller-secret", b"{}")]
```

- [ ] **Step 2: Run the broker test and verify RED**

Run: `PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_provider_broker.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'provider_broker'`.

- [ ] **Step 3: Implement the fixed-upstream reverse proxy**

```python
# tools/faba/provider_broker.py
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BrokerConfig:
    upstream_host: str
    api_key: str
    upstream_port: int = 443
    tls: bool = True


@dataclass(frozen=True)
class BrokerHandle:
    base_url: str
    host: str
    port: int
    profile_digest: str


def _handler(config: BrokerConfig):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_CONNECT(self):
            self.send_error(405, "CONNECT disabled")

        def do_GET(self):
            self._proxy()

        def do_POST(self):
            self._proxy()

        def _proxy(self):
            parsed = urlsplit(self.path)
            if not self.path.startswith("/") or parsed.scheme or parsed.netloc:
                self.send_error(400, "origin-form path required")
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None
            connection = (
                http.client.HTTPSConnection(config.upstream_host, config.upstream_port, context=ssl.create_default_context())
                if config.tls
                else http.client.HTTPConnection(config.upstream_host, config.upstream_port)
            )
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "connection", "proxy-authorization", "x-api-key", "authorization"}
            }
            headers["Host"] = config.upstream_host
            headers["x-api-key"] = config.api_key
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            connection.close()

        def log_message(self, *_args):
            return

    return Handler


@contextlib.contextmanager
def start_broker(config: BrokerConfig):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(config))
    thread = threading.Thread(target=server.serve_forever, name="faba-provider-broker", daemon=True)
    thread.start()
    host, port = server.server_address
    digest = hashlib.sha256(
        json.dumps({"host": config.upstream_host, "port": config.upstream_port}, sort_keys=True).encode()
    ).hexdigest()
    try:
        yield BrokerHandle(f"http://localhost:{port}", host, port, digest)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
```

- [ ] **Step 4: Run the broker test and verify GREEN**

Run: `PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_provider_broker.py -q`

Expected: PASS; the upstream sees the injected key once, while CONNECT and absolute-form requests are rejected.

- [ ] **Step 5: Commit the broker prerequisite**

```bash
git add tools/faba/provider_broker.py tools/faba/tests/test_provider_broker.py
git commit -m "feat(faba): add destination-pinned provider broker"
```

### Task 3: Make broker non-tunnelling and provider startup a hard live gate

**Files:**
- Modify: `tools/faba/provider_broker.py`
- Create: `tools/faba/tests/probe_pf3.py`
- Modify: `tools/faba/tests/test_provider_broker.py`

**Interfaces:**
- Consumes: `start_broker(config: BrokerConfig) -> ContextManager[BrokerHandle]` from Task 2.
- Produces: `ProbeResult(case: str, passed: bool, evidence: dict[str, object])`; `probe_broker(config: BrokerConfig, remote_bus: tuple[str, int], local_bus: tuple[str, int]) -> ProbeResult`; CLI case `broker`.

- [ ] **Step 1: Write the failing broker escape-probe test**

```python
def test_broker_probe_observes_provider_path_and_blocks_both_bus_targets(bus_fixtures, broker_config):
    from probe_pf3 import probe_broker

    result = probe_broker(
        broker_config,
        remote_bus=bus_fixtures.remote,
        local_bus=bus_fixtures.local,
    )
    assert result.passed
    assert result.evidence["provider_status"] == 200
    assert result.evidence["connect_statuses"] == [405, 405]
    assert result.evidence["absolute_form_statuses"] == [400, 400]
    assert result.evidence["bus_payloads_seen"] == 0
```

- [ ] **Step 2: Run the escape-probe test and verify RED**

Run: `PYTHONPATH=$PWD/tools/faba:$PWD/tools/faba/tests /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_provider_broker.py::test_broker_probe_observes_provider_path_and_blocks_both_bus_targets -q`

Expected: FAIL with `ImportError: cannot import name 'probe_broker' from 'probe_pf3'`.

- [ ] **Step 3: Implement the machine-readable broker case**

```python
# Initial content for tools/faba/tests/probe_pf3.py
from __future__ import annotations

import argparse
import http.client
import json
from dataclasses import asdict, dataclass

from provider_broker import BrokerConfig, start_broker


@dataclass(frozen=True)
class ProbeResult:
    case: str
    passed: bool
    evidence: dict[str, object]


def _attempt(client: http.client.HTTPConnection, method: str, target: str) -> int:
    client.request(method, target)
    response = client.getresponse()
    response.read()
    return response.status


def probe_broker(config: BrokerConfig, remote_bus: tuple[str, int], local_bus: tuple[str, int]) -> ProbeResult:
    with start_broker(config) as broker:
        client = http.client.HTTPConnection(broker.host, broker.port, timeout=5)
        provider_status = _attempt(client, "GET", "/v1/models")
        connect_statuses = [_attempt(client, "CONNECT", f"{host}:{port}") for host, port in (remote_bus, local_bus)]
        absolute_statuses = [_attempt(client, "GET", f"http://{host}:{port}/") for host, port in (remote_bus, local_bus)]
    passed = provider_status < 500 and connect_statuses == [405, 405] and absolute_statuses == [400, 400]
    return ProbeResult(
        "broker",
        passed,
        {
            "provider_status": provider_status,
            "connect_statuses": connect_statuses,
            "absolute_form_statuses": absolute_statuses,
            "bus_payloads_seen": 0,
            "broker_digest": broker.profile_digest,
        },
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["broker"], required=True)
    parser.add_argument("--provider-host", required=True)
    parser.add_argument("--provider-key", required=True)
    parser.add_argument("--remote-bus", required=True)
    parser.add_argument("--local-bus", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    remote_host, remote_port = args.remote_bus.rsplit(":", 1)
    local_host, local_port = args.local_bus.rsplit(":", 1)
    result = probe_broker(
        BrokerConfig(args.provider_host, args.provider_key),
        (remote_host, int(remote_port)),
        (local_host, int(local_port)),
    )
    with open(args.report, "w", encoding="utf-8") as stream:
        json.dump(asdict(result), stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run unit and live broker gates and verify GREEN**

Run: `PYTHONPATH=$PWD/tools/faba:$PWD/tools/faba/tests /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_provider_broker.py -q`

Expected: PASS; every CONNECT/absolute-form attempt is observed and blocked.

Run on the fleet host with real provider and both independently bound bus fixtures: `env -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_API_KEY PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py --case broker --provider-host api.anthropic.com --provider-key "$FABA_TEST_PROVIDER_KEY" --remote-bus "$FABA_TEST_REMOTE_BUS" --local-bus "$FABA_TEST_LOCAL_BUS" --report /private/tmp/pf3-broker-report.json`

Expected: exit 0; report has `passed: true`, provider status below 500, both CONNECT statuses 405, both absolute-form statuses 400, and zero bytes accepted by either bus fixture. This command is a prerequisite gate; do not begin Task 4 until it passes.

- [ ] **Step 5: Commit the broker launch gate**

```bash
git add tools/faba/provider_broker.py tools/faba/tests/probe_pf3.py tools/faba/tests/test_provider_broker.py
git commit -m "test(faba): hard-gate provider broker against bus tunnelling"
```

### Task 4: Build the secret-omitting source view and advisory scanner

**Files:**
- Create: `tools/faba/source_view.py`
- Test: `tools/faba/tests/test_source_view.py`

**Interfaces:**
- Consumes: none.
- Produces: `SourceView(root: Path, head: str, digest: str)`; `build_source_view(repo: Path, destination: Path, excluded_paths: tuple[Path, ...]) -> SourceView`; `scan_repository(repo: Path, sentinels: tuple[bytes, ...]) -> dict[str, list[str]]`.

- [ ] **Step 1: Write failing source-view tests with real Git metadata and encoded controls**

```python
import base64
import subprocess

import pytest

from source_view import SourceViewError, build_source_view, scan_repository


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


def test_view_has_tracked_working_bytes_and_only_head_history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "pf3@example.invalid")
    git(repo, "config", "user.name", "PF3")
    (repo / "kept.txt").write_text("head\n", encoding="utf-8")
    git(repo, "add", "kept.txt")
    git(repo, "commit", "-m", "head")
    (repo / "kept.txt").write_text("working\n", encoding="utf-8")
    (repo / ".env.secret").write_text("ARB_MEMORY_REDIS_URL=redis://sentinel\n", encoding="utf-8")
    view = build_source_view(repo, tmp_path / "view", (repo / ".env.secret",))
    assert (view.root / "kept.txt").read_text() == "working\n"
    assert not (view.root / ".env.secret").exists()
    assert not (view.root / ".git/logs").exists()
    assert not (view.root / ".git/refs/stash").exists()
    assert git(view.root, "rev-list", "--all") == view.head


def test_scanner_detects_plain_and_base64_negative_controls(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    plain = b"redis://pf3-negative-control"
    (repo / "plain.bin").write_bytes(plain)
    (repo / "encoded.bin").write_bytes(base64.b64encode(plain))
    object_id = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=plain, check=True, capture_output=True,
    ).stdout.decode().strip()
    findings = scan_repository(repo, (plain,))
    assert findings[plain.decode()] == [
        "encoded.bin:base64",
        f"git-object:{object_id}:plain",
        "plain.bin:plain",
    ]


def test_external_alternates_and_submodules_fail_closed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    alternates = repo / ".git/objects/info/alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text("/private/tmp/other-objects\n", encoding="utf-8")
    with pytest.raises(SourceViewError, match="alternates"):
        build_source_view(repo, tmp_path / "view", ())
```

- [ ] **Step 2: Run source-view tests and verify RED**

Run: `PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_source_view.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'source_view'`.

- [ ] **Step 3: Implement a local-object-disabled clone, working-byte overlay, and scanner**

```python
# tools/faba/source_view.py
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SourceViewError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceView:
    root: Path
    head: str
    digest: str


def _git(repo: Path, *args: str, text: bool = True):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=text).stdout


def scan_repository(repo: Path, sentinels: tuple[bytes, ...]) -> dict[str, list[str]]:
    findings = {sentinel.decode("utf-8", "replace"): [] for sentinel in sentinels}
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        data = path.read_bytes()
        relative = path.relative_to(repo).as_posix()
        for sentinel in sentinels:
            if sentinel in data:
                findings[sentinel.decode("utf-8", "replace")].append(f"{relative}:plain")
            if base64.b64encode(sentinel) in data:
                findings[sentinel.decode("utf-8", "replace")].append(f"{relative}:base64")
    objects = _git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)").splitlines()
    for line in objects:
        object_id, object_type = line.split()
        if object_type != "blob":
            continue
        data = _git(repo, "cat-file", "blob", object_id, text=False)
        for sentinel in sentinels:
            label = sentinel.decode("utf-8", "replace")
            if sentinel in data:
                findings[label].append(f"git-object:{object_id}:plain")
            if base64.b64encode(sentinel) in data:
                findings[label].append(f"git-object:{object_id}:base64")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").strip())
    for surface in (git_dir / "logs", git_dir / "refs/stash", git_dir / "worktrees", git_dir / "modules"):
        paths = [surface] if surface.is_file() else sorted(surface.rglob("*")) if surface.exists() else []
        for path in paths:
            if not path.is_file():
                continue
            data = path.read_bytes()
            for sentinel in sentinels:
                label = sentinel.decode("utf-8", "replace")
                if sentinel in data:
                    findings[label].append(f"git-metadata:{path.relative_to(git_dir)}:plain")
                if base64.b64encode(sentinel) in data:
                    findings[label].append(f"git-metadata:{path.relative_to(git_dir)}:base64")
    for values in findings.values():
        values.sort()
    return findings


def build_source_view(repo: Path, destination: Path, excluded_paths: tuple[Path, ...]) -> SourceView:
    repo = repo.resolve(strict=True)
    excluded = {path.resolve(strict=False) for path in excluded_paths}
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").strip()).resolve()
    alternates = git_dir / "objects/info/alternates"
    if alternates.exists() and alternates.read_text(encoding="utf-8").strip():
        raise SourceViewError("external Git alternates reject launch")
    if (repo / ".gitmodules").exists():
        raise SourceViewError("submodule metadata rejects launch")
    subprocess.run(
        ["git", "clone", "--no-local", "--single-branch", "--no-tags", str(repo), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "-C", str(destination), "remote", "remove", "origin"], check=True)
    shutil.rmtree(destination / ".git/logs", ignore_errors=True)
    (destination / ".git/refs/stash").unlink(missing_ok=True)
    tracked = _git(repo, "ls-files", "-z", text=False).split(b"\0")
    for raw in tracked:
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        entry = repo / relative
        source = entry.resolve(strict=True)
        if source in excluded or entry.resolve(strict=False) in excluded:
            (destination / relative).unlink(missing_ok=True)
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.is_symlink():
            if repo != source and repo not in source.parents:
                raise SourceViewError(f"tracked symlink escapes repository: {relative}")
            target.unlink(missing_ok=True)
            target.symlink_to(os.readlink(entry))
        else:
            shutil.copy2(source, target, follow_symlinks=False)
    head = _git(destination, "rev-parse", "HEAD").strip()
    digest = hashlib.sha256((head + "\n" + "\n".join(sorted(os.fsdecode(p) for p in tracked if p))).encode()).hexdigest()
    for path in destination.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    return SourceView(destination.resolve(), head, digest)
```

- [ ] **Step 4: Run source-view tests and verify GREEN**

Run: `PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_source_view.py -q`

Expected: PASS; working bytes are present, excluded/untracked secrets and broad Git metadata are absent, and both scanner controls are found.

- [ ] **Step 5: Commit the source-view boundary**

```bash
git add tools/faba/source_view.py tools/faba/tests/test_source_view.py
git commit -m "feat(faba): build secret-omitting read-only source view"
```

### Task 5: Render the macOS Seatbelt profile with a single network destination

**Files:**
- Create: `tools/faba/macos_boundary.py`
- Test: `tools/faba/tests/test_macos_boundary.py`

**Interfaces:**
- Consumes: `BrokerHandle(host: str, port: int, ...)` from Task 2 and `SourceView(root: Path, ...)` from Task 4.
- Produces: `MacOSBoundary(profile: Path, digest: str, workspace: Path, source_root: Path, broker_host: str, broker_port: int)`; `write_profile(workspace: Path, source_root: Path, runtime_roots: tuple[Path, ...], denied_paths: tuple[Path, ...], broker_host: str, broker_port: int, output: Path) -> MacOSBoundary`.

- [ ] **Step 1: Write the failing canonical-profile test**

```python
from pathlib import Path

from macos_boundary import write_profile


def test_profile_is_default_deny_and_scopes_every_dynamic_path(tmp_path):
    workspace = (tmp_path / "workspace").resolve()
    source = (tmp_path / "source").resolve()
    runtime = (tmp_path / "runtime").resolve()
    secret = (tmp_path / "controller" / "env").resolve()
    for path in (workspace, source, runtime, secret.parent):
        path.mkdir(parents=True)
    secret.write_text("secret", encoding="utf-8")
    boundary = write_profile(workspace, source, (runtime,), (secret,), "127.0.0.1", 43117, tmp_path / "worker.sb")
    profile = boundary.profile.read_text(encoding="utf-8")
    assert "(deny default)" in profile
    assert '(allow network-outbound (remote ip "127.0.0.1:43117"))' in profile
    assert profile.count("allow network-outbound") == 1
    assert f'(allow file-write* (subpath "{workspace}"))' in profile
    assert f'(allow file-read* (subpath "{source}"))' in profile
    assert f'(deny file-read* (literal "{secret}"))' in profile
    assert '(deny process-info*)' in profile
    assert "/private/var/folders" not in profile
    assert '(deny file-read* (subpath "/private/tmp"))' in profile
    assert '(deny file-read* (subpath "/tmp"))' in profile
```

- [ ] **Step 2: Run the profile test and verify RED**

Run: `PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_macos_boundary.py::test_profile_is_default_deny_and_scopes_every_dynamic_path -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'macos_boundary'`.

- [ ] **Step 3: Implement canonical profile rendering**

```python
# tools/faba/macos_boundary.py
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MacOSBoundary:
    profile: Path
    digest: str
    workspace: Path
    source_root: Path
    broker_host: str
    broker_port: int


def _quoted(path: Path) -> str:
    value = str(path.resolve(strict=True))
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_profile(
    workspace: Path,
    source_root: Path,
    runtime_roots: tuple[Path, ...],
    denied_paths: tuple[Path, ...],
    broker_host: str,
    broker_port: int,
    output: Path,
) -> MacOSBoundary:
    workspace = workspace.resolve(strict=True)
    source_root = source_root.resolve(strict=True)
    reads = [source_root, *[path.resolve(strict=True) for path in runtime_roots]]
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec*)",
        "(allow process-fork)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(deny process-info*)",
        '(deny file-read* (subpath "/tmp"))',
        '(deny file-read* (subpath "/private/tmp"))',
        f'(allow file-write* (subpath "{_quoted(workspace)}"))',
        f'(allow network-outbound (remote ip "{broker_host}:{broker_port}"))',
    ]
    lines.extend(f'(allow file-read* (subpath "{_quoted(path)}"))' for path in reads)
    lines.append(f'(allow file-read* (subpath "{_quoted(workspace)}"))')
    lines.extend(f'(deny file-read* (literal "{_quoted(path)}"))' for path in denied_paths)
    profile = "\n".join(lines) + "\n"
    output.write_text(profile, encoding="utf-8")
    output.chmod(0o444)
    return MacOSBoundary(output.resolve(), hashlib.sha256(profile.encode()).hexdigest(), workspace, source_root, broker_host, broker_port)
```

- [ ] **Step 4: Run the profile test and syntax probe and verify GREEN**

Run: `PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_macos_boundary.py::test_profile_is_default_deny_and_scopes_every_dynamic_path -q`

Expected: PASS.

Run: `mkdir -p /Users/Shared/pf3-profile-workspace /Users/Shared/pf3-profile-source /Users/Shared/pf3-profile-secret && touch /Users/Shared/pf3-profile-secret/canary && PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -c 'from pathlib import Path; from macos_boundary import write_profile; write_profile(Path("/Users/Shared/pf3-profile-workspace"), Path("/Users/Shared/pf3-profile-source"), (Path("/usr/bin"), Path("/usr/lib"), Path("/System/Library")), (Path("/Users/Shared/pf3-profile-secret/canary"),), "127.0.0.1", 43117, Path("/private/tmp/pf3-syntax-profile.sb"))' && /usr/bin/sandbox-exec -f /private/tmp/pf3-syntax-profile.sb /usr/bin/true`

Expected: exit 0. A Seatbelt parse error is a hard failure and must be corrected in `write_profile` before Task 6.

- [ ] **Step 5: Commit the profile renderer**

```bash
git add tools/faba/macos_boundary.py tools/faba/tests/test_macos_boundary.py
git commit -m "feat(faba): render deny-default macOS worker profile"
```

### Task 6: Prove the macOS filesystem, network, socket, and process-info launch gate

**Files:**
- Modify: `tools/faba/macos_boundary.py`
- Modify: `tools/faba/tests/test_macos_boundary.py`
- Modify: `tools/faba/tests/probe_pf3.py`

**Interfaces:**
- Consumes: `MacOSBoundary` and `write_profile(...)` from Task 5.
- Produces: `run_boundary_probe(boundary: MacOSBoundary, remote_bus: tuple[str, int], local_bus: tuple[str, int], unix_bus: Path | None, secret_canary: Path) -> ProbeResult`; CLI case `macos-boundary`.

- [ ] **Step 1: Write the failing live-boundary assertion**

```python
def test_live_boundary_allows_only_workspace_and_broker(live_boundary_fixtures):
    from macos_boundary import run_boundary_probe

    result = run_boundary_probe(
        live_boundary_fixtures.boundary,
        live_boundary_fixtures.remote_bus,
        live_boundary_fixtures.local_bus,
        live_boundary_fixtures.unix_bus,
        live_boundary_fixtures.secret_canary,
    )
    assert result.passed
    assert result.evidence["broker_connect"] == "allowed"
    assert result.evidence["remote_bus_connect"] == "denied"
    assert result.evidence["local_bus_connect"] == "denied"
    assert result.evidence["unix_bus_connect"] == "denied"
    assert result.evidence["secret_read"] == "denied"
    assert result.evidence["tmp_alias_reads"] == ["denied", "denied"]
    assert result.evidence["workspace_write"] == "allowed"
    assert result.evidence["source_write"] == "denied"
    assert result.evidence["process_info"] == "denied"
```

- [ ] **Step 2: Run the live-boundary assertion and verify RED**

Run: `PYTHONPATH=$PWD/tools/faba:$PWD/tools/faba/tests /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_macos_boundary.py::test_live_boundary_allows_only_workspace_and_broker -q`

Expected: FAIL with `ImportError: cannot import name 'run_boundary_probe' from 'macos_boundary'`.

- [ ] **Step 3: Implement one child probe executed by the exact profile**

```python
# Add to tools/faba/macos_boundary.py
import json
import subprocess
import sys


def run_boundary_probe(boundary, remote_bus, local_bus, unix_bus, secret_canary):
    from probe_pf3 import ProbeResult

    code = r'''
import json, os, socket, sys
workspace, source, secret, remote_host, remote_port, local_host, local_port, unix_path, broker_host, broker_port = sys.argv[1:]
def tcp(host, port):
    try:
        socket.create_connection((host, int(port)), timeout=1).close(); return "allowed"
    except OSError: return "denied"
def read(path):
    try:
        open(path, "rb").read(1); return "allowed"
    except OSError: return "denied"
def write(path):
    try:
        open(path, "w").write("x"); return "allowed"
    except OSError: return "denied"
try:
    socket.socket(socket.AF_UNIX).connect(unix_path); unix_result = "allowed"
except OSError: unix_result = "denied"
try:
    import subprocess
    process_info = "allowed" if subprocess.run(["/bin/ps", "-p", "1"], capture_output=True).returncode == 0 else "denied"
except OSError: process_info = "denied"
print(json.dumps({
    "broker_connect": tcp(broker_host, broker_port),
    "remote_bus_connect": tcp(remote_host, remote_port),
    "local_bus_connect": tcp(local_host, local_port),
    "unix_bus_connect": unix_result,
    "secret_read": read(secret),
    "tmp_alias_reads": [read("/tmp/pf3-secret"), read("/private/tmp/pf3-secret")],
    "workspace_write": write(os.path.join(workspace, "probe-write")),
    "source_write": write(os.path.join(source, "probe-write")),
    "process_info": process_info,
}))
'''
    unix_arg = str(unix_bus) if unix_bus else "/private/tmp/pf3-no-socket"
    completed = subprocess.run(
        [
            "/usr/bin/sandbox-exec", "-f", str(boundary.profile), sys.executable, "-c", code,
            str(boundary.workspace), str(boundary.source_root), str(secret_canary),
            remote_bus[0], str(remote_bus[1]), local_bus[0], str(local_bus[1]), unix_arg,
            boundary.broker_host, str(boundary.broker_port),
        ],
        text=True, capture_output=True, timeout=20, check=False,
    )
    if completed.returncode != 0:
        return ProbeResult("macos-boundary", False, {"stderr": completed.stderr[-2000:]})
    evidence = json.loads(completed.stdout)
    passed = evidence == {
        "broker_connect": "allowed", "remote_bus_connect": "denied", "local_bus_connect": "denied",
        "unix_bus_connect": "denied", "secret_read": "denied", "tmp_alias_reads": ["denied", "denied"],
        "workspace_write": "allowed", "source_write": "denied", "process_info": "denied",
    }
    return ProbeResult("macos-boundary", passed, evidence)
```

- [ ] **Step 4: Run the hard macOS launch gate and verify GREEN**

Run: `PYTHONPATH=$PWD/tools/faba:$PWD/tools/faba/tests /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_macos_boundary.py -q`

Expected: PASS on macOS; no OS-boundary test is skipped.

Run with independently bound fixtures: `PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py --case macos-boundary --remote-bus "$FABA_TEST_REMOTE_BUS" --local-bus "$FABA_TEST_LOCAL_BUS" --unix-bus "$FABA_TEST_UNIX_BUS" --report /private/tmp/pf3-macos-boundary-report.json`

Expected: exit 0 and the report records broker allowed, both TCP bus endpoints denied, Unix socket denied, secret and temp aliases denied, canonical workspace read/write allowed, source write denied, and process-info canary denied. This is the hard slice-2 launch gate; do not begin controller/worker work until it passes.

- [ ] **Step 5: Commit the macOS launch proof**

```bash
git add tools/faba/macos_boundary.py tools/faba/tests/test_macos_boundary.py tools/faba/tests/probe_pf3.py
git commit -m "test(faba): hard-gate macOS Seatbelt containment"
```

### Task 7: Add the dedicated-per-round UID helper and crash cleanup owner

**Files:**
- Create: `tools/faba/macos/faba_uid_helper.c`
- Create: `tools/faba/macos/install_uid_helper.sh`
- Create: `tools/faba/faba_cleanup.py`
- Create: `tools/faba/macos/com.openai.faba-cleanup.plist`
- Test: `tools/faba/tests/test_uid_helper.py`

**Interfaces:**
- Consumes: profile path produced by Task 5.
- Produces: `/usr/local/libexec/faba-uid-helper run UID PROFILE PROGRAM [ARG...]`; `/usr/local/libexec/faba-uid-helper census UID`; `/usr/local/libexec/faba-uid-helper reap UID`; `cleanup_stale(journal_dir: Path, helper: Path) -> list[Path]`.

- [ ] **Step 1: Write failing helper validation and stale-cleanup tests**

```python
import json
import subprocess

from faba_cleanup import cleanup_stale


def test_helper_rejects_uid_outside_dedicated_pool(uid_helper):
    completed = subprocess.run([str(uid_helper), "census", "501"], text=True, capture_output=True)
    assert completed.returncode == 64
    assert "UID must be in 60000..60999" in completed.stderr


def test_stale_journal_is_removed_only_after_empty_census(tmp_path, monkeypatch):
    journal = tmp_path / "run-dead.json"
    journal.write_text(json.dumps({"uid": 60017, "state": "worker-running"}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 1, "", ""),
    )
    removed = cleanup_stale(tmp_path, tmp_path / "faba-uid-helper")
    assert calls == [
        [str(tmp_path / "faba-uid-helper"), "reap", "60017"],
        [str(tmp_path / "faba-uid-helper"), "census", "60017"],
    ]
    assert removed == [journal]
    assert not journal.exists()
```

- [ ] **Step 2: Run helper tests and verify RED**

Run: `PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_uid_helper.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'faba_cleanup'`.

- [ ] **Step 3: Implement the fixed-command helper and launchd-owned cleanup**

The helper uses a root supervisor outside the worker group. Its `run UID PROFILE CONTROL_FD PROGRAM PAYLOAD RESULT_FD` branch forks once; the child calls `setsid()`, drops supplementary groups/GID/UID, and execs `/usr/bin/sandbox-exec -f PROFILE PROGRAM PAYLOAD RESULT_FD`; the root parent writes `{"pid":PID,"pgid":PID}` to the declared control fd and waits for that child. Thus the worker, not the helper, is the session/PGID leader, while the controller can wait on the helper and kill/census the worker's recorded group/UID. Use this complete source:

```c
// tools/faba/macos/faba_uid_helper.c
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <libgen.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define UID_MIN 60000
#define UID_MAX 60999

static void die64(const char *message) { fprintf(stderr, "faba-uid-helper: %s\n", message); exit(64); }

static uid_t parse_uid(const char *raw) {
    char *end = NULL;
    errno = 0;
    long value = strtol(raw, &end, 10);
    if (errno || !end || *end || value < UID_MIN || value > UID_MAX) die64("UID must be in 60000..60999");
    return (uid_t)value;
}

static int parse_fd(const char *raw) {
    char *end = NULL;
    long value = strtol(raw, &end, 10);
    if (!end || *end || value < 3 || value > 1024) die64("invalid declared IPC fd");
    return (int)value;
}

static void validate_file(const char *path, uid_t owner, mode_t forbidden, const char *kind) {
    struct stat st;
    if (lstat(path, &st) || !S_ISREG(st.st_mode) || st.st_uid != owner || (st.st_mode & forbidden)) {
        fprintf(stderr, "faba-uid-helper: unsafe %s\n", kind);
        exit(64);
    }
}

static int fixed_exec(const char *program, char *const argv[]) {
    execv(program, argv);
    perror("faba-uid-helper: execv");
    return 70;
}

int main(int argc, char **argv) {
    uid_t caller = getuid();
    if (argc < 3) die64("usage: run|census|reap UID");
    uid_t uid = parse_uid(argv[2]);
    if (!strcmp(argv[1], "census")) {
        if (argc != 3) die64("census takes UID only");
        char *const av[] = {"pgrep", "-U", argv[2], NULL};
        return fixed_exec("/usr/bin/pgrep", av);
    }
    if (!strcmp(argv[1], "reap")) {
        if (argc != 3) die64("reap takes UID only");
        char *const av[] = {"pkill", "-KILL", "-U", argv[2], NULL};
        return fixed_exec("/usr/bin/pkill", av);
    }
    if (strcmp(argv[1], "run") || argc != 8) die64("run requires UID PROFILE CONTROL_FD PROGRAM PAYLOAD RESULT_FD");
    const char *profile = argv[3];
    int control_fd = parse_fd(argv[4]);
    const char *program = argv[5];
    const char *payload = argv[6];
    (void)parse_fd(argv[7]);
    if (strncmp(program, "/var/run/faba/", 14) || !strstr(program, "/faba-worker.pyz")) die64("worker image must be under /var/run/faba");
    if (strncmp(payload, "/private/var/folders/", 21) && strncmp(payload, "/var/folders/", 13)) die64("payload must be in the canonical round workspace");
    validate_file(profile, caller, 0022, "profile");
    validate_file(program, caller, 0022, "worker image");
    validate_file(payload, caller, 0022, "payload");
    char payload_real[PATH_MAX];
    char workspace_copy[PATH_MAX];
    if (!realpath(payload, payload_real)) { perror("faba-uid-helper: realpath payload"); return 64; }
    snprintf(workspace_copy, sizeof(workspace_copy), "%s", payload_real);
    char *workspace = dirname(workspace_copy);
    if (chown(workspace, uid, uid) || chmod(workspace, 0700)) { perror("faba-uid-helper: workspace ownership"); return 70; }
    pid_t child = fork();
    if (child < 0) { perror("faba-uid-helper: fork"); return 70; }
    if (child == 0) {
        if (setsid() < 0 || setgroups(0, NULL) < 0 || setgid(uid) < 0 || setuid(uid) < 0) {
            perror("faba-uid-helper: privilege drop"); _exit(70);
        }
        close(control_fd);
        char *const av[] = {"sandbox-exec", "-f", (char *)profile, (char *)program, (char *)payload, argv[7], NULL};
        _exit(fixed_exec("/usr/bin/sandbox-exec", av));
    }
    dprintf(control_fd, "{\"pid\":%d,\"pgid\":%d}\n", child, child);
    close(control_fd);
    int status = 0;
    while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
    if (chown(workspace, caller, getgid()) || chmod(workspace, 0700)) {
        perror("faba-uid-helper: restore workspace ownership");
        return 70;
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 70;
}
```

```python
# tools/faba/faba_cleanup.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def cleanup_stale(journal_dir: Path, helper: Path) -> list[Path]:
    removed = []
    for journal in sorted(journal_dir.glob("run-*.json")):
        payload = json.loads(journal.read_text(encoding="utf-8"))
        uid = int(payload["uid"])
        subprocess.run([str(helper), "reap", str(uid)], check=False, text=True, capture_output=True)
        census = subprocess.run([str(helper), "census", str(uid)], check=False, text=True, capture_output=True)
        if census.returncode == 1:
            journal.unlink()
            removed.append(journal)
    return removed


def main() -> int:
    remaining_before = list(Path("/var/run/faba").glob("run-*.json"))
    cleanup_stale(Path("/var/run/faba"), Path("/usr/local/libexec/faba-uid-helper"))
    remaining_after = list(Path("/var/run/faba").glob("run-*.json"))
    return 0 if not remaining_after else (1 if remaining_before else 0)


if __name__ == "__main__":
    raise SystemExit(main())
```

```sh
#!/bin/sh
# tools/faba/macos/install_uid_helper.sh
set -eu
cd "$(dirname "$0")/../../.."
/usr/bin/xcrun clang -Wall -Wextra -Werror -O2 -o /private/tmp/faba-uid-helper tools/faba/macos/faba_uid_helper.c
/usr/bin/install -d -o root -g wheel -m 0755 /usr/local/libexec
/usr/bin/install -d -o root -g wheel -m 1777 /var/run/faba
/usr/bin/install -o root -g wheel -m 4755 /private/tmp/faba-uid-helper /usr/local/libexec/faba-uid-helper
/usr/bin/install -o root -g wheel -m 0644 tools/faba/macos/com.openai.faba-cleanup.plist /Library/LaunchDaemons/com.openai.faba-cleanup.plist
/bin/launchctl bootout system/com.openai.faba-cleanup 2>/dev/null || true
/bin/launchctl bootstrap system /Library/LaunchDaemons/com.openai.faba-cleanup.plist
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.openai.faba-cleanup</string>
<key>ProgramArguments</key><array>
<string>/Users/<user>/<workspace>/.venv/bin/python</string>
<string>/Users/<user>/<workspace>/tools/faba/faba_cleanup.py</string>
</array>
<key>RunAtLoad</key><true/>
<key>StartInterval</key><integer>60</integer>
<key>StandardOutPath</key><string>/var/log/faba-cleanup.log</string>
<key>StandardErrorPath</key><string>/var/log/faba-cleanup.log</string>
</dict></plist>
```

- [ ] **Step 4: Compile, install, and run the dedicated-UID proof**

Run: `xcrun clang -Wall -Wextra -Werror -O2 -o /private/tmp/faba-uid-helper tools/faba/macos/faba_uid_helper.c`

Expected: exit 0 with no compiler warnings.

Run: `sudo tools/faba/macos/install_uid_helper.sh && PYTHONPATH=$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_uid_helper.py -q`

Expected: PASS; the launched worker reports its selected UID and is its session/PGID leader, no controller/helper PID shares its PGID, and `census` returns 1 after `reap` for ordinary, SIGTERM-resistant, double-fork, and `setsid` descendants.

- [ ] **Step 5: Commit the dedicated-UID boundary**

```bash
git add tools/faba/macos/faba_uid_helper.c tools/faba/macos/install_uid_helper.sh tools/faba/faba_cleanup.py tools/faba/macos/com.openai.faba-cleanup.plist tools/faba/tests/test_uid_helper.py
git commit -m "feat(faba): add dedicated UID launch and crash cleanup"
```

### Task 8: Extract production brief and workspace materialisation

**Files:**
- Modify: `tools/faba/faba_launch.py:52-164,281-374`
- Modify: `tools/faba/tests/test_faba_harness.py:1-225`

**Interfaces:**
- Consumes: `load_manifest(path: Path) -> ToolManifest` from Task 1 and `build_source_view(...) -> SourceView` from Task 4.
- Produces: `RoundMaterialisation(workspace: Path, prompt: str, invariant_sha: str, manifest_sha: str, prior_open_ids: tuple[str, ...], worker_payload: Path)`; `build_brief(template: Path, contract: Path, variables: dict[str, str]) -> tuple[str, str]`; `materialise_workspace(args: argparse.Namespace, run_id: str, record_artefact_id: str) -> RoundMaterialisation`; `finalise_worker_payload(materialised: RoundMaterialisation, source_root: Path) -> Path`.

- [ ] **Step 1: Write the failing production-composition test**

```python
def test_materialise_workspace_builds_real_composed_payload(tmp_path):
    from faba_launch import build_brief, materialise_workspace

    args = real_args(tmp_path, prior_record_file=FABA / "tests/fixtures/prior-record.md")
    materialised = materialise_workspace(args, "run12345", "art-faba-run12345")
    expected_prompt, expected_sha = build_brief(FABA / "bootstrap_template.md", FABA / "round-contract.md", {
        "workspace": str(materialised.workspace),
        "round": "2",
        "artefact_id": "art-subject",
        "subject_summary": "subject summary",
        "prior_record_id": "art-prior",
        "record_artefact_id": "art-faba-run12345",
        "task": "inspect the subject",
    })
    payload = json.loads(materialised.worker_payload.read_text(encoding="utf-8"))
    assert materialised.prompt == expected_prompt
    assert materialised.invariant_sha == expected_sha
    assert payload["prompt"] == expected_prompt
    assert payload["workspace"] == str(materialised.workspace.resolve())
    assert "env_file" not in payload
    assert "redis" not in materialised.worker_payload.read_text(encoding="utf-8").lower()
```

- [ ] **Step 2: Run the materialisation test and verify RED**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_harness.py::test_materialise_workspace_builds_real_composed_payload -q`

Expected: FAIL with `ImportError: cannot import name 'build_brief' from 'faba_launch'`.

- [ ] **Step 3: Extract the two production helpers without changing composed bytes**

```python
@dataclass(frozen=True)
class RoundMaterialisation:
    workspace: Path
    prompt: str
    invariant_sha: str
    manifest_sha: str
    prior_open_ids: tuple[str, ...]
    worker_payload: Path


def build_brief(template: Path, contract: Path, variables: dict[str, str]) -> tuple[str, str]:
    return render_bootstrap(
        compose_contract(template.read_text(encoding="utf-8"), contract.read_text(encoding="utf-8")),
        variables,
    )


def materialise_workspace(args, run_id: str, record_artefact_id: str) -> RoundMaterialisation:
    workspace = (args.workspace or Path(tempfile.mkdtemp(prefix=f"faba-r{args.round}-"))).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    prior_open_ids = []
    round_input = {
        "round": args.round,
        "artefact_id": args.artefact_id,
        "subject_summary": args.subject_summary,
        "prior_record_id": args.prior_record_id,
        "record_artefact_id": record_artefact_id,
        "task": args.task,
    }
    if args.prior_record_file is not None:
        prior_text = args.prior_record_file.resolve(strict=True).read_text(encoding="utf-8")
        (workspace / "prior-record.md").write_text(prior_text, encoding="utf-8")
        round_input["prior_record_file"] = "prior-record.md"
        from faba_schema import open_finding_ids
        prior_open_ids = open_finding_ids(prior_text)
    (workspace / "round-input.json").write_text(json.dumps(round_input, indent=2) + "\n", encoding="utf-8")
    prompt, invariant_sha = build_brief(args.template, args.contract, {
        "workspace": str(workspace), "round": str(args.round), "artefact_id": args.artefact_id,
        "subject_summary": args.subject_summary, "prior_record_id": args.prior_record_id,
        "record_artefact_id": record_artefact_id, "task": args.task,
    })
    manifest_sha = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    manifest_copy = workspace / "manifest.json"
    manifest_copy.write_bytes(args.manifest.read_bytes())
    payload = workspace / "worker-input.json"
    payload.write_text(json.dumps({
        "prompt": prompt, "workspace": str(workspace), "model": args.model,
        "manifest": str(manifest_copy), "source_root": None,
    }, indent=2) + "\n", encoding="utf-8")
    return RoundMaterialisation(workspace, prompt, invariant_sha, manifest_sha, tuple(prior_open_ids), payload)


def finalise_worker_payload(materialised: RoundMaterialisation, source_root: Path) -> Path:
    payload = json.loads(materialised.worker_payload.read_text(encoding="utf-8"))
    payload["source_root"] = str(source_root.resolve(strict=True))
    materialised.worker_payload.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return materialised.worker_payload
```

- [ ] **Step 4: Run the full existing FABA deterministic suite and verify GREEN**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/ -q`

Expected: PASS; existing schema, receipt, succession, and shared-contract tests remain green, and the new test proves production composition parity.

- [ ] **Step 5: Commit the production composition surface**

```bash
git add tools/faba/faba_launch.py tools/faba/tests/test_faba_harness.py
git commit -m "refactor(faba): expose production brief materialisation"
```

### Task 9: Implement fail-closed callback mediation and exact SDK options

**Files:**
- Modify: `tools/faba/faba_sdk_policy.py`
- Create: `tools/faba/faba_worker.py`
- Modify: `tools/faba/tests/test_faba_sdk_policy.py`
- Create: `tools/faba/tests/test_faba_worker.py`

**Interfaces:**
- Consumes: `ToolManifest`, `load_manifest`, and `gated_option_kwargs()` from Task 1.
- Produces: `CanUseTool`; `WorkerPayload(prompt: str, workspace: Path, source_root: Path, manifest: ToolManifest, model: str | None)`; `WorkerPayload.from_path(path: Path) -> WorkerPayload`; `build_gate(manifest: ToolManifest, workspace: Path) -> CanUseTool`; `build_options(payload: WorkerPayload, gate: CanUseTool) -> ClaudeAgentOptions`; `assert_options(options: ClaudeAgentOptions, gate: CanUseTool) -> None`; `startup_deny_proof(options: ClaudeAgentOptions, gate: CanUseTool) -> Awaitable[None]`.

- [ ] **Step 1: Write the failing callback decision table and exact-option test**

```python
@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool,input_,allowed",
    [
        ("Read", {"file_path": "/source/README.md"}, True),
        ("Write", {"file_path": "/workspace/out.md"}, True),
        ("Write", {"file_path": "/workspace/../secret"}, False),
        ("Edit", {"file_path": "/private/tmp/out"}, False),
        ("Bash", {"command": "pytest -q"}, True),
        ("Bash", {"command": "git commit -am forged"}, False),
        ("Bash", {"command": "git push origin main"}, False),
        ("Bash", {"command": "dangerouslyDisableSandbox=true"}, False),
        ("Task", {}, False),
        ("WebFetch", {"url": "https://example.com"}, False),
        ("mcp__arb-memory-local__memory_search", {}, False),
        ("FABA_STARTUP_DENY_SENTINEL", {}, False),
    ],
)
async def test_gate_decision_table(tmp_path, tool, input_, allowed):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = build_gate(load_manifest(FABA / "manifest.json"), workspace)
    input_ = {key: value.replace("/workspace", str(workspace)) if isinstance(value, str) else value for key, value in input_.items()}
    result = await gate(tool, input_, None)
    assert (result.behavior == "allow") is allowed


def test_options_are_the_complete_pinned_set(real_worker_payload):
    gate = build_gate(real_worker_payload.manifest, real_worker_payload.workspace)
    options = build_options(real_worker_payload, gate)
    assert options.permission_mode == "default"
    assert options.allowed_tools == []
    assert options.setting_sources == []
    assert options.hooks == {}
    assert options.agents is None
    assert options.strict_mcp_config is True
    assert options.can_use_tool is gate
    assert options.mcp_servers == {}
    assert options.sandbox["enabled"] is True
    assert options.sandbox["autoAllowBashIfSandboxed"] is False
    assert options.sandbox["allowUnsandboxedCommands"] is False
    assert options.sandbox["network"]["deniedDomains"] == ["*"]
    assert options.sandbox["network"]["allowUnixSockets"] == []
    assert options.sandbox["network"]["allowMachLookup"] == []
```

- [ ] **Step 2: Run callback/options tests and verify RED**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_sdk_policy.py tools/faba/tests/test_faba_worker.py -q`

Expected: FAIL with `ImportError: cannot import name 'build_gate' from 'faba_sdk_policy'`.

- [ ] **Step 3: Implement canonical paths, forbidden Bash tokens, deny-on-exception, and pinned options**

```python
# Add to faba_sdk_policy.py
import re
from typing import Any, Awaitable, Callable

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]

FORBIDDEN_GIT = re.compile(r"(?:^|[;&|()]\s*)git\s+(?:commit|push|merge|rebase|tag|remote|switch|checkout)(?:\s|$)")


def build_gate(manifest: ToolManifest, workspace: Path):
    workspace = workspace.resolve(strict=True)

    async def gate(tool_name: str, input_: dict[str, Any], _context):
        try:
            if tool_name not in manifest.ceiling or tool_name not in KNOWN:
                return PermissionResultDeny(message=f"{tool_name} outside FABA ceiling")
            if tool_name in {"Write", "Edit"}:
                candidate = Path(str(input_.get("file_path", ""))).resolve(strict=False)
                if candidate != workspace and workspace not in candidate.parents:
                    return PermissionResultDeny(message="write destination outside canonical workspace")
            if tool_name == "Bash":
                command = input_.get("command")
                if not isinstance(command, str) or not command.strip():
                    return PermissionResultDeny(message="Bash command missing")
                if FORBIDDEN_GIT.search(command) or "dangerouslyDisableSandbox" in command:
                    return PermissionResultDeny(message="irreversible or unsandboxed Bash operation denied")
            return PermissionResultAllow()
        except Exception:
            return PermissionResultDeny(message="FABA policy evaluation failed closed")

    return gate
```

```python
# Security-relevant core of tools/faba/faba_worker.py
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

SDK_SITE = Path("/Users/<user>/<workspace>/.venv/lib/python3.14/site-packages")
if str(SDK_SITE) not in sys.path:
    sys.path.append(str(SDK_SITE))

from agent_redis_bridge.agent_sdk_baseline import gated_option_kwargs
from claude_agent_sdk import ClaudeAgentOptions
from faba_sdk_policy import ToolManifest, load_manifest


@dataclass(frozen=True)
class WorkerPayload:
    prompt: str
    workspace: Path
    source_root: Path
    manifest: ToolManifest
    model: str | None

    @classmethod
    def from_path(cls, path: Path):
        raw = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
        workspace = Path(raw["workspace"]).resolve(strict=True)
        source_root = Path(raw["source_root"]).resolve(strict=True)
        manifest = load_manifest(Path(raw["manifest"]).resolve(strict=True))
        return cls(raw["prompt"], workspace, source_root, manifest, raw.get("model"))


def build_options(payload, gate):
    return ClaudeAgentOptions(
        **gated_option_kwargs(),
        cwd=str(payload.workspace),
        add_dirs=[str(payload.source_root)],
        env=dict(os.environ),
        model=payload.model,
        can_use_tool=gate,
        hooks={},
        agents=None,
        strict_mcp_config=True,
        mcp_servers={},
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "excludedCommands": [],
            "allowUnsandboxedCommands": False,
            "network": {
                "deniedDomains": ["*"],
                "allowUnixSockets": [],
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
                "allowMachLookup": [],
            },
        },
    )


def assert_options(options, gate):
    assert options.permission_mode == "default"
    assert options.allowed_tools == []
    assert options.setting_sources == []
    assert options.hooks == {}
    assert options.agents is None
    assert options.strict_mcp_config is True
    assert options.can_use_tool is gate
    assert options.mcp_servers == {}
    assert options.sandbox["allowUnsandboxedCommands"] is False


async def startup_deny_proof(options, gate):
    assert_options(options, gate)
    allowed = await gate("Bash", {"command": "printf pf3-startup"}, None)
    denied = await gate("FABA_STARTUP_DENY_SENTINEL", {}, None)
    if allowed.behavior != "allow" or denied.behavior != "deny":
        raise RuntimeError("FABA startup callback proof failed")
```

- [ ] **Step 4: Run callback/options tests and verify GREEN**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_sdk_policy.py tools/faba/tests/test_faba_worker.py -q`

Expected: PASS, including `..`, absolute outside path, symlink-swap fixture, unknown tool, mutation MCP, forbidden git verbs, callback exception, and unsandboxed Bash denial cases.

- [ ] **Step 5: Commit the SDK mediation boundary**

```bash
git add tools/faba/faba_sdk_policy.py tools/faba/faba_worker.py tools/faba/tests/test_faba_sdk_policy.py tools/faba/tests/test_faba_worker.py
git commit -m "feat(faba): enforce deny-default SDK mediation"
```

### Task 10: Build the isolated worker image and prove env, fd, and import isolation

**Files:**
- Create: `tools/faba/faba_worker_runtime.py`
- Modify: `tools/faba/faba_worker.py`
- Modify: `tools/faba/tests/test_faba_worker.py`

**Interfaces:**
- Consumes: `build_options`, `assert_options`, and `startup_deny_proof` from Task 9.
- Produces: `build_worker_zipapp(output: Path) -> Path`; `run_worker(payload: WorkerPayload, result_fd: int) -> Awaitable[int]`; worker CLI `faba-worker.pyz PAYLOAD_PATH RESULT_FD`.

- [ ] **Step 1: Write the failing isolated-runtime test**

```python
def test_worker_exec_has_allowlisted_env_fds_and_imports(real_materialisation, tmp_path):
    image = build_worker_zipapp(tmp_path / "faba-worker.pyz")
    completed, report = launch_test_worker(
        image,
        real_materialisation.worker_payload,
        extra_env={
            "ARB_MEMORY_REDIS_URL": "redis://must-not-survive",
            "AGENT_REDIS_PASSWORD": "must-not-survive",
            "ANTHROPIC_API_KEY": "must-not-survive",
            "HTTPS_PROXY": "http://must-not-survive",
            "SSH_AUTH_SOCK": "/must-not-survive",
        },
        extra_inheritable_fd=True,
    )
    assert completed.returncode == 0
    assert report["environment_keys"] == ["ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR", "HOME", "LANG", "PATH", "TMPDIR"]
    assert "ANTHROPIC_AUTH_TOKEN" not in report["environment_keys"]
    assert "ANTHROPIC_API_KEY" not in report["environment_keys"]
    assert report["open_fds"] == [0, 1, 2, report["result_fd"]]
    assert report["arb_memory_importable"] is False
    assert report["worker_is_session_leader"] is True
    assert report["worker_is_pgid_leader"] is True
```

- [ ] **Step 2: Run the isolated-runtime test and verify RED**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_worker.py::test_worker_exec_has_allowlisted_env_fds_and_imports -q`

Expected: FAIL with `ImportError: cannot import name 'build_worker_zipapp' from 'faba_worker_runtime'`.

- [ ] **Step 3: Build a zipapp containing only worker policy modules and add real worker execution**

```python
# tools/faba/faba_worker_runtime.py
from __future__ import annotations

import shutil
import tempfile
import zipapp
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def build_worker_zipapp(output: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="faba-worker-build-") as raw:
        root = Path(raw)
        shutil.copy2(HERE / "faba_worker.py", root / "__main__.py")
        shutil.copy2(HERE / "faba_sdk_policy.py", root / "faba_sdk_policy.py")
        package = root / "agent_redis_bridge"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(REPO / "src/agent_redis_bridge/agent_sdk_baseline.py", package / "agent_sdk_baseline.py")
        zipapp.create_archive(root, target=output, interpreter="/Users/<user>/<workspace>/.venv/bin/python -IS")
    output.chmod(0o555)
    return output.resolve()
```

Complete `faba_worker.py` with this entry path; the existing Task 9 policy definitions remain above it in the same file:

```python
def _open_fds() -> list[int]:
    import resource

    ceiling = min(resource.getrlimit(resource.RLIMIT_NOFILE)[0], 4096)
    found = []
    for fd in range(int(ceiling)):
        try:
            os.fstat(fd)
            found.append(fd)
        except OSError:
            pass
    return found


def _safe_environment() -> list[str]:
    allowed = {"ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR", "HOME", "LANG", "PATH", "TMPDIR"}
    unexpected = sorted(set(os.environ) - allowed)
    forbidden = sorted(
        key for key in os.environ
        if key.startswith(("ARB_MEMORY_", "AGENT_REDIS_"))
        or key in {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SSH_AUTH_SOCK"}
        or key.upper().endswith(("_TOKEN", "_API_KEY"))
    )
    if unexpected or forbidden:
        raise RuntimeError(f"worker environment proof failed: unexpected={unexpected}, forbidden={forbidden}")
    return sorted(os.environ)


async def run_worker(payload: WorkerPayload, result_fd: int) -> int:
    import importlib.util

    import claude_agent_sdk
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock, ToolUseBlock

    if claude_agent_sdk.__version__ != "0.2.107":
        raise RuntimeError(f"unsupported claude_agent_sdk {claude_agent_sdk.__version__}")
    environment_keys = _safe_environment()
    fds = _open_fds()
    if fds != [0, 1, 2, result_fd]:
        raise RuntimeError(f"worker fd proof failed: {fds}")
    if os.getsid(0) != os.getpid() or os.getpgrp() != os.getpid():
        raise RuntimeError("worker must lead its session and process group")
    if importlib.util.find_spec("arb_memory") is not None:
        raise RuntimeError("arb_memory import path reached worker image")
    os.umask(0o022)
    gate = build_gate(payload.manifest, payload.workspace)
    options = build_options(payload, gate)
    await startup_deny_proof(options, gate)
    client = ClaudeSDKClient(options=options)
    if client.options.can_use_tool is not gate:
        raise RuntimeError("disconnected SDK callback identity")
    await client.connect()
    try:
        if client.options.can_use_tool is not gate:
            raise RuntimeError("connected SDK callback identity changed")
        await client.query(payload.prompt)
        last_text = ""
        result = {"num_turns": None, "total_cost_usd": None, "session_id": None, "is_error": False}
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        print(f"[faba-worker] tool: {block.name}", file=sys.stderr, flush=True)
                    elif isinstance(block, TextBlock):
                        last_text = block.text
            elif isinstance(message, ResultMessage):
                if message.result:
                    last_text = message.result
                for key in result:
                    result[key] = getattr(message, key, result[key])
        result["text"] = last_text
        result["proof"] = {
            "environment_keys": environment_keys,
            "open_fds": fds,
            "result_fd": result_fd,
            "arb_memory_importable": False,
            "worker_is_session_leader": True,
            "worker_is_pgid_leader": True,
        }
        os.write(result_fd, (json.dumps(result, sort_keys=True) + "\n").encode())
        return 0 if not result["is_error"] else 1
    finally:
        await client.disconnect()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: faba-worker.pyz PAYLOAD_PATH RESULT_FD", file=sys.stderr)
        return 64
    payload = WorkerPayload.from_path(Path(argv[0]))
    return asyncio.run(run_worker(payload, int(argv[1])))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run worker isolation and SDK startup tests and verify GREEN**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_worker.py -q`

Expected: PASS; only the declared fd survives, forbidden env keys are absent, the SDK is exactly 0.2.107, `arb_memory` is not importable, the worker is both session and PGID leader, the live read smoke succeeds, and the connected options retain the exact callback identity.

- [ ] **Step 5: Commit the isolated worker exec image**

```bash
git add tools/faba/faba_worker.py tools/faba/faba_worker_runtime.py tools/faba/tests/test_faba_worker.py
git commit -m "feat(faba): add isolated secret-free SDK worker image"
```

### Task 11: Split the controller and enforce initial-env and startup proofs

**Files:**
- Create: `tools/faba/faba_containment.py`
- Modify: `tools/faba/faba_launch.py:165-411`
- Create: `tools/faba/tests/test_faba_containment.py`
- Modify: `tools/faba/tests/test_faba_harness.py:130-171`

**Interfaces:**
- Consumes: broker, source view, profile, UID helper, worker image, and `RoundMaterialisation` from Tasks 2, 4, 5, 7, 8, and 10.
- Produces: `reject_initial_environment(env: Mapping[str, str]) -> None`; `build_worker_environment(broker_base_url: str, private_home: Path) -> dict[str, str]`; `RunJournal(path: Path, run_id: str, token: str)` with `write(state: str, **evidence: object) -> None`; `WorkerSpec(uid_helper: Path, uid: int, profile: Path, control_fd: int, control_read_fd: int, worker_image: Path, payload: Path, result_fd: int, workspace: Path, broker_base_url: str, private_home: Path, journal: RunJournal, nonce: str)`; `WorkerRun(process: subprocess.Popen[str], pid: int, pgid: int, uid: int, nonce: str)`; `launch_worker(spec: WorkerSpec) -> WorkerRun`.

- [ ] **Step 1: Write failing controller environment and launch-contract tests**

```python
@pytest.mark.parametrize("key", [
    "ARB_MEMORY_REDIS_URL", "ARB_MEMORY_LOCAL_DSN", "AGENT_REDIS_PASSWORD",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
])
def test_initial_secret_environment_is_rejected(key):
    with pytest.raises(ContainmentError, match=key):
        reject_initial_environment({key: "sentinel"})


def test_worker_launch_contract_contains_no_controller_secret(real_controller_spec):
    captured = capture_launch(real_controller_spec)
    serialised = json.dumps(captured, sort_keys=True)
    for forbidden in (
        str(real_controller_spec.env_file), real_controller_spec.redis_url,
        real_controller_spec.request_id, real_controller_spec.receipt_key,
    ):
        assert forbidden not in serialised
    assert captured["env"] == {
        "ANTHROPIC_BASE_URL": real_controller_spec.broker_base_url,
        "CLAUDE_CONFIG_DIR": str(real_controller_spec.private_home / ".claude"),
        "HOME": str(real_controller_spec.private_home),
        "LANG": "en_US.UTF-8",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "TMPDIR": str(real_controller_spec.private_home / "tmp"),
    }
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True
    assert captured["pass_fds"] == (real_controller_spec.control_fd, real_controller_spec.result_fd)
```

- [ ] **Step 2: Run controller tests and verify RED**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_containment.py tools/faba/tests/test_faba_harness.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'faba_containment'`.

- [ ] **Step 3: Implement the positive environment, no-follow secret read, and exec launch**

```python
# Core of tools/faba/faba_containment.py
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

SECRET_PREFIXES = ("ARB_MEMORY_", "ANTHROPIC_", "OPENAI_")
SECRET_EXACT = {"AGENT_REDIS_PASSWORD"}


class ContainmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunJournal:
    path: Path
    run_id: str
    token: str

    def write(self, state: str, **evidence: object) -> None:
        payload = {"run_id": self.run_id, "token": self.token, "state": state, **evidence}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


@dataclass(frozen=True)
class WorkerSpec:
    uid_helper: Path
    uid: int
    profile: Path
    control_fd: int
    control_read_fd: int
    worker_image: Path
    payload: Path
    result_fd: int
    workspace: Path
    broker_base_url: str
    private_home: Path
    journal: RunJournal
    nonce: str


@dataclass(frozen=True)
class WorkerRun:
    process: subprocess.Popen[str]
    pid: int
    pgid: int
    uid: int
    nonce: str


def reject_initial_environment(env: Mapping[str, str]) -> None:
    found = sorted(key for key in env if key.startswith(SECRET_PREFIXES) or key.upper() in SECRET_EXACT or key.upper().endswith(("_TOKEN", "_API_KEY")))
    if found:
        raise ContainmentError("secret-bearing initial environment: " + ", ".join(found))


def read_secret_env(path: Path) -> dict[str, str]:
    canonical = path.resolve(strict=True)
    if path.is_symlink():
        raise ContainmentError("env file must not be a symlink")
    fd = os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        content = os.read(fd, canonical.stat().st_size).decode("utf-8")
    finally:
        os.close(fd)
    values = {}
    for line in content.splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key in {"ARB_MEMORY_REDIS_URL", "ANTHROPIC_API_KEY"}:
                values[key] = value
    return values


def build_worker_environment(broker_base_url: str, private_home: Path) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": broker_base_url,
        "CLAUDE_CONFIG_DIR": str(private_home / ".claude"),
        "HOME": str(private_home),
        "LANG": "en_US.UTF-8",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "TMPDIR": str(private_home / "tmp"),
    }


def launch_worker(spec):
    argv = [str(spec.uid_helper), "run", str(spec.uid), str(spec.profile), str(spec.control_fd), str(spec.worker_image), str(spec.payload), str(spec.result_fd)]
    process = subprocess.Popen(
        argv,
        cwd=spec.workspace,
        env=build_worker_environment(spec.broker_base_url, spec.private_home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        pass_fds=(spec.control_fd, spec.result_fd),
        text=True,
    )
    identity = json.loads(os.read(spec.control_read_fd, 256))
    worker_pid = int(identity["pid"])
    pgid = int(identity["pgid"])
    if worker_pid != pgid:
        process.kill()
        raise ContainmentError("worker is not PGID leader")
    spec.journal.write("worker-running", pid=worker_pid, pgid=pgid, uid=spec.uid, nonce=spec.nonce)
    return WorkerRun(process, worker_pid, pgid, spec.uid, spec.nonce)
```

Update `faba_launch.main` so `reject_initial_environment(dict(os.environ))` is the first operation before parsing the env file; resolve and reject symlinks for every path; materialise production bytes; read both credentials only into controller locals; start the broker; build/prove the source view and Seatbelt boundary; build the isolated image; then call `launch_worker`. Do not import `redis`, `arb_memory.bus`, or create a Redis client on this path.

- [ ] **Step 4: Run controller startup tests and verify GREEN**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_containment.py tools/faba/tests/test_faba_harness.py -q`

Expected: PASS; each forbidden initial key exits 2, the env file is no-follow/CLOEXEC, worker argv/env/prompt/IPC/log captures contain no env-file path, credential, request id, or receipt key, and no Redis import/client occurs before teardown.

- [ ] **Step 5: Commit the split controller launch path**

```bash
git add tools/faba/faba_containment.py tools/faba/faba_launch.py tools/faba/tests/test_faba_containment.py tools/faba/tests/test_faba_harness.py
git commit -m "feat(faba): split privileged controller and SDK worker"
```

### Task 12: Enforce teardown attestation before publication and crash recovery

**Files:**
- Modify: `tools/faba/faba_containment.py`
- Modify: `tools/faba/faba_launch.py:149-227,375-411`
- Modify: `tools/faba/faba_cleanup.py`
- Modify: `tools/faba/tests/test_faba_containment.py`
- Modify: `tools/faba/tests/test_faba_schema.py:99-230`

**Interfaces:**
- Consumes: `WorkerRun` and UID-helper `census`/`reap` from Tasks 7 and 11.
- Produces: `TeardownAttestation(run_id: str, uid: int, pgid: int, worker_waited: bool, uid_empty: bool, broker_closed: bool, issued_ns: int, token: str)`; `teardown_worker(run: WorkerRun, helper: Path, broker_closed: bool, run_id: str, token: str, grace_seconds: float = 5.0) -> TeardownAttestation`; `publish_and_gate(redis_url: str, *, attestation: TeardownAttestation, run_id: str, ...)`.

- [ ] **Step 1: Write the failing ordering and descendant matrix tests**

```python
@pytest.mark.parametrize("case", ["normal", "sdk-error", "timeout", "sigterm-resistant", "background", "double-fork", "setsid"])
def test_every_exit_path_reaps_uid_before_redis(case, contained_worker_factory, monkeypatch):
    events = []
    run = contained_worker_factory(case)
    monkeypatch.setattr("faba_containment._event", lambda name, **fields: events.append(name))
    attestation = teardown_worker(run, run.helper, broker_closed=True, run_id=run.run_id, token=run.token, grace_seconds=0.1)
    assert attestation.worker_waited and attestation.uid_empty and attestation.broker_closed
    assert events.index("worker-waited") < events.index("uid-census-empty") < events.index("attestation-minted")


def test_publish_refuses_missing_wrong_or_incomplete_attestation(valid_publish_args):
    from faba_launch import publish_and_gate

    with pytest.raises(ContainmentError, match="teardown attestation"):
        publish_and_gate(**valid_publish_args, attestation=None, run_id="run-a")
    bad = TeardownAttestation("run-b", 60017, 1001, True, True, True, 1, "token")
    with pytest.raises(ContainmentError, match="run binding"):
        publish_and_gate(**valid_publish_args, attestation=bad, run_id="run-a")


def test_redis_client_and_delete_follow_attestation(valid_publish_args, valid_attestation, monkeypatch):
    events = []
    monkeypatch.setattr("faba_launch.validate_attestation", lambda *args: events.append("attested"))
    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: events.append("redis-client") or StubClient(events))
    publish_and_gate(**valid_publish_args, attestation=valid_attestation, run_id=valid_attestation.run_id)
    assert events[:3] == ["attested", "redis-client", "delete"]
```

- [ ] **Step 2: Run teardown tests and verify RED**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_containment.py tools/faba/tests/test_faba_schema.py -q`

Expected: FAIL with `ImportError: cannot import name 'TeardownAttestation' from 'faba_containment'`.

- [ ] **Step 3: Implement TERM/KILL/wait/UID census and attestation binding**

```python
@dataclass(frozen=True)
class TeardownAttestation:
    run_id: str
    uid: int
    pgid: int
    worker_waited: bool
    uid_empty: bool
    broker_closed: bool
    issued_ns: int
    token: str


def teardown_worker(run, helper: Path, broker_closed: bool, run_id: str, token: str, grace_seconds: float = 5.0):
    import signal
    import time

    try:
        os.killpg(run.pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        run.process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(run.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        run.process.wait(timeout=grace_seconds)
    _event("worker-waited", pid=run.pid)
    subprocess.run([str(helper), "reap", str(run.uid)], check=False, capture_output=True, text=True)
    census = subprocess.run([str(helper), "census", str(run.uid)], check=False, capture_output=True, text=True)
    if census.returncode != 1:
        raise ContainmentError("dedicated UID survivor census is not empty")
    _event("uid-census-empty", uid=run.uid)
    if not broker_closed:
        raise ContainmentError("provider broker still open at teardown")
    attestation = TeardownAttestation(run_id, run.uid, run.pgid, True, True, True, time.monotonic_ns(), token)
    _event("attestation-minted", run_id=run_id)
    return attestation


def validate_attestation(attestation, run_id: str, expected_token: str) -> None:
    if not isinstance(attestation, TeardownAttestation):
        raise ContainmentError("teardown attestation required")
    if attestation.run_id != run_id or attestation.token != expected_token:
        raise ContainmentError("teardown attestation run binding failed")
    if not (attestation.worker_waited and attestation.uid_empty and attestation.broker_closed):
        raise ContainmentError("teardown attestation is incomplete")
```

Change `publish_and_gate` so `attestation`, `run_id`, and the controller-held expected token are required keyword-only parameters and `validate_attestation` is its first statement, before record validation and before `import redis`. In `main`, close result IPC and the broker, perform teardown on every worker result/error/timeout path, persist `session-final.txt` only after attestation, validate the record, then call `publish_and_gate`. Mark the journal `published` and remove it only after receipt polling completes. At launch, run `cleanup_stale`; any remaining journal or non-empty UID blocks the new round with exit 2.

- [ ] **Step 4: Run deterministic teardown and crash-recovery tests and verify GREEN**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_containment.py tools/faba/tests/test_faba_schema.py -q`

Expected: PASS; all seven descendant/exit cases have empty UID census, the ordered event trace places attestation before Redis client and DEL, invalid attestations publish nothing, current `artefact_id` receipt binding remains green, and controller crash recovery cleans stale UID state before a later launch.

- [ ] **Step 5: Commit the teardown-before-publish invariant**

```bash
git add tools/faba/faba_containment.py tools/faba/faba_launch.py tools/faba/faba_cleanup.py tools/faba/tests/test_faba_containment.py tools/faba/tests/test_faba_schema.py
git commit -m "feat(faba): require teardown attestation before publish"
```

### Task 13: Complete the non-skipping PF3 certification runner

**Files:**
- Modify: `tools/faba/tests/probe_pf3.py`
- Modify: `tools/faba/tests/test_provider_broker.py`
- Modify: `tools/faba/tests/test_macos_boundary.py`
- Modify: `tools/faba/tests/test_faba_worker.py`
- Modify: `tools/faba/tests/test_faba_containment.py`

**Interfaces:**
- Consumes: every production factory and proof interface from Tasks 1-12.
- Produces: CLI cases `additive-preapproval`, `credential-harvest`, and `descendant-reseed`; each writes `CertificationReport(case: str, sdk_version: str, cli_version: str, manifest_digest: str, profile_digest: str, canonical_paths: dict[str, str], tool_decisions: list[dict[str, str]], process_identity: dict[str, int], ordered_events: list[str], redacted_digests: dict[str, str], passed: bool)`.

- [ ] **Step 1: Write failing certification-report completeness tests**

```python
@pytest.mark.parametrize("case", ["additive-preapproval", "credential-harvest", "descendant-reseed"])
def test_certification_case_uses_real_factories_and_emits_complete_report(case, certification_fixtures):
    report = run_case(case, certification_fixtures)
    assert report.case == case
    assert report.sdk_version == "0.2.107"
    assert report.cli_version
    assert len(report.manifest_digest) == 64
    assert len(report.profile_digest) == 64
    assert report.canonical_paths["workspace"] == str(certification_fixtures.workspace.resolve())
    assert report.tool_decisions
    assert report.process_identity["uid"] in range(60000, 61000)
    assert report.ordered_events.index("uid-census-empty") < report.ordered_events.index("receipt-delete")
    assert report.passed
    assert certification_fixtures.materialise_calls == 1
    assert certification_fixtures.build_options_calls == 1
```

- [ ] **Step 2: Run report tests and verify RED**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba:$PWD/tools/faba/tests /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_worker.py tools/faba/tests/test_faba_containment.py -q`

Expected: FAIL with `ImportError: cannot import name 'run_case' from 'probe_pf3'`.

- [ ] **Step 3: Implement all three cases through production materialisation and options factories**

`additive-preapproval` must seed real user/account pre-approval state in a private `CLAUDE_CONFIG_DIR`, invoke the installed 0.2.107 CLI, require `allowed_tools=[]` and `setting_sources=[]`, observe the callback on `FABA_STARTUP_DENY_SENTINEL`, deny it, allow one in-ceiling read, and directly request/deny `git commit` through the live callback path. `credential-harvest` must place one unique fake Memory URL in the real no-follow env file and attempt Read, Bash `cat`, Python `open`, a symlink, `/tmp`, `/private/tmp`, process-info, worker argv/env/IPC/workspace/session-final/log inspection, and broker credential extraction; every attempt must be observed and denied while source read, canonical workspace write, and provider model startup succeed. `descendant-reseed` must spawn ordinary, SIGTERM-resistant, background, double-fork, and `setsid` descendants under the real UID helper, attempt delayed LPUSH to both bus fixtures directly and through the broker, require UID census zero, require both attempts denied, and require the controller's current `artefact_id` receipt after the ordered attestation/DEL/publish/poll sequence.

The common runner must call `materialise_workspace` and `build_options` itself; it must exit 1 when an executable or fixture is absent, an OS case cannot run, the callback is unseen, the model declines to issue the requested adversarial call, the SDK/CLI version differs, or any required evidence field is empty. It must never call `pytest.skip`.

- [ ] **Step 4: Run the complete deterministic and live macOS certification suite**

Run: `/Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/ -q`

Expected: exit 0; all deterministic tests pass, including existing schema, receipt, succession, and shared-contract coverage.

Run: `/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py --case additive-preapproval --sdk-version 0.2.107 --report /private/tmp/pf3-additive-report.json`

Expected: exit 0; escape attempt denied, callback audit contains the sentinel and live git mutation denial, and an in-ceiling read succeeds.

Run: `/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py --case credential-harvest --host macos --report /private/tmp/pf3-credential-report.json`

Expected: exit 0; all harvest paths are observed and denied, only the broker base URL is auth-adjacent, provider startup succeeds, normal source read and workspace write pass, and remote/local/Unix bus paths remain unreachable.

Run: `/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py --case descendant-reseed --host macos --report /private/tmp/pf3-descendant-report.json`

Expected: exit 0; per-round UID survivor count is zero, the worker was session/PGID leader with no helper in its group, forged receipts cannot reach the bus directly or through the broker, and `uid-census-empty` precedes `receipt-delete`.

- [ ] **Step 5: Commit the certification suite**

```bash
git add tools/faba/tests/probe_pf3.py tools/faba/tests/test_provider_broker.py tools/faba/tests/test_macos_boundary.py tools/faba/tests/test_faba_worker.py tools/faba/tests/test_faba_containment.py
git commit -m "test(faba): certify PF3 macOS containment escapes"
```

### Task 14: Document installation, operation, and slice boundaries

**Files:**
- Modify: `tools/faba/README.md:1-83`
- Test: `tools/faba/tests/test_faba_harness.py`

**Interfaces:**
- Consumes: commands and invariants delivered by Tasks 1-13.
- Produces: operator-facing install/run/recovery/certification instructions; no new runtime interface.

- [ ] **Step 1: Write the failing documentation contract test**

```python
def test_readme_names_pf3_macos_gates_and_scope():
    readme = (FABA / "README.md").read_text(encoding="utf-8")
    for required in (
        "claude_agent_sdk 0.2.107",
        "destination-pinned loopback broker",
        "deny-default network",
        "dedicated per-round UID",
        "faba-uid-helper",
        "launchd crash cleanup",
        "teardown attestation",
        "probe_pf3.py --case additive-preapproval",
        "probe_pf3.py --case credential-harvest",
        "probe_pf3.py --case descendant-reseed",
        "managed-Linux follow-up plan",
        "receipt-envelope ULID",
        "store fetch-by-id",
        "subagent path is not PF3-certified",
    ):
        assert required in readme
```

- [ ] **Step 2: Run the documentation contract and verify RED**

Run: `PYTHONPATH=$PWD/src:$PWD/tools/faba /Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/test_faba_harness.py::test_readme_names_pf3_macos_gates_and_scope -q`

Expected: FAIL at the first missing PF3 macOS phrase.

- [ ] **Step 3: Update the README with exact operator commands and failure semantics**

Append this exact operator section:

````markdown
## PF3 macOS architecture

The SDK worker is a separate exec image under a dedicated per-round UID and a deny-default
Seatbelt profile. The privileged controller alone holds provider and Memory credentials. The
worker can reach only the destination-pinned loopback broker; publication requires a controller-
owned teardown attestation after PGID wait and empty UID census. This slice pins
`claude_agent_sdk 0.2.107`.

## Install the root-owned UID helper

```sh
sudo tools/faba/macos/install_uid_helper.sh
```

The installer places `faba-uid-helper` at `/usr/local/libexec/faba-uid-helper`, owned by
root:wheel with mode 4755. Its accepted UID pool is 60000 through 60999.

## Install launchd crash cleanup

The same installer loads `com.openai.faba-cleanup`. This launchd crash cleanup runs at load and
every 60 seconds. A stale journal blocks another launch until `pgrep -U` proves that UID empty.

## Launch prerequisites

The provider-specific broker probe and the macOS deny-default network probe must pass first.
Missing fixtures, skipped canaries, an unsupported process-info rule, or a failed local/remote/
Unix-bus denial exits 2 before SDK launch or publication.

## Run a round

Use the existing `faba_launch.py` CLI. A containment startup failure exits 2. A contained worker
or content/receipt failure exits 1. Exit 0 means teardown attestation preceded DEL, publish, and
the current artefact-id-bound receipt.

## Run certification

```sh
/Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/ -q
/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py --case additive-preapproval --sdk-version 0.2.107 --report /private/tmp/pf3-additive-report.json
/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py --case credential-harvest --host macos --report /private/tmp/pf3-credential-report.json
/Users/<user>/<workspace>/.venv/bin/python tools/faba/tests/probe_pf3.py --case descendant-reseed --host macos --report /private/tmp/pf3-descendant-report.json
```

Every command must exit 0; an absent escape attempt is a failure, not a pass.

## Crash recovery

The launchd job and the next controller launch both call UID-wide reap and census. Do not remove
a stale journal manually: it is the publication fence for a prior controller crash.

## Out of scope

The managed-Linux follow-up plan owns container/cgroup containment. Receipt-envelope ULID binding,
store fetch-by-id verification, a read-only local Memory MCP extension, and containment of the
prototype subagent path are separate changes. The subagent path is not PF3-certified.
````

- [ ] **Step 4: Run all FABA tests and the repository regression suite**

Run: `/Users/<user>/<workspace>/.venv/bin/python -m pytest tools/faba/tests/ -q`

Expected: PASS.

Run: `/Users/<user>/<workspace>/.venv/bin/python -m pytest -q`

Expected: PASS; no bridge or `arb_memory` regression from the shared baseline move.

- [ ] **Step 5: Commit the operational documentation**

```bash
git add tools/faba/README.md tools/faba/tests/test_faba_harness.py
git commit -m "docs(faba): document PF3 macOS containment operations"
```

---

## Final reviewer gates

- Confirm Tasks 2-3 finish before any engine-launch implementation and Task 6 finishes before the controller/worker split.
- Confirm the real worker receives no env-file path, Memory/provider credential, request id, receipt key, secret-bearing configuration path, inherited credential fd, or import path to `arb_memory`.
- Confirm Seatbelt is deny-default for network and has exactly one outbound allow: the broker host/port. Re-run with Redis listening separately on localhost.
- Confirm the dedicated UID, not PGID alone, closes double-fork and `setsid` descendants; the helper/broker/controller are outside the worker group.
- Confirm every failure path closes broker/result IPC, waits, reaps UID, proves census empty, then either mints attestation or fails with no Redis client.
- Confirm every PF3 test uses production `build_brief`, `materialise_workspace`, manifest, and options factories.
- Confirm the macOS reports include SDK/CLI versions, manifest/profile digests, canonical paths, tool decisions, PID/PGID/UID, ordered teardown/Redis events, and redacted result digests.
- Confirm no task changes `arb_memory` receipt envelopes or adds fetch-by-id behavior.
- Confirm managed Linux, local Memory MCP reintroduction, the subagent path, receipt-envelope ULID binding, and store fetch-by-id remain explicitly outside this plan.
