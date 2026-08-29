# Model-version-aware seat calibration (live-loop extension) — design+spec (v2 — panel round 1 absorbed)

**Origin:** /learn `learn-panel-judge-calibration-measure-seat-bia-606a6002`, eval →
needs-mark → Mark resolved APPROVE AS REDIRECTED: extend the EXISTING calibration loop
(`scripts/calib` → `src/agent_redis_bridge/calibration.py`, doctrine in
`docs/calibration-loop.md`), lightweight and model-versioned. Explicitly NOT a periodic
replay corpus (stale-on-arrival under fleet model drift — the seats' catch) and NOT a new
subsystem. Every existing invariant holds unchanged: **report-only** (no auto-tuning of
weights or gates, ever), manual evidence-backed ground truth, close-time records, no prose
backfill.

## The gap this closes

Calibration records identify reviewers as seat ids, but a seat's MODEL changes under it
(codex GPT-5.5→5.6, agy engine updates, GLM versions, asdk model swaps). Stats pooled
across model versions blur exactly the drift the loop exists to surface: "agy's
confirmation rate dropped" is uninterpretable without knowing whether agy's model changed
mid-window. Observed same-class fact: pi-family seats systematically soft-label severity —
a temperament that is model-specific, not seat-id-specific.

## Behavior contract

1. **Record enrichment:** `record_decision` gains an optional `--seat-model seat=model` /
   `seat_models` mapping captured at close time (like every other field — never
   backfilled). Where the orchestrator knows the model (asdk seats: explicit; codex/agy/pi:
   the engine's reported model string when available, else the engine name), it records it;
   unknown stays `unknown` honestly.
2. **Report grouping:** `build_report` per-reviewer stats gain a `by_model` breakdown
   (same counters, grouped by model string). **Join semantics pinned (GLM P1):** the
   grouping model is the one recorded ON THE DECISION entry at close time; outcome entries
   join to their decision (finding/decision id, as today) and INHERIT the decision's model —
   an outcome entry never carries or overrides a model. Shown in `format_report` only when
   a reviewer has ≥2 distinct models on record — otherwise output is byte-identical,
   **pinned by a committed golden-report file compared with byte equality** (explicit, per
   GLM: no weaker "looks unchanged" assertion).
3. **Drift flag (report-only, as a QUESTION), fully defined (agy P1s ×3):** a model
   version is QUALIFIED for a seat when it has ≥N decisions THAT HAVE an annotated outcome
   (N=5; unresolved decisions never count) AND a computable confirmation rate (versions
   whose rate is None are excluded BEFORE any comparison — no None arithmetic, ever).
   Delta = `max(rates) − min(rates)` across ALL of the seat's qualified versions (not
   consecutive pairs — A:80% → B:65% → C:50% must flag on the 30-point A↔C spread). Flag
   at delta ≥ 30 points; the anomalies section names seat, versions, rates, and record
   counts — phrased as "investigate", never as a weight change, per the loop's
   non-negotiable invariant.
4. **Backward compatibility:** existing JSONL entries without `seat_models` load and
   report exactly as today (they pool into `unknown`). No migration.

## Verification obligations

Unit tests in the existing calibration test module's style: record round-trip with and
without seat_models; report grouping appears only at ≥2 models; drift-anomaly line at the
threshold boundary (29/30 points; N=4/5); legacy entries unchanged output (byte-compare a
golden report); CLI arg parsing. Full existing calibration tests untouched and green.

## Non-goals

Replay corpora; automated ground truth; any weighting/gating; bias-probe experiments
(position/verbosity tests are a possible later study ON TOP of this data, not this build);
capturing model versions the harness cannot actually observe (no guessing).
