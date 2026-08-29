#!/usr/bin/env python3
"""Seed the canonical agent-memory corpus into pi-memory's GLOBAL store.

Translates codex auto-memory frontmatter -> pi-memory frontmatter (donor format:
metadata.node_type=memory, originSessionId/lastWriteSessionId camelCase, JSON-quoted
scalars). Bodies carry over unchanged. Idempotent: skips topics whose name already
exists in the target store (never clobbers organic pi topics)."""
import json, os, sys, re, glob

SEEDS = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.expanduser(os.environ.get("PI_AUTO_MEMORY_DIR", "~/.pi/agent/memory")) + "/global"
SESSION = "seeded-by-claude-orchestrator-20260721"

def parse(raw):
    m = re.match(r"^---\n(.*?)\n---\n\n?(.*)$", raw, re.S)
    fields = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^\s*([a-zA-Z_]+):\s*(.*)$", line)
        if km:
            v = km.group(2)
            try: v = json.loads(v)
            except Exception: pass
            fields[km.group(1)] = v
    return fields, m.group(2)

os.makedirs(TARGET, mode=0o700, exist_ok=True)
seeded, skipped = [], []
for f in sorted(glob.glob(os.path.join(SEEDS, "*.md"))):
    base = os.path.basename(f)
    if base in ("README.md", "MEMORY.md"):
        continue
    name = base[:-3]
    dest = os.path.join(TARGET, base)
    if os.path.exists(dest):
        skipped.append(name); continue
    fields, body = parse(open(f).read())
    desc = str(fields.get("description", ""))[:240]
    mtype = fields.get("type", "reference")
    if mtype not in ("user", "feedback", "project", "reference"): mtype = "reference"
    doc = (f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(desc)}\n"
           f"metadata:\n  node_type: memory\n  type: {mtype}\n"
           f"  originSessionId: {json.dumps(SESSION)}\n"
           f"  lastWriteSessionId: {json.dumps(SESSION)}\n"
           f"  sourceProjectKey: {json.dumps('seeded:codex-arb-artifacts')}\n---\n\n{body.strip()}\n")
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out: out.write(doc)
    seeded.append(name)
print(f"seeded: {len(seeded)} -> {TARGET}"); [print(f"  + {n}") for n in seeded]
print(f"skipped (existing): {skipped}")
