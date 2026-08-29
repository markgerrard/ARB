from __future__ import annotations

from pathlib import Path
import base64
import sys

import anyio
import pytest
from mcp.types import ImageContent

from arb_files.config import load_settings
from arb_files.mcp.door_tools import FileTools
from arb_files.store import FilesStore

sys.path.append(str(Path(__file__).parent))
from fakes import FakeS3  # noqa: E402


BASE = {
    "ARB_FILES_ENDPOINT": "https://e",
    "ARB_FILES_REGION": "lon1",
    "ARB_FILES_BUCKET": "arb-files",
    "ARB_FILES_ACCESS_KEY": "AK",
    "ARB_FILES_SECRET_KEY": "SK",
}


def _tools(scopes=("files.read", "files.write"), env=None):
    settings = load_settings({**BASE, **(env or {})})
    fake = FakeS3()
    store = FilesStore(settings, client_factory=lambda: fake)

    def require(scope):
        if scope not in scopes:
            raise PermissionError(f"{scope} required")

    return FileTools(store, settings, require_scope=require, actor=lambda: "client-1"), fake


def test_write_requires_files_write_scope():
    tools, _fake = _tools(scopes=("files.read",))
    with pytest.raises(PermissionError):
        anyio.run(tools.file_put_inline, "a.txt", "hi", "text/plain", False)


def test_put_inline_rejects_disallowed_mime():
    tools, _fake = _tools()
    with pytest.raises(ValueError):
        anyio.run(tools.file_put_inline, "a.png", "x", "image/png", False)


def test_put_inline_enforces_size_cap():
    tools, _fake = _tools()
    big = "a" * (262144 + 1)
    with pytest.raises(ValueError):
        anyio.run(tools.file_put_inline, "big.txt", big, "text/plain", False)


def test_get_inline_image_returns_image_content():
    tools, fake = _tools()
    fake.objects["agent-files/p.png"] = {
        "Body": b"\x89PNG",
        "ContentType": "image/png",
        "Metadata": {},
        "LastModified": None,
        "ETag": '"x"',
    }
    out = anyio.run(tools.file_get_inline, "p.png")
    assert isinstance(out, ImageContent)
    assert out.mimeType == "image/png"
    assert base64.b64decode(out.data) == b"\x89PNG"


def test_get_inline_oversize_directs_to_url():
    tools, fake = _tools(env={"ARB_FILES_INLINE_GET_MAX": "1"})
    fake.objects["agent-files/big.bin"] = {
        "Body": b"x" * 10,
        "ContentType": "application/octet-stream",
        "Metadata": {},
        "LastModified": None,
        "ETag": '"x"',
    }
    with pytest.raises(ValueError):
        anyio.run(tools.file_get_inline, "big.bin")


def test_put_url_uses_actor_for_presigned_metadata():
    tools, fake = _tools()
    out = anyio.run(tools.file_put_url, "u.bin", "application/octet-stream", False)
    assert out["headers"]["x-amz-meta-uploaded-by"] == "client-1"
    assert fake.presigned[-1]["Params"]["Metadata"]["uploaded-by"] == "client-1"


def test_write_rate_limit_is_keyed_by_actor():
    tools, _fake = _tools(env={"ARB_FILES_WRITE_RATE_PER_MIN": "1"})
    anyio.run(tools.file_put_inline, "a.txt", "a", "text/plain", False)
    with pytest.raises(ValueError, match="rate limit exceeded"):
        anyio.run(tools.file_put_inline, "b.txt", "b", "text/plain", False)


def test_write_rate_limit_isolated_between_actors():
    actor = {"value": "client-a"}
    settings = load_settings({**BASE, "ARB_FILES_WRITE_RATE_PER_MIN": "1"})
    fake = FakeS3()
    store = FilesStore(settings, client_factory=lambda: fake)
    tools = FileTools(
        store,
        settings,
        require_scope=lambda _scope: None,
        actor=lambda: actor["value"],
    )

    anyio.run(tools.file_put_inline, "a.txt", "a", "text/plain", False)
    with pytest.raises(ValueError, match="rate limit exceeded"):
        anyio.run(tools.file_put_inline, "a2.txt", "a2", "text/plain", False)

    actor["value"] = "client-b"
    anyio.run(tools.file_put_inline, "b.txt", "b", "text/plain", False)
    assert fake.objects["agent-files/b.txt"]["Metadata"]["uploaded-by"] == "client-b"
