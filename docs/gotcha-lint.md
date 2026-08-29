# gotcha-lint — graduating recurring gotchas to a mechanical check

Pattern D (`orchestrator-patterns.md`) says: after a class of bug recurs (~3×),
capture it and brief reviewers on it. That is **discipline** — it relies on the
orchestrator remembering to brief and the reviewer remembering to look. The
external review of this system flagged the missing half: *graduation* — promoting
a thrice-recurring gotcha from "remembered" to an automated check. `gotcha-lint`
is that half.

## The two stages of a gotcha

| Stage | `enforce` | What gotcha-lint does | Where it's surfaced |
|---|---|---|---|
| **briefing** | `false` | warns (exit 0) | injected into review briefs via `review-brief --gotchas` |
| **graduated** | `true` | **fails (exit 1)** on match | CI / pre-review gate |

A gotcha **graduates** when it (a) has recurred `>= 3` times (`GRADUATE_AT`) AND
(b) is expressible as a regex with acceptable false-positive rate. Context-
dependent gotchas that can't be cleanly grepped (e.g. "token leak in error
messages") stay **briefing-only** forever — they're surfaced into briefs, not
enforced. `gotcha-lint --check-graduation` fails if a briefing gotcha has hit the
threshold but hasn't been promoted, so the graduation itself doesn't rely on
memory.

## Registry

A JSON file, default `.gotchas.json` at the repo root (per project — the bug
classes are domain-specific). Schema per entry:

```json
{
  "id": "pest-tothrow-throwable",
  "description": "why it bites",
  "hint": "how to fix it",
  "pattern": "toThrow\\(\\s*\\\\?Throwable",   // Python regex, matched per line
  "include": ["*.php"],                          // fnmatch globs (default: all files)
  "exclude": ["*vendor/*"],                      // see the glob caveat below
  "occurrences": 3,                              // times it has bitten (drives graduation)
  "enforce": true                                // graduated -> fail on match
}
```

See `docs/examples/gotchas.example.json`.

**fnmatch glob caveat:** `include`/`exclude` use `fnmatch`, where `*` spans `/`.
So `*.php` matches a `.php` file at any depth, and to exclude a vendor dir use
`*vendor/*` — **not** `*/vendor/*`, which requires a character before the slash
and so misses a root-level `vendor/…`.

## Usage

```sh
gotcha-lint                      # scan all tracked files against .gotchas.json
gotcha-lint --diff main..HEAD    # scan ONLY the added lines of a diff (CI default)
gotcha-lint --list               # show the registry (briefing + graduated)
gotcha-lint --check-graduation   # fail if a briefing gotcha is due for promotion
gotcha-lint --registry path/to/.gotchas.json --root /path/to/repo
```

`--diff RANGE` is the CI mode: it matches only added lines, so it catches *new*
occurrences without flagging pre-existing debt.

## Wiring into a project

1. Copy `docs/examples/gotchas.example.json` to the project repo as `.gotchas.json`
   and edit. Start every gotcha at `enforce:false`.
2. CI step (fails the build on a graduated match in the PR diff):
   ```sh
   gotcha-lint --diff "origin/${BASE_BRANCH}...HEAD"
   ```
   plus a periodic `gotcha-lint --check-graduation` to nag overdue promotions.
3. Reviews: `review-brief --gotchas .gotchas.json …` appends the briefing-stage
   gotchas to the brief so reviewers are always briefed — the discipline half of
   Pattern D, made structural too.

## Why both halves

The linter handles the greppable, graduated gotchas by construction. The brief
injection handles the rest (and the not-yet-graduated ones). Together they remove
the "the orchestrator will remember" dependency that Pattern D otherwise carries.
