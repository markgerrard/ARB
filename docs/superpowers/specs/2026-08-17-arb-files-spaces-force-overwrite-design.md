# ARB Files Spaces force-overwrite compatibility design

Status: v2 draft after design-v1 panel remediation
Author: `codex-arbmem-prod`
Base: `4ad7c6002bdd001c56872c71c652fbb972a7f1f1`

Design-v1 panel: `panel-arb-files-force-design-20260817T012347Z-048962`
(`needs-changes/P1`, reconcile-gated close emitted with zero gaps)

## Problem

ARB Files cannot replace an existing object on the production DigitalOcean Spaces backend.
Both force-write surfaces read the current ETag and send it as `If-Match`, but they forward the
quoted ETag returned by `HeadObject`. Spaces rejects that `PutObject` request with HTTP 412 even
when the object is unchanged. `FilesStore.put_bytes` then reports the 412 as a concurrent-writer
conflict, although no concurrent writer exists.

This affects:

- `file_put_inline(..., force=true)`, which calls `PutObject` through boto3; and
- `file_put_url(..., force=true)`, which signs the same quoted value into the required
  `If-Match` request header.

## Evidence

The defect was first recorded in ARB Memory artefact `art-2dcbcb72f12721ff` v1. It was then
reproduced from `codex-arbmem-prod` against a uniquely named production object on 2026-08-17:

1. create-only write succeeded with ETag `"6b167cb7746f747a816eb8aa9cdc6046"`;
2. inline force-write returned `stale write; another writer changed the object after read`;
3. presigned force-write carried the exact same quoted ETag and returned HTTP 412; and
4. the live object's ETag, size, timestamp, and uploader metadata remained unchanged.

A narrower boto3 and presigned-wire probe against the same production Spaces bucket then isolated
the compatibility rule and proved that the accepted representation still enforces compare-and-swap.
The added negative controls are stored independently as `art-57d21475e76f959c` v1:

| Operation | `If-Match` form | Result |
|---|---|---|
| `PutObject` | quoted ETag from `HeadObject` | HTTP 412 `PreconditionFailed` |
| `PutObject` | same ETag without surrounding quotes | HTTP 200 |
| `PutObject` | deliberately wrong unquoted ETag | HTTP 412 `PreconditionFailed` |
| presigned `PutObject` | current unquoted ETag | HTTP 200 |
| presigned `PutObject` | stale unquoted ETag | HTTP 412 `PreconditionFailed` |

The successful presigned write was read back and contained the expected replacement bytes. The
negative-control probe used raw `DeleteObject` cleanup and a following `HeadObject` returned 404;
it created no recovery-trash key and emitted no production audit event. The earlier MCP-level probe
was removed through the recoverable delete path and its trash key was recorded.

DigitalOcean documents Spaces as only partially S3 compatible. AWS documents `If-Match` as a
valid conditional write for `PutObject`; therefore the production request is valid S3 behavior
but incompatible with Spaces' `PutObject` ETag parsing.

## Constraints

1. Preserve compare-and-swap semantics. A changed ETag must still fail rather than clobbering an
   unseen concurrent version.
2. Preserve the recovery copy and audit behavior already established for authorised force writes.
3. Fix both inline and presigned force writes.
4. Do not require bucket versioning, a new lock service, or a delete-then-create window.
5. Keep create-only `If-None-Match: *` behavior unchanged.
6. Prove the backend-specific dimension against real Spaces; a fake-only test is insufficient
   because the current fake accepts the invalid production shape.
7. Fail closed if an existing object has a missing, empty, or otherwise unusable conditional ETag;
   never degrade a force overwrite to an unconditional PUT.
8. Leave no live or `.trash/` object residue and emit no event to the production audit sink during
   backend-preserving verification.

## Design

Add one private ETag serializer for `PutObject` conditional writes. Its exact rule is:

1. require a string value;
2. if `len(value) >= 2` and the first and last characters are both `"`, return `value[1:-1]`;
3. otherwise return the value unchanged; and
4. reject an empty result.

This is not `.strip('"')`, and it does not assume a hex-only ETag. Multipart values such as
`"hex-3"` retain their suffix. A weak value such as `W/"value"` is passed through unchanged and
therefore fails closed if Spaces does not accept it.

Use that serialized value in exactly two places:

- `FilesStore.put_bytes`: the boto3 `IfMatch` parameter; and
- `FilesStore.presign_put`: both the boto3 presign parameter and returned `If-Match` header.

Do not change the stored/head ETag, trash-key derivation, or `CopyObject` calls. The serializer is
intentionally scoped to the two `PutObject` conditional-write sites; no current `CopyObject` call
has an ETag precondition, so no other call site needs normalization.

The `file_put_url(force=true)` response schema is unchanged, but its returned `If-Match` header
value deliberately changes from quoted to unquoted. Clients must send the returned headers
verbatim. Rebuilding `If-Match` from the quoted `file_head` ETag is unsupported and will invalidate
the SigV4 request.

When `head()` reports that the prior object exists, both write paths require the serializer to
produce a non-empty conditional value. Missing or empty values raise a backend error before URL
issuance or PUT; truthiness must never decide whether the guard is attached.

No 412 fallback or unconditional retry is added. A fallback is unnecessary once Spaces receives
the accepted representation, and unconditional retry would weaken the race guard. After this fix,
a 412 from the conditional write continues to mean the prior ETag no longer matches.

## Failure behavior

- Existing object, unchanged: normalized `If-Match` succeeds and the replacement becomes live.
- Existing object, changed after `HeadObject`: normalized old ETag fails 412 and the existing
  stale-write error remains correct.
- Existing object with a missing or empty ETag: fail before issuing an unconditional write.
- Missing object with `force=true`: behavior stays unconditional create, matching the current
  contract.
- Existing object with `force=false`: `If-None-Match: *` continues to reject the write.
- Backend unavailable: existing retryable backend error remains unchanged.

## Verification

### Unit and interface tests

- Pin the direct force path to an unquoted `IfMatch` value.
- Pin the presigned force path's signed parameter and returned header to the same unquoted value.
- Keep the stale-ETag test proving a real mismatch still fails.
- Prove a missing or empty prior ETag cannot fall through to an unconditional force PUT or URL.
- Pin the one-pair algorithm for quoted, unquoted, multipart, weak, and degenerate inputs.
- Keep create-only and force-create behavior unchanged.
- Make the S3 fake compare ETags semantically rather than requiring the response-header quote
  representation.

### Backend-preserving test

Add a run-isolated Spaces E2E test that uses the patched `FilesStore` from a disposable path, a
local capturing audit sink, and a UUID-bearing key under `agent-files/_e2e/`. It must:

1. create a unique object;
2. replace it through the patched `FilesStore.put_bytes(force=True)` path and verify the bytes;
3. issue `FilesStore.presign_put(force=True)`, PUT with exactly the returned headers, require 200,
   and verify the second replacement;
4. reuse the now-stale presigned URL and headers and require 412 with bytes unchanged;
5. send a deliberately wrong unquoted conditional through the same serializer/client boundary and
   require 412; and
6. in `finally`, paginate over and raw-delete every key containing the run UUID, including keys
   under `.trash/`, then assert no matching residue remains.

Run the patched E2E from a disposable path inside the production MCP container so credentials stay
inside the container and the running service code is not replaced. Redirect the store audit sink to
the local capture list. The live keys remain under `agent-files/_e2e/`; force-path recovery copies
may appear under `agent-files/.trash/` and are part of the mandatory raw cleanup set.

The E2E is a deployment gate: if either wrong/stale unquoted condition returns 200, abandon this
approach rather than adding a fallback or retry.

### Existing audit semantics

Today the recovery copy and `op=overwrite` audit record are created before the conditional PUT.
Therefore a later 412 can leave a recovery copy and an authorization-shaped audit record even
though the replacement did not land; presigned records already carry `via: presign` to disclose
that distinction. This parser-compatibility fix does not silently redefine those records. The spec
must document that pre-fix overwrite records on this backend are not proof that a write landed and
track audit reconciliation as a separate follow-up rather than expanding this hotfix into an audit
schema migration.

## Rejected alternatives

- **Delete then recreate:** creates an unguarded interval and can delete a concurrent writer's
  version after the initial read.
- **Bucket versioning plus rollback:** safe in principle but versioning is currently disabled and
  enabling it is an infrastructure change unnecessary for this parser mismatch.
- **Distributed lock:** adds a new availability and stale-lock problem while the backend's CAS
  primitive already works with the accepted ETag representation.
- **Retry unconditionally after 412:** can silently clobber a real concurrent update.
- **Normalize every ETag use:** broader than the evidence and unnecessary; only the two
  `PutObject` conditional-write sites currently consume the `HeadObject` ETag.

## Rollback

Revert the serializer use and redeploy the previous image. Create-only writes and reads are
unchanged by either direction. No bucket or database migration is involved. After rollback, force
writes return to the known HTTP-412 failure documented in `art-2dcbcb72f12721ff` v1; operators must
use that incident's recoverable workaround until the fixed deployment is restored.
