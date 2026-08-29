from __future__ import annotations

import base64

from arb_files.paths import resolve_local_path


class LocalFileTools:
    def __init__(self, store, *, seat_id: str, settings):
        self.store = store
        self.seat_id = seat_id
        self.s = settings

    async def file_list(self, prefix: str = "") -> dict:
        return self.store.list(prefix)

    async def file_head(self, name: str) -> dict:
        return self.store.head(name)

    async def file_get(self, name: str, to_path: str | None = None) -> dict:
        data, content_type = self.store.get_bytes(name)
        if to_path:
            dest = resolve_local_path(self.s.local_root, to_path)
            with open(dest, "wb") as handle:
                handle.write(data)
            return {
                "name": name,
                "written_to": dest,
                "size": len(data),
                "content_type": content_type,
            }
        return {
            "name": name,
            "content_b64": base64.b64encode(data).decode("ascii"),
            "content_type": content_type,
            "size": len(data),
        }

    async def file_put(
        self,
        name: str,
        from_path: str | None = None,
        content_b64: str | None = None,
        content_type: str = "application/octet-stream",
        force: bool = False,
    ) -> dict:
        if from_path:
            src = resolve_local_path(self.s.local_root, from_path)
            with open(src, "rb") as handle:
                data = handle.read()
        elif content_b64 is not None:
            data = base64.b64decode(content_b64)
        else:
            raise ValueError("provide from_path or content_b64")
        return self.store.put_bytes(
            name,
            data,
            content_type,
            uploaded_by=self.seat_id,
            force=force,
        )

    async def file_delete(self, name: str) -> dict:
        return self.store.delete(name, actor=self.seat_id)

    async def file_get_url(self, name: str) -> dict:
        return self.store.presign_get(name)

    async def file_put_url(
        self,
        name: str,
        content_type: str | None = None,
        force: bool = False,
    ) -> dict:
        return self.store.presign_put(
            name,
            content_type,
            uploaded_by=self.seat_id,
            force=force,
        )
