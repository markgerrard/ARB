"""E2E smoke vs real DO Spaces.

Run:
  set -a; . envs/arb-files.env; set +a
  ARB_FILES_E2E=1 PYTHONPATH=src /Users/<user>/<workspace>/.venv/bin/python -m tests.arb_files.e2e_local_mcp

Skips unless ARB_FILES_E2E=1.
"""
from __future__ import annotations

import base64
import os
import sys
import urllib.error
import urllib.request
import uuid


def _put_url(url: str, headers: dict, body: bytes) -> int:
    req = urllib.request.Request(url, data=body, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _run_keys(store, settings, run: str) -> list[str]:
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": settings.bucket, "Prefix": settings.prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = store.client.list_objects_v2(**kwargs)
        keys.extend(
            item["Key"]
            for item in response.get("Contents", [])
            if run in item.get("Key", "")
        )
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")


def _cleanup_raw(store, settings, run: str) -> list[str]:
    keys = _run_keys(store, settings, run)
    for key in keys:
        store.client.delete_object(Bucket=settings.bucket, Key=key)
    return keys


def _assert_no_run_residue(store, settings, run: str) -> list[str]:
    leftovers = _run_keys(store, settings, run)
    assert leftovers == [], f"residue after cleanup: {leftovers}"
    return leftovers


def main() -> int:
    if os.environ.get("ARB_FILES_E2E") != "1":
        print("SKIP (set ARB_FILES_E2E=1)")
        return 0

    from arb_files.config import load_settings
    from arb_files.mcp.door_tools import FileTools
    from arb_files.mcp.local_tools import LocalFileTools
    from arb_files.store import FilesStore

    settings = load_settings(os.environ)
    run = uuid.uuid4().hex[:12]
    base = f"_e2e/{run}"
    events = []
    store = FilesStore(settings, audit_sink=events.append)
    local = LocalFileTools(store, seat_id="e2e-seat", settings=settings)

    def require(_scope: str) -> None:
        return None

    door = FileTools(store, settings, require_scope=require, actor=lambda: "e2e-door")
    name = f"{base}/probe.txt"
    upload_name = f"{base}/upload.bin"

    try:
        import anyio

        anyio.run(
            local.file_put,
            name,
            None,
            base64.b64encode(b"e2e-hello").decode("ascii"),
            "text/plain",
            False,
        )
        assert anyio.run(door.file_head, name)["exists"], "door head after local put"
        data = anyio.run(local.file_get, name, None)
        assert base64.b64decode(data["content_b64"]) == b"e2e-hello", "local roundtrip"
        listed = anyio.run(door.file_list, base + "/")
        assert any(item["name"].endswith("probe.txt") for item in listed["items"]), "door list"

        put = anyio.run(door.file_put_url, upload_name, "application/octet-stream", False)
        status = _put_url(put["url"], put["headers"], b"uploaded")
        assert 200 <= status < 300, f"presigned PUT failed: {status}"
        second = _put_url(put["url"], put["headers"], b"again")
        assert second == 412, f"conditional re-PUT expected 412, got {second}"
        assert anyio.run(door.file_head, upload_name)["uploaded_by"] == "e2e-door"

        deleted = anyio.run(door.file_delete, name)
        assert deleted["deleted"] and events, "delete+audit"
        assert not anyio.run(door.file_head, name)["exists"], "gone after delete"
        from tests.arb_files.e2e_spaces_force import test_force_overwrite_real_spaces

        test_force_overwrite_real_spaces()
        print(f"E2E OK run={run}")
        return 0
    finally:
        _cleanup_raw(store, settings, run)
        _assert_no_run_residue(store, settings, run)


if __name__ == "__main__":
    sys.exit(main())
