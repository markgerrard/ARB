"""Derive EVERY evidentiary number about the kimi-spike artifacts from COMMITTED bytes.

Why this exists: the surface-probe design's author (warm session) cited working-tree
numbers three times in one hour — including inside the paragraph confessing the previous
violation — because every convenient tool (grep/wc/Read) reads the working tree by
default, while committed bytes cost a deliberate `git show`. Discipline demonstrably does
not survive that default. So the numbers are now MACHINE-DERIVED from a named SHA, and any
doc claim that disagrees with this script's output is wrong by definition.

Usage:
  .venv/bin/python derive_evidence.py <SHA>            # print the evidence block
  .venv/bin/python derive_evidence.py <SHA> --against <doc.md>
        # exit 1 if any "R1×N"/"R2×N"-style or headline count in the doc disagrees

Output is a markdown block meant to be pasted VERBATIM into a spec, with the generating
command line included, so a reviewer can re-run it against the same SHA and diff.
"""
import json, re, subprocess, sys, collections

DIR = "docs/superpowers/probes/2026-07-17-kimi-spike"


def show(sha: str, path: str) -> str:
    r = subprocess.run(["git", "show", f"{sha}:{DIR}/{path}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"FATAL: git show {sha}:{DIR}/{path} failed: {r.stderr.strip()}")
    return r.stdout


def derive(sha: str) -> dict:
    asks_raw = [json.loads(l) for l in show(sha, "review_asks.jsonl").splitlines() if l.strip()]
    res_raw = [json.loads(l) for l in show(sha, "review_results.jsonl").splitlines() if l.strip()]

    bash_asks, sessions_hits, truncated = 0, 0, 0
    payloads = []
    for d in asks_raw:
        a = d.get("ask") or {}
        tc = a.get("toolCall") or {}
        texts = [((c.get("content") or {}).get("text") or "") for c in (tc.get("content") or [])]
        blob = "\n".join(texts)
        if tc.get("title") == "Bash":
            bash_asks += 1
            payloads.append(blob)
            if blob.rstrip().endswith("…"):
                truncated += 1
        sessions_hits += blob.count("kimi-code/sessions")
    # sessions paths can also appear outside Bash asks (plan bodies)
    whole = show(sha, "review_asks.jsonl")
    sessions_total = whole.count("kimi-code/sessions")

    cells = collections.Counter(r["cell"].split("#")[0] for r in res_raw if "cell" in r)

    # the 4-part oracle, applied row by row, vs the crude recorded `delivered`
    disagreements = []
    for r in res_raw:
        if "delivered" not in r:
            continue
        t = r.get("text") or ""
        cites = len(re.findall(r"[\w/]+\.\w+:\d+", t))
        strong = (r.get("stopReason") == "end_turn" and r.get("text_len", 0) > 200
                  and not r.get("mutated") and cites > 0)
        if bool(r["delivered"]) != strong:
            disagreements.append({"cell": r["cell"], "crude": r["delivered"],
                                  "strong": strong, "citations": cites})

    return {
        "sha": sha,
        "asks_lines": len(asks_raw),
        "bash_asks": bash_asks,
        "distinct_bash_payloads": len(set(payloads)),
        "truncated_bash_payloads": truncated,
        "kimi_code_sessions_hits": sessions_total,
        "result_rows": len(res_raw),
        "result_cells": dict(cells),
        "oracle_disagreements": disagreements,
    }


def block(d: dict) -> str:
    lines = [
        f"<!-- GENERATED — do not hand-edit. Regenerate:",
        f"     .venv/bin/python {DIR}/derive_evidence.py {d['sha']} -->",
        f"| fact @ `{d['sha']}` | value |",
        "|---|---|",
        f"| ask-log lines | {d['asks_lines']} |",
        f"| Bash asks | {d['bash_asks']} |",
        f"| distinct Bash payloads | {d['distinct_bash_payloads']} |",
        f"| truncated (`…`) Bash payloads | {d['truncated_bash_payloads']} |",
        f"| `kimi-code/sessions` occurrences | {d['kimi_code_sessions_hits']} |",
        f"| result rows | {d['result_rows']} |",
        f"| result cells | {json.dumps(d['result_cells'])} |",
        f"| crude-vs-4-part oracle disagreements | {json.dumps(d['oracle_disagreements'])} |",
    ]
    return "\n".join(lines)


GEN_START = "<!-- GENERATED — do not hand-edit."
GEN_END_MARKER = "oracle disagreements |"  # last row of the block


def extract_generated_block(doc: str) -> str | None:
    """Pull the pasted evidence block out of a document, verbatim.

    r3/sol P1: the old `--against` validated only two regexes and printed a correct
    block to stdout without ever comparing it to the document — vacuous verification
    (`[[vacuously-green-guard-fail-loud]]`). This locates the actual pasted block so it
    can be compared byte-for-byte against the freshly-derived one.
    """
    start = doc.find(GEN_START)
    if start == -1:
        return None
    end = doc.find(GEN_END_MARKER, start)
    if end == -1:
        return None
    end = doc.find("|\n", end)
    if end == -1:
        end = doc.find("|", end + len(GEN_END_MARKER))
    return doc[start:end + 1].strip()


def main() -> int:
    sha = sys.argv[1]
    d = derive(sha)
    canonical = block(d)
    print(canonical)
    if "--against" in sys.argv:
        doc = open(sys.argv[sys.argv.index("--against") + 1]).read()
        found = extract_generated_block(doc)
        if found is None:
            print(f"\nCHECK FAILED vs {sha}: no GENERATED evidence block found in document",
                  file=sys.stderr)
            return 1
        # Compare the WHOLE block byte-for-byte after normalising whitespace-only diffs.
        norm = lambda s: "\n".join(line.rstrip() for line in s.strip().splitlines())
        if norm(found) != norm(canonical):
            # Show the first differing line so the failure is actionable, not opaque.
            fl = norm(found).splitlines()
            cl = norm(canonical).splitlines()
            diff = next((f"doc:{a!r} != derived:{b!r}"
                         for a, b in zip(fl, cl) if a != b),
                        f"length {len(fl)} != {len(cl)}")
            print(f"\nCHECK FAILED vs {sha}: generated block does not match committed bytes\n"
                  f"  first diff: {diff}", file=sys.stderr)
            return 1
        # Belt-and-braces: any loose numeric claims elsewhere must also agree.
        loose = []
        for m in re.finditer(r"R1×(\d+), R2×(\d+)", doc):
            if (d["result_cells"].get("R1-inline-readonly") != int(m.group(1))
                    or d["result_cells"].get("R2-shell-requiring") != int(m.group(2))):
                loose.append(m.group(0))
        for m in re.finditer(r"(\d+) Bash asks", doc):
            if int(m.group(1)) != d["bash_asks"]:
                loose.append(m.group(0))
        if loose:
            print(f"\nCHECK FAILED vs {sha}: loose numeric claims disagree: {loose}",
                  file=sys.stderr)
            return 1
        print(f"\nCHECK PASSED vs {sha} (full block matched byte-for-byte)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
