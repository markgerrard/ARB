#!/usr/bin/env python3
"""Generate scenarios/floor-correctness-full.json from the built fixture repo.

Marker-free source (a seat must not pattern-match a "# SEED" comment); locations resolved by a
unique code signature per line. Rebuild the repo first (build_floor_correctness_full.sh), then run
this. The manifest is the human-authored map of 5 distinct logic-error KINDS + 19 distinct
"looks-buggy-but-correct" control loci spanning those kinds.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent / "repos" / "floor-correctness-full"
OUT = Path(__file__).resolve().parents[1] / "scenarios" / "floor-correctness-full.json"
REPO_DECLARED = "../fixtures/repos/floor-correctness-full"
CLS = "correctness"

SEEDS = [
    ("K1-off-by-one-drops-last", "tiers.py", "total_tiered", 'for i in range(len(amounts) - 1):',
     "total_tiered() claims to sum EVERY tier but range(len-1) drops the last amount"),
    ("K2-inverted-boolean", "pricing.py", "is_free_shipping", 'return total > 50 and is_member',
     "is_free_shipping() docstring says 'over $50 OR member' but uses 'and', excluding non-member big carts"),
    ("K3-falsy-vs-absent", "cart.py", "item_quantity", 'if not qty:',
     "item_quantity() treats a valid quantity of 0 as 'unset' and forces it to 1"),
    ("K4-mutable-default", "cart.py", "add_item", 'def add_item(item, items=[]):',
     "add_item() uses a mutable default list, so the cart accumulates across calls"),
    ("K5-lost-remainder", "money.py", "split_evenly", 'return [total_cents // n] * n',
     "split_evenly() must sum back to total but integer floor drops the remainder cents"),
]

CONTROLS = [
    ("c01-exclude-last-intentional", "tiers.py", "sum_except_last", 'for i in range(len(amounts) - 1):',
     "identical range(len-1) to K1 but docstring says EXCLUDE the last (a precomputed total) — correct"),
    ("c02-header-skip", "tiers.py", "drop_header", 'return rows[1:]',
     "rows[1:] intentionally drops the header row — correct"),
    ("c03-or-default-idiom", "pricing.py", "apply_or_default", 'return label or default',
     "'label or default' is correct: empty/None label legitimately means unset"),
    ("c04-and-required", "pricing.py", "both_required", 'return has_coupon and has_account',
     "'and' is correct here — a stacked discount genuinely requires both"),
    ("c05-de-morgan", "pricing.py", "not_both", 'return not (express and gift_wrap)',
     "not(a and b) correctly expresses mutual exclusivity"),
    ("c06-is-none-distinct", "cart.py", "quantity_or_zero", 'if qty is None:',
     "uses 'is None' so an explicit 0 is preserved (the correct counterpart to K3)"),
    ("c07-none-sentinel", "cart.py", "add_item_safe", 'if items is None:',
     "None-sentinel default gives a fresh list per call (the correct counterpart to K4)"),
    ("c08-local-accumulator", "cart.py", "cart_total", 'total = 0',
     "accumulator is local, so each call starts at 0 — correct despite looking state-y"),
    ("c09-rounded-cents", "money.py", "to_cents", 'return int(round(dollars * 100))',
     "rounds before int() so no truncation error — correct money conversion"),
    ("c10-ceiling-div", "money.py", "page_count", 'return (item_count + per_page - 1) // per_page',
     "the +per-1 idiom is correct ceiling division, not an off-by-one"),
    ("c11-intentional-floor", "money.py", "floor_units", 'return total_cents // unit_cents',
     "integer floor is intentional (whole units only), documented — correct"),
    ("c12-remainder-distributed", "money.py", "split_with_remainder", 'base, extra = divmod(total_cents, n)',
     "correctly distributes leftover cents (the correct counterpart to K5)"),
    ("c13-len-zero", "validate.py", "is_empty", 'return len(x) == 0',
     "len==0 is a correct emptiness test"),
    ("c14-bool-cart", "validate.py", "has_items", 'return bool(cart)',
     "bool(cart) correctly tests non-emptiness"),
    ("c15-clamp", "validate.py", "clamp", 'return max(lo, min(v, hi))',
     "max(lo, min(v, hi)) is the correct clamp despite the nested look"),
    ("c16-modular-wrap", "schedule.py", "next_index", 'return (i + 1) % n',
     "(i+1) % n is correct wraparound, not an off-by-one"),
    ("c17-last-index", "schedule.py", "last_index", 'return len(items) - 1',
     "len-1 is the correct final index"),
    ("c18-bounds-check", "schedule.py", "in_range", 'return 0 <= i < n',
     "0 <= i < n is the correct half-open bounds test"),
    ("c19-guarded-float-div", "util.py", "safe_div", 'return a / b if b else 0.0',
     "guards the zero divisor and uses float division — correct"),
]


# why-clean clusters (panel-adjudicated). Correlated controls -> nu_s CI on cluster count (13
# effective), not nominal 19 (measurement-principles P1 instance 5).
CLUSTERS = {
    "c01-exclude-last-intentional": "index-boundary-correct", "c02-header-skip": "index-boundary-correct",
    "c16-modular-wrap": "index-boundary-correct", "c17-last-index": "index-boundary-correct",
    "c18-bounds-check": "index-boundary-correct",
    "c04-and-required": "correct-boolean", "c05-de-morgan": "correct-boolean",
    "c03-or-default-idiom": "falsy-fallback",
    "c13-len-zero": "emptiness-check", "c14-bool-cart": "emptiness-check",
    "c06-is-none-distinct": "is-none-preserves-zero",
    "c07-none-sentinel": "none-sentinel",
    "c08-local-accumulator": "local-accumulator",
    "c09-rounded-cents": "rounded-money",
    "c10-ceiling-div": "ceiling-division",
    "c11-intentional-floor": "intentional-floor",
    "c12-remainder-distributed": "remainder-distributed",
    "c15-clamp": "clamp",
    "c19-guarded-float-div": "guarded-division",
}


def _line_of(fname, symbol, sig):
    # Scope the signature search to the named function's body so identical code in different
    # functions (intentional: a control can share a seed's code, differing only by intent) resolves
    # to the right line.
    text = (REPO / fname).read_text().splitlines()
    starts = [i for i, ln in enumerate(text) if ln.lstrip().startswith(f"def {symbol}(")]
    if len(starts) != 1:
        raise SystemExit(f"def {symbol}( in {fname}: expected 1, got {len(starts)}")
    start = starts[0]
    end = next((j for j in range(start + 1, len(text)) if text[j].lstrip().startswith("def ")), len(text))
    hits = [k + 1 for k in range(start, end) if sig in text[k]]
    if len(hits) != 1:
        raise SystemExit(f"signature {sig!r} in {fname}:{symbol}: expected 1 hit, got {hits}")
    return hits[0]


def _loc(fname, symbol, sig):
    return {"file": fname, "line": _line_of(fname, symbol, sig), "symbol": symbol}


def _sha(ref):
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", ref],
                          capture_output=True, text=True, check=True).stdout.strip()


def main():
    if not REPO.exists():
        raise SystemExit(f"build the fixture first: {REPO} missing")
    scenario = {
        "id": "floor-correctness-full",
        "description": "Full-power correctness floor (a cart/pricing service, deliberately non-security "
                       "to confirm the suite generalizes past the posture taxonomy): 5 distinct logic-error "
                       "kinds (off-by-one, inverted boolean, falsy-vs-absent, mutable-default state, "
                       "lost-remainder numeric) + 19 distinct looks-buggy-but-correct controls spanning "
                       "those kinds (several share code with a seed but differ by documented intent — the "
                       "investigation test). 5 seeds = I_min, 19 controls = T -> CLASS-LEVEL capable. "
                       "Rebuild with build_floor_correctness_full.sh; regenerate with this script.",
        "subject": {"repo": REPO_DECLARED, "base": _sha("HEAD~1"), "head": _sha("HEAD")},
        "seeded_defects": [
            {"id": i, "class": CLS, "legible": True, "location": _loc(f, sym, sig), "description": d}
            for (i, f, sym, sig, d) in SEEDS
        ],
        "control_loci": [
            {"id": i, "class": CLS, "cluster": CLUSTERS[i], "location": _loc(f, sym, sig),
             "description": d}
            for (i, f, sym, sig, d) in CONTROLS
        ],
        "panel": [
            {"seat": "codex", "model": "gpt-5.5", "harness": "codex"},
            {"seat": "agy", "model": "gemini", "harness": "agy-print"},
        ],
        "power": {"V": 0.40, "nu": 0.10, "alpha": 0.05, "I_min": 5, "R_min": 3, "matcher_gate": 0.85},
    }
    OUT.write_text(json.dumps(scenario, indent=2) + "\n")
    print(f"wrote {OUT} — {len(SEEDS)} seeds, {len(CONTROLS)} controls")


if __name__ == "__main__":
    sys.exit(main())
