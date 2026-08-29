"""Authenticated transport adapter for the scored Pi SDK cell broker."""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .base import EngineError


def _identity_value(broker: Any) -> str:
    receipt_chain = getattr(broker, "_receipt_chain", None)
    identity = getattr(receipt_chain, "identity", None)
    if not isinstance(identity, Mapping):
        raise EngineError("scored Pi broker has no canonical receipt identity")
    cell_id = identity.get("cell_id")
    attempt_id = identity.get("attempt_id")
    if not isinstance(cell_id, str) or not cell_id or not isinstance(attempt_id, str) or not attempt_id:
        raise EngineError("scored Pi broker receipt identity is incomplete")
    return f"{cell_id}/{attempt_id}"


class PiSdkBrokerAdapter:
    """Expose the authenticated broker to Pi through the Node MCP shim."""

    def __init__(
        self,
        broker: Any,
        *,
        cwd: str | Path,
        node_command: str = "node",
        socket_gid: int | None = None,
    ) -> None:
        if not getattr(broker, "is_authenticated", False) or not callable(getattr(broker, "handle_tool_request", None)):
            raise EngineError("scored Pi broker is not authenticated or callable")
        self.broker = broker
        self.cwd = Path(cwd)
        self.identity = _identity_value(broker)
        # macOS limits AF_UNIX paths to roughly 104 bytes; worktree paths can
        # exceed that before the per-cell suffix is added.
        self.socket_path = Path("/tmp") / f"arb-pi-broker-{uuid.uuid4().hex}.sock"
        self.node_command = node_command
        if socket_gid is None:
            socket_gid = getattr(broker, "socket_gid", None)
        if socket_gid is None or (
            isinstance(socket_gid, bool) or not isinstance(socket_gid, int) or socket_gid <= 0
        ):
            raise EngineError("scored Pi broker requires the provisioned tool-plane GID")
        self.socket_gid = socket_gid
        self.token = secrets.token_urlsafe(32)
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()

    def start(self) -> None:
        if self._listener is not None:
            return
        if self.socket_path.exists():
            raise EngineError("scored Pi broker socket path already exists")
        self._stop.clear()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            if self.socket_gid is not None:
                os.chown(self.socket_path, -1, self.socket_gid)
            # A controller-provisioned private group (normally the tool UID's
            # group) is the only cross-UID access path. Never make the socket
            # world-readable/writable; token and canonical identity remain
            # required after the filesystem boundary.
            os.chmod(self.socket_path, 0o660 if self.socket_gid is not None else 0o600)
            listener.listen(1)
            listener.settimeout(0.2)
        except OSError as exc:
            listener.close()
            self.socket_path.unlink(missing_ok=True)
            raise EngineError(f"scored Pi broker socket bind failed: {exc}") from exc
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, name="pi-sdk-broker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            connection.close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1)
        self._thread = None
        self.socket_path.unlink(missing_ok=True)

    def server_spec(self) -> dict[str, Any]:
        return {
            "name": "cell-broker",
            "command": self.node_command,
            "args": [str(Path(__file__).resolve().parents[3] / "tools" / "pi-sdk-host" / "cell-broker.mjs")],
            "env": {
                "PI_SDK_BROKER_SOCKET": str(self.socket_path),
                "PI_SDK_BROKER_TOKEN": self.token,
                "PI_SDK_BROKER_IDENTITY": self.identity,
            },
        }

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._connections_lock:
                self._connections.add(connection)
            try:
                with connection:
                    self._serve_connection(connection)
            finally:
                with self._connections_lock:
                    self._connections.discard(connection)

    def _serve_connection(self, connection: socket.socket) -> None:
        connection.settimeout(2)
        reader = connection.makefile("r", encoding="utf-8")
        writer = connection.makefile("w", encoding="utf-8")
        try:
            first = json.loads(reader.readline())
            if not isinstance(first, Mapping) or first.get("token") != self.token or first.get("identity") != self.identity:
                writer.write(json.dumps({"ok": False, "error": "broker authentication failed"}) + "\n")
                writer.flush()
                return
            # Authentication is bounded, but a valid MCP session may be idle
            # while Pi reasons between tool calls.
            connection.settimeout(None)
            writer.write(json.dumps({"ok": True, "identity": self.identity}) + "\n")
            writer.flush()
            for line in reader:
                if self._stop.is_set():
                    return
                try:
                    request = json.loads(line)
                    if not isinstance(request, Mapping) or request.get("kind") != "tool":
                        raise ValueError("invalid broker request")
                    result = self.broker.handle_tool_request(dict(request.get("params", {})))
                    response = {"ok": True, "result": result}
                except Exception as exc:  # broker errors become tool errors, never host execution
                    response = {"ok": False, "error": str(exc)}
                writer.write(json.dumps(response, separators=(",", ":"), sort_keys=True) + "\n")
                writer.flush()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        finally:
            reader.close()
            writer.close()
