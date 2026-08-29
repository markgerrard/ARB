from __future__ import annotations

import base64
import time

from mcp.types import ImageContent


WRITE_MIME_ALLOWLIST = {"text/plain", "text/markdown", "application/json"}


class FileTools:
    def __init__(self, store, settings, *, require_scope=None, actor=None):
        self.store = store
        self.s = settings
        self._require_scope = require_scope or self._default_require_scope
        self._actor = actor or self._default_actor
        self._read_hits: dict[str, list[float]] = {}
        self._write_hits: dict[str, list[float]] = {}

    def _default_require_scope(self, scope: str) -> None:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None or scope not in (getattr(token, "scopes", None) or []):
            raise PermissionError(f"{scope} scope required")

    def _default_actor(self) -> str:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        return getattr(token, "client_id", None) or "mcp"

    def _check_rate(self, hits_by_actor: dict[str, list[float]], limit: int) -> None:
        actor = self._actor()
        now = time.monotonic()
        window_start = now - 60.0
        hits = [stamp for stamp in hits_by_actor.get(actor, []) if stamp >= window_start]
        if len(hits) >= limit:
            hits_by_actor[actor] = hits
            raise ValueError("rate limit exceeded")
        hits.append(now)
        hits_by_actor[actor] = hits

    def _require_read(self) -> None:
        self._require_scope("files.read")
        self._check_rate(self._read_hits, self.s.read_rate_per_min)

    def _require_write(self) -> None:
        self._require_scope("files.write")
        self._check_rate(self._write_hits, self.s.write_rate_per_min)

    async def file_list(self, prefix: str = "") -> dict:
        self._require_read()
        return self.store.list(prefix)

    async def file_head(self, name: str) -> dict:
        self._require_read()
        return self.store.head(name)

    async def file_get_url(self, name: str) -> dict:
        self._require_read()
        return self.store.presign_get(name)

    async def file_get_inline(self, name: str):
        self._require_read()
        head = self.store.head(name)
        if not head["exists"]:
            raise KeyError(f"not found: {name}")
        content_type = head.get("content_type") or "application/octet-stream"
        size = head.get("size") or 0
        if content_type.startswith("image/"):
            if size > self.s.inline_get_image_max:
                raise ValueError(f"image too large for inline ({size}B); use file_get_url")
            data, _content_type = self.store.get_bytes(name)
            return ImageContent(
                type="image",
                data=base64.b64encode(data).decode("ascii"),
                mimeType=content_type,
            )
        if size > self.s.inline_get_max:
            raise ValueError(f"too large for inline ({size}B); use file_get_url")
        data, _content_type = self.store.get_bytes(name)
        return {
            "content_b64": base64.b64encode(data).decode("ascii"),
            "content_type": content_type,
            "size": size,
        }

    async def file_put_inline(
        self,
        name: str,
        content: str,
        content_type: str = "text/plain",
        force: bool = False,
    ) -> dict:
        self._require_write()
        if content_type not in WRITE_MIME_ALLOWLIST:
            raise ValueError(f"unsupported mime {content_type!r}; binaries use file_put_url")
        data = content.encode("utf-8")
        if len(data) > self.s.inline_put_max:
            raise ValueError("content too large for inline; use file_put_url")
        return self.store.put_bytes(
            name,
            data,
            content_type,
            uploaded_by=self._actor(),
            force=force,
        )

    async def file_put_url(
        self,
        name: str,
        content_type: str | None = None,
        force: bool = False,
    ) -> dict:
        self._require_write()
        return self.store.presign_put(
            name,
            content_type,
            uploaded_by=self._actor(),
            force=force,
        )

    async def file_delete(self, name: str) -> dict:
        self._require_write()
        return self.store.delete(name, actor=self._actor())
