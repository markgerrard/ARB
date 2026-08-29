# ARB Files force-overwrite follow-ups

Owner: `arb-files`
Status: deferred after the Spaces force-overwrite hotfix

The hotfix is intentionally narrow. These obligations remain tracked:

1. Schedule the real-Spaces harness as a recurring parser/representation-drift canary.
2. Differentiate weak or otherwise unexpected non-empty ETag backend failures without weakening
   the conditional write.
3. Close the pre-existing missing-object `force=True` create TOCTOU.
4. Evaluate `CopySourceIfMatch` (or an equivalent guard) so the recovery copy and its audit record
   are pinned to the same source version as the later conditional PUT.
5. Reconcile historical overwrite audit events whose authorization-shaped record preceded a failed
   replacement.

Pre-fix `op=overwrite` records on Spaces prove authorization and recovery-copy creation, not that the
replacement landed, because audit emission precedes the conditional PUT. No schema or ordering change
is part of this hotfix.

The exact 82-byte v1 probe recovery object
`agent-files/.trash/2026-08-17/6b167cb7746f747a816eb8aa9cdc6046/artefacts/_probe/arb-files-force-overwrite-1786926758590-b4c65175.txt`
was raw-deleted on 2026-08-17 and a direct `HeadObject` returned 404 afterward. ARB Files rejects the
reserved `.trash` namespace, so a second connector vantage was unavailable without moving production
credentials; no credentials were moved.
