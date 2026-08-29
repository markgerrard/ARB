# Verification inspected the wrong object

A verification step is only as good as the **binding** between what it inspects and what it claims
to inspect. This class covers checks that ran correctly *against the wrong thing* — the check
passes or fails convincingly, but its subject silently isn't the artifact under test. The
conclusions then carry the full confidence of "verified" while being about something else entirely.

## Canonical instances — three in ONE session (2026-08-07), three different bindings broken

| Check | What it actually inspected | Consequence |
|---|---|---|
| grep a deployed dir for new-brand strings after an extract-and-swap deploy | the OLD tree — a mid-`&&` failure (nested tarball layout) meant the swap never happened | a peer was formally accused of shipping a stale artifact; their rebuild was wasted work; retraction required |
| `grep -cE 'subscribed to channel\|presence set to online'` returning 2 read as subscribe+presence | TWO presence lines from adjacent restarts inside the time window; zero subscribes existed | "rollback recovered" declared while all four seats sat deaf for hours |
| regex extraction of workflow `on:` triggers, truncated ~110 chars per block | a prefix of each trigger block | a push-triggered workflow classified "dispatch-only / inert"; disproven later by `gh run list` data |

## Detection moves

1. **Verify the swap before the content.** After any deploy/extract/replace, first prove the
   replacement happened (backup dir exists, inode/mtime changed, entry hash differs) — only then
   judge content. A failed swap makes "inspect the target" equal "inspect the previous version".
2. **Content-check deliveries in a fresh scratch dir**, never in the deployment target. The
   binding "this dir = that tarball" must be constructed by you, not assumed.
3. **Never accept a count over an OR-alternation as evidence.** `grep -c 'A|B'` proves neither
   which alternative matched nor from which run. Print the exact matched lines with timestamps and
   PIDs; the transition you claim must be visible in them.
4. **Truncated extractions are samples, not summaries.** Any regex/`head`-bounded read of config
   used for a completeness claim ("only trigger is X") must be checked against the full object —
   or better, against *measured behavior* (run history, not file reading).
5. Before accusing an external party on verification evidence, **re-derive the chain of custody**
   from their bytes to your observation. If any link is an assumption, test the link first.

## Related

[`prediction-written-as-result.md`](prediction-written-as-result.md) — claims outrunning evidence;
[`mocked-subprocess-shape-never-matched-live`](mocked-subprocess-shape-never-matched-live.md) — the
implementation-side twin;
[`claim-scope-exceeds-evidence-scope`](claim-scope-exceeds-evidence-scope.md) — the generalization
failure these instances feed.
