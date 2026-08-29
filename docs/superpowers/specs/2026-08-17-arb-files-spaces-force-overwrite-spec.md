# ARB Files Spaces force-overwrite compatibility specification

Status: draft for specification panel
Author: `codex-arbmem-prod`
Base: `4ad7c6002bdd001c56872c71c652fbb972a7f1f1`

Approved design: `art-78b0b746573c987c` v2
Backend evidence: `art-57d21475e76f959c` v1
Design approval: `panel-arb-files-force-design-v2-20260817T014926Z-b78f84`
(`approve/P2`, three independent approvals, reconcile-gated close emitted with zero gaps)

## Objective

Make force overwrites work on the configured DigitalOcean Spaces backend without weakening the
existing compare-and-swap guard. Both `file_put_inline(..., force=true)` and
`file_put_url(..., force=true)` must send the unquoted strong ETag representation that Spaces
accepts for `PutObject If-Match`; stale or wrong values must continue to return HTTP 412.

## Production behavior

### Conditional ETag serializer

Add a private module-level helper in `src/arb_files/store.py` with this contract:

```python
def _put_if_match_etag(value: object) -> str:
    """Return the ETag representation accepted by Spaces PutObject or raise."""
```

It must:

1. reject non-strings;
2. remove exactly one surrounding double-quote pair when `len(value) >= 2` and both the first and
   last characters are `"`;
3. otherwise leave the string unchanged;
4. reject an empty result; and
5. never use `.strip('"')`, assume a hex-only alphabet, or alter the stored/head ETag.

Expected examples:

| Input | Result |
|---|---|
| `"abc"` | `abc` |
| `"abc-3"` | `abc-3` |
| `abc` | `abc` |
| `W/"abc"` | `W/"abc"` (attached as a condition; no unconditional fallback) |
| `""` | error |
| empty string | error |
| `None` | error |

The error presented by `FilesStore` is retryable backend failure text, not a concurrency success or
an unconditional write. Weak/unexpected non-empty values remain conditional and therefore fail
closed on the target backend; this hotfix does not introduce a hex-only or S3-provider-specific
validator.

### Direct force write

For `FilesStore.put_bytes(force=True)`:

- keep `head`, recovery copy, and current audit ordering unchanged;
- when the prior object exists, call `_put_if_match_etag(prior.get("etag"))` and always set
  `kwargs["IfMatch"]` to its result;
- never use truthiness to decide whether the guard is present;
- if serialization fails, do not call `put_object`;
- preserve missing-object `force=True` as the existing unconditional-create contract;
- preserve 412 mapping to the existing retryable stale-write error; and
- preserve `IfNoneMatch="*"` for non-force writes.

### Presigned force write

For `FilesStore.presign_put(force=True)`:

- keep `head`, recovery copy, audit event, and `via: "presign"` behavior unchanged;
- when the prior object exists, serialize its ETag once;
- use the identical result for `Params["IfMatch"]` and returned `headers["If-Match"]`;
- if serialization fails, do not call `generate_presigned_url`;
- preserve missing-object `force=True` as the existing unconditional-create contract; and
- preserve all non-force and metadata headers.

The response dictionary schema is unchanged, but the existing-object force response's `If-Match`
header value deliberately changes from quoted to unquoted. Clients must send every returned header
verbatim. Rebuilding the header from `file_head` is unsupported and can invalidate SigV4.

## Unit and interface verification

Update or add tests that prove:

1. the helper implements every table row above and removes only one pair;
2. direct existing-object force sends `IfMatch="deadbeef"`, never the quoted form;
3. presigned existing-object force signs and returns the same `deadbeef` value;
4. missing and quoted-empty ETags raise before `put_object` or URL generation;
5. a weak/non-empty value is still attached as `IfMatch`, never treated as absence;
6. a simulated concurrent change still raises the stale-write error;
7. force-create on a missing object preserves its existing behavior;
8. non-force create retains `IfNoneMatch="*"`; and
9. the fake compares conditional ETags semantically: one surrounding response-header quote pair is
   not part of the opaque comparison value.

Do not weaken the fake so far that the call-shape assertions disappear: tests must separately pin
the exact unquoted wire value.

## Real-Spaces verification and reproducible transcript

Extend `tests/arb_files/e2e_local_mcp.py` (or add one adjacent env-gated module invoked by it) so the
checked-in harness performs this ordered run when `ARB_FILES_E2E=1`:

1. create a UUID-bearing key under `agent-files/_e2e/` through the patched store;
2. record the quoted `HeadObject` ETag as `etag_v1`;
3. replace through `FilesStore.put_bytes(force=True)`, require success, verify bytes, and record
   `etag_v2`;
4. call `FilesStore.presign_put(force=True)`, PUT with exactly its returned headers before the URL's
   TTL, require HTTP 200, verify bytes, and record `etag_v3`;
5. replay the same now-stale presigned URL and headers before TTL expiry, require HTTP 412, and
   verify bytes remain unchanged;
6. construct `wrong_etag` by changing one character of `etag_v3` while preserving its strong opaque
   shape, pass it through `_put_if_match_etag`, send a raw conditional `PutObject`, require HTTP
   412, and verify bytes remain unchanged; and
7. print a JSON transcript containing the ordered cell names, literal ETag values, HTTP statuses,
   available backend request IDs, final-byte digest, and cleanup result. Do not print credentials or
   presigned URLs.

Use `FilesStore(settings, audit_sink=events.append)` so no production audit event is emitted. In
`finally`, paginate over the configured bucket prefix and raw-delete every key containing the run
UUID, including recovery copies below `.trash/`. Then perform a separately paginated assertion that
no UUID-bearing key remains. Cleanup must call the raw client's `delete_object`, not
`FilesStore.delete`, so it cannot create more trash.

The run is a deployment gate. Any negative conditional returning 200, any expected success not
returning 2xx, changed bytes after a 412, non-empty production audit sink, or cleanup residue blocks
deployment. The same harness is the reusable parser-drift canary; scheduling it periodically is a
tracked follow-up and not part of this hotfix's runtime service.

Run the patched harness from a disposable copy inside `deploy-mcp-1` so the production credentials
remain inside the container and the serving `/app` code is not replaced. Delete the disposable copy
afterward.

## Documentation and evidence reconciliation

Update `deploy/buzz/README.md` so it no longer claims that Spaces lacks CAS rather than having an
ETag-format incompatibility. The Buzz conclusion remains operationally unchanged: its A3 probe sends
the quoted form and therefore Spaces still fails that application gate. State explicitly that:

- ARB Files proved sequential direct and presigned CAS with unquoted values;
- Buzz's 32-writer gate has not been rerun with an unquoted representation; and
- this change does not authorize moving Buzz's git object store to Spaces.

The checked-in E2E and its JSON output provide the reproducible transcript missing from
`art-57d21475e76f959c` v1. The exact 82-byte v1 probe recovery object identified by the design panel
was raw-deleted on 2026-08-17 and confirmed absent by `HeadObject` 404.

Document that pre-fix `op=overwrite` records on this backend are authorization-shaped records, not
proof that the replacement landed, because audit emission precedes the conditional PUT. Do not
change the audit schema or event ordering in this hotfix.

## Load-bearing gate metadata

Classify `src/arb_files/**` in `skills/bridge-protocol/gate/layer_registry.json` as a storage adapter
requiring `interface` and `backend` dimensions. Add `src/arb_files/store.py` to
`load_bearing_components.json` with:

- interface evidence: the focused unit/interface tests exercising both direct and presigned paths;
- backend evidence: the real-Spaces env-gated E2E described above; and
- fake allowance limited to pure serializer and request-shape checks that do not certify Spaces
  behavior.

These registry/manifest files are mutable gate data, not part of the certified trust-root object;
no trust-root rotation is required.

## Explicit non-goals and tracked follow-ups

This hotfix does not:

- change the existing `force=True` missing-object unconditional-create contract; its create-race is
  a pre-existing TOCTOU to track separately;
- add `CopySourceIfMatch` to the recovery copy; the pre-existing recovery-copy/audit mismatch remains
  a fail-closed follow-up;
- reconcile historic audit events or migrate the audit schema;
- enable bucket versioning, add a distributed lock, delete before create, or retry unconditionally
  after 412; or
- change `_trash_key`'s established path derivation.

## Acceptance criteria

The implementation is acceptable only when:

- focused unit/interface tests pass through `scripts/tree-provenance-run`;
- the checked-in real-Spaces harness passes through `scripts/tree-provenance-run` from the disposable
  production-container copy and emits the complete transcript;
- a post-run raw scan confirms zero UUID-bearing live or trash objects;
- `deploy/buzz/README.md` carries the reconciled, scoped statement;
- gate metadata classifies the changed production file and names dimension-preserving tests;
- the full relevant test suite and `git diff --check` pass; and
- the later implementation tri-review has no open P0/P1.

## Rollback

Revert the code and documentation changes and redeploy the previous image. There is no data or schema
migration. Reads and create-only writes remain unchanged. Force overwrites then return to the known
HTTP-412 failure in `art-2dcbcb72f12721ff` v1; use that incident's recoverable workaround until the
fixed deployment is restored.
