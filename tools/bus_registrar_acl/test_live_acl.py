from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import pytest
import redis

from arb_registration.bus_acl import AclProvisioner, RedisConnector


def _server_binary() -> str:
    configured = os.environ.get("VALKEY_SERVER_BIN")
    if configured:
        return configured
    installed = Path("/opt/valkey/current/bin/valkey-server")
    if installed.exists():
        return str(installed)
    pytest.fail("VALKEY_SERVER_BIN is required for the live reconnect ACL proof")


def _port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_ready(port: int, username: str, password: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if redis.Redis(
                host="127.0.0.1", port=port, username=username,
                password=password, decode_responses=True,
            ).ping():
                return
        except redis.RedisError:
            time.sleep(0.05)
    pytest.fail("ephemeral Valkey did not become ready")


def test_real_valkey_reconnect_allow_and_exact_noperm_matrix(tmp_path):
    admin_password = "admin-" + os.urandom(24).hex()
    digest = hashlib.sha256(admin_password.encode()).hexdigest()
    acl = tmp_path / "users.acl"
    credentials = tmp_path / "credentials.json"
    acl.write_text(
        "user default off resetpass resetkeys resetchannels -@all\n"
        f"user arb-admin on #{digest} allcommands allkeys allchannels\n"
    )
    credentials.write_text(json.dumps({"arb-admin": admin_password}) + "\n")
    acl.chmod(0o640)
    credentials.chmod(0o600)
    port = _port()
    process = subprocess.Popen(
        [
            _server_binary(), "--port", str(port), "--bind", "127.0.0.1",
            "--protected-mode", "no", "--save", "", "--appendonly", "no",
            "--aclfile", str(acl), "--dir", str(tmp_path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_ready(port, "arb-admin", admin_password)
        provisioner = AclProvisioner(
            acl_path=acl,
            credentials_path=credentials,
            connector=RedisConnector(host="127.0.0.1", port=port, tls=False),
        )
        first = provisioner.provision("host-b", ("codex",))
        assert set(first) == {"codex-orch-host-b"}
        codex_password = first["codex-orch-host-b"]["password"]
        acl_users = {
            line.split()[1] for line in acl.read_text().splitlines()
            if line.startswith("user ")
        }
        assert "codex-orch-host-b" in acl_users
        assert {
            "claude-orch-host-b", "pi-orch-host-b", "arb-worker-host-b",
        }.isdisjoint(acl_users)

        roles = provisioner.provision("host-b", ("worker",))
        assert set(roles) == {"codex-orch-host-b", "arb-worker-host-b"}
        assert roles["codex-orch-host-b"]["password"] == codex_password
        saved = json.loads(credentials.read_text())
        assert "claude-orch-host-b" not in saved
        assert "pi-orch-host-b" not in saved
        assert all(item["db"] == 12 for item in roles.values())
        assert acl.stat().st_mode & 0o777 == 0o640
        assert credentials.stat().st_mode & 0o777 == 0o600
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
