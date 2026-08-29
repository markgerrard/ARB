"""Consumer-side write path for gate attestations.

F4 (spec art-8742dfc1ca4b8be8 v6 §7.1) lives here rather than in a column constraint because it is a
CROSS-ARTEFACT property: validating a row destined for `attestations` requires reading `artefacts`,
which that table cannot see. That placement is a genuine weakening versus F2's column-level
NOT NULL - the check can be bypassed by anything that INSERTs directly rather than calling this
function. Recorded as a known weakening rather than glossed.

WHAT F4 ACTUALLY ESTABLISHES TODAY (named residual - panel P1-C, run
panel-gate-slice1b-20260726T143935Z-bfbc65). This function enforces:

    "an artefact exists whose `author` string is on the allowlist"

It does NOT enforce "the cited re-run was harness-authored". Two residuals, both open:

RESIDUAL 1 - AUTHORSHIP IS UNBOUND ON ONE INGRESS. `artefacts.author` is writer-supplied on the
DIRECT BUS path, where it travels from the client verbatim:

    writer.py:54-55   `author=intent.get("author", "mcp")` feeds bus.memory_write's author param
    bus.py:62-81      that param is `setdefault`-applied to HINTS ONLY; `artefact` passes through
                      the payload unaltered
    store.py:128      `author=artefact.get("author", "unknown")` - straight from the client dict

Scope, stated precisely: the ordinary MCP tool path DOES bind it - `mcp/tools.py:169` sets
`author = self._author_from_token()` from the access-token client id, so an MCP caller cannot choose
its own author. The direct bus write is the unbound ingress, and it is the one that matters here
because a verifier holding the bus credential can store its own transcript with
`author="harness-runner"` and satisfy F4 - F2's self-report defect moved one pointer deeper, i.e.
exactly what F4 was added to close.

RESIDUAL 2 - RELEVANCE IS NOT ESTABLISHED AT ANY STRENGTH. Neither this function nor the FK ties the
cited artefact to THIS claim. `insert_attestation` resolves the artefact by id+version and checks its
author; it never reads `claim_id`, `claims.probe_artefact_id`/`probe_artefact_version`, or content.
One allowlisted artefact therefore satisfies every attestation ever written, forever. Spec §11 asks
that "the verifier's rerun_artefact reproduces the author package's result"; that is unimplemented
and belongs to the slice owning the harness re-run path.

Spec §7.1 justifies the design with "the store already records artefact authorship
(schema.sql:11-12)": that citation proves the COLUMNS EXIST, not that they are ATTESTED (see
CLAUDE.md, cross-slice claims need citation).

Residual 1 is fail-closed today only incidentally: no production path emits `harness-runner`, so the
shipped allowlist below refuses every real artefact and the allowlist member is a placeholder.

CONTRACT OBLIGATIONS on the slice that owns the write path: (a) bind `artefact["author"]` /
`["source"]` server-side from the authenticated principal on the direct bus path, as `mcp/tools.py`
already does for its own; (b) tie the cited re-run to the claim's probe. Until BOTH land, "F4 closed"
MUST NOT enter the arc trail. What F4 carries today is the author-allowlist check alone; the
EXISTENCE half is carried by the FK on (rerun_artefact_id, rerun_artefact_version), not by this
check, and RELEVANCE is carried by nothing.

Naming note: `HarnessIdentityRefused` keeps its name for API stability (callers and tests import it),
but its message text says "allowlisted author string" rather than "harness-authored", because the
weaker phrase is the true one.
"""

from __future__ import annotations

from arb_memory.store import fetch_artefact

# Artefact authors that count as machinery rather than a participant. Slice 1c widens this
# from configuration; the frozenset is the injection point so tests never mutate a global.
HARNESS_AUTHORS: frozenset[str] = frozenset({"harness-runner"})


class HarnessIdentityRefused(Exception):
    """The cited re-run artefact's `author` is not on the allowlist, or does not resolve.

    Name retained for API stability; the wording below deliberately says "allowlisted author
    string" rather than "harness-authored", because `author` is unbound on the direct bus
    ingress (see module docstring) and the stronger phrase would overclaim.
    """

    def __init__(self, artefact_ref: str, author: str | None):
        self.artefact_ref = artefact_ref
        self.author = author
        if author is None:
            super().__init__(
                f"re-run artefact {artefact_ref} does not resolve; "
                f"cannot check its author against the allowlist, refusing"
            )
        else:
            super().__init__(
                f"re-run artefact {artefact_ref} is authored by {author!r}, "
                f"which is not on the harness-author allowlist; citing your own output is "
                f"self-report"
            )


def insert_attestation(
    conn,
    *,
    claim_id: str,
    verifier_seat: str,
    verifier_family: str,
    family_provenance: str,
    restatement: str,
    mechanism: str,
    falsifier: str,
    falsifier_kind: str,
    rerun_artefact_id: str,
    rerun_artefact_version: int,
    harness_authors: frozenset[str] = HARNESS_AUTHORS,
) -> None:
    """Write an attestation, refusing unless the cited re-run resolves AND its `author` is on
    `harness_authors` (F4). That is weaker than "harness-authored" - see module docstring."""
    ref = f"{rerun_artefact_id} v{rerun_artefact_version}"
    artefact = fetch_artefact(conn, rerun_artefact_id, rerun_artefact_version)
    if artefact is None:
        raise HarnessIdentityRefused(ref, None)
    author = artefact.get("author")
    if author not in harness_authors:
        raise HarnessIdentityRefused(ref, author)

    conn.execute(
        "INSERT INTO attestations (claim_id, verifier_seat, verifier_family, family_provenance, "
        "restatement, mechanism, falsifier, falsifier_kind, rerun_artefact_id, "
        "rerun_artefact_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (claim_id, verifier_seat, verifier_family, family_provenance, restatement, mechanism,
         falsifier, falsifier_kind, rerun_artefact_id, rerun_artefact_version),
    )
