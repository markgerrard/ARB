# H2 Graduation Runbook

Use this when deciding whether H2 has earned the flip from shadow mode to block
mode.

## Shadow Mode

H2 ships in shadow mode. The gate derives candidate environmental assumptions
from the review diff and requires a reviewer disposition for each candidate, but
unanswered candidates produce a loud non-blocking notice instead of blocking the
review.

The gate itself is pure: it returns an `H2Record` and performs no I/O. A separate
collector appends those records as JSONL to the shadow log:

1. `$ARB_H2_SHADOW_LOG`, if set.
2. `$XDG_STATE_HOME/arb/h2-shadow-log.jsonl`, if `XDG_STATE_HOME` is set.
3. `~/.local/state/arb/h2-shadow-log.jsonl`.

Each line is one JSON object with the `H2Record` fields: `run_id`, `h2_mode`,
`derived`, `dispositions`, `coverage_acknowledged`, and `complete`. Incomplete
runs stay in the log for audit, but the graduation query ignores them.

## Graduation Query

Run the committed query over the JSONL shadow log. Graduation requires all of
these over complete runs:

- at least 10 complete runs;
- at least 20 disposed candidates;
- discrimination present: at least one `not_load_bearing` and at least one
  `answered` or `flag`;
- `FP_rate = not_load_bearing / (answered + not_load_bearing + flag) < 0.10`.

Exact invocation:

```bash
PYTHONPATH=. /Users/<user>/<workspace>/.venv/bin/python3 - <<'PY'
import json
import os
from pathlib import Path

from skills.defect_hunts.h2_graduation import is_graduation_ready

if os.environ.get("ARB_H2_SHADOW_LOG"):
    log_path = Path(os.environ["ARB_H2_SHADOW_LOG"])
elif os.environ.get("XDG_STATE_HOME"):
    log_path = Path(os.environ["XDG_STATE_HOME"]) / "arb" / "h2-shadow-log.jsonl"
else:
    log_path = Path.home() / ".local" / "state" / "arb" / "h2-shadow-log.jsonl"

with log_path.open() as stream:
    records = [json.loads(line) for line in stream if line.strip()]

print(is_graduation_ready(records))
PY
```

Only a printed `True` means the automatic graduation criterion is met.

## Flip To Block

When the query prints `True`, edit `skills/bridge-protocol/gate/gate.py` and set:

```python
H2_MODE = "block"
```

This is a gate logic change. It changes the gate object hash, so the operator
must follow the normal gate-change-to-re-pin workflow and re-pin
`skills/bridge-protocol/gate/trust_root.json`. That re-pin is the operator's
"earned-it" action; do not treat the mode flip as complete until the trust root
has been re-pinned and the gate checks pass.

## Non-Goal

Spec §9 deliberately leaves graduation-gaming by deliberate mis-disposition out
of scope. ARB's threat model here is honest operator mistakes, not a malicious
trusted operator shaping dispositions to deceive the metric.

If ARB is productized for an untrusted operator, re-scope this residual and add
the named fixes from §9: a proportion FP floor, `not_load_bearing >= 2` spanning
at least two distinct runs, and adversarial-disposition guards.
