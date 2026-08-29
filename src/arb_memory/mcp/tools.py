from __future__ import annotations

import json
import re
import time

from mcp.server.auth.middleware.auth_context import get_access_token

from arb_memory import graph
from arb_memory import store
from arb_memory.embed import embed as default_embed
from arb_memory.hash import artefact_hash
from arb_memory.mcp.config import Settings, load_settings, mcp_connect

WRITE_MIME_ALLOWLIST = {"text/plain", "text/markdown", "application/json"}
ARTEFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
WRITE_AWAIT_HTTP_TIMEOUT = 35.0


def _default_conn_factory():
    return mcp_connect()


def _current_access_token() -> str:
    token = get_access_token()
    if token is None:
        return "anonymous"
    return token.token


def derive_artefact_id(content: str, mime: str) -> str:
    return f"art-{artefact_hash(content, None, mime)[:16]}"


def validate_content(content: str, mime: str, settings) -> None:
    if not content:
        raise ValueError("content must not be empty")
    if len(content.encode("utf-8")) > settings.write_max_content_bytes:
        raise ValueError("content too large")
    if mime not in WRITE_MIME_ALLOWLIST:
        raise ValueError(f"unsupported mime {mime!r}")
    if mime == "application/json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("content is not valid JSON") from exc


def validate_text(text: str, settings) -> None:
    if not text:
        raise ValueError("text must not be empty")
    if len(text) > settings.write_max_text_chars:
        raise ValueError("text too long")


def validate_tags(tags: list[str] | None, settings) -> None:
    if tags is None:
        return
    if len(tags) > settings.write_max_tags:
        raise ValueError("too many tags")
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError("tag must be a string")
        if len(tag) > settings.write_max_tag_chars:
            raise ValueError("tag too long")


def validate_artefact_id(artefact_id: str) -> None:
    if not ARTEFACT_ID_RE.fullmatch(artefact_id):
        raise ValueError("invalid artefact_id")


class MemoryTools:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        conn_factory=None,
        embed=None,
        writer_url=None,
        writer_token=None,
        http_client=None,
    ):
        self.settings = settings or load_settings()
        self.conn_factory = conn_factory or _default_conn_factory
        self.embed = embed or default_embed
        self.writer_url = writer_url
        self.writer_token = writer_token
        self.http_client = http_client
        self._search_hits: dict[str, list[float]] = {}
        self._graph_hits: dict[str, list[float]] = {}
        self._write_hits: dict[str, list[float]] = {}

    def _check_search_allowed(self, access_token: str) -> None:
        now = time.monotonic()
        window_start = now - 60.0
        hits = [stamp for stamp in self._search_hits.get(access_token, []) if stamp >= window_start]
        if len(hits) >= self.settings.search_rate_per_min:
            self._search_hits[access_token] = hits
            raise ValueError("search rate limit exceeded")
        hits.append(now)
        self._search_hits[access_token] = hits

    def _check_graph_allowed(self, access_token: str) -> None:
        now = time.monotonic()
        window_start = now - 60.0
        hits = [stamp for stamp in self._graph_hits.get(access_token, []) if stamp >= window_start]
        if len(hits) >= self.settings.graph_rate_per_min:
            self._graph_hits[access_token] = hits
            raise ValueError("graph rate limit exceeded")
        hits.append(now)
        self._graph_hits[access_token] = hits

    def _require_write_scope(self) -> None:
        token = get_access_token()
        if token is None or "memory.write" not in (getattr(token, "scopes", None) or []):
            raise PermissionError("memory.write scope required")

    def _check_write_allowed(self, access_token: str) -> None:
        now = time.monotonic()
        window_start = now - 60.0
        hits = [stamp for stamp in self._write_hits.get(access_token, []) if stamp >= window_start]
        if len(hits) >= self.settings.write_rate_per_min:
            self._write_hits[access_token] = hits
            raise ValueError("write rate limit exceeded")
        hits.append(now)
        self._write_hits[access_token] = hits

    def _author_from_token(self) -> str:
        token = get_access_token()
        return getattr(token, "client_id", None) or "mcp"

    async def _publish(self, intent: dict, *, await_result: bool = False) -> dict:
        if self.http_client is None or not self.writer_url:
            raise RuntimeError("write transport not configured")
        try:
            request = dict(intent)
            if await_result:
                request["await"] = True
            resp = await self.http_client.post(
                f"{self.writer_url}/publish",
                json=request,
                headers={"Authorization": f"Bearer {self.writer_token}"},
                **({"timeout": WRITE_AWAIT_HTTP_TIMEOUT} if await_result else {}),
            )
        except Exception as exc:
            # The writer XADDs BEFORE it answers, and it refuses a client-supplied request_id
            # (writer.py), so there is no key by which we could reconcile a failed round-trip.
            # We therefore cannot know whether the write landed — saying "NOT stored" here is a
            # claim this door has no channel to substantiate, and it has been wrong in
            # production. "retry shortly" on a write that DID land bumps a phantom version, or
            # trips expected_version on the guarded path.
            raise RuntimeError(
                "memory store outcome UNKNOWN - the item may or may not have been stored; "
                "check with memory_get before retrying"
            ) from exc
        if resp.status_code // 100 == 4:
            # 4xx is auth/validation: refused at the door of the writer, before the XADD, so
            # this genuinely did not store. This is the only branch entitled to say so.
            raise RuntimeError("memory store refused the write - item NOT stored")
        if resp.status_code // 100 != 2:
            raise RuntimeError(
                "memory store outcome UNKNOWN - the item may or may not have been stored; "
                "check with memory_get before retrying"
            )
        return resp.json()

    async def memory_store(
        self,
        content: str,
        *,
        artefact_id: str | None = None,
        mime: str = "text/plain",
        await_result: bool = False,
        expected_version: int | None = None,
        access_token: str | None = None,
    ) -> dict:
        self._require_write_scope()
        token = access_token or _current_access_token()
        self._check_write_allowed(token)
        # Guarded-path validation before _publish so a bad call never reaches the bus.
        if expected_version is not None:
            if not await_result:
                raise ValueError("expected_version requires await_result")
            # bool is an int subclass — refuse True/False explicitly.
            if isinstance(expected_version, bool) or not isinstance(expected_version, int):
                raise ValueError("expected_version must be a non-negative int")
            if expected_version < 0:
                raise ValueError("expected_version must be a non-negative int")
            if artefact_id is None:
                raise ValueError("expected_version requires an explicit artefact_id")
        validate_content(content, mime, self.settings)
        if artefact_id is None:
            artefact_id = derive_artefact_id(content, mime)
        else:
            validate_artefact_id(artefact_id)
        author = self._author_from_token()
        # Auto-index the stored document so memory_search can find it: an indexing hint over the
        # (embedding-capped) content, which the consumer auto-links to this artefact in the same
        # intent. Without this, a memory_store'd artefact is fetch-by-id only and invisible to search.
        index_hint = {
            "text": content[: self.settings.write_index_chars],
            "metadata": {"kind": "artefact_index", "artefact_id": artefact_id},
        }
        artefact = {
            "artefact_id": artefact_id,
            "content": content,
            "mime": mime,
            "source": "mcp",
            "author": author,
        }
        # Only set the key when non-None so ordinary writes stay byte-identical on the wire.
        if expected_version is not None:
            artefact["expected_version"] = expected_version
        intent = {
            "artefact": artefact,
            "hints": [index_hint],
            "author": author,
        }
        res = await self._publish(intent, await_result=await_result)
        if await_result:
            outcome = res.get("artefact_outcome")
            version = res.get("version")
            # Guard-not-live detector (design §5): production-boundary arithmetic on the
            # receipt. An unguarded old consumer returns stored with a version that cannot
            # match expected_version+1; fail closed rather than claim protection.
            if expected_version is not None and outcome == "stored" and version != expected_version + 1:
                return {
                    "accepted": False,
                    "ulid": res["ulid"],
                    "artefact_outcome": "guard_not_live",
                    **{
                        key: res[key]
                        for key in ("artefact_id", "version", "hints_stored", "duplicate", "timed_out")
                        if key in res
                    },
                }
            if expected_version is not None and outcome == "deduped" and version != expected_version:
                return {
                    "accepted": False,
                    "ulid": res["ulid"],
                    "artefact_outcome": "guard_not_live",
                    **{
                        key: res[key]
                        for key in ("artefact_id", "version", "hints_stored", "duplicate", "timed_out")
                        if key in res
                    },
                }
            # On the guarded path, accepted is outcome-derived; unguarded keeps always-true.
            if expected_version is not None:
                accepted = outcome in ("stored", "deduped")
            else:
                accepted = True
            return {"accepted": accepted, "ulid": res["ulid"], **{
                key: res[key]
                for key in ("artefact_outcome", "artefact_id", "version", "hints_stored", "duplicate", "timed_out")
                if key in res
            }}
        return {"accepted": True, "ulid": res["ulid"], "artefact_id": artefact_id}

    async def memory_remember(
        self,
        text: str,
        *,
        tags: list[str] | None = None,
        artefact_id: str | None = None,
        artefact_version: int | None = None,
        await_result: bool = False,
        access_token: str | None = None,
    ) -> dict:
        self._require_write_scope()
        token = access_token or _current_access_token()
        self._check_write_allowed(token)
        validate_text(text, self.settings)
        validate_tags(tags, self.settings)
        if artefact_id is not None or artefact_version is not None:
            if artefact_id is None or artefact_version is None:
                raise ValueError("artefact_id and artefact_version must both be set")
            validate_artefact_id(artefact_id)
            conn = self.conn_factory()
            # A just-stored artefact may not be consumer-persisted yet; clients should retry.
            if store.fetch_artefact(conn, artefact_id, artefact_version) is None:
                raise ValueError("linked artefact not found")
        author = self._author_from_token()
        metadata = {"tags": list(tags)} if tags else {}
        hint = {
            "text": text,
            "metadata": metadata,
            "artefact_id": artefact_id,
            "artefact_version": artefact_version,
        }
        intent = {"artefact": None, "hints": [hint], "author": author}
        res = await self._publish(intent, await_result=await_result)
        if await_result:
            return {"accepted": True, "ulid": res["ulid"], **{
                key: res[key]
                for key in ("artefact_outcome", "artefact_id", "version", "hints_stored", "duplicate", "timed_out")
                if key in res
            }}
        return {"accepted": True, "ulid": res["ulid"]}

    async def memory_search(self, query: str, k: int = 8, *, access_token: str | None = None) -> list[dict]:
        if len(query) > self.settings.search_max_query_chars:
            raise ValueError("query too long")
        token = access_token or _current_access_token()
        self._check_search_allowed(token)
        conn = self.conn_factory()
        return store.retrieve(conn, query, k=k, embed=self.embed)

    async def memory_related(
        self, artefact_id: str, version: int | None = None, k: int = 5,
        threshold: float = 0.35, *, access_token: str | None = None,
    ) -> list[dict]:
        token = access_token or _current_access_token()
        graph.validate_related_params(k, threshold)
        conn = self.conn_factory()
        if not graph.artefact_exists(conn, artefact_id, version):
            raise ValueError("artefact not found")
        self._check_graph_allowed(token)
        rows = graph.related_artefacts(
            conn, artefact_id, version, k=k, threshold=threshold,
            subject_hints=graph.subject_mode(version),
        )
        return [
            {"artefact_id": aid, "version": ver, "distance": dist}
            for aid, ver, dist in rows
        ]

    async def memory_references(
        self, artefact_id: str, version: int | None = None, *, access_token: str | None = None,
    ) -> dict:
        token = access_token or _current_access_token()
        conn = self.conn_factory()
        if not graph.artefact_exists(conn, artefact_id, version):
            raise ValueError("artefact not found")
        self._check_graph_allowed(token)
        resolved = version if version is not None else graph.latest_version(conn, artefact_id)
        return graph.references(conn, artefact_id, resolved)

    async def memory_get(self, artefact_id: str, version: int) -> dict | None:
        conn = self.conn_factory()
        return store.fetch_artefact(conn, artefact_id, version)

    async def memory_recent(self, limit: int = 10) -> list[dict]:
        conn = self.conn_factory()
        return store.recent_artefacts(conn, min(max(1, limit), 100))
