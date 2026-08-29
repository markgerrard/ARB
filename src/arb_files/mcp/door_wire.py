from __future__ import annotations

import logging


log = logging.getLogger("arb_files.door")


def register_file_tools(server, env, *, store_factory=None) -> bool:
    if not env.get("ARB_FILES_BUCKET"):
        return False
    try:
        from arb_files.audit import default_audit_sink
        from arb_files.config import load_settings
        from arb_files.mcp.door_tools import FileTools
        from arb_files.store import FilesStore

        settings = load_settings(env)
        store = store_factory() if store_factory else FilesStore(settings, audit_sink=default_audit_sink)
        tools = FileTools(store, settings)
    except Exception:
        log.exception("ARB Files door tools not registered (config/back-end error); memory door unaffected")
        return False

    async def file_list(prefix: str = "") -> dict:
        """List files in the shared ARB Files pool by name/prefix. Pass a purpose prefix to scope:
        `artefacts/` (notes/reports/generated docs), `handoffs/` (files passed to another
        agent/session), or `builds/` (build outputs). Returns items + truncation state;
        get content_type/uploaded_by via file_head.
        NOTE: ARB Files lists by name/prefix only — it is NOT content/topic searchable. To find a
        file by topic or description (rather than exact name or folder), use `memory_search`: files
        worth finding are cross-linked via `memory_remember` pointers to their `agent-files/<name>` path."""
        return await tools.file_list(prefix)

    async def file_head(name: str) -> dict:
        """Fetch a file's existence + metadata (size, content_type, uploaded_by) without
        downloading it. Use to check whether a name is already taken before writing, or to read
        provenance. (To find a file by topic rather than exact name, use `memory_search` — files are
        cross-linked via `memory_remember` pointers.)"""
        return await tools.file_head(name)

    async def file_get_url(name: str) -> dict:
        """Create a presigned GET URL for any-size file download."""
        return await tools.file_get_url(name)

    async def file_get_inline(name: str):
        """Fetch a small file inline; images are returned as MCP image content."""
        return await tools.file_get_inline(name)

    async def file_put_inline(
        name: str,
        content: str,
        content_type: str = "text/plain",
        force: bool = False,
    ) -> dict:
        """Store a small text/markdown/JSON artefact you authored, by NAME, in the shared ARB Files pool.

        NAMING — prefix the name by purpose (names are free-form paths; pick a clear, human-meaningful one):
          • `artefacts/<name>`   — notes, reports, generated docs
          • `handoffs/<name>`    — a file you're passing to another agent or session
          • `builds/<id>/<name>` — build outputs
        DISCOVERABILITY — ARB Files is fetch-by-exact-name and is NOT searchable. If others should be able
        to find this file later by topic, ALSO call `memory_remember` with a one-line description and this
        file's `agent-files/<name>` path, so it surfaces in `memory_search` (the ARB Memory↔ARB Files bridge).
        Use `file_put_url` instead for binaries or anything larger than a small text snippet."""
        return await tools.file_put_inline(name, content, content_type, force)

    async def file_put_url(
        name: str,
        content_type: str | None = None,
        force: bool = False,
    ) -> dict:
        """Create a presigned PUT URL (+ required headers) for a binary or large-file upload — a human or
        seat performs the actual PUT with the returned url+headers.

        NAMING — prefix by purpose: `artefacts/<name>`, `handoffs/<name>`, or `builds/<id>/<name>`.
        DISCOVERABILITY — after the file lands, leave a `memory_remember` note with a short description +
        the `agent-files/<name>` path so it's findable via `memory_search` (ARB Files is fetch-by-name only,
        not searchable)."""
        return await tools.file_put_url(name, content_type, force)

    async def file_delete(name: str) -> dict:
        """Delete a file through the recoverable delete path."""
        return await tools.file_delete(name)

    for tool in (
        file_list,
        file_head,
        file_get_url,
        file_get_inline,
        file_put_inline,
        file_put_url,
        file_delete,
    ):
        server.add_tool(tool, name=tool.__name__)
    return True
