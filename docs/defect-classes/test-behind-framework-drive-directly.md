# Test behind a framework → drive your code directly (the framework face)

When the component under test is invoked **through a framework/SDK that does its own validation**, an
end-to-end test proves almost nothing about your code: the framework rejects the easy/obvious violations
*before* your code runs, so the test goes green even against a **no-op implementation** of the very check it
claims to verify. A face of [`fake-cheaper-than-real`](fake-cheaper-than-real.md): the framework is "cheaper
than reality" by short-circuiting the adversarial path your test needed to travel.

## Detection move

- **Drive your component's methods DIRECTLY** with crafted inputs (unit-test `provider.exchange_*`/`load_*`),
  not only through the framework's HTTP/dispatch path.
- **Seed adversarial rows directly** via the store (a foreign-`resource` token row, a malicious client row)
  so the validation path is actually exercised — minting-then-checking only tests the happy resource.
- **First ask: "what does the framework enforce vs what must I enforce?"** Test only the latter, at *your*
  boundary. **Re-verify the split against the framework SOURCE**, not its docs — versions drift, and the SDK
  routinely does *less* than the design assumes.

## Canonical instance

ARB Memory Phase 3, the OAuth provider behind the `mcp` 1.28 SDK. cold-Opus source-verified that
`handlers/token.py` already rejects wrong `client_id`/`redirect_uri`/PKCE-verifier/expiry **before**
`provider.exchange_authorization_code` is ever called, and `bearer_auth.py` never checks `resource`. So a
battery of "wrong X rejected" tests run end-to-end passed against a provider doing **zero** one-use enforcement
and **zero** audience binding — the exact two things the SDK does NOT do and the provider must. Fix: the
security tests were rewritten to call the provider methods directly and seed a foreign-`resource` token row,
so audience rejection and code one-use are actually pinned.

See the sibling [`fixture-supplies-what-code-lacks`](fixture-supplies-what-code-lacks.md) and the corollary
[`deny-proofs-need-adversarial-verification`](deny-proofs-need-adversarial-verification.md).
