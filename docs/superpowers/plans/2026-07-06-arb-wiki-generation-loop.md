# ARB Wiki generation loop Implementation Plan

> **Status: round 2 panel-confirmed 2026-07-06 — ready to dispatch.** Round 2 (run
> `panel-wiki-loop-plan-r2-20260706T194514Z-fdb79e`): all three seats converged on ONE
> remaining item — the `_run_store`/`text=True` fix was prose rather than literal code
> (codex `needs-changes`/P2, agy `needs-changes`/P2, cold-Opus `approve`/P2 nit) — now
> written out verbatim in Task 3 (helper + fake-`run_fn` test, red expectation updated).
> Cold-Opus additionally hand-traced all six round-1 fixes complete (beta baseline keeps
> every mutation needle live; rmtree-before-store safe since intents copy full content;
> `.expanduser()` no-op for tmp_path tests) and re-verified Task 0 against real source.
> Implements the 2-round panel-confirmed design at
> `docs/superpowers/specs/2026-07-06-arb-wiki-generation-loop-design.md`. Round 1
> (run `panel-wiki-loop-plan-20260706T193612Z-805a18`; codex `needs-changes`/P1, agy
> `needs-changes`/P2, cold-Opus `needs-changes`/P1): codex and cold-Opus **independently**
> proved the same P1 — the "good" fixtures' beta page cited one sibling where
> `sibling_min=2`, so Task 1 and three Task-2 tests could never go green (fixed everywhere:
> beta cites both siblings); codex additionally caught the `subprocess.run(input=<str>)`
> missing-text-mode P1 in the store callable (now `text=True` with a `_run_store` helper +
> fake-run test) and the missing `Path` test import; agy caught `Path("~/...")`
> non-expansion (now `.expanduser()`, would have created a literal `~` dir), the temp-dir
> disk leak (now try/finally `shutil.rmtree`), and the CLI import list. Both bridge seats +
> cold-Opus confirmed Task 0 byte-accurate against source, the constraint name correct, and
> the batch/state machine sound.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate the ARB Wiki refresh: fix the shipped `upsert_artefact` rollback bug
(prerequisite), then a config-driven loop that detects repo change, dispatches a seat to
generate wiki pages, validates them against the graph-export contract, and stores them
batch-safely as ARB Memory artefacts via write intents enqueued on the prod bus.

**Architecture:** Task 0 fixes `src/arb_memory/store.py` + `schema.sql` (latest-version-only
dedup; drop the blocking UNIQUE constraint). Tasks 1–3 add
`src/agent_redis_bridge/wiki_refresh.py` (pure logic: config, validation, intents, state
machine — dispatch/store/git injected as callables) and `scripts/arb-wiki-refresh` (thin CLI
wiring real subprocess/ssh callables) plus `configs/arb-wiki.json` seeded with the pilot's
five <workspace> pages.

**Tech Stack:** Python 3, `pytest`; no new dependencies (stdlib `json`/`hashlib`/`fcntl`/
`tempfile`/`subprocess`; the remote script uses the container's existing `redis` +
`arb_memory.bus`).

## Global Constraints

- Read the design spec in full first; every panel callout in it is binding (positional
  hint-linking: ONE intent per page carrying artefact + index hint; resume from persisted
  batch, never regenerate; manifest last; atomic state writes + exclusive run lock; the
  validation contract verbatim).
- Deterministic intent ulid = `hashlib.sha256(f"{nonce}:{artefact_id}".encode()).hexdigest()[:32]`
  — same shape as `bus.new_ulid()` (`uuid.uuid4().hex`, 32 hex chars).
- Tests for the loop are pure-logic (injected callables, `tmp_path` for state); only Task 0
  touches Postgres (existing `scratch` fixture).
- Do NOT modify `src/arb_memory/bus.py`, `vault_export.py`, grants, or the droplet cron.

---

### Task 0: `upsert_artefact` latest-version dedup (prerequisite, shipped-bug fix)

**Files:**
- Modify: `src/arb_memory/store.py:20-65` (`upsert_artefact`)
- Modify: `src/arb_memory/schema.sql` (artefacts table constraint)
- Test: `tests/arb_memory/test_store.py`

**Interfaces:**
- Produces: unchanged signature `upsert_artefact(conn, artefact_id, *, ...) -> tuple[str, int]`;
  new semantics: dedup returns the existing version iff the LATEST version's hash matches;
  content matching only a historical version inserts a new version.

- [ ] **Step 1: Write the failing tests**

Add to `tests/arb_memory/test_store.py`:

```python
def test_upsert_artefact_reverted_content_creates_new_latest_version(scratch):
    upsert_artefact(scratch, "osc", content="content A")
    upsert_artefact(scratch, "osc", content="content B")
    aid, version = upsert_artefact(scratch, "osc", content="content A")

    assert (aid, version) == ("osc", 3)
    row = scratch.execute(
        "SELECT content FROM artefacts WHERE artefact_id = %s ORDER BY version DESC LIMIT 1",
        ("osc",),
    ).fetchone()
    assert row[0] == "content A"


def test_upsert_artefact_unchanged_from_latest_still_noops(scratch):
    upsert_artefact(scratch, "stable", content="same")
    aid, version = upsert_artefact(scratch, "stable", content="same")
    assert (aid, version) == ("stable", 1)
    count = scratch.execute(
        "SELECT count(*) FROM artefacts WHERE artefact_id = %s", ("stable",)
    ).fetchone()[0]
    assert count == 1
```

(`upsert_artefact` is already imported in this test file's existing imports; if not, add it.)

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_store.py -v -k "reverted or noops"`
Expected: `reverted` FAILS — current code returns `("osc", 1)` (any-historical-version match);
`noops` PASSES (sanity guard). If `reverted` instead fails with a unique violation, that is
the same bug's other half — proceed.

- [ ] **Step 3: Implement**

In `src/arb_memory/schema.sql`, the artefacts table currently ends:

```sql
    PRIMARY KEY (artefact_id, version),
    UNIQUE (artefact_id, content_hash)
);
```

Change to (and add the idempotent drop for existing DBs directly after the table's
`CREATE INDEX` line, alongside the existing `ALTER TABLE artefacts ...` migration lines):

```sql
    PRIMARY KEY (artefact_id, version)
);
```
```sql
ALTER TABLE artefacts DROP CONSTRAINT IF EXISTS artefacts_artefact_id_content_hash_key;
```

In `src/arb_memory/store.py::upsert_artefact`, replace the dedup query:

```python
    content_hash = artefact_hash(content, content_bytes, mime)
    row = conn.execute(
        "SELECT version FROM artefacts WHERE artefact_id = %s AND content_hash = %s",
        (artefact_id, content_hash),
    ).fetchone()
    if row is not None:
        return artefact_id, row[0]
```

with:

```python
    content_hash = artefact_hash(content, content_bytes, mime)
    # Dedup against the LATEST version only: content that matches a merely-historical
    # version must create a new version, or a reverted artefact (A -> B -> A) silently
    # strands every latest-version consumer (the vault export) on stale content forever.
    row = conn.execute(
        "SELECT version, content_hash FROM artefacts"
        " WHERE artefact_id = %s ORDER BY version DESC LIMIT 1",
        (artefact_id,),
    ).fetchone()
    if row is not None and row[1] == content_hash:
        return artefact_id, row[0]
```

- [ ] **Step 4: Green + full arb_memory regression**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_store.py -v -k "reverted or noops"`
— both PASS. Then `.venv/bin/python3 -m pytest tests/arb_memory/ -q` — no regressions
(baseline 533 passed / 1 skipped, +2 new).

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/store.py src/arb_memory/schema.sql tests/arb_memory/test_store.py
git commit -m "$(cat <<'EOF'
fix(arb-memory): upsert_artefact dedups against the latest version only

The old query matched (artefact_id, content_hash) at ANY historical
version, so reverted content (A -> B -> A) silently no-op'd and every
latest-version consumer -- the vault export above all -- stayed stranded
on stale content forever. Dedup now compares only the latest version's
hash; the (artefact_id, content_hash) UNIQUE constraint, which would have
blocked the historical re-insert, is dropped (idempotent migration line;
verified by all three round-2 panel seats that nothing relies on it).

Found by agy (P1) in the ARB Wiki generation loop spec review; see
docs/superpowers/specs/2026-07-06-arb-wiki-generation-loop-design.md.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp
EOF
)"
```

---

### Task 1: `wiki_refresh.py` — config, validation, brief, intents (pure logic)

**Files:**
- Create: `src/agent_redis_bridge/wiki_refresh.py`
- Test: `tests/test_wiki_refresh.py`

**Interfaces (consumed by Tasks 2–3):**
- `load_config(path: str) -> dict` — parsed+validated config; raises `WikiConfigError` on
  missing/invalid fields.
- `all_page_ids(config: dict) -> set[str]` — union of every repo's page ids.
- `render_brief(repo: dict, output_dir: str) -> str` — the generation brief text.
- `validate_pages(repo: dict, output_dir: str, all_ids: set[str]) -> list[str]` — list of
  human-readable violations, empty when valid.
- `build_intents(repo: dict, output_dir: str, nonce: str, head_sha: str, generated_at: str)
  -> list[dict]` — ordered intents (pages sorted by id, manifest LAST), each
  `{"ulid": ..., "artefact": {...}, "hints": [...]}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_wiki_refresh.py`:

```python
import json
import re
from pathlib import Path

import pytest

from agent_redis_bridge.wiki_refresh import (
    WikiConfigError,
    all_page_ids,
    build_intents,
    load_config,
    render_brief,
    validate_pages,
)


CONFIG = {
    "repos": [
        {
            "name": "demo",
            "path": "/tmp/demo-repo",
            "seat": {"engine": "codex", "target_id": "codex-demo-dev", "timeout": 3600},
            "pages": [
                {"id": "wiki-demo-alpha", "title": "Alpha", "scope": "the alpha subsystem"},
                {"id": "wiki-demo-beta", "title": "Beta", "scope": "the beta subsystem"},
                {"id": "wiki-demo-gamma", "title": "Gamma", "scope": "the gamma subsystem"},
            ],
        },
        {
            "name": "other",
            "path": "/tmp/other-repo",
            "seat": {"engine": "codex", "target_id": "codex-other-dev", "timeout": 3600},
            "pages": [
                {"id": "wiki-other-home", "title": "Home", "scope": "everything"},
                {"id": "wiki-other-ops", "title": "Ops", "scope": "operations"},
            ],
        },
    ]
}


def _write_config(tmp_path, config=CONFIG):
    path = tmp_path / "arb-wiki.json"
    path.write_text(json.dumps(config))
    return str(path)


def _page(body_refs=("wiki-demo-beta", "wiki-demo-gamma"), title="# Alpha\n", filler_words=200):
    filler = " ".join(["word"] * filler_words)
    see_also = ", ".join(f"`{r}`" for r in body_refs)
    return f"{title}\n{filler}\n\nSee also: {see_also}\n"


def _write_pages(tmp_path, contents: dict):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    for page_id, text in contents.items():
        (out / f"{page_id}.md").write_text(text)
    return str(out)


def test_load_config_rejects_missing_fields(tmp_path):
    bad = {"repos": [{"name": "x", "pages": []}]}
    with pytest.raises(WikiConfigError):
        load_config(_write_config(tmp_path, bad))


def test_all_page_ids_spans_repos(tmp_path):
    config = load_config(_write_config(tmp_path))
    assert all_page_ids(config) == {
        "wiki-demo-alpha", "wiki-demo-beta", "wiki-demo-gamma",
        "wiki-other-home", "wiki-other-ops",
    }


def test_render_brief_contains_pages_rules_and_output_dir(tmp_path):
    config = load_config(_write_config(tmp_path))
    brief = render_brief(config["repos"][0], "/tmp/outdir")
    for needle in ("wiki-demo-alpha", "the alpha subsystem", "/tmp/outdir",
                   "backticked", "See also:", "300"):
        assert needle in brief
    assert "[[" not in brief.replace("`[[...]]`", "")  # rules mention the ban, never emit raw


def test_validate_passes_a_good_set(tmp_path):
    config = load_config(_write_config(tmp_path))
    out = _write_pages(tmp_path, {
        "wiki-demo-alpha": _page(),
        "wiki-demo-beta": _page(("wiki-demo-alpha", "wiki-demo-gamma"), "# Beta\n"),
        "wiki-demo-gamma": _page(("wiki-demo-alpha", "wiki-demo-beta"), "# Gamma\n"),
    })
    assert validate_pages(config["repos"][0], out, all_page_ids(config)) == []


@pytest.mark.parametrize("mutation, needle", [
    (lambda t: "", "missing or empty"),
    (lambda t: t.replace("# Alpha", "Alpha untitled"), "title"),
    (lambda t: t.replace("word word", "[[link]] word", 1), "[["),
    (lambda t: t.rsplit("See also:", 1)[0], "See also"),
    (lambda t: t + "\ntrailing prose after the line\n", "final"),
    (lambda t: t.replace("`wiki-demo-beta`", "`wiki-demo-betaa`"), "unknown"),
    (lambda t: t.replace("`wiki-demo-beta`", "wiki-demo-beta"), "bare"),
    (lambda t: t.replace("`wiki-demo-beta`, `wiki-demo-gamma`", "`wiki-demo-alpha`"), "self"),
    (lambda t: t.replace(" ".join(["word"] * 200), "short"), "length"),
    (lambda t: t.replace("word word word", "word wiki-demo-gamma word", 1), "bare"),
])
def test_validate_rejects_each_contract_violation(tmp_path, mutation, needle):
    config = load_config(_write_config(tmp_path))
    good = {
        "wiki-demo-alpha": _page(),
        "wiki-demo-beta": _page(("wiki-demo-alpha", "wiki-demo-gamma"), "# Beta\n"),
        "wiki-demo-gamma": _page(("wiki-demo-alpha", "wiki-demo-beta"), "# Gamma\n"),
    }
    good["wiki-demo-alpha"] = mutation(good["wiki-demo-alpha"])
    out = _write_pages(tmp_path, good)
    violations = validate_pages(config["repos"][0], out, all_page_ids(config))
    assert violations, "expected a violation"
    assert any(needle.lower() in v.lower() for v in violations)


def test_validate_two_page_repo_allows_single_sibling(tmp_path):
    config = load_config(_write_config(tmp_path))
    out = _write_pages(tmp_path, {
        "wiki-other-home": _page(("wiki-other-ops",), "# Home\n"),
        "wiki-other-ops": _page(("wiki-other-home",), "# Ops\n"),
    })
    assert validate_pages(config["repos"][1], out, all_page_ids(config)) == []


def test_validate_accepts_cross_repo_citation_and_rejects_typo(tmp_path):
    config = load_config(_write_config(tmp_path))
    good = _page()
    cross_ok = good.replace("word word word", "word `wiki-other-home` word", 1)
    cross_typo = good.replace("word word word", "word `wiki-other-hom` word", 1)
    out = _write_pages(tmp_path, {
        "wiki-demo-alpha": cross_ok,
        "wiki-demo-beta": _page(("wiki-demo-alpha", "wiki-demo-gamma"), "# Beta\n"),
        "wiki-demo-gamma": _page(("wiki-demo-alpha", "wiki-demo-beta"), "# Gamma\n"),
    })
    assert validate_pages(config["repos"][0], out, all_page_ids(config)) == []
    out2 = _write_pages(tmp_path, {
        "wiki-demo-alpha": cross_typo,
        "wiki-demo-beta": _page(("wiki-demo-alpha", "wiki-demo-gamma"), "# Beta\n"),
        "wiki-demo-gamma": _page(("wiki-demo-alpha", "wiki-demo-beta"), "# Gamma\n"),
    })
    assert validate_pages(config["repos"][0], out2, all_page_ids(config))


def test_build_intents_shape_ordering_and_determinism(tmp_path):
    config = load_config(_write_config(tmp_path))
    out = _write_pages(tmp_path, {
        "wiki-demo-alpha": _page(),
        "wiki-demo-beta": _page(("wiki-demo-alpha", "wiki-demo-gamma"), "# Beta\n"),
        "wiki-demo-gamma": _page(("wiki-demo-alpha", "wiki-demo-beta"), "# Gamma\n"),
    })
    intents = build_intents(config["repos"][0], out, "nonce-1", "abc123", "2026-07-06T00:00:00Z")

    assert [i["artefact"]["artefact_id"] for i in intents] == [
        "wiki-demo-alpha", "wiki-demo-beta", "wiki-demo-gamma", "wiki-demo-manifest",
    ]  # pages sorted, manifest LAST
    page = intents[0]
    assert page["artefact"]["mime"] == "text/markdown"
    assert page["artefact"]["source"] == "wiki"
    assert page["artefact"]["author"] == "codex-demo-dev"
    assert len(page["hints"]) == 1  # ONE intent per page: artefact + its index hint together
    assert page["hints"][0]["metadata"]["kind"] == "artefact_index"
    assert page["hints"][0]["metadata"]["artefact_id"] == "wiki-demo-alpha"
    manifest = json.loads(intents[-1]["artefact"]["content"])
    assert manifest["head_sha"] == "abc123"
    assert set(manifest["pages"]) == {"wiki-demo-alpha", "wiki-demo-beta", "wiki-demo-gamma"}

    again = build_intents(config["repos"][0], out, "nonce-1", "abc123", "2026-07-06T00:00:00Z")
    assert [i["ulid"] for i in again] == [i["ulid"] for i in intents]  # nonce-stable
    other = build_intents(config["repos"][0], out, "nonce-2", "abc123", "2026-07-06T00:00:00Z")
    assert [i["ulid"] for i in other] != [i["ulid"] for i in intents]  # nonce-scoped
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/python3 -m pytest tests/test_wiki_refresh.py -v 2>&1 | head -20`
Expected: collection error — `agent_redis_bridge.wiki_refresh` does not exist.

- [ ] **Step 3: Implement `src/agent_redis_bridge/wiki_refresh.py` (core half)**

```python
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


INDEX_HINT_CHARS = 4000
MIN_PAGE_CHARS = 500
MAX_PAGE_CHARS = 6000
BACKTICKED_WIKI_RE = re.compile(r"`(wiki-[A-Za-z0-9._-]+)`")
BARE_WIKI_RE = re.compile(r"(?<![`A-Za-z0-9._/-])(wiki-[A-Za-z0-9._-]+)(?![`A-Za-z0-9._/-])")


class WikiConfigError(ValueError):
    pass


def load_config(path: str) -> dict:
    config = json.loads(Path(path).read_text())
    repos = config.get("repos")
    if not isinstance(repos, list) or not repos:
        raise WikiConfigError("config must have a non-empty 'repos' list")
    for repo in repos:
        for field in ("name", "path", "seat", "pages"):
            if field not in repo:
                raise WikiConfigError(f"repo missing field {field!r}")
        for field in ("engine", "target_id", "timeout"):
            if field not in repo["seat"]:
                raise WikiConfigError(f"seat missing field {field!r} in repo {repo['name']!r}")
        if not repo["pages"]:
            raise WikiConfigError(f"repo {repo['name']!r} has no pages")
        for page in repo["pages"]:
            for field in ("id", "title", "scope"):
                if field not in page:
                    raise WikiConfigError(f"page missing field {field!r} in repo {repo['name']!r}")
    return config


def all_page_ids(config: dict) -> set[str]:
    return {page["id"] for repo in config["repos"] for page in repo["pages"]}


BRIEF_TEMPLATE = """# ARB Wiki refresh brief — {repo_name}

READ-ONLY task: do NOT modify, commit, or write anything inside the repo at {repo_path}.
All output goes to {output_dir} (create the directory).

Write one markdown page per entry below about the repo at {repo_path}, grounded in its actual
source and docs — read the real files, cite real file paths inline where load-bearing.

## Pages (filename = <id>.md in the output dir)

{page_list}

## Format rules (load-bearing — the export pipeline depends on them)

- First line: `# <human title>`.
- 300-500 words each, prose over bullet-dumps, for a reader who has never seen the repo.
- Each page ENDS with a `See also:` line citing sibling pages **by backticked id** (backticks
  and exact ids matter). Cross-reference sibling pages inline the same way where natural.
- Never write wiki-style double-bracket links (the `[[...]]` syntax) anywhere.
- Never mention a wiki page id without backticks.
- Plain markdown otherwise; no frontmatter.

Reply with the list of files written.
"""


def render_brief(repo: dict, output_dir: str) -> str:
    page_list = "\n".join(
        f"{i}. `{page['id']}.md` — {page['title']}: {page['scope']}"
        for i, page in enumerate(repo["pages"], 1)
    )
    return BRIEF_TEMPLATE.format(
        repo_name=repo["name"], repo_path=repo["path"],
        output_dir=output_dir, page_list=page_list,
    )


def validate_pages(repo: dict, output_dir: str, all_ids: set[str]) -> list[str]:
    violations: list[str] = []
    page_ids = [page["id"] for page in repo["pages"]]
    sibling_min = min(2, len(page_ids) - 1)
    out = Path(output_dir)

    for page_id in page_ids:
        path = out / f"{page_id}.md"
        if not path.is_file() or not path.read_text().strip():
            violations.append(f"{page_id}: file missing or empty")
            continue
        text = path.read_text()
        if not (MIN_PAGE_CHARS <= len(text) <= MAX_PAGE_CHARS):
            violations.append(f"{page_id}: length {len(text)} outside bounds")
        if not text.splitlines()[0].startswith("# "):
            violations.append(f"{page_id}: first line is not a '# ' title")
        if "[[" in text:
            violations.append(f"{page_id}: contains [[ syntax")

        lines = [line for line in text.splitlines() if line.strip()]
        final = lines[-1] if lines else ""
        if not final.startswith("See also:"):
            violations.append(f"{page_id}: final non-empty line is not the See also: line")
        else:
            tokens = BACKTICKED_WIKI_RE.findall(final)
            stripped = BACKTICKED_WIKI_RE.sub("", final[len("See also:"):])
            if BARE_WIKI_RE.search(stripped):
                violations.append(f"{page_id}: bare (unbackticked) id on the See also: line")
            if len(tokens) < sibling_min:
                violations.append(f"{page_id}: See also: has {len(tokens)} entries, need >= {sibling_min}")
            for token in tokens:
                if token == page_id:
                    violations.append(f"{page_id}: See also: cites itself (self)")
                elif token not in page_ids:
                    violations.append(f"{page_id}: See also: unknown id {token!r}")

        for token in BACKTICKED_WIKI_RE.findall(text):
            if token not in all_ids and token != f"wiki-{repo['name']}-manifest":
                violations.append(f"{page_id}: backticked reference to unknown id {token!r}")
        body_wo_backticked = BACKTICKED_WIKI_RE.sub("", text)
        if BARE_WIKI_RE.search(body_wo_backticked):
            violations.append(f"{page_id}: bare (unbackticked) wiki-* token in body")
    return violations


def _intent_ulid(nonce: str, artefact_id: str) -> str:
    return hashlib.sha256(f"{nonce}:{artefact_id}".encode()).hexdigest()[:32]


def _page_intent(repo: dict, artefact_id: str, content: str, nonce: str) -> dict:
    return {
        "ulid": _intent_ulid(nonce, artefact_id),
        "artefact": {
            "artefact_id": artefact_id,
            "content": content,
            "mime": "text/markdown",
            "source": "wiki",
            "author": repo["seat"]["target_id"],
        },
        "hints": [{
            "text": content[:INDEX_HINT_CHARS],
            "metadata": {"kind": "artefact_index", "artefact_id": artefact_id},
        }],
    }


def build_intents(repo: dict, output_dir: str, nonce: str, head_sha: str, generated_at: str) -> list[dict]:
    out = Path(output_dir)
    intents = []
    page_ids = sorted(page["id"] for page in repo["pages"])
    for page_id in page_ids:
        content = (out / f"{page_id}.md").read_text()
        intents.append(_page_intent(repo, page_id, content, nonce))
    manifest_id = f"wiki-{repo['name']}-manifest"
    manifest = {
        "repo": repo["name"], "head_sha": head_sha,
        "generated_at": generated_at, "pages": page_ids,
    }
    manifest_intent = _page_intent(repo, manifest_id, json.dumps(manifest, sort_keys=True), nonce)
    manifest_intent["artefact"]["mime"] = "application/json"
    intents.append(manifest_intent)  # manifest LAST: batch-complete marker
    return intents
```

- [ ] **Step 4: Green**

Run: `.venv/bin/python3 -m pytest tests/test_wiki_refresh.py -v` — all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/wiki_refresh.py tests/test_wiki_refresh.py
git commit -m "$(cat <<'EOF'
feat(wiki): ARB Wiki refresh core — config, brief, validation, intents

Pure-logic half of the generation loop: config load/validation, the
generation brief template (pilot-derived format rules), the full panel
validation contract (final-line See-also with min(2, siblings) configured
ids, cross-repo citation checks against the whole config, bare wiki-*
tokens rejected), and batch intent construction (one intent per page
carrying artefact + index hint positionally, deterministic
(nonce, artefact_id) ulids, manifest last).

Per docs/superpowers/specs/2026-07-06-arb-wiki-generation-loop-design.md
(2-round panel-confirmed).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp
EOF
)"
```

---

### Task 2: state machine + batch-safe run orchestration

**Files:**
- Modify: `src/agent_redis_bridge/wiki_refresh.py` (append)
- Test: `tests/test_wiki_refresh.py` (append)

**Interfaces:**
- `refresh_repo(repo, *, state_dir: str, all_ids: set[str], git_head, dispatch_fn, store_fn,
  now_fn, force: bool = False, mkdtemp_fn=None) -> str` — returns one of
  `"skipped-unchanged" | "resumed" | "refreshed"`. `git_head(repo_path) -> sha`;
  `dispatch_fn(repo, brief_path, output_dir) -> None` (raises on failure);
  `store_fn(intents: list[dict]) -> None` (raises on failure — Task 3 wires ssh);
  `now_fn() -> iso-str`.
- State layout in `state_dir`: `state.json` (`{"<repo>": {"head_sha", "generated_at"}}`),
  `pending-<repo>.json` (`{"nonce", "head_sha", "intents": [...]}`), `lock` (flock target —
  held by the CLI in Task 3, not by `refresh_repo`).
- All file writes atomic: write `<name>.tmp` then `os.replace`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_wiki_refresh.py`)

```python
from agent_redis_bridge.wiki_refresh import refresh_repo


class Recorder:
    def __init__(self, head="abc123", dispatch_writes=None, store_raises_after=None):
        self.head = head
        self.dispatch_calls = 0
        self.stored_batches = []
        self.dispatch_writes = dispatch_writes or {}
        self.store_raises_after = store_raises_after

    def git_head(self, repo_path):
        return self.head

    def dispatch(self, repo, brief_path, output_dir):
        self.dispatch_calls += 1
        for page_id, text in self.dispatch_writes.items():
            (Path(output_dir) / f"{page_id}.md").write_text(text)

    def store(self, intents):
        if self.store_raises_after is not None:
            self.stored_batches.append(intents[: self.store_raises_after])
            raise RuntimeError("interrupted mid-batch")
        self.stored_batches.append(intents)


GOOD_PAGES = {
    "wiki-demo-alpha": _page(),
    "wiki-demo-beta": _page(("wiki-demo-alpha", "wiki-demo-gamma"), "# Beta\n"),
    "wiki-demo-gamma": _page(("wiki-demo-alpha", "wiki-demo-beta"), "# Gamma\n"),
}


def _repo(tmp_path):
    config = load_config(_write_config(tmp_path))
    return config["repos"][0], all_page_ids(config)


def test_refresh_skips_unchanged(tmp_path):
    repo, ids = _repo(tmp_path)
    rec = Recorder(dispatch_writes=GOOD_PAGES)
    state_dir = str(tmp_path / "state")
    assert refresh_repo(repo, state_dir=state_dir, all_ids=ids, git_head=rec.git_head,
                        dispatch_fn=rec.dispatch, store_fn=rec.store,
                        now_fn=lambda: "T0") == "refreshed"
    assert refresh_repo(repo, state_dir=state_dir, all_ids=ids, git_head=rec.git_head,
                        dispatch_fn=rec.dispatch, store_fn=rec.store,
                        now_fn=lambda: "T1") == "skipped-unchanged"
    assert rec.dispatch_calls == 1


def test_refresh_validation_failure_stores_nothing(tmp_path):
    repo, ids = _repo(tmp_path)
    bad = dict(GOOD_PAGES)
    bad["wiki-demo-alpha"] = bad["wiki-demo-alpha"].replace("# Alpha", "untitled")
    rec = Recorder(dispatch_writes=bad)
    with pytest.raises(Exception):
        refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=rec.dispatch, store_fn=rec.store, now_fn=lambda: "T0")
    assert rec.stored_batches == []


def test_interrupted_store_resumes_same_batch_without_redispatch(tmp_path):
    repo, ids = _repo(tmp_path)
    state_dir = str(tmp_path / "state")
    rec = Recorder(dispatch_writes=GOOD_PAGES, store_raises_after=2)
    with pytest.raises(RuntimeError):
        refresh_repo(repo, state_dir=state_dir, all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=rec.dispatch, store_fn=rec.store, now_fn=lambda: "T0")
    pending = json.loads((Path(state_dir) / "pending-demo.json").read_text())
    first_ulids = [i["ulid"] for i in pending["intents"]]

    # second run: dispatch would produce DIFFERENT content -- must not be consulted
    rec2 = Recorder(dispatch_writes={k: v.replace("word", "changed") for k, v in GOOD_PAGES.items()})
    assert refresh_repo(repo, state_dir=state_dir, all_ids=ids, git_head=rec2.git_head,
                        dispatch_fn=rec2.dispatch, store_fn=rec2.store,
                        now_fn=lambda: "T1") == "resumed"
    assert rec2.dispatch_calls == 0  # resume NEVER regenerates
    assert [i["ulid"] for i in rec2.stored_batches[0]] == first_ulids  # byte-identical batch
    assert not (Path(state_dir) / "pending-demo.json").exists()  # cleared on success
    state = json.loads((Path(state_dir) / "state.json").read_text())
    assert state["demo"]["head_sha"] == "abc123"


def test_force_discards_pending_and_regenerates(tmp_path):
    repo, ids = _repo(tmp_path)
    state_dir = str(tmp_path / "state")
    rec = Recorder(dispatch_writes=GOOD_PAGES, store_raises_after=1)
    with pytest.raises(RuntimeError):
        refresh_repo(repo, state_dir=state_dir, all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=rec.dispatch, store_fn=rec.store, now_fn=lambda: "T0")
    rec2 = Recorder(dispatch_writes=GOOD_PAGES)
    assert refresh_repo(repo, state_dir=state_dir, all_ids=ids, git_head=rec2.git_head,
                        dispatch_fn=rec2.dispatch, store_fn=rec2.store,
                        now_fn=lambda: "T1", force=True) == "refreshed"
    assert rec2.dispatch_calls == 1


def test_state_not_advanced_when_store_fails(tmp_path):
    repo, ids = _repo(tmp_path)
    state_dir = str(tmp_path / "state")
    rec = Recorder(dispatch_writes=GOOD_PAGES, store_raises_after=0)
    with pytest.raises(RuntimeError):
        refresh_repo(repo, state_dir=state_dir, all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=rec.dispatch, store_fn=rec.store, now_fn=lambda: "T0")
    state_path = Path(state_dir) / "state.json"
    assert not state_path.exists() or "demo" not in json.loads(state_path.read_text())
```

- [ ] **Step 2: Red** — `pytest tests/test_wiki_refresh.py -v -k refresh` → ImportError on
  `refresh_repo`.

- [ ] **Step 3: Implement** (append to `wiki_refresh.py`)

```python
import os
import shutil
import tempfile
import uuid
```

(Place these with the module's top-of-file imports, not mid-file — round-1 cold-Opus nit.)

```python


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def refresh_repo(repo, *, state_dir, all_ids, git_head, dispatch_fn, store_fn, now_fn,
                 force=False, mkdtemp_fn=None):
    # expanduser is load-bearing: Path("~/.arb-wiki") does NOT expand, and .mkdir() on it
    # would create a literal '~' directory in the cwd (round-1 agy, 100% confidence).
    state_root = Path(state_dir).expanduser()
    state_root.mkdir(parents=True, exist_ok=True)
    state_path = state_root / "state.json"
    pending_path = state_root / f"pending-{repo['name']}.json"

    if force and pending_path.is_file():
        pending_path.unlink()

    if pending_path.is_file():
        pending = _read_json(pending_path, None)
        store_fn(pending["intents"])  # resume from the persisted batch -- NEVER regenerate
        _record_complete(state_path, repo["name"], pending["head_sha"], now_fn())
        pending_path.unlink()
        return "resumed"

    head_sha = git_head(repo["path"])
    state = _read_json(state_path, {})
    if not force and state.get(repo["name"], {}).get("head_sha") == head_sha:
        return "skipped-unchanged"

    output_dir = (mkdtemp_fn or tempfile.mkdtemp)()
    try:
        brief_path = Path(output_dir) / "_brief.md"
        brief_path.write_text(render_brief(repo, output_dir))
        dispatch_fn(repo, str(brief_path), output_dir)

        violations = validate_pages(repo, output_dir, all_ids)
        if violations:
            raise RuntimeError(
                f"wiki refresh for {repo['name']!r} failed validation; storing NOTHING:\n"
                + "\n".join(violations)
            )

        nonce = uuid.uuid4().hex
        intents = build_intents(repo, output_dir, nonce, head_sha, now_fn())
    finally:
        # the intent batch (or the validation error text) carries everything the temp
        # dir held; leaving one behind per run is a disk leak (round-1 agy)
        shutil.rmtree(output_dir, ignore_errors=True)
    _atomic_write(pending_path, json.dumps(
        {"nonce": nonce, "head_sha": head_sha, "intents": intents}))
    store_fn(intents)
    _record_complete(state_path, repo["name"], head_sha, now_fn())
    pending_path.unlink()
    return "refreshed"


def _record_complete(state_path: Path, repo_name: str, head_sha: str, generated_at: str) -> None:
    state = _read_json(state_path, {})
    state[repo_name] = {"head_sha": head_sha, "generated_at": generated_at}
    _atomic_write(state_path, json.dumps(state, sort_keys=True))
```

Note `mkdtemp_fn` lets tests pin the output dir if needed; the tests above rely on
`dispatch_fn` receiving `output_dir` and writing there, which works with the real
`tempfile.mkdtemp` too.

- [ ] **Step 4: Green + whole-file run** — `pytest tests/test_wiki_refresh.py -v` all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/wiki_refresh.py tests/test_wiki_refresh.py
git commit -m "$(cat <<'EOF'
feat(wiki): batch-safe refresh state machine

Change detection by repo HEAD against state.json; interrupted stores
persist the full validated intent batch per-repo (pending-<repo>.json,
atomic writes) and resume by re-enqueueing THAT batch -- never
regenerating, so a resumed set can't mix LLM generations (round-2 codex).
Validation failure stores nothing; state advances only after the full
batch enqueues; --force discards pending and regenerates under a new
nonce.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp
EOF
)"
```

---

### Task 3: CLI wrapper, real callables, config seed

**Files:**
- Create: `scripts/arb-wiki-refresh`
- Create: `configs/arb-wiki.json` (the spec's seed config verbatim — the five pilot pages)
- Modify: `src/agent_redis_bridge/wiki_refresh.py` (append `main()` + real callables)
- Test: `tests/test_wiki_refresh.py` (append: remote-script construction test)

**Interfaces:**
- `build_store_script(intents: list[dict]) -> str` — the remote python program with the
  base64 payload EMBEDDED as a literal (a `python3 -` script cannot also read data from the
  stdin that delivers it), enqueueing via `arb_memory.bus.memory_write(..., ulid=intent ulid)`
  in order.
- `main(argv) -> int` — `--config configs/arb-wiki.json --state-dir ~/.arb-wiki
  [--repo NAME] [--force]`; holds an exclusive `fcntl.flock` on `<state-dir>/lock` for the
  whole run; wires `git_head` (`git -C <path> rev-parse HEAD`), `dispatch_fn`
  (`scripts/dispatch-dev --adhoc` with the repo's seat + standard env overrides), `store_fn`
  (`ssh arb-prod docker compose -f deploy/docker-compose.yml exec -T memory python3 -`
  fed `build_store_script(intents)` on stdin).

- [ ] **Step 1: Write the failing test** (append)

```python
def test_build_store_script_embeds_payload_and_orders_intents(tmp_path):
    from agent_redis_bridge.wiki_refresh import build_store_script
    import base64

    repo, ids = _repo(tmp_path)
    out = _write_pages(tmp_path, GOOD_PAGES)
    intents = build_intents(repo, out, "n", "sha", "T0")
    script = build_store_script(intents)

    assert "sys.stdin" not in script  # payload must be embedded, not read from stdin
    assert "memory_write" in script and "ARB_MEMORY_REDIS_URL" in script
    b64 = re.search(r'"([A-Za-z0-9+/=]{100,})"', script).group(1)
    decoded = json.loads(base64.b64decode(b64))
    assert [i["ulid"] for i in decoded] == [i["ulid"] for i in intents]  # order preserved


def test_run_store_uses_ssh_argv_text_mode_and_script_stdin(tmp_path):
    from agent_redis_bridge.wiki_refresh import _run_store, build_store_script

    repo, ids = _repo(tmp_path)
    out = _write_pages(tmp_path, GOOD_PAGES)
    intents = build_intents(repo, out, "n", "sha", "T0")
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls.update(kwargs)

    _run_store(intents, run_fn=fake_run)

    assert calls["cmd"][0] == "ssh" and calls["cmd"][1] == "arb-prod"
    assert "docker compose" in calls["cmd"][2] and "python3 -" in calls["cmd"][2]
    assert calls["input"] == build_store_script(intents)
    assert calls["text"] is True   # str input on a binary pipe raises TypeError (round-1 P1)
    assert calls["check"] is True
```

(Add `import re` to the test file's imports if missing.)

- [ ] **Step 2: Red** — ImportError on `build_store_script` AND `_run_store` (both tests).

- [ ] **Step 3: Implement** (append to `wiki_refresh.py`; then the thin CLI + config seed)

```python
import base64


STORE_SCRIPT_TEMPLATE = '''\
import base64, json, os
import redis
from arb_memory.bus import memory_write

intents = json.loads(base64.b64decode("{payload_b64}"))
client = redis.from_url(os.environ["ARB_MEMORY_REDIS_URL"], decode_responses=True)
for intent in intents:
    memory_write(
        client,
        artefact=intent["artefact"],
        hints=intent["hints"],
        source=intent["artefact"]["source"],
        author=intent["artefact"]["author"],
        ulid=intent["ulid"],
    )
print(f"enqueued {{len(intents)}} intents")
'''


def build_store_script(intents: list[dict]) -> str:
    payload_b64 = base64.b64encode(json.dumps(intents).encode()).decode("ascii")
    return STORE_SCRIPT_TEMPLATE.format(payload_b64=payload_b64)


def _run_store(intents: list[dict], *, run_fn=None) -> None:
    runner = run_fn or subprocess.run  # subprocess imported at module top (see import list)
    runner(
        [
            "ssh", "arb-prod",
            "cd /home/claude/AgentRedisBridge && "
            "docker compose -f deploy/docker-compose.yml exec -T memory python3 -",
        ],
        input=build_store_script(intents),
        text=True,   # load-bearing: str input on a binary stdin pipe raises TypeError
        check=True,
    )
```

`main()` (same file): argparse as specified; `fcntl.flock(open(lock_path, "w"), LOCK_EX)`
held for the run; real callables via `subprocess.run(..., check=True)`:
- `git_head`: `["git", "-C", repo_path, "rev-parse", "HEAD"]` → stripped stdout.
- `dispatch_fn`: `scripts/dispatch-dev --engine <seat.engine> --target-id <seat.target_id>
  --timeout <seat.timeout> --adhoc "<READ-ONLY preamble> Read the brief at <brief_path> and
  execute it."` with `FROM_AGENT_ID=claude-bridge-dev BRANCH=dev
  AGENT_ENV_FILE=envs/agent-redis-bridge-dev.env` in the env.
- `store_fn`: `_run_store` (literal implementation and its fake-`run_fn` test are in Steps
  1/3 above — round-2 codex + agy convergent P2 turned the round-1 prose into code). The CLI
  passes `store_fn=_run_store`.
- `main()` and the real callables need these imports at the module top (round-1 agy):
  `argparse`, `fcntl`, `subprocess`, `sys`.
- Iterate configured repos (respecting `--repo`), print each outcome, exit nonzero if any
  repo raised.

`scripts/arb-wiki-refresh`:

```python
#!/usr/bin/env python3
"""ARB Wiki generation loop. See docs/superpowers/specs/2026-07-06-arb-wiki-generation-loop-design.md."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agent_redis_bridge.wiki_refresh import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

`chmod +x scripts/arb-wiki-refresh`. `configs/arb-wiki.json` = the spec's seed block verbatim.

- [ ] **Step 4: Green + full-file + no-regression**

`pytest tests/test_wiki_refresh.py -v` all PASS; `pytest tests/arb_memory/ -q` unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/wiki_refresh.py scripts/arb-wiki-refresh configs/arb-wiki.json tests/test_wiki_refresh.py
git commit -m "$(cat <<'EOF'
feat(wiki): arb-wiki-refresh CLI, remote store script, config seed

Thin CLI holding an exclusive run lock, wiring real callables: git HEAD,
dispatch-dev --adhoc generation, and the ssh store hop whose remote
python3 - script embeds the base64 intent batch as a literal (stdin
delivers the script, so data cannot also ride it -- round-1 agy).
configs/arb-wiki.json seeds the five pilot <workspace> pages.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp
EOF
)"
```

---

## Definition of Done

- [ ] Task 0 red-first pair green; full `tests/arb_memory/` no regressions.
- [ ] All `tests/test_wiki_refresh.py` green (config, validation matrix, intents,
  state machine incl. the resume-without-redispatch proof, store script).
- [ ] Out of scope for the dispatched worker (orchestrator, after merge): deploy the Task-0
  schema/store fix to prod (image rebuild + recreate + one-time
  `python -m arb_memory setup`-equivalent: the idempotent `ALTER ... DROP CONSTRAINT` runs
  when schema.sql is next applied — the plan's orchestrator applies it manually via the
  memory container since prod schema changes are operator steps), then run
  `scripts/arb-wiki-refresh --force --repo workspace-dev` live end-to-end and verify the
  refreshed pages land as version 2 artefacts and the manifest appears in the vault after
  the nightly export (or a manual wrapper run).

## Self-review notes

- Spec coverage: prerequisite fix → Task 0 (both bug halves gated by one test); positional
  hint-linking → `_page_intent` (one intent, artefact+hint) + shape test; batch protocol →
  Task 2 (pending file with FULL intents, resume-without-redispatch test proves the round-2
  codex property, force-new-nonce, atomic writes) + Task 3 lock; validation contract → the
  parametrized matrix incl. cross-repo, bare-token, self, min(2, siblings), final-line;
  stdin fix → `build_store_script` embeds payload (tested: `"sys.stdin" not in script`);
  manifest-last → intents ordering test.
- The `BARE_WIKI_RE` lookarounds reuse the exporter's boundary class so "bare token" means
  exactly what the exporter would fail to link.
- Type consistency: `refresh_repo`'s injected callables match the test Recorder and the CLI
  wiring; `build_intents` output feeds both `pending-<repo>.json` and `store_fn` unchanged.
