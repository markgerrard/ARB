"""Stdio transport for `codex app-server`.

Framing is newline-delimited JSON — one message per line — NOT the LSP-style
`Content-Length` framing "JSON-RPC over stdio" usually implies. Verified from
a shipping reference ACP adapter for codex, which also
DELETES the `jsonrpc` field on write and re-adds it on read: codex app-server
neither wants nor emits it, so this transport sends exactly what it is handed.

Non-JSON lines are skipped rather than fatal (the server may print banners to
stdout), matching upstream — but they are RECORDED in `skipped_lines` instead of
silently dropped, because a stream that is quietly discarding everything looks
identical to a stream that is merely quiet.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, IO


class CodexStdioClosed(RuntimeError):
    """The app-server stream is closed — the peer exited or we shut it down."""


class StdioTransport:
    def __init__(
        self,
        *,
        stdin: IO[str],
        stdout: IO[str],
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._on_close = on_close
        self._closed = False
        self.skipped_lines: list[str] = []

    def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise CodexStdioClosed("codex-stdio-closed: cannot send on a closed transport")
        self._stdin.write(json.dumps(message) + "\n")
        self._stdin.flush()

    def receive(self) -> dict[str, Any]:
        while True:
            line = self._stdout.readline()
            if line == "":
                raise CodexStdioClosed(
                    "codex-stdio-closed: app-server stdout reached EOF"
                )
            stripped = line.strip()
            if not stripped:
                continue
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                self.skipped_lines.append(stripped)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._on_close is not None:
            self._on_close()


def spawn_codex_app_server(
    codex_bin: str = "codex",
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> StdioTransport:
    """Spawn `codex app-server` and wrap its stdio."""
    return spawn_stdio_agent(codex_bin, ["app-server"], cwd=cwd, env=env)


def spawn_stdio_agent(
    command: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> StdioTransport:
    """Spawn any NDJSON-over-stdio agent and wrap it.

    The transport above was always generic — only the spawn was codex-shaped.
    grok Build is launched as `grok agent stdio`, so the warm grok runtime needs
    this same framing with a different argv rather than a second transport.

    stderr is left attached to the parent on purpose: agents log there, and
    swallowing it during a live smoke is how a startup failure turns into an
    unexplained hang at the first `receive()`.
    """
    process = subprocess.Popen(  # noqa: S603 - explicit binary, no shell
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        cwd=cwd,
        env=env,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None

    def _terminate() -> None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - slow-exit path
            process.kill()

    return StdioTransport(
        stdin=process.stdin, stdout=process.stdout, on_close=_terminate
    )
