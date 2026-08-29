Canonical audit stance vocabulary:

```text
abstain | approve | block | needs-changes | timed-out
```

`parse_stance` rejects any other stance. Incident note: on 2026-07-06, a reviewer emitted
`approve-with-nits`; the non-canonical stance cost a vote re-fire.

The fence carries an optional `severity` (`none | P2 | P1 | P0`, the seat's highest surviving
finding). Include it — it feeds triage. Since 2026-07-08 an **omitted** `severity` defaults to
`none` instead of dropping the whole vote (the pre-fix behaviour cost re-fires when a seat ended
with just `{"stance":"approve"}`); a **present-but-invalid** severity (e.g. `P3`) is still
rejected. So: always emit `severity`, but a forgetful seat no longer loses its vote.

Verdict-to-stance mapping from `scripts/review-brief`:

```text
APPROVE→`approve`, APPROVE WITH NOTES→`needs-changes`, REQUEST CHANGES→`block`,
cannot-assess→`abstain`.
```
