"""Held-out oracle — inputs + sha256(expected), NEVER the expected plaintext.

The probe runs the candidate `wrap` in an unsandboxed subprocess, so it could read
any file on disk. If this file held the expected OUTPUTS, a malicious impl could read
them and return them without implementing anything (a false-PASS — the "held-out"
oracle would not actually be held-out; tri-model delta review, agy P0). So we ship only
the **sha256 of the canonical expected output** per input. The verifier hashes the
impl's output and compares; reading this file (or git history, or any rglob) yields only
digests, from which an output cannot be derived (preimage resistance) — a PASS requires
genuinely computing the correct output. Answer-absent-from-the-reachable-namespace by
construction; no sandbox or file-moving needed.

Inputs are fresh (distinct from both the visible contract cases and the prior held-out
set) so any plaintext expected in git history is for different inputs and useless.

Canonical hash form (MUST match verifier.py): sha256(json.dumps(out, ensure_ascii=False,
separators=(",", ":")).encode()). Regenerate via the reference impl if inputs change.
"""

# (input_args, sha256_hex_of_canonical_expected_output)
CASES = [
    (("one two three four", 7), "563ebd2d643d0ebe068231bd09652722ac2e9bbe9ce8f4c2b0d71b7c7e18c97e"),
    (("alpha    beta", 4), "138bf4722f7ae17122c7282d0eb156499d349940e129bd4cdf27c8ffdcbb3d25"),
    (("ab cd ef", 5), "40283ed8d2f5064ddd4a47d6a396bc4b6fb3331f7a16acca60a44f32b0a62e5c"),
    (("  pad  me  ", 6), "9298f724d85dd7830615db0ab988ee83609f95eca24088ebb6b0ee56408beeda"),
    (("wraparound supercalifragilistic z", 3), "1a85dee5cbda6b896a650154e5bf37d99d914b68402afadb6eb3f26a2aeeaff7"),
]
