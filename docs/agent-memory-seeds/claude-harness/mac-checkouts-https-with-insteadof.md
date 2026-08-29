---
name: mac-checkouts-https-with-insteadof
description: "Mark's Macs write HTTPS origins but transport over SSH via a global insteadOf rewrite; servers use SSH outright — any hermetic-git code must assume config text lies"
metadata: 
  node_type: memory
  type: project
  originSessionId: 5bb925d0-80ee-483f-96bb-e04e8c412b41
  modified: 2026-07-28T01:28:14.626Z
---

Established 2026-07-28 (Mark, confirming a live finding). On Mark's **Macs**, checkouts carry
HTTPS in `.git/config` (`https://github.com/markgerrard/ARB.git`) while
`~/.gitconfig` holds `url.git@github.com:.insteadOf https://github.com/` (+ the `git://` form),
so git actually transports over **SSH**. Evidence: a push whose config said `https://` reported
`To github.com:markgerrard/ARB.git` — the colon (SSH) form. On the **servers** the
config says SSH outright, so the rewrite is inert there. Macs are the ONLY environment where the
written remote and git's real behaviour differ.

**Why this bites:** any code that sanitizes git's environment for security (pinning
`GIT_CONFIG_GLOBAL=/dev/null` to stop operator config steering a probe) simultaneously disables
the rewrite — so an HTTPS-written remote is suddenly taken literally and dials HTTPS for real.
This produced three P1s in ARB Slice 1d-iii r3: reads over HTTPS proving only public
readability, and cross-scope pushurl accumulation defeating push-denial.

**How to apply:** when writing hermetic-git code, never infer transport from the configured URL
text on a Mac, and never let operator-scoped config decide a security outcome. Owner's standing
decision for the ARB exempt lane (2026-07-28): **require a literal SSH-form origin and refuse
otherwise**; conversion is `git remote set-url origin git@github.com:owner/repo.git`, which
changes nothing behaviourally on the Macs (insteadOf already sent them over SSH) — it just makes
the config state the truth. `/Volumes/<workspace>/repos/ARB` and `/Users/<user>/<workspace>` are both
HTTPS-written today and need that conversion before they can host exempt work.

Related: [[slice1d-iii-multi-repo-decision]].
