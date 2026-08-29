# ARB Files MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build ARB Files — a file-exchange plane (DO Spaces backend) exposed as `file_*` tools on the existing ARB Memory OAuth door plus a read-write local stdio MCP for seats.

**Architecture:** A backend-agnostic `FilesStore` (boto3 over DO Spaces) wraps name→key validation, atomic clobber via `If-None-Match`, presign, provenance, and audited-recoverable delete. Two transports consume it: door-side `FileTools` (OAuth scope-gated, presign + inline) registered on `src/arb_memory/mcp/server.py`, and a local stdio `arb-files-local` MCP (direct CRUD, `local_path`-validated) for seats. Tests inject a fake S3 client; a separate E2E smoke runs against real Spaces.

**Tech Stack:** Python, boto3 (new dep), `mcp` FastMCP SDK (==1.28.0, already used), DigitalOcean Spaces (S3-compatible).

**Spec:** `docs/superpowers/specs/2026-06-29-arb-files-mcp-design.md` (panel-approved). Read it first.

## Global Constraints

- **Mirror ARB Memory patterns** — `src/arb_memory/mcp/{config,read_tools,local_server,tools,server}.py`, `src/arb_memory/run.py`. Match style, injection seams (`conn_factory`→`client_factory`), and test shape (`tests/arb_memory/`).
- **Namespace:** every object lives under prefix `agent-files/` (config `ARB_FILES_PREFIX`, default `agent-files/`). The prefix is **server-prepended**, never client-supplied.
- **Containment: convention-level** on the shared `arb-files` bucket (Decision #5). Delete-safety, not the key, carries the blast-radius risk.
- **Fail-closed:** validation/scope/path errors raise **before** any S3 or filesystem call.
- **Clobber guard is atomic:** non-`force` puts use `If-None-Match: *` (confirmed honored by DO Spaces → `412`). `force=true` omits it.
- **boto3 is a real new dependency** (confirmed absent; aws CLI bundles its own, non-importable). Add to a `arb-files` optional-dependencies extra; do not hand-roll SigV4.
- **Caps (env, host-configurable):** `inline_put_max` 256 KiB; `inline_get_max` 5 MiB (non-image); `inline_get_image_max` ~3.5 MiB; `presign_ttl` 900 s; `list_max` 1000.
- **Credentials:** `envs/arb-files.env` (0600, gitignored) — `ARB_FILES_ENDPOINT/BUCKET_URL/REGION/BUCKET/ACCESS_KEY/SECRET_KEY`. Never echo secret values; reference by path.
- **TDD, frequent commits.** Each task: failing test → run-fail → minimal impl → run-pass → commit.
- **Run tests with:** `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files/<file> -v`.

---

## Panel revisions (BINDING — these supersede any conflicting task text below)

The plan was reviewed by codex + cold-Opus + pi-GLM (all BUILD_WITH_CHANGES). Apply these — where a
revision conflicts with the original task code/test, **the revision wins**. (The per-reviewer plan
reports are not included in this copy.)

**R1 — Presigned PUT returns required headers + signs provenance (Tasks 4, 8; removes Task 5
`restamp_provenance` as dead code).** A URL alone can't satisfy the signed `If-None-Match` condition,
and human-PUT provenance lands blank. `presign_put` gains `uploaded_by` and returns `headers`; the
uploader (extension/seat) sends them.

```python
    def presign_put(self, name: str, content_type: str | None = None, *,
                    uploaded_by: str, force: bool = False) -> dict:
        key = to_key(self.s.prefix, name)
        params = {"Bucket": self.s.bucket, "Key": key}
        headers = {}
        meta = {"uploaded-by": uploaded_by, "uploaded-at": self._now().isoformat()}
        params["Metadata"] = meta
        for k, v in meta.items():
            headers[f"x-amz-meta-{k}"] = v
        if content_type:
            params["ContentType"] = content_type; headers["Content-Type"] = content_type
        if not force:
            params["IfNoneMatch"] = "*"; headers["If-None-Match"] = "*"
        url = self.client.generate_presigned_url("put_object", Params=params, ExpiresIn=self.s.presign_ttl)
        return {"url": url, "method": "PUT", "expires_in": self.s.presign_ttl, "headers": headers}
```
Door `file_put_url` passes `uploaded_by=self._actor()`. **Delete the `restamp_provenance` method and its
test from Task 5** (provenance now rides the signed headers). Test (Task 4): assert returned `headers`
contains `If-None-Match: "*"` and `x-amz-meta-uploaded-by`.

**R2 — `force` overwrite is audited + recoverable (Tasks 3, 5).** `put_bytes(force=True)` on an
existing key must trash the prior version and emit an audit event before overwriting:

```python
    def put_bytes(self, name, data, content_type, *, uploaded_by, force=False) -> dict:
        key = to_key(self.s.prefix, name)
        if force:
            prior = self.head(name)
            if prior["exists"]:
                date = self._now().date().isoformat()
                trash_key = f"{self.s.prefix}.trash/{date}/{name}"
                self.client.copy_object(Bucket=self.s.bucket, Key=trash_key,
                                        CopySource={"Bucket": self.s.bucket, "Key": key})
                self._emit_audit({"op": "overwrite", "name": name, "actor": uploaded_by,
                                  "etag": prior.get("etag"), "ts": self._now().isoformat(),
                                  "recovery_key": trash_key})
        kwargs = dict(Bucket=self.s.bucket, Key=key, Body=data, ContentType=content_type,
                      Metadata={"uploaded-by": uploaded_by, "uploaded-at": self._now().isoformat()})
        if not force:
            kwargs["IfNoneMatch"] = "*"
        try:
            resp = self.client.put_object(**kwargs)
        except Exception as exc:
            if _err_code(exc) in ("PreconditionFailed", "412"):
                raise ValueError("exists; pass force=true to overwrite") from exc
            raise RuntimeError("files backend unavailable; retry") from exc
        return {"name": name, "size": len(data), "etag": resp.get("ETag")}
```
(`put_bytes` thus needs `self._now`/`_emit_audit` from Task 5 — **move the `now=`/`audit_sink`
`__init__` wiring and `_emit_audit` into Task 3**, and have Task 3's `FakeS3.put_object` accept the
extra params.) Test: `test_force_overwrite_trashes_and_audits`.

**R3 — Audit BEFORE the destructive delete (Task 5).** Order is copy→**audit**→delete, so an audit
failure leaves the live object intact:

```python
    def delete(self, name, *, actor) -> dict:
        key = to_key(self.s.prefix, name); h = self.head(name)
        if not h["exists"]: raise KeyError(f"not found: {name}")
        date = self._now().date().isoformat(); trash_key = f"{self.s.prefix}.trash/{date}/{name}"
        self.client.copy_object(Bucket=self.s.bucket, Key=trash_key,
                                CopySource={"Bucket": self.s.bucket, "Key": key})
        self._emit_audit({"op": "delete", "name": name, "actor": actor, "etag": h.get("etag"),
                          "ts": self._now().isoformat(), "recovery_key": trash_key})   # before delete
        self.client.delete_object(Bucket=self.s.bucket, Key=key)
        return {"deleted": True, "name": name, "recovery": {"trash_key": trash_key}}
```
Change `test_audit_failure_not_swallowed` to also assert `st.head("a")["exists"] is True` after the raise.

**R4 — `list` contract + `.trash` exclusion + prefix validation (Tasks 3, 6, 8).** S3 list returns no
user-metadata, so `list` returns only cheap fields and **excludes the `.trash/` prefix**; provenance is
obtained via `file_head` (that's what it's for). Prefix is validated leniently (trailing slash / empty
allowed), NOT via the strict name validator:

```python
    def list(self, prefix: str = "") -> dict:
        # lenient prefix: allow "", "sub/", reject ".." segments and leading "/"
        for seg in prefix.split("/"):
            if seg in (".", ".."): raise ValueError("invalid prefix segment")
        if prefix.startswith("/"): raise ValueError("prefix must not start with '/'")
        full = f"{self.s.prefix}{prefix}"
        resp = self.client.list_objects_v2(Bucket=self.s.bucket, Prefix=full, MaxKeys=self.s.list_max)
        trash = f"{self.s.prefix}.trash/"
        items = [{"name": self._name_from_key(c["Key"]), "size": c.get("Size"),
                  "modified": _iso(c.get("LastModified")), "etag": c.get("ETag")}
                 for c in resp.get("Contents", []) if not c["Key"].startswith(trash)]
        return {"items": items, "is_truncated": bool(resp.get("IsTruncated")),
                "next_token": resp.get("NextContinuationToken")}
```
Drop `uploaded_by`/`content_type` from the list-item assertions (Task 3 test). **Spec note:** the
list-item shape is `{name,size,modified,etag}`; provenance/content_type via `file_head` — update the
spec's `file_list` row to match. Test: `test_list_excludes_trash`, `test_list_subprefix_ok`.

**R5 — Local FS confinement fail-closed, default `AGENT_WORKDIR` (Tasks 1, 6, 7).** `local_root`
defaults to `ARB_FILES_LOCAL_ROOT or AGENT_WORKDIR`; `resolve_local_path` raises when root is unset:

```python
# config.load_settings:  local_root=env.get("ARB_FILES_LOCAL_ROOT") or env.get("AGENT_WORKDIR")
# paths.resolve_local_path:
def resolve_local_path(root: str | None, path: str) -> str:
    if not root:
        raise ValueError("ARB_FILES_LOCAL_ROOT/AGENT_WORKDIR must be set for local_path operations")
    if not os.path.isabs(path): raise ValueError("local path must be absolute")
    real = os.path.realpath(path); root_real = os.path.realpath(root)
    if not (real == root_real or real.startswith(root_real + os.sep)):
        raise ValueError("local path escapes allowed root")
    return real
```
Replace Task 6 `test_no_root_requires_absolute` with `test_no_root_rejects_even_absolute` (asserts
`ValueError` for `root=None`). `run_local_mcp` needs no change once config defaults from `AGENT_WORKDIR`.

**R6 — Per-token rate limiting on `FileTools` (Task 8).** Mirror `MemoryTools._check_*_allowed`. Add
`read_rate_per_min:int=60`, `write_rate_per_min:int=30` to `Settings`; in `FileTools` keep
`self._read_hits/_write_hits: dict[str,list[float]]` keyed by `self._actor()`, check+append on each
read/write tool, raise `ValueError("rate limit exceeded")` over cap. Test: 31st write in a window raises.

**R7 — Door wiring: extract a tested seam + fail-soft (Task 9).** Replace the inline block + source-string
tests with a real helper and runtime tests:

```python
# src/arb_files/mcp/door_wire.py
import logging
log = logging.getLogger("arb_files.door")
def register_file_tools(server, env, *, store_factory=None) -> bool:
    if not env.get("ARB_FILES_BUCKET"):
        return False
    try:
        from arb_files.config import load_settings
        from arb_files.store import FilesStore
        from arb_files.mcp.door_tools import FileTools
        settings = load_settings(env)
        store = store_factory() if store_factory else FilesStore(settings)
        ft = FileTools(store, settings)
    except Exception:
        log.exception("ARB Files door tools not registered (config/back-end error); memory door unaffected")
        return False
    # define the 7 async wrappers (as in the original Task 9 block) and server.add_tool each
    ...
    return True
```
`server.py` calls `register_file_tools(server, os.environ)` once after the memory `add_tool`s. Tests
(Task 9, runtime not source): (a) `register_file_tools(server, {})` returns False and adds no `file_*`;
(b) with `{"ARB_FILES_BUCKET":"b",...}` + a fake `store_factory`, returns True and
`anyio.run(server.list_tools)` includes the 7 tools; (c) a `store_factory` that raises → returns False,
no crash.

**R8 — E2E exercises transports + a real presigned PUT, and cleans up everything (Task 11).** Drive
`LocalFileTools` and `FileTools` (inject a `require_scope`/`actor` that grant `files.*`), and perform an
actual presigned PUT with the returned headers (assert a second conditional PUT → HTTP 412). Clean up
with the **raw boto3 client**, sweeping every key containing the run id under `agent-files/` **including
`.trash/`**; assert none remain. (Use `urllib.request` for the presigned PUT to avoid a curl dependency.)

**R9 — boto3 floor (Task 1).** `If-None-Match` conditional writes need a recent botocore: pin
`boto3>=1.34.59`.

**R10 — `build_local_server` store is required (Task 7).** Change the interface to
`build_local_server(settings, *, store, seat_id)` (no `store=None`); `run_local_mcp` already passes one.

---

### Task 1: Dependency extra + config loader

**Files:**
- Modify: `pyproject.toml` (add `arb-files` optional-dependencies extra + console script)
- Create: `src/arb_files/__init__.py`
- Create: `src/arb_files/config.py`
- Test: `tests/arb_files/__init__.py`, `tests/arb_files/test_config.py`

**Interfaces:**
- Produces: `arb_files.config.Settings` (frozen dataclass) and `load_settings(env: Mapping) -> Settings`.

```python
# Settings fields (all with the defaults from Global Constraints unless env overrides):
#   endpoint: str        # ARB_FILES_ENDPOINT  (required)
#   region: str          # ARB_FILES_REGION    (required)
#   bucket: str          # ARB_FILES_BUCKET    (required)
#   access_key: str      # ARB_FILES_ACCESS_KEY (required)
#   secret_key: str      # ARB_FILES_SECRET_KEY (required)
#   prefix: str = "agent-files/"
#   presign_ttl: int = 900
#   inline_put_max: int = 262144
#   inline_get_max: int = 5_242_880
#   inline_get_image_max: int = 3_670_016
#   list_max: int = 1000
#   local_root: str | None  # ARB_FILES_LOCAL_ROOT (optional; for local_path confinement)
```

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_files/test_config.py
import pytest
from arb_files.config import load_settings, Settings

BASE = {
    "ARB_FILES_ENDPOINT": "https://<region>.digitaloceanspaces.com",
    "ARB_FILES_REGION": "lon1",
    "ARB_FILES_BUCKET": "arb-files",
    "ARB_FILES_ACCESS_KEY": "AK",
    "ARB_FILES_SECRET_KEY": "SK",
}

def test_loads_required_and_defaults():
    s = load_settings(BASE)
    assert s.bucket == "arb-files"
    assert s.prefix == "agent-files/"
    assert s.presign_ttl == 900
    assert s.inline_put_max == 262144
    assert s.inline_get_image_max == 3_670_016

def test_missing_required_fails_closed():
    with pytest.raises(ValueError):
        load_settings({"ARB_FILES_BUCKET": "arb-files"})

def test_prefix_normalised_with_trailing_slash():
    s = load_settings({**BASE, "ARB_FILES_PREFIX": "agent-files"})
    assert s.prefix == "agent-files/"

def test_env_overrides_caps():
    s = load_settings({**BASE, "ARB_FILES_PRESIGN_TTL": "60", "ARB_FILES_LIST_MAX": "50"})
    assert s.presign_ttl == 60 and s.list_max == 50
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'arb_files'`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/arb_files/__init__.py
```
```python
# src/arb_files/config.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping

@dataclass(frozen=True)
class Settings:
    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key: str
    prefix: str = "agent-files/"
    presign_ttl: int = 900
    inline_put_max: int = 262144
    inline_get_max: int = 5_242_880
    inline_get_image_max: int = 3_670_016
    list_max: int = 1000
    local_root: str | None = None

_REQUIRED = ("ARB_FILES_ENDPOINT", "ARB_FILES_REGION", "ARB_FILES_BUCKET",
             "ARB_FILES_ACCESS_KEY", "ARB_FILES_SECRET_KEY")

def load_settings(env: Mapping[str, str]) -> Settings:
    missing = [k for k in _REQUIRED if not env.get(k)]
    if missing:
        raise ValueError(f"ARB Files config missing: {', '.join(missing)}")
    prefix = env.get("ARB_FILES_PREFIX", "agent-files/")
    if not prefix.endswith("/"):
        prefix += "/"
    def _int(key, default):
        v = env.get(key)
        return int(v) if v else default
    return Settings(
        endpoint=env["ARB_FILES_ENDPOINT"], region=env["ARB_FILES_REGION"],
        bucket=env["ARB_FILES_BUCKET"], access_key=env["ARB_FILES_ACCESS_KEY"],
        secret_key=env["ARB_FILES_SECRET_KEY"], prefix=prefix,
        presign_ttl=_int("ARB_FILES_PRESIGN_TTL", 900),
        inline_put_max=_int("ARB_FILES_INLINE_PUT_MAX", 262144),
        inline_get_max=_int("ARB_FILES_INLINE_GET_MAX", 5_242_880),
        inline_get_image_max=_int("ARB_FILES_INLINE_GET_IMAGE_MAX", 3_670_016),
        list_max=_int("ARB_FILES_LIST_MAX", 1000),
        local_root=env.get("ARB_FILES_LOCAL_ROOT"),
    )
```
```toml
# pyproject.toml — add under [project.optional-dependencies]
arb-files = [
    "boto3>=1.34",
    "mcp==1.28.0",
]
# pyproject.toml — add under [project.scripts]
arb-files-local-mcp = "arb_files.run:run_local_mcp"
```
```python
# tests/arb_files/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files/test_config.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/arb_files/__init__.py src/arb_files/config.py tests/arb_files/
git commit -m "feat(arb-files): config loader + boto3 dependency extra"
```

---

### Task 2: Name validation + key mapping

**Files:**
- Create: `src/arb_files/names.py`
- Test: `tests/arb_files/test_names.py`

**Interfaces:**
- Produces:
  - `arb_files.names.validate_name(name: str) -> None` — raises `ValueError` on invalid.
  - `arb_files.names.to_key(prefix: str, name: str) -> str` — validates then returns `f"{prefix}{name}"`.
- Rule: `name` matches `^[A-Za-z0-9._/-]{1,256}$`, no leading `/`, and **no path-segment** in `{"", ".", ".."}` after splitting on `/`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_files/test_names.py
import pytest
from arb_files.names import validate_name, to_key

@pytest.mark.parametrize("ok", ["report.pdf", "a/b/c.txt", "build-123_final.zip", "x"])
def test_valid_names_pass(ok):
    validate_name(ok)  # no raise

@pytest.mark.parametrize("bad", [
    "", "/leading", "a//b", "../escape", "a/../b", "a/./b", "a/..", "..",
    "with space.txt", "bad\\name", "a"*257, "unié.txt", "a/", "tab\tname",
])
def test_invalid_names_raise(bad):
    with pytest.raises(ValueError):
        validate_name(bad)

def test_to_key_prepends_prefix():
    assert to_key("agent-files/", "a/b.txt") == "agent-files/a/b.txt"

def test_to_key_validates_before_prepend():
    with pytest.raises(ValueError):
        to_key("agent-files/", "../mono-backup/x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files/test_names.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/arb_files/names.py
from __future__ import annotations
import re

_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
_BAD_SEGMENTS = {"", ".", ".."}

def validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError("invalid file name")
    if name.startswith("/"):
        raise ValueError("file name must not start with '/'")
    for seg in name.split("/"):
        if seg in _BAD_SEGMENTS:
            raise ValueError(f"invalid path segment {seg!r}")

def to_key(prefix: str, name: str) -> str:
    validate_name(name)
    return f"{prefix}{name}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files/test_names.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/arb_files/names.py tests/arb_files/test_names.py
git commit -m "feat(arb-files): fail-closed name validation + prefix key mapping"
```

---

### Task 3: FilesStore core — list/head/get/put with fake S3 client + atomic clobber

**Files:**
- Create: `src/arb_files/store.py`
- Create: `tests/arb_files/fakes.py` (in-memory fake S3 client)
- Test: `tests/arb_files/test_store_crud.py`

**Interfaces:**
- Produces `arb_files.store.FilesStore(settings, *, client_factory=None, audit_sink=None)` with:
  - `list(prefix: str = "") -> dict` → `{"items": [ {name,size,modified,content_type,uploaded_by} ], "is_truncated": bool, "next_token": str|None}`
  - `head(name: str) -> dict` → `{exists, name, size, content_type, etag, modified, uploaded_by, uploaded_at}`
  - `get_bytes(name: str) -> tuple[bytes, str]` → `(data, content_type)`; raises `FileNotFoundError`-style `KeyError` if absent.
  - `put_bytes(name, data: bytes, content_type, *, uploaded_by, force=False) -> dict` → `{name, size, etag}`; raises `ValueError("exists; pass force=true")` when key exists and not force (via `If-None-Match`/`412`).
- The injected client mirrors the boto3 S3 client surface used: `put_object`, `get_object`, `head_object`, `list_objects_v2`, `delete_object`, `copy_object`, `generate_presigned_url`. `ClientError` shape: `botocore.exceptions.ClientError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_files/fakes.py
from __future__ import annotations
from datetime import datetime, timezone

class FakeClientError(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)

class FakeS3:
    """In-memory S3 stand-in covering the subset FilesStore uses."""
    def __init__(self):
        self.objects = {}  # key -> {"Body": bytes, "ContentType": str, "Metadata": {}, "LastModified": dt, "ETag": str}

    def put_object(self, Bucket, Key, Body, ContentType="application/octet-stream",
                   Metadata=None, IfNoneMatch=None, **kw):
        if IfNoneMatch == "*" and Key in self.objects:
            raise FakeClientError("PreconditionFailed")
        self.objects[Key] = {"Body": Body, "ContentType": ContentType,
                             "Metadata": Metadata or {},
                             "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc),
                             "ETag": '"deadbeef"'}
        return {"ETag": '"deadbeef"'}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeClientError("404")
        o = self.objects[Key]
        return {"ContentLength": len(o["Body"]), "ContentType": o["ContentType"],
                "ETag": o["ETag"], "LastModified": o["LastModified"], "Metadata": o["Metadata"]}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        o = self.objects[Key]
        class _B:
            def __init__(self, b): self._b = b
            def read(self): return self._b
        return {"Body": _B(o["Body"]), "ContentType": o["ContentType"], "Metadata": o["Metadata"]}

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        page = keys[:MaxKeys]
        contents = [{"Key": k, "Size": len(self.objects[k]["Body"]),
                     "LastModified": self.objects[k]["LastModified"], "ETag": self.objects[k]["ETag"]}
                    for k in page]
        return {"Contents": contents, "IsTruncated": len(keys) > MaxKeys,
                "NextContinuationToken": "tok" if len(keys) > MaxKeys else None}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        return {}

    def copy_object(self, Bucket, Key, CopySource, Metadata=None, MetadataDirective=None, **kw):
        src = CopySource["Key"] if isinstance(CopySource, dict) else CopySource.split("/", 1)[1]
        o = dict(self.objects[src])
        if MetadataDirective == "REPLACE" and Metadata is not None:
            o["Metadata"] = Metadata
        self.objects[Key] = o
        return {"ETag": o["ETag"]}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=900, HttpMethod=None):
        return f"https://signed.example/{Params['Key']}?op={op}&exp={ExpiresIn}"
```
```python
# tests/arb_files/test_store_crud.py
import pytest
from arb_files.config import load_settings
from arb_files.store import FilesStore
from tests.arb_files.fakes import FakeS3, FakeClientError

BASE = {"ARB_FILES_ENDPOINT": "https://e", "ARB_FILES_REGION": "lon1",
        "ARB_FILES_BUCKET": "arb-files", "ARB_FILES_ACCESS_KEY": "AK", "ARB_FILES_SECRET_KEY": "SK"}

def _store(fake=None):
    fake = fake or FakeS3()
    s = load_settings(BASE)
    return FilesStore(s, client_factory=lambda: fake), fake

def test_put_then_head_and_get():
    st, fake = _store()
    st.put_bytes("a/b.txt", b"hello", "text/plain", uploaded_by="seat-1")
    assert "agent-files/a/b.txt" in fake.objects
    h = st.head("a/b.txt")
    assert h["exists"] and h["size"] == 5 and h["uploaded_by"] == "seat-1"
    data, ct = st.get_bytes("a/b.txt")
    assert data == b"hello" and ct == "text/plain"

def test_clobber_guard_blocks_overwrite():
    st, _ = _store()
    st.put_bytes("x", b"1", "text/plain", uploaded_by="s")
    with pytest.raises(ValueError):
        st.put_bytes("x", b"2", "text/plain", uploaded_by="s")  # no force
    st.put_bytes("x", b"2", "text/plain", uploaded_by="s", force=True)  # ok
    assert st.get_bytes("x")[0] == b"2"

def test_head_absent():
    st, _ = _store()
    assert st.head("nope")["exists"] is False

def test_list_includes_name_and_provenance_and_truncation():
    st, _ = _store()
    st.put_bytes("one.txt", b"1", "text/plain", uploaded_by="s")
    out = st.list()
    assert out["items"][0]["name"] == "one.txt"
    assert out["is_truncated"] is False

def test_name_validation_before_s3():
    st, fake = _store()
    with pytest.raises(ValueError):
        st.put_bytes("../escape", b"x", "text/plain", uploaded_by="s")
    assert fake.objects == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files/test_store_crud.py -v`
Expected: FAIL (`ImportError: cannot import name 'FilesStore'`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/arb_files/store.py
from __future__ import annotations
from typing import Callable
from arb_files.config import Settings
from arb_files.names import to_key, validate_name

def _err_code(exc) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")

class FilesStore:
    def __init__(self, settings: Settings, *, client_factory: Callable | None = None, audit_sink=None):
        self.s = settings
        self._client_factory = client_factory or self._default_client_factory
        self._client = None
        self.audit_sink = audit_sink

    def _default_client_factory(self):
        import boto3
        from botocore.config import Config
        return boto3.client(
            "s3", endpoint_url=self.s.endpoint, region_name=self.s.region,
            aws_access_key_id=self.s.access_key, aws_secret_access_key=self.s.secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )

    @property
    def client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _name_from_key(self, key: str) -> str:
        return key[len(self.s.prefix):] if key.startswith(self.s.prefix) else key

    def list(self, prefix: str = "") -> dict:
        full = to_key(self.s.prefix, prefix) if prefix else self.s.prefix
        resp = self.client.list_objects_v2(Bucket=self.s.bucket, Prefix=full, MaxKeys=self.s.list_max)
        items = []
        for c in resp.get("Contents", []):
            items.append({
                "name": self._name_from_key(c["Key"]), "size": c.get("Size"),
                "modified": _iso(c.get("LastModified")), "content_type": None, "uploaded_by": None,
            })
        return {"items": items, "is_truncated": bool(resp.get("IsTruncated")),
                "next_token": resp.get("NextContinuationToken")}

    def head(self, name: str) -> dict:
        key = to_key(self.s.prefix, name)
        try:
            h = self.client.head_object(Bucket=self.s.bucket, Key=key)
        except Exception as exc:
            if _err_code(exc) in ("404", "NoSuchKey", "NotFound"):
                return {"exists": False, "name": name}
            raise RuntimeError("files backend unavailable; retry") from exc
        meta = h.get("Metadata", {})
        return {"exists": True, "name": name, "size": h.get("ContentLength"),
                "content_type": h.get("ContentType"), "etag": h.get("ETag"),
                "modified": _iso(h.get("LastModified")),
                "uploaded_by": meta.get("uploaded-by"), "uploaded_at": meta.get("uploaded-at")}

    def get_bytes(self, name: str) -> tuple[bytes, str]:
        key = to_key(self.s.prefix, name)
        try:
            o = self.client.get_object(Bucket=self.s.bucket, Key=key)
        except Exception as exc:
            if _err_code(exc) in ("404", "NoSuchKey", "NotFound"):
                raise KeyError(f"not found: {name}") from exc
            raise RuntimeError("files backend unavailable; retry") from exc
        return o["Body"].read(), o.get("ContentType", "application/octet-stream")

    def put_bytes(self, name: str, data: bytes, content_type: str, *, uploaded_by: str, force: bool = False) -> dict:
        key = to_key(self.s.prefix, name)
        kwargs = dict(Bucket=self.s.bucket, Key=key, Body=data, ContentType=content_type,
                      Metadata={"uploaded-by": uploaded_by, "uploaded-at": _now_iso()})
        if not force:
            kwargs["IfNoneMatch"] = "*"
        try:
            resp = self.client.put_object(**kwargs)
        except Exception as exc:
            if _err_code(exc) in ("PreconditionFailed", "412"):
                raise ValueError("exists; pass force=true to overwrite") from exc
            raise RuntimeError("files backend unavailable; retry") from exc
        return {"name": name, "size": len(data), "etag": resp.get("ETag")}

def _iso(dt):
    return dt.isoformat() if dt is not None and hasattr(dt, "isoformat") else dt

def _now_iso():
    # Injected/overridable in tests; avoid module import cycle.
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files/test_store_crud.py -v`
Expected: PASS (5 tests). Note: `head` provenance comes from `Metadata`; the fake stores it, so `uploaded_by == "seat-1"`.

- [ ] **Step 5: Commit**

```bash
git add src/arb_files/store.py tests/arb_files/fakes.py tests/arb_files/test_store_crud.py
git commit -m "feat(arb-files): FilesStore CRUD with atomic If-None-Match clobber guard"
```

---

### Task 4: Presigned GET/PUT (with signed conditional)

**Files:**
- Modify: `src/arb_files/store.py` (add `presign_get`, `presign_put`)
- Test: `tests/arb_files/test_store_presign.py`

**Interfaces:**
- Produces:
  - `FilesStore.presign_get(name) -> dict` → `{url, method:"GET", expires_in, size, content_type}` (HEAD first for size/ct; raises `KeyError` if absent).
  - `FilesStore.presign_put(name, content_type=None, *, force=False) -> dict` → `{url, method:"PUT", expires_in}`; non-force signs `If-None-Match: *` into the URL params.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_files/test_store_presign.py
import pytest
from arb_files.config import load_settings
from arb_files.store import FilesStore
from tests.arb_files.fakes import FakeS3

BASE = {"ARB_FILES_ENDPOINT": "https://e", "ARB_FILES_REGION": "lon1",
        "ARB_FILES_BUCKET": "arb-files", "ARB_FILES_ACCESS_KEY": "AK", "ARB_FILES_SECRET_KEY": "SK"}

def _store():
    fake = FakeS3(); s = load_settings(BASE)
    return FilesStore(s, client_factory=lambda: fake), fake

def test_presign_get_requires_existing_object():
    st, _ = _store()
    with pytest.raises(KeyError):
        st.presign_get("missing")

def test_presign_get_returns_url_and_meta():
    st, _ = _store()
    st.put_bytes("a.txt", b"hi", "text/plain", uploaded_by="s")
    out = st.presign_get("a.txt")
    assert out["method"] == "GET" and out["expires_in"] == 900
    assert "agent-files/a.txt" in out["url"] and out["size"] == 2

def test_presign_put_nonforce_signs_conditional():
    st, fake = _store()
    captured = {}
    orig = fake.generate_presigned_url
    def spy(op, Params=None, ExpiresIn=900, HttpMethod=None):
        captured.update(Params or {}); return orig(op, Params=Params, ExpiresIn=ExpiresIn, HttpMethod=HttpMethod)
    fake.generate_presigned_url = spy
    out = st.presign_put("b.bin", content_type="application/octet-stream")
    assert out["method"] == "PUT"
    assert captured.get("IfNoneMatch") == "*"

def test_presign_put_force_omits_conditional():
    st, fake = _store()
    captured = {}
    fake.generate_presigned_url = lambda op, Params=None, ExpiresIn=900, HttpMethod=None: (captured.update(Params or {}) or "u")
    st.presign_put("b.bin", force=True)
    assert "IfNoneMatch" not in captured
```

- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files/test_store_presign.py -v` → FAIL (`AttributeError: presign_get`).

- [ ] **Step 3: Write minimal implementation** (append to `FilesStore`)

```python
    def presign_get(self, name: str) -> dict:
        h = self.head(name)
        if not h["exists"]:
            raise KeyError(f"not found: {name}")
        key = to_key(self.s.prefix, name)
        url = self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.s.bucket, "Key": key}, ExpiresIn=self.s.presign_ttl)
        return {"url": url, "method": "GET", "expires_in": self.s.presign_ttl,
                "size": h.get("size"), "content_type": h.get("content_type")}

    def presign_put(self, name: str, content_type: str | None = None, *, force: bool = False) -> dict:
        key = to_key(self.s.prefix, name)
        params = {"Bucket": self.s.bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        if not force:
            params["IfNoneMatch"] = "*"
        url = self.client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=self.s.presign_ttl, HttpMethod="PUT")
        return {"url": url, "method": "PUT", "expires_in": self.s.presign_ttl}
```

- [ ] **Step 4: Run** → PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/arb_files/store.py tests/arb_files/test_store_presign.py
git commit -m "feat(arb-files): presigned GET/PUT with signed If-None-Match for non-force"
```

---

### Task 5: Audited, recoverable delete + provenance re-stamp

**Files:**
- Modify: `src/arb_files/store.py` (add `delete`, `restamp_provenance`)
- Test: `tests/arb_files/test_store_delete.py`

**Interfaces:**
- Produces:
  - `FilesStore.delete(name, *, actor: str) -> dict` → `{deleted: True, name, recovery: {...}}`. Soft-delete: server-side `copy_object` to `agent-files/.trash/<date>/<name>` then `delete_object`. Emits an audit event via `audit_sink(event: dict)` (op/name/actor/etag/ts); an audit-emit failure raises (no silent drop).
  - `FilesStore.restamp_provenance(name, *, uploaded_by) -> None` — `copy_object` in place with `MetadataDirective="REPLACE"`, for the human presigned-PUT path.
- The `.trash/` date segment uses an injectable `now` to stay deterministic in tests: add `now: Callable[[], datetime] | None` to `FilesStore.__init__`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_files/test_store_delete.py
from datetime import datetime, timezone
import pytest
from arb_files.config import load_settings
from arb_files.store import FilesStore
from tests.arb_files.fakes import FakeS3

BASE = {"ARB_FILES_ENDPOINT": "https://e", "ARB_FILES_REGION": "lon1",
        "ARB_FILES_BUCKET": "arb-files", "ARB_FILES_ACCESS_KEY": "AK", "ARB_FILES_SECRET_KEY": "SK"}

def _store(audit=None):
    fake = FakeS3(); s = load_settings(BASE)
    fixed = lambda: datetime(2026, 6, 29, tzinfo=timezone.utc)
    return FilesStore(s, client_factory=lambda: fake, audit_sink=audit, now=fixed), fake

def test_delete_is_soft_and_recoverable():
    events = []
    st, fake = _store(audit=events.append)
    st.put_bytes("doomed.txt", b"x", "text/plain", uploaded_by="s")
    out = st.delete("doomed.txt", actor="seat-2")
    assert out["deleted"] and "recovery" in out
    assert "agent-files/doomed.txt" not in fake.objects               # gone from live
    assert "agent-files/.trash/2026-06-29/doomed.txt" in fake.objects  # recoverable

def test_delete_emits_audit_event():
    events = []
    st, _ = _store(audit=events.append)
    st.put_bytes("a", b"x", "text/plain", uploaded_by="s")
    st.delete("a", actor="seat-2")
    assert events and events[0]["op"] == "delete" and events[0]["actor"] == "seat-2"

def test_audit_failure_not_swallowed():
    def boom(_): raise RuntimeError("audit down")
    st, _ = _store(audit=boom)
    st.put_bytes("a", b"x", "text/plain", uploaded_by="s")
    with pytest.raises(RuntimeError):
        st.delete("a", actor="seat-2")

def test_restamp_provenance_sets_uploaded_by():
    st, fake = _store()
    st.put_bytes("h.bin", b"x", "application/octet-stream", uploaded_by="anonymous")
    st.restamp_provenance("h.bin", uploaded_by="client-xyz")
    assert fake.objects["agent-files/h.bin"]["Metadata"]["uploaded-by"] == "client-xyz"
```

- [ ] **Step 2: Run** → FAIL (`TypeError: now` / `AttributeError: delete`).

- [ ] **Step 3: Write minimal implementation**

Update `__init__` signature to `def __init__(self, settings, *, client_factory=None, audit_sink=None, now=None):` and store `self._now = now or _default_now`. Add `_default_now` and methods:

```python
    def delete(self, name: str, *, actor: str) -> dict:
        key = to_key(self.s.prefix, name)
        h = self.head(name)
        if not h["exists"]:
            raise KeyError(f"not found: {name}")
        date = self._now().date().isoformat()
        trash_key = f"{self.s.prefix}.trash/{date}/{name}"
        self.client.copy_object(Bucket=self.s.bucket, Key=trash_key,
                                CopySource={"Bucket": self.s.bucket, "Key": key})
        self.client.delete_object(Bucket=self.s.bucket, Key=key)
        self._emit_audit({"op": "delete", "name": name, "actor": actor,
                          "etag": h.get("etag"), "ts": self._now().isoformat(),
                          "recovery_key": trash_key})
        return {"deleted": True, "name": name, "recovery": {"trash_key": trash_key}}

    def restamp_provenance(self, name: str, *, uploaded_by: str) -> None:
        key = to_key(self.s.prefix, name)
        self.client.copy_object(Bucket=self.s.bucket, Key=key,
                                CopySource={"Bucket": self.s.bucket, "Key": key},
                                Metadata={"uploaded-by": uploaded_by, "uploaded-at": self._now().isoformat()},
                                MetadataDirective="REPLACE")

    def _emit_audit(self, event: dict) -> None:
        if self.audit_sink is None:
            return
        self.audit_sink(event)  # deliberately not swallowed — evidence plane
```
```python
# module-level
def _default_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
```
(Replace `_now_iso()` use in `put_bytes` with `self._now().isoformat()`.)

- [ ] **Step 4: Run** → PASS (4 tests). Re-run Task 3 + 4 suites to confirm no regression: `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files -v`.

- [ ] **Step 5: Commit**

```bash
git add src/arb_files/store.py tests/arb_files/test_store_delete.py
git commit -m "feat(arb-files): soft-delete + audit event + provenance re-stamp"
```

---

### Task 6: Local read-write tools + local_path validation

**Files:**
- Create: `src/arb_files/mcp/__init__.py`
- Create: `src/arb_files/mcp/local_tools.py`
- Create: `src/arb_files/paths.py` (local_path confinement)
- Test: `tests/arb_files/test_local_tools.py`, `tests/arb_files/test_paths.py`

**Interfaces:**
- Produces `arb_files.paths.resolve_local_path(root: str|None, path: str) -> str` — abspath+realpath; raises `ValueError` if outside `root` (when root set) or not absolute.
- Produces `arb_files.mcp.local_tools.LocalFileTools(store, *, seat_id, settings)` with async methods: `file_list`, `file_head`, `file_get`, `file_put`, `file_delete`, `file_get_url`, `file_put_url`. `file_get(name, to_path=None)`/`file_put(name, from_path=None, content_b64=None, content_type=...)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/arb_files/test_paths.py
import os, pytest
from arb_files.paths import resolve_local_path

def test_absolute_inside_root_ok(tmp_path):
    root = str(tmp_path); p = os.path.join(root, "a.txt")
    assert resolve_local_path(root, p) == os.path.realpath(p)

def test_outside_root_rejected(tmp_path):
    with pytest.raises(ValueError):
        resolve_local_path(str(tmp_path), "/etc/passwd")

def test_relative_rejected(tmp_path):
    with pytest.raises(ValueError):
        resolve_local_path(str(tmp_path), "a.txt")

def test_no_root_requires_absolute():
    with pytest.raises(ValueError):
        resolve_local_path(None, "rel/path")
```
```python
# tests/arb_files/test_local_tools.py
import anyio, pytest
from arb_files.config import load_settings
from arb_files.store import FilesStore
from arb_files.mcp.local_tools import LocalFileTools
from tests.arb_files.fakes import FakeS3

BASE = {"ARB_FILES_ENDPOINT": "https://e", "ARB_FILES_REGION": "lon1",
        "ARB_FILES_BUCKET": "arb-files", "ARB_FILES_ACCESS_KEY": "AK", "ARB_FILES_SECRET_KEY": "SK"}

def _tools():
    s = load_settings(BASE); fake = FakeS3()
    st = FilesStore(s, client_factory=lambda: fake)
    return LocalFileTools(st, seat_id="seat-A", settings=s), fake

def test_put_inline_b64_then_get():
    import base64
    t, _ = _tools()
    b64 = base64.b64encode(b"payload").decode()
    anyio.run(t.file_put, "p.bin", None, b64, "application/octet-stream", False)
    out = anyio.run(t.file_get, "p.bin", None)
    assert base64.b64decode(out["content_b64"]) == b"payload"

def test_delete_records_actor_as_seat():
    events = []
    s = load_settings(BASE); fake = FakeS3()
    st = FilesStore(s, client_factory=lambda: fake, audit_sink=events.append)
    t = LocalFileTools(st, seat_id="seat-A", settings=s)
    import base64
    anyio.run(t.file_put, "d", None, base64.b64encode(b"x").decode(), "text/plain", False)
    anyio.run(t.file_delete, "d")
    assert events[0]["actor"] == "seat-A"
```

- [ ] **Step 2: Run** both → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/arb_files/mcp/__init__.py
```
```python
# src/arb_files/paths.py
from __future__ import annotations
import os

def resolve_local_path(root: str | None, path: str) -> str:
    if not os.path.isabs(path):
        raise ValueError("local path must be absolute")
    real = os.path.realpath(path)
    if root is not None:
        root_real = os.path.realpath(root)
        if not (real == root_real or real.startswith(root_real + os.sep)):
            raise ValueError("local path escapes allowed root")
    return real
```
```python
# src/arb_files/mcp/local_tools.py
from __future__ import annotations
import base64
from arb_files.paths import resolve_local_path

class LocalFileTools:
    def __init__(self, store, *, seat_id: str, settings):
        self.store = store; self.seat_id = seat_id; self.s = settings

    async def file_list(self, prefix: str = "") -> dict:
        return self.store.list(prefix)

    async def file_head(self, name: str) -> dict:
        return self.store.head(name)

    async def file_get(self, name: str, to_path: str | None = None) -> dict:
        data, ct = self.store.get_bytes(name)
        if to_path:
            dest = resolve_local_path(self.s.local_root, to_path)
            with open(dest, "wb") as fh:
                fh.write(data)
            return {"name": name, "written_to": dest, "size": len(data), "content_type": ct}
        return {"name": name, "content_b64": base64.b64encode(data).decode(), "content_type": ct, "size": len(data)}

    async def file_put(self, name: str, from_path: str | None = None,
                       content_b64: str | None = None, content_type: str = "application/octet-stream",
                       force: bool = False) -> dict:
        if from_path:
            src = resolve_local_path(self.s.local_root, from_path)
            with open(src, "rb") as fh:
                data = fh.read()
        elif content_b64 is not None:
            data = base64.b64decode(content_b64)
        else:
            raise ValueError("provide from_path or content_b64")
        return self.store.put_bytes(name, data, content_type, uploaded_by=self.seat_id, force=force)

    async def file_delete(self, name: str) -> dict:
        return self.store.delete(name, actor=self.seat_id)

    async def file_get_url(self, name: str) -> dict:
        return self.store.presign_get(name)

    async def file_put_url(self, name: str, content_type: str | None = None, force: bool = False) -> dict:
        return self.store.presign_put(name, content_type, force=force)
```

- [ ] **Step 4: Run** → PASS. Full suite green: `PYTHONPATH=src .venv/bin/python -m pytest tests/arb_files -v`.

- [ ] **Step 5: Commit**

```bash
git add src/arb_files/mcp/__init__.py src/arb_files/paths.py src/arb_files/mcp/local_tools.py tests/arb_files/test_paths.py tests/arb_files/test_local_tools.py
git commit -m "feat(arb-files): local read-write tools + local_path confinement"
```

---

### Task 7: Local stdio server + console entrypoint

**Files:**
- Create: `src/arb_files/mcp/local_server.py`
- Create: `src/arb_files/run.py`
- Test: `tests/arb_files/test_local_server.py`

**Interfaces:**
- Produces `arb_files.mcp.local_server.build_local_server(settings, *, store=None, seat_id) -> FastMCP` registering the 7 local tools.
- Produces `arb_files.run.run_local_mcp() -> None` — loads settings from env, builds store + server, `server.run(transport="stdio")`. (Mirror `arb_memory.run.run_local_read_mcp`.)

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_files/test_local_server.py
from arb_files.config import load_settings
from arb_files.mcp.local_server import build_local_server
from tests.arb_files.fakes import FakeS3
from arb_files.store import FilesStore

BASE = {"ARB_FILES_ENDPOINT": "https://e", "ARB_FILES_REGION": "lon1",
        "ARB_FILES_BUCKET": "arb-files", "ARB_FILES_ACCESS_KEY": "AK", "ARB_FILES_SECRET_KEY": "SK"}

def test_server_registers_expected_tools():
    s = load_settings(BASE)
    store = FilesStore(s, client_factory=lambda: FakeS3())
    server = build_local_server(s, store=store, seat_id="seat-A")
    import anyio
    names = {t.name for t in anyio.run(server.list_tools)}
    assert {"file_list","file_head","file_get","file_put","file_delete","file_get_url","file_put_url"} <= names
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/arb_files/mcp/local_server.py
from __future__ import annotations
from mcp.server.fastmcp import FastMCP
from arb_files.mcp.local_tools import LocalFileTools

def build_local_server(settings, *, store, seat_id: str) -> FastMCP:
    tools = LocalFileTools(store, seat_id=seat_id, settings=settings)
    server = FastMCP("arb-files-local")
    for n in ("file_list","file_head","file_get","file_put","file_delete","file_get_url","file_put_url"):
        server.add_tool(getattr(tools, n), name=n)
    return server
```
```python
# src/arb_files/run.py
from __future__ import annotations
import os, socket

def run_local_mcp() -> None:
    from arb_files.config import load_settings
    from arb_files.store import FilesStore
    from arb_files.mcp.local_server import build_local_server
    settings = load_settings(os.environ)
    seat_id = os.environ.get("ARB_FILES_SEAT_ID") or f"seat-{socket.gethostname()}"
    store = FilesStore(settings)
    server = build_local_server(settings, store=store, seat_id=seat_id)
    server.run(transport="stdio")
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/arb_files/mcp/local_server.py src/arb_files/run.py tests/arb_files/test_local_server.py
git commit -m "feat(arb-files): local stdio MCP server + arb-files-local-mcp entrypoint"
```

---

### Task 8: Door tools (scope-gated, inline caps, MIME allowlist, image return type)

**Files:**
- Create: `src/arb_files/mcp/door_tools.py`
- Test: `tests/arb_files/test_door_tools.py`

**Interfaces:**
- Produces `arb_files.mcp.door_tools.FileTools(store, settings, *, require_scope=None, actor=None)` with async tools: `file_list`, `file_head`, `file_get_url`, `file_get_inline`, `file_put_inline`, `file_put_url`, `file_delete`.
  - Write tools call `self._require_write_scope()` (mirrors `MemoryTools._require_write_scope`: raises `PermissionError` unless `files.write` in token scopes). For testability accept an injectable `require_scope` callable and `actor` callable; defaults use `mcp.server.auth.middleware.auth_context.get_access_token`.
  - `file_put_inline(name, content, content_type="text/plain", force=False)`: enforce `content_type in {"text/plain","text/markdown","application/json"}` (`ValueError` otherwise); enforce `len(content.encode()) <= inline_put_max`.
  - `file_get_inline(name)`: HEAD-gate; if `content_type` starts `image/` and `size <= inline_get_image_max`, return `mcp.types.ImageContent(type="image", data=<b64>, mimeType=ct)`; elif non-image and `size <= inline_get_max`, return `{content_b64, content_type, size}`; else `ValueError` pointing at `file_get_url`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_files/test_door_tools.py
import base64, anyio, pytest
from mcp.types import ImageContent
from arb_files.config import load_settings
from arb_files.store import FilesStore
from arb_files.mcp.door_tools import FileTools
from tests.arb_files.fakes import FakeS3

BASE = {"ARB_FILES_ENDPOINT": "https://e", "ARB_FILES_REGION": "lon1",
        "ARB_FILES_BUCKET": "arb-files", "ARB_FILES_ACCESS_KEY": "AK", "ARB_FILES_SECRET_KEY": "SK"}

def _tools(scopes=("files.read","files.write")):
    s = load_settings(BASE); fake = FakeS3()
    st = FilesStore(s, client_factory=lambda: fake)
    def require(scope):
        if scope not in scopes: raise PermissionError(f"{scope} required")
    return FileTools(st, s, require_scope=require, actor=lambda: "client-1"), fake

def test_write_requires_files_write_scope():
    t, _ = _tools(scopes=("files.read",))
    with pytest.raises(PermissionError):
        anyio.run(t.file_put_inline, "a.txt", "hi", "text/plain", False)

def test_put_inline_rejects_disallowed_mime():
    t, _ = _tools()
    with pytest.raises(ValueError):
        anyio.run(t.file_put_inline, "a.png", "x", "image/png", False)

def test_put_inline_enforces_size_cap():
    t, _ = _tools()
    big = "a" * (262144 + 1)
    with pytest.raises(ValueError):
        anyio.run(t.file_put_inline, "big.txt", big, "text/plain", False)

def test_get_inline_image_returns_ImageContent():
    t, fake = _tools()
    fake.objects["agent-files/p.png"] = {"Body": b"\x89PNG", "ContentType": "image/png",
        "Metadata": {}, "LastModified": None, "ETag": '"x"'}
    out = anyio.run(t.file_get_inline, "p.png")
    assert isinstance(out, ImageContent) and out.mimeType == "image/png"

def test_get_inline_oversize_directs_to_url():
    t, fake = _tools()
    fake.objects["agent-files/big.bin"] = {"Body": b"x"*10, "ContentType":"application/octet-stream",
        "Metadata": {}, "LastModified": None, "ETag": '"x"'}
    # shrink the cap so 10 bytes is "oversize"
    object.__setattr__(t.s, "inline_get_max", 1)
    with pytest.raises(ValueError):
        anyio.run(t.file_get_inline, "big.bin")
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/arb_files/mcp/door_tools.py
from __future__ import annotations
import base64
from mcp.types import ImageContent

WRITE_MIME_ALLOWLIST = {"text/plain", "text/markdown", "application/json"}

class FileTools:
    def __init__(self, store, settings, *, require_scope=None, actor=None):
        self.store = store; self.s = settings
        self._require_scope = require_scope or self._default_require_scope
        self._actor = actor or self._default_actor

    def _default_require_scope(self, scope: str) -> None:
        from mcp.server.auth.middleware.auth_context import get_access_token
        token = get_access_token()
        if token is None or scope not in (getattr(token, "scopes", None) or []):
            raise PermissionError(f"{scope} scope required")

    def _default_actor(self) -> str:
        from mcp.server.auth.middleware.auth_context import get_access_token
        token = get_access_token()
        return getattr(token, "client_id", None) or "mcp"

    def _require_write_scope(self):
        self._require_scope("files.write")

    async def file_list(self, prefix: str = "") -> dict:
        self._require_scope("files.read"); return self.store.list(prefix)

    async def file_head(self, name: str) -> dict:
        self._require_scope("files.read"); return self.store.head(name)

    async def file_get_url(self, name: str) -> dict:
        self._require_scope("files.read"); return self.store.presign_get(name)

    async def file_get_inline(self, name: str):
        self._require_scope("files.read")
        h = self.store.head(name)
        if not h["exists"]:
            raise KeyError(f"not found: {name}")
        ct = h.get("content_type") or "application/octet-stream"
        size = h.get("size") or 0
        if ct.startswith("image/"):
            if size > self.s.inline_get_image_max:
                raise ValueError(f"image too large for inline ({size}B); use file_get_url")
            data, _ = self.store.get_bytes(name)
            return ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=ct)
        if size > self.s.inline_get_max:
            raise ValueError(f"too large for inline ({size}B); use file_get_url")
        data, _ = self.store.get_bytes(name)
        return {"content_b64": base64.b64encode(data).decode(), "content_type": ct, "size": size}

    async def file_put_inline(self, name: str, content: str, content_type: str = "text/plain",
                              force: bool = False) -> dict:
        self._require_write_scope()
        if content_type not in WRITE_MIME_ALLOWLIST:
            raise ValueError(f"unsupported mime {content_type!r}; binaries use file_put_url")
        data = content.encode("utf-8")
        if len(data) > self.s.inline_put_max:
            raise ValueError("content too large for inline; use file_put_url")
        return self.store.put_bytes(name, data, content_type, uploaded_by=self._actor(), force=force)

    async def file_put_url(self, name: str, content_type: str | None = None, force: bool = False) -> dict:
        self._require_write_scope(); return self.store.presign_put(name, content_type, force=force)

    async def file_delete(self, name: str) -> dict:
        self._require_write_scope(); return self.store.delete(name, actor=self._actor())
```

- [ ] **Step 4: Run** → PASS (5 tests). Full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/arb_files/mcp/door_tools.py tests/arb_files/test_door_tools.py
git commit -m "feat(arb-files): door FileTools — scope gating, MIME allowlist, ImageContent return"
```

---

### Task 9: Wire file tools + scopes onto the ARB Memory door

**Files:**
- Modify: `src/arb_memory/mcp/server.py` (add `files.read`/`files.write` to `valid_scopes`/`default_scopes`; instantiate `FileTools`; `add_tool` the 7 tools)
- Test: `tests/arb_files/test_door_wiring.py`

**Interfaces:**
- Consumes `FileTools` (Task 8). The door builds a `FilesStore` from env at startup (lazy — only if `ARB_FILES_BUCKET` is set, so the memory door still boots without files config). Guard: wrap files-store construction in a try/`if env present` so a missing ARB Files env does not break the memory door.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_files/test_door_wiring.py
# Verifies the scope list now advertises files.* and the builder registers file_* tools.
import inspect
from arb_memory.mcp import server as srv

def test_valid_scopes_include_files():
    src = inspect.getsource(srv)
    assert '"files.read"' in src and '"files.write"' in src

def test_file_tools_registered_in_builder():
    src = inspect.getsource(srv)
    for n in ("file_list","file_get_url","file_put_inline","file_put_url","file_delete","file_head","file_get_inline"):
        assert n in src
```

(Integration-level assertion kept source-based to avoid standing up the full OAuth app in a unit test; the real wiring is exercised by the E2E in Task 11.)

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Write minimal implementation**

In `src/arb_memory/mcp/server.py`:
1. At the `ClientRegistrationOptions` (line ~320-323): change to
   `valid_scopes=["memory.read", "memory.write", "files.read", "files.write"]` and
   `default_scopes=["memory.read", "memory.write", "files.read", "files.write"]`. Leave `required_scopes=["memory.read"]`.
2. After the `memory_*` `add_tool` block (line ~375), add:

```python
    # ARB Files tools (optional — only if files backend configured; the memory door
    # must still boot without ARB Files env).
    import os as _os
    if _os.environ.get("ARB_FILES_BUCKET"):
        from arb_files.config import load_settings as _files_settings
        from arb_files.store import FilesStore as _FilesStore
        from arb_files.mcp.door_tools import FileTools as _FileTools
        _fs = _FilesStore(_files_settings(_os.environ))
        _ft = _FileTools(_fs, _files_settings(_os.environ))

        async def file_list(prefix: str = "") -> dict:
            """List files under agent-files/<prefix>. Returns items + is_truncated."""
            return await _ft.file_list(prefix)
        async def file_head(name: str) -> dict:
            """Existence/metadata probe for a file (size, content_type, uploaded_by)."""
            return await _ft.file_head(name)
        async def file_get_url(name: str) -> dict:
            """Presigned GET URL for any-size download (human clicks the link)."""
            return await _ft.file_get_url(name)
        async def file_get_inline(name: str):
            """Inline fetch: images return as vision content; small non-images as base64."""
            return await _ft.file_get_inline(name)
        async def file_put_inline(name: str, content: str, content_type: str = "text/plain", force: bool = False) -> dict:
            """Store a small text artefact the assistant authored (text/markdown/json)."""
            return await _ft.file_put_inline(name, content, content_type, force)
        async def file_put_url(name: str, content_type: str | None = None, force: bool = False) -> dict:
            """Presigned PUT URL for uploading a binary/large file (human or seat PUTs)."""
            return await _ft.file_put_url(name, content_type, force)
        async def file_delete(name: str) -> dict:
            """Delete a file (soft-delete to .trash + audit event; recoverable)."""
            return await _ft.file_delete(name)

        for _tool in (file_list, file_head, file_get_url, file_get_inline,
                      file_put_inline, file_put_url, file_delete):
            server.add_tool(_tool, name=_tool.__name__)
```

- [ ] **Step 4: Run** → PASS. Confirm the memory door still imports cleanly without ARB Files env: `PYTHONPATH=src .venv/bin/python -c "import arb_memory.mcp.server"`.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/mcp/server.py tests/arb_files/test_door_wiring.py
git commit -m "feat(arb-files): register file_* tools + files.* scopes on the ARB Memory door"
```

---

### Task 10: Seat provisioning docs (tools/arb-files-local)

**Files:**
- Create: `tools/arb-files-local/README.md`
- Create: `tools/arb-files-local/PROVISIONING.md`

(No test — docs task. Mirror `tools/arb-memory-local/{README,PROVISIONING}.md` shape: what the local MCP is, the `arb-files-local-mcp` console entry, the MCP client `command`/`args`/`env` config block pointing at the venv entrypoint with `ARB_FILES_*` env (referenced from `envs/arb-files.env`, never inlined secrets), and the `ARB_FILES_SEAT_ID` / `ARB_FILES_LOCAL_ROOT` knobs.)

- [ ] **Step 1: Write the docs** mirroring `tools/arb-memory-local/README.md`, documenting:
  - install: `pip install -e '.[arb-files]'`
  - run: `arb-files-local-mcp` (stdio)
  - MCP client config block (command = venv `arb-files-local-mcp`, env sourced from `envs/arb-files.env` + `ARB_FILES_SEAT_ID`, `ARB_FILES_LOCAL_ROOT`)
  - the 7 tools and the read-write note + delete-is-soft note.
- [ ] **Step 2: Commit**

```bash
git add tools/arb-files-local/
git commit -m "docs(arb-files): seat provisioning for the local read-write MCP"
```

---

### Task 11: E2E smoke against real DO Spaces (both transports)

**Files:**
- Create: `tests/arb_files/e2e_local_mcp.py` (NOT collected by default pytest run — guarded by env, like `tests/arb_memory/e2e_local_read_mcp.py`)

**Interfaces:**
- Consumes the real backend via `envs/arb-files.env`. Uses a disposable prefix `agent-files/_e2e/<run-uuid>/` and asserts writer-quiesced cleanup (memory `run-isolated-verdict`).

- [ ] **Step 1: Write the E2E script**

```python
# tests/arb_files/e2e_local_mcp.py
"""E2E smoke vs real DO Spaces. Run:
  set -a; . envs/arb-files.env; set +a
  ARB_FILES_E2E=1 PYTHONPATH=src .venv/bin/python -m tests.arb_files.e2e_local_mcp
Skips unless ARB_FILES_E2E=1.
"""
from __future__ import annotations
import base64, os, sys, uuid

def main() -> int:
    if os.environ.get("ARB_FILES_E2E") != "1":
        print("SKIP (set ARB_FILES_E2E=1)"); return 0
    from arb_files.config import load_settings
    from arb_files.store import FilesStore
    s = load_settings(os.environ)
    run = uuid.uuid4().hex[:12]
    base = f"_e2e/{run}"
    events = []
    st = FilesStore(s, audit_sink=events.append)
    name = f"{base}/probe.txt"
    try:
        st.put_bytes(name, b"e2e-hello", "text/plain", uploaded_by="e2e")
        assert st.head(name)["exists"], "head after put"
        try:
            st.put_bytes(name, b"x", "text/plain", uploaded_by="e2e")  # expect clobber block
            print("FAIL: clobber guard did not fire"); return 1
        except ValueError:
            pass
        data, ct = st.get_bytes(name); assert data == b"e2e-hello", "roundtrip"
        url = st.presign_get(name)["url"]; assert "http" in url
        listed = st.list(base); assert any(i["name"].endswith("probe.txt") for i in listed["items"]), "list"
        out = st.delete(name, actor="e2e"); assert out["deleted"] and events, "delete+audit"
        assert not st.head(name)["exists"], "gone after delete"
        # cleanup the trash copy too
        trash = out["recovery"]["trash_key"][len(s.prefix):]
        st.delete(trash, actor="e2e-cleanup")
        print(f"E2E OK run={run}"); return 0
    finally:
        # best-effort sweep of the run prefix
        for it in st.list(base)["items"]:
            try: st.delete(it["name"], actor="e2e-cleanup")
            except Exception: pass

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it** (real backend):

```bash
cd /Users/<user>/<workspace>
set -a; . envs/arb-files.env; set +a
ARB_FILES_E2E=1 PYTHONPATH=src .venv/bin/python -m tests.arb_files.e2e_local_mcp
```
Expected: `E2E OK run=<id>` and exit 0. Verify no residue: `agent-files/_e2e/` is empty afterward.

- [ ] **Step 3: Commit**

```bash
git add tests/arb_files/e2e_local_mcp.py
git commit -m "test(arb-files): E2E smoke vs real DO Spaces (env-guarded, run-isolated)"
```

---

## Self-Review (completed against the spec)

- **Tool surface** (`file_list/head/get_url/get_inline/put_inline/put_url/delete`): Tasks 6 (local), 8 (door), 9 (wiring) — covered.
- **Atomic clobber (`If-None-Match`)**: Task 3 (direct) + Task 4 (presigned) — covered.
- **Delete safety (audit + recoverable)**: Task 5 + **R2/R3** (force-overwrite audited; audit-before-delete) — covered.
- **ImageContent return type**: Task 8 (`test_get_inline_image_returns_ImageContent`) — covered.
- **Presigned-PUT headers + provenance**: **R1** (`presign_put` returns `headers` incl. `If-None-Match` + `x-amz-meta-*`) — covered; `restamp_provenance` removed as dead code.
- **`local_path` fail-closed (default `AGENT_WORKDIR`)**: **R5** — covered.
- **Door wiring tested at runtime + fail-soft**: **R7** — covered.
- **E2E exercises both transports + real presigned PUT + full cleanup**: **R8** — covered.
- **Per-token rate limiting**: **R6** — covered.
- **MIME allowlist + inline caps (image vs non-image)**: Task 8 — covered.
- **`file_head` + `file_list` truncation**: Tasks 3/6/8 — covered.
- **Provenance + presigned-PUT re-stamp**: Tasks 3/5 — covered.
- **`local_path` validation**: Task 6 (`paths.py` + tests) — covered.
- **Scope gating (`files.read`/`files.write`) + advertisement**: Tasks 8/9 — covered.
- **Convention-level containment (no dedicated bucket)**: honored — prefix-only, no bucket change.
- **boto3 pinned dep, virtual-host addressing, s3v4**: Task 1 (extra) + Task 3 (client factory) — covered.
- **E2E run-isolated vs real Spaces**: Task 11 — covered.
- **Verify-at-build (ChatGPT, claude.ai image canary, DO versioning)**: out of this build's scope (live-connector + ops verification), noted in spec; the soft-delete path makes delete-recovery work regardless of versioning availability.

**Out of scope (per spec):** macOS shell extension, dedicated-bucket migration, file search/indexing, prod deployment.
