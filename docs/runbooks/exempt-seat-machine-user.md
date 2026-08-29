# Exempt-seat machine user — one-time owner setup

**Audience:** human owner (Mark) and the `example-org` organization owner/team that
provisions GitHub access. **Not** an implementation or automation script.

**Stage:** Slice 1d-iii. This runbook describes owner actions only. Repository code and
`scripts/seat-preflight` **consume** the identity after it exists; they never create the
account, add org members, grant repository access, or move repositories.

**§9.3 residual (MUST state):** a same-UID process on this Mac can still push with the
operator's ambient credentials (SSH agent, `~/.ssh` default keys, credential helpers)
regardless of the exempt worktree's configured remote and `core.sshCommand`. 1d-iii
constrains the **configured** remote for ordinary `git push` / `git push origin` from that
worktree under the machine-user key. It does **not** provide OS-level isolation. Stronger
isolation needs a separate OS account or container with no writable Git credential.

---

## Why this shape (rejected alternatives)

| Alternative | Why it fails the multi-repository contract |
|---|---|
| **Per-repository deploy keys** | GitHub deploy keys are single-repository credentials. The fleet works on many targets; one deploy key per repo is the wrong unit. |
| **Fine-grained PAT** | A fine-grained PAT is limited to resources owned by **one** selected user or organization. It cannot span `example-org` org repos and personal-account repos. It would also replace the required SSH `core.sshCommand` mechanism with HTTPS. |
| **Write-capable personal collaborator** | GitHub private personal repositories offer collaborators **read/write**, not Read. Adding `arb-exempt-bot` as a personal collaborator and calling it “Read” is forbidden. Move the repo into an organization first, or mark it ineligible. |

GitHub's documented multi-repository automation shape is a **machine user** plus **one SSH
identity**. That is what this runbook provisions.

---

## One-time owner setup (do not automate from this repository)

### 1. Create the machine-user account

Manually create the single GitHub account **`arb-exempt-bot`**. Never automate account
creation from this repository or from implementation code.

### 2. SSH identity

```bash
# Once, as the owner — private key never committed
ssh-keygen -t ed25519 -f ~/.ssh/arb-exempt-bot -C arb-exempt-bot
chmod 700 ~/.ssh
chmod 600 ~/.ssh/arb-exempt-bot
chmod 644 ~/.ssh/arb-exempt-bot.pub
```

- Add the **public** key to the `arb-exempt-bot` GitHub account (Settings → SSH keys).
- Pin GitHub in `~/.ssh/known_hosts` (or a dedicated known_hosts file referenced by the
  SSH command).
- Record the fingerprint:

```bash
ssh-keygen -lf ~/.ssh/arb-exempt-bot.pub -E sha256
# Expected (this fleet, provisioned 2026-07-27):
# 256 SHA256:<fingerprint> arb-exempt-bot (ED25519)
```

- Supervisor env for every exempt seat (process/secret environment, not app-repo `.env`
  that workers inherit as ambient authority for remotes):

```bash
export BRIDGE_EXEMPT_GIT_SSH_COMMAND='ssh -i ~/.ssh/arb-exempt-bot -o IdentitiesOnly=yes'
export BRIDGE_EXEMPT_GIT_KEY_FINGERPRINT='SHA256:<fingerprint>'
export BRIDGE_EXEMPT_PROVISIONING_LEDGER='/path/to/exempt-provisioning-ledger.json'
export BRIDGE_WORKTREE_LANE=exempt
```

`IdentitiesOnly=yes` is mandatory so the operator's SSH agent cannot answer for the remote.

### 3. Organization membership and Read team

1. Add `arb-exempt-bot` as a **paid** `example-org` organization member.
2. Create or use the team **`arb-exempt-readonly`**.
3. Grant that team **exactly Read** on every intended org target.
4. Verify organization base permission and any direct/other-team grants do **not** raise
   effective access above Read.
5. Record the paid organization-seat cost and owner in the ledger (below).

### 4. Non-org targets

- Grant only an actual **Read** role.
- For a private personal-account repository (example: former `<personal-owner>/AgentRedisBridge`):
  GitHub does not offer a read-only collaborator role. **Move it into the organization**
  before provisioning, or mark it ineligible for the exempt lane.
- **Never** add a write-capable collaborator as a substitute for Read.

**Canonical location (2026-07-27):** AgentRedisBridge lives at
**`example-org/AgentRedisBridge`**. Older checkouts may still name
`<personal-owner>/AgentRedisBridge` as `origin`; GitHub redirects. The ledger records the
canonical org location and lists the old path as an alias. Resolver evidence is always
keyed to the checkout's resolved URL; preflight accepts the alias only when the ledger
says so.

### 5. Provisioning ledger

Maintain a machine-readable ledger (JSON) at the path given by
`BRIDGE_EXEMPT_PROVISIONING_LEDGER`. Required fields:

| Field | Meaning |
|---|---|
| `machine_user` | `arb-exempt-bot` |
| `fingerprint` | `SHA256:…` of the one key |
| `key_label` | GitHub SSH key label |
| `targets[].repo` | Canonical `owner/repo` |
| `targets[].aliases` | Optional redirect/old paths |
| `targets[].owner_kind` | `organization` / `public-personal` / … |
| `targets[].grant_path` | Exact grant path/team (e.g. `example-org/arb-exempt-readonly Read`) |
| seat ids/hosts, creation date, review/expiry date, rotation owner | Operational metadata |

Example skeleton (fill operational fields; never put private key material here):

```json
{
  "machine_user": "arb-exempt-bot",
  "fingerprint": "SHA256:<fingerprint>",
  "key_label": "arb-exempt-bot",
  "paid_seat_owner": "Mark / example-org org owner",
  "rotation_owner": "Mark",
  "created": "2026-07-27",
  "review_by": "2027-01-27",
  "targets": [
    {
      "repo": "example-org/AgentRedisBridge",
      "aliases": ["<personal-owner>/AgentRedisBridge"],
      "owner_kind": "organization",
      "grant_path": "example-org/arb-exempt-readonly Read",
      "seats": [],
      "hosts": []
    },
    {
      "repo": "example-org/project-b",
      "owner_kind": "organization",
      "grant_path": "example-org/arb-exempt-readonly Read",
      "seats": [],
      "hosts": []
    },
    {
      "repo": "example-org/project-c",
      "owner_kind": "organization",
      "grant_path": "example-org/arb-exempt-readonly Read",
      "seats": [],
      "hosts": []
    }
  ]
}
```

### 6. Live proof per ledger target (owner)

For **each** ledger target, using only the machine-user key:

```bash
export GIT_SSH_COMMAND='ssh -i ~/.ssh/arb-exempt-bot -o IdentitiesOnly=yes'

# Identity
ssh -T git@github.com -i ~/.ssh/arb-exempt-bot -o IdentitiesOnly=yes
# expect: Hi arb-exempt-bot!

# Read-positive (exit 0)
git ls-remote git@github.com:<owner>/<repo>.git HEAD

# Push-permission-denied dry-run (NEVER a real push)
# From a throwaway clone or any checkout of that remote:
git push --dry-run origin HEAD:refs/heads/arb-exempt-deny-proof-<nonce>
```

**Accepted push-denial signature** (normalized complete lines + exit 128), captured
2026-07-27 from `arb-exempt-bot` against the provisioned Read role:

```text
ERROR: Write access to repository not granted.
fatal: Could not read from remote repository.
exit: 128
```

Record the normalized fetch-success and push-permission-denial evidence **keyed to the
resolved target URL**. Evidence for repository A never proves repository B.

**Unprovisioned / no-Read target:** `ls-remote` must fail; registration and arm
hard-refuse with `exempt-remote-read-unavailable` (or a more specific catalog class).
**Never** fall back to operator credentials, SSH agent, credential helper, alternate URL,
PAT, or a per-repository deploy key.

**Writable control (isolated, disposable):** prove that a dry-run exit 0 produces the loud
`exempt-push-credential-writable` blocker. **Never** grant write to the machine-user key
for that control; revoke/remove the disposable control afterward.

### 7. Rotation, revocation, emergency removal

- **Key rotation:** generate a new key, add pubkey to the account, update fingerprint in
  ledger + `BRIDGE_EXEMPT_GIT_KEY_FINGERPRINT`, re-prove every target, then remove the old
  pubkey from GitHub and delete the old private key.
- **Per-target removal:** drop team Read (or org membership grant) for that repo; remove
  the ledger entry; re-run preflight — must fail closed for seats whose checkout resolves
  to that target.
- **Emergency:** remove the GitHub SSH key and/or remove `arb-exempt-bot` from
  `arb-exempt-readonly` / the organization. All exempt seats fail preflight/arm until
  restored.

---

## Checkout origin must be literal SSH form

The exempt lane admits only the **canonical** SSH-form `origin` written in the
checkout config: **exactly** `git@github.com:owner/repo.git` (`.git` suffix
required; no `ssh://` spelling). HTTPS-written origins and non-canonical SSH
spellings (`git@github.com:owner/repo` without `.git`, or
`ssh://git@github.com/owner/repo[.git]`) are refused at preflight **and** arm
with catalog code `exempt-origin-not-ssh` and a one-line conversion. This is the
same predicate at both doors — a personal global `~/.gitconfig`
`url.git@github.com:.insteadOf https://github.com/` that makes interactive
`git` transport over SSH does not count.

**Why the lane ignores operator-scoped `insteadOf` rewrites:** a personal config
rule must not decide where a security proof is sent. The rem2 hermetic probe env
correctly stopped honouring that rewrite while probing — which first exposed
genuine HTTPS remotes to the lane. Identity-collapse (treating HTTPS + SSH as one
target) then tried to accommodate them and opened multi-round holes (same-repo
HTTPS `pushurl` alongside worktree SSH, read leg dialing HTTPS public fetch).
**The remote must say what it does.** Convert once per checkout:

```bash
git remote set-url origin git@github.com:<owner>/<repo>.git
```

**Plain `git remote get-url` can lie under an `insteadOf` rewrite.** Global or
system `url.*.insteadOf` rewrites change what `get-url` prints without changing
the config-file bytes. Confirm the SSH requirement with a hermetic form, or by reading
the config file directly.

Neutralizing global/system config is **not sufficient on its own**: a command-scope
`GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` triple still applies and
still rewrites what `get-url` prints. Clear those too — verified: with such a triple
active, the global/system-only form printed a rewritten host, and clearing the triple
printed the true one.

```bash
# Safe — clears command-scope config injection AND global/system config
env -u GIT_CONFIG_COUNT -u GIT_CONFIG_PARAMETERS \
    $(env | sed -n 's/^\(GIT_CONFIG_KEY_[0-9]*\)=.*/-u \1/p') \
    $(env | sed -n 's/^\(GIT_CONFIG_VALUE_[0-9]*\)=.*/-u \1/p') \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
  git remote get-url --all origin
```

Or read the written bytes. `.git` is a **file**, not a directory, inside a linked
worktree, so a plain `.git/config` grep fails there — resolve the common dir first:

```bash
# Works in a normal checkout AND a linked worktree
grep -A2 '\[remote "origin"\]' "$(git rev-parse --git-common-dir)/config"

# Worktree-scoped overrides, when extensions.worktreeConfig is on:
cat "$(git rev-parse --git-dir)/config.worktree" 2>/dev/null
```

**Trust boundary:** the lane hermetic env neutralizes **global/system**
`insteadOf` only. **Repo-local** rewrites in the checkout's own `.git/config`
remain visible to name-based `get-url` and sit inside the checkout trust
boundary — convert the remote URL itself rather than relying on a local rewrite.

After conversion, re-run preflight. Do not rely on global `insteadOf` to make an
HTTPS-written origin "look like" SSH to the lane.

### Restricted networks (SSH over port 443), Enterprise hosts, SSH certificates

The admitted form is `git@github.com:owner/repo.git` and nothing else. Rewriting the
**remote URL** to a port-443 or alternate-host spelling is refused **by design**.
Verified behaviour (2026-07-28, against `resolve_target_remote`): both
`ssh://git@ssh.github.com:443/owner/repo.git` and `git@ssh.github.com:owner/repo.git`
fail with **`exempt-origin-invalid`** — detail `unsupported origin form: …`, and **no
conversion line**, because the lane does not parse a non-`github.com` host at all. Only a
`github.com` host that is merely spelled non-canonically (e.g. a missing `.git`, or
`ssh://git@github.com/owner/repo`) gets `exempt-origin-not-ssh` with the exact
`git remote set-url` line to run.

**Remedy for a port-22-blocked network: leave the remote canonical and put the transport
detail in ssh config.** The lane's SSH command may not pass `-F`
(`validate_ssh_command` rejects it, `exempt_git.py:646-649`), so ssh reads the operator's
normal config files and a host alias applies to the lane's probes as well as to ordinary
`git`:

```sshconfig
# ~/.ssh/config — transport detail only; the remote URL stays canonical
Host github.com
  HostName ssh.github.com
  Port 443
  User git
```

**SSH certificate authorities:** a CA-signed key changes how ssh authenticates, not the
URL form. Keep the canonical remote; the certificate/key configuration lives on the ssh
side. (Not exercised on this fleet — verify against your own setup before relying on it.)

**GitHub Enterprise Server targets are out of contract.** The canonical form names
`github.com`, so a GHES repository cannot satisfy admission: verified 2026-07-28,
`git@ghe.example.com:owner/repo.git` fails `exempt-origin-invalid` with detail
`unsupported origin form: …` and **no conversion line** — the refusal offers no advice to
follow, correctly, because none would work. Mark GHES repositories ineligible for the
exempt lane; supporting them needs a lane change, not an operator conversion.

## Preflight contract (what code checks)

When `BRIDGE_WORKTREE_LANE=exempt`, `scripts/seat-preflight`:

1. Derives the target repository from the seat's **actual checkout** `origin` (never from
   `BRIDGE_EXEMPT_GIT_REMOTE_URL`). Resolution is read-only on `AGENT_WORKDIR` and
   requires a **literal SSH-form** origin (see above); HTTPS is
   `exempt-origin-not-ssh`.
2. Checks local key file existence, **modes (parent dir ≤ 0700, key 0600 — no
   group/other bits)**, fingerprint match to the recorded `arb-exempt-bot`
   fingerprint, and GitHub SSH identity `Hi arb-exempt-bot!`.
3. Requires `BRIDGE_EXEMPT_PROVISIONING_LEDGER` and a matching ledger entry
   (canonical or alias). Missing ledger is a hard refusal at preflight **and** arm
   (same catalog code); there is no manufactured default ledger.
4. Runs the same target-specific read-positive / push-classifier proof as arm, but
   **only against a throwaway local clone** of `AGENT_WORKDIR` that is deleted
   afterward. Preflight does **not** write `extensions.worktreeConfig`, origin, or
   `core.sshCommand` into the seat's live checkout. Proof legs dial the verified
   target SSH URL (not an ambiguous remote name).
5. Fail-closed on missing ledger, missing Read, unreadable target, non-SSH origin,
   multi-URL origin/pushurl accumulation, or writable dry-run.
6. Never falls back to operator credentials. Every git/ssh probe (including
   preflight resolve/clone/`set-url`) runs with a scrubbed env that pins
   `GIT_SSH_COMMAND` to the validated machine-user command. The scrub set is the
   runtime union of `git rev-parse --local-env-vars` (git's authoritative
   repo-scoping list: `GIT_DIR`, `GIT_COMMON_DIR`, `GIT_CONFIG`, `GIT_WORK_TREE`,
   `GIT_INDEX_FILE`, and the rest git declares) and explicit non-repo controls
   (`GIT_SSH*`, `GIT_ASKPASS`, `SSH_ASKPASS`, `GIT_PROXY_COMMAND`, `GIT_EXEC_PATH`,
   `GIT_CONFIG_GLOBAL`/`SYSTEM`/`NOSYSTEM`/`COUNT`/`PARAMETERS`, plus
   `GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*` by prefix). File-based config is
   neutralized (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`) so ambient
   `insteadOf` rewrites cannot retarget resolution or probes. If
   `--local-env-vars` fails or is empty, probes **refuse** (no short static fallback).

**`AGENT_WORKDIR` end-state after preflight:** unchanged. Origin URL, push URL,
`core.sshCommand`, and `extensions.worktreeConfig` on the live seat checkout are
not modified by the check. Arm is the path that configures a **fresh disposable
worktree** created for that arm.

Gated seats do not require exempt Git variables.

Arm re-resolves and re-proves on the **newly created worktree** so a prior target's
evidence cannot carry over. Arm also requires the provisioning ledger — the same
SSH-only admission and proof contract as preflight.

---

## Private material

Never commit private keys, PATs, or SSH private material. Public fingerprint and ledger
metadata are fine to store in operator-controlled paths outside the worker-writable tree
when possible.
