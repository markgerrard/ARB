from __future__ import annotations

from pathlib import Path
import base64
import sys

import anyio

from arb_files.config import load_settings
from arb_files.mcp.local_tools import LocalFileTools
from arb_files.store import FilesStore

sys.path.append(str(Path(__file__).parent))
from fakes import FakeS3  # noqa: E402


BASE = {
    "ARB_FILES_ENDPOINT": "https://e",
    "ARB_FILES_REGION": "lon1",
    "ARB_FILES_BUCKET": "arb-files",
    "ARB_FILES_ACCESS_KEY": "AK",
    "ARB_FILES_SECRET_KEY": "SK",
    "ARB_FILES_LOCAL_ROOT": "/tmp",
}


def _tools(root="/tmp", audit=None):
    env = dict(BASE)
    env["ARB_FILES_LOCAL_ROOT"] = root
    settings = load_settings(env)
    fake = FakeS3()
    store = FilesStore(settings, client_factory=lambda: fake, audit_sink=audit)
    return LocalFileTools(store, seat_id="seat-A", settings=settings), fake


def test_put_inline_b64_then_get():
    tools, _fake = _tools()
    content_b64 = base64.b64encode(b"payload").decode()
    anyio.run(tools.file_put, "p.bin", None, content_b64, "application/octet-stream", False)
    out = anyio.run(tools.file_get, "p.bin", None)
    assert base64.b64decode(out["content_b64"]) == b"payload"


def test_put_from_path_and_get_to_path(tmp_path):
    tools, _fake = _tools(root=str(tmp_path))
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_bytes(b"from-path")
    anyio.run(tools.file_put, "p.txt", str(src), None, "text/plain", False)
    out = anyio.run(tools.file_get, "p.txt", str(dest))
    assert out["written_to"] == str(dest)
    assert dest.read_bytes() == b"from-path"


def test_delete_records_actor_as_seat():
    events = []
    tools, _fake = _tools(audit=events.append)
    anyio.run(
        tools.file_put,
        "d",
        None,
        base64.b64encode(b"x").decode(),
        "text/plain",
        False,
    )
    anyio.run(tools.file_delete, "d")
    assert events[0]["actor"] == "seat-A"


def test_file_put_url_uses_seat_as_uploaded_by():
    tools, fake = _tools()
    out = anyio.run(tools.file_put_url, "u.bin", "application/octet-stream", False)
    assert out["headers"]["x-amz-meta-uploaded-by"] == "seat-A"
    assert fake.presigned[-1]["Params"]["Metadata"]["uploaded-by"] == "seat-A"
