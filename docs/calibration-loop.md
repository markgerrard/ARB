# Decision-quality calibration loop

The review machinery decides each case safely but has no mechanism for getting
*better at deciding* over time — it logs verdicts but never checks whether the
`consensus-accept` calls held up, or whether a seat's flags have a good hit rate.
This loop closes that. `scripts/calib` (→ `agent_redis_bridge.calibration`).

## The non-negotiable invariant: report-only

The loop **only reports**. It surfaces per-reviewer / per-decision-type stats and
flags anomalies **as questions** for a human to investigate. It **never** auto-tunes
reviewer weights or gates.

Why this line is absolute: auto-down-weighting a "noisy" seat silently kills the
diversity that catches correlated blind spots. A seat with a low confirmation rate
might be the *only* one that ever looks at security — and a low rate might mean
security findings are hard to confirm, or that confirmation lags, not that the seat
is bad. The report names the anomaly; a human investigates the mechanism. That is
the disagreement rule applied to the loop's own output: a surprising stat is a
STOP-and-investigate, not a tune.

## Ground truth is manual (and that's the point)

"Held / regressed / confirmed / false_positive" is a claim about the world. Only a
human observation backed by an **evidence artifact** establishes it — `annotate`
requires `--evidence`. No commit pattern is treated as fact.

A git-hint layer that *suggests* outcomes ("this finding's file was reverted 3
commits later — confirm regressed?") for a human to adjudicate is a legitimate
**v1.5** — but it is **not built**, and it must earn its place: ship the manual
layer, run it, and only add hints once you can point at annotations you'd have
missed without them. Auto-*inferred* outcomes-as-fact are the prohibited
observation→inference→fact workflow one level down; they'd build a confident-but-
wrong signal about reviewer trustworthiness, which is exactly what evidence-first
refuses.

## No prose backfill

Records are captured at **close-time** (`record`), when verdicts/taxonomy/diff/
finding-ids are live — never parsed out of triage prose (that is summary→fact
inference, and a parsed record is a weaker epistemic class indistinguishable in the
log from a clean one). An empty log that fills with trustworthy close-time records
beats a populated one you can't trust; `report` says "Insufficient data" honestly
until reviews close. If you want history, hand-record it via
`annotate … --source backfill:<doc>` at the same evidentiary standard (cited
artifact), labelled as the weaker retrospective class — never parsed.

## Assessment-age is first-class

A "held" is only held *as of when it was looked at*; a call that regresses six
months later was logged "held" for six months. Every outcome carries `assessed_at`.
The report flags "held" marks older than `--stale-days` (default 90) for
re-confirmation, so the held-rate measures "correct", not "not caught yet".

## Usage

```sh
# at review close — capture the decision (run this when you close a triple review)
calib record --scope phase3-i5b --decision consensus-accept \
  --reviewer codex=approve --reviewer agy=needs-changes --reviewer coldopus=approve \
  --finding f1:agy:"divide-by-zero guard" --diff-range 1c043b3..60e9f1b
# -> prints the decision id

# later, when an outcome is known (manual, evidence-backed)
calib annotate --decision <id> --finding f1 --status false_positive \
  --evidence "agy's guard: legacy doesn't guard either; declined as faithful — see triage doc"
calib annotate --decision <id> --status held \
  --evidence "no regression in circulation worker across 3 prod runs, re-checked 2026-08"

calib report            # the calibration report (report-only)
calib report --strict   # exit 1 if any anomaly flagged (periodic CI nag)
calib list              # dump the raw append-only log
```

Decision taxonomy (`--decision`) and statuses mirror `quorum-decision-taxonomy.md`:
finding outcomes are `confirmed` | `false_positive`; decision outcomes are
`held` | `regressed`.

## What it deliberately does NOT do (yet)

- No git-derived outcome inference (v1.5, must earn its place).
- No automatic backfill from triage docs.
- No weight/gate adjustment — ever. That line does not move.
