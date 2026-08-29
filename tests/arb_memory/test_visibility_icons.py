"""The visibility app serves its favicons without a login gate and refuses anything else on those handlers."""
from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from arb_memory import visibility

STATIC = Path(visibility.__file__).parent / "static"


def _app():
    # Construction must not touch the bus or Postgres; the icon routes never do.
    return visibility.build_visibility_app(
        bus_redis_url="redis://127.0.0.1:6379/15",
        bus_prefix="agent_scratch:",
        dsn="postgresql://unused@127.0.0.1:5432/unused",
        public_base_url="http://testserver",
    )


def test_favicons_are_served_as_png_without_auth():
    client = TestClient(_app())
    for name in ("favicon-32.png", "apple-touch-icon.png"):
        r = client.get(f"/{name}")
        assert r.status_code == 200, name
        assert r.headers["content-type"].startswith("image/png")
        assert r.content == (STATIC / name).read_bytes()
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
