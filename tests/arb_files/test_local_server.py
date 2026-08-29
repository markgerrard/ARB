import anyio

from arb_files.config import load_settings
from arb_files.mcp.local_server import build_local_server
from arb_files.store import FilesStore

from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent))
from fakes import FakeS3  # noqa: E402


BASE = {
    "ARB_FILES_ENDPOINT": "https://e",
    "ARB_FILES_REGION": "lon1",
    "ARB_FILES_BUCKET": "arb-files",
    "ARB_FILES_ACCESS_KEY": "AK",
    "ARB_FILES_SECRET_KEY": "SK",
}


def test_server_registers_expected_tools():
    settings = load_settings(BASE)
    store = FilesStore(settings, client_factory=lambda: FakeS3())
    server = build_local_server(settings, store=store, seat_id="seat-A")
    names = {tool.name for tool in anyio.run(server.list_tools)}
    assert {
        "file_list",
        "file_head",
        "file_get",
        "file_put",
        "file_delete",
        "file_get_url",
        "file_put_url",
    } <= names
