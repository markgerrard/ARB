"""Real-Spaces force-overwrite deployment gate.

Run explicitly; ordinary test discovery intentionally does not collect this module:

    ARB_FILES_E2E=1 PYTHONPATH=src python -m tests.arb_files.e2e_spaces_force
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

from arb_files.config import load_settings
from arb_files.store import FilesStore, _put_if_match_etag
from tests.arb_files.e2e_local_mcp import _assert_no_run_residue, _cleanup_raw


class _RecordingClient:
    def __init__(self, client):
        self._client = client
        self.put_responses = []

    def __getattr__(self, name):
        return getattr(self._client, name)

    def put_object(self, **kwargs):
        response = self._client.put_object(**kwargs)
        self.put_responses.append(response)
        return response


def _response_cell(name: str, response: dict) -> dict:
    metadata = response.get("ResponseMetadata", {})
    return {
        "name": name,
        "status": metadata.get("HTTPStatusCode"),
        "request_id": metadata.get("RequestId"),
    }


def _url_put_cell(name: str, url: str, headers: dict, body: bytes) -> dict:
    request = urllib.request.Request(url, data=body, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {
                "name": name,
                "status": response.status,
                "request_id": response.headers.get("x-amz-request-id"),
            }
    except urllib.error.HTTPError as exc:
        return {
            "name": name,
            "status": exc.code,
            "request_id": exc.headers.get("x-amz-request-id") if exc.headers else None,
        }


def _fresh_head(client, settings, key: str) -> dict:
    return client.head_object(Bucket=settings.bucket, Key=key)


def _wrong_strong_etag(etag: str) -> str:
    if len(etag) < 3 or etag[0] != '"' or etag[-1] != '"':
        raise AssertionError(f"expected quoted strong ETag, got {etag!r}")
    body = etag[1:-1]
    opaque, separator, suffix = body.rpartition("-")
    if not (separator and suffix.isdigit() and opaque):
        opaque, separator, suffix = body, "", ""
    replacement = "0" if opaque[0] != "0" else "1"
    mutated = replacement + opaque[1:]
    return f'"{mutated}{separator}{suffix}"'


def _assert_bytes(store: FilesStore, name: str, expected: bytes) -> None:
    actual, _content_type = store.get_bytes(name)
    assert actual == expected


def test_force_overwrite_real_spaces() -> None:
    if os.environ.get("ARB_FILES_E2E") != "1":
        import pytest

        pytest.skip("set ARB_FILES_E2E=1")

    settings = load_settings(os.environ)
    run = uuid.uuid4().hex
    name = f"_e2e/{run}/force.txt"
    key = f"{settings.prefix}{name}"
    events = []
    store = FilesStore(settings, audit_sink=events.append)
    recorder = _RecordingClient(store.client)
    store._client = recorder
    cells = []
    transcript = {"run": run, "name": name, "cells": cells}

    body_v1 = b"arb-files-force-v1"
    body_v2 = b"arb-files-force-v2"
    body_v3 = b"arb-files-force-v3"

    try:
        store.put_bytes(name, body_v1, "text/plain", uploaded_by="e2e-force")
        cells.append(_response_cell("create", recorder.put_responses[-1]))
        assert 200 <= cells[-1]["status"] < 300
        _assert_bytes(store, name, body_v1)
        etag_v1 = _fresh_head(recorder, settings, key)["ETag"]

        store.put_bytes(name, body_v2, "text/plain", uploaded_by="e2e-force", force=True)
        cells.append(_response_cell("direct_force", recorder.put_responses[-1]))
        assert 200 <= cells[-1]["status"] < 300
        _assert_bytes(store, name, body_v2)
        etag_v2 = _fresh_head(recorder, settings, key)["ETag"]

        presigned = store.presign_put(name, "text/plain", uploaded_by="e2e-force", force=True)
        presigned_cell = _url_put_cell(
            "presigned_force", presigned["url"], presigned["headers"], body_v3
        )
        cells.append(presigned_cell)
        assert 200 <= presigned_cell["status"] < 300
        _assert_bytes(store, name, body_v3)
        etag_v3 = _fresh_head(recorder, settings, key)["ETag"]

        stale_cell = _url_put_cell(
            "stale_presigned_replay", presigned["url"], presigned["headers"], body_v3
        )
        cells.append(stale_cell)
        assert stale_cell["status"] == 412
        _assert_bytes(store, name, body_v3)

        wrong_etag = _wrong_strong_etag(etag_v3)
        try:
            recorder._client.put_object(
                Bucket=settings.bucket,
                Key=key,
                Body=b"wrong-token-write",
                ContentType="text/plain",
                IfMatch=_put_if_match_etag(wrong_etag),
            )
        except Exception as exc:
            response = getattr(exc, "response", {})
            metadata = response.get("ResponseMetadata", {})
            wrong_cell = {
                "name": "wrong_token",
                "status": metadata.get("HTTPStatusCode"),
                "request_id": metadata.get("RequestId"),
            }
        else:
            raise AssertionError("wrong If-Match token unexpectedly succeeded")
        cells.append(wrong_cell)
        assert wrong_cell["status"] == 412
        _assert_bytes(store, name, body_v3)

        assert len(events) == 2, events
        direct_event, presign_event = events
        assert direct_event["op"] == "overwrite" and "via" not in direct_event
        assert presign_event["op"] == "overwrite" and presign_event["via"] == "presign"
        assert run in direct_event["recovery_key"] and run in presign_event["recovery_key"]

        transcript.update(
            {
                "etag_v1": etag_v1,
                "etag_v2": etag_v2,
                "etag_v3": etag_v3,
                "wrong_etag": wrong_etag,
                "final_sha256": hashlib.sha256(body_v3).hexdigest(),
                "audit_events": events,
            }
        )
    finally:
        deleted = _cleanup_raw(store, settings, run)
        residue = _assert_no_run_residue(store, settings, run)
        transcript["cleanup"] = {"deleted_keys": deleted, "residue": residue}

    print(json.dumps(transcript, sort_keys=True))


def main() -> int:
    if os.environ.get("ARB_FILES_E2E") != "1":
        print("SKIP (set ARB_FILES_E2E=1; skipped runs are not backend evidence)")
        return 0
    test_force_overwrite_real_spaces()
    return 0


if __name__ == "__main__":
    sys.exit(main())
