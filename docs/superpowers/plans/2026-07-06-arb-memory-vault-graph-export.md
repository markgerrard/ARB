# ARB Memory vault graph export Implementation Plan

> **Status: round 2 panel-confirmed 2026-07-06 — ready to dispatch.** Round 2 (targeted, run
> `panel-vault-graph-plan-r2-20260706T182340Z-3e24cf`): codex `approve`/none, agy
> `approve`/none (executed the blank-line arithmetic and the verbatim-test split logic in
> Python rather than reasoning about it), cold-Opus `approve`/none (confirmed both body
> endings converge on exactly `\n\n`, Task 3 loses nothing vs Task 2, and noted the hoisted
> collision pass is strictly safer than the original interleaved loop). No remaining or new
> findings. Implements the 2-round panel-confirmed design at
> `docs/superpowers/specs/2026-07-06-arb-memory-vault-graph-export-design.md`. Round 1
> (codex `needs-changes`/P2, agy `needs-changes`/P2, cold-Opus `approve`/P2,
> run `panel-vault-graph-plan-20260706T181321Z-d5c24f`): **one substantive P2 found
> independently by all three seats** — the footer emitted a single newline rather than a true
> blank line for bodies lacking a trailing newline, and the guarding test was too weak to
> catch it; fixed with a conditional prepend + a `\n\n`-asserting test covering both body
> endings. Cold-Opus verified everything else cold (E1 regex traced char-by-char, E2 SQL incl.
> degenerate cases, test-vector math, export_vault diff completeness, grants, and that no
> pre-existing test has two artefacts with live hints — the fake_embed distance worry is
> moot). Its two nits also addressed: Task 3 now shows the complete final `export_vault`
> (no fragment splicing), and the body-verbatim-above-marker property gained an explicit test.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the shipped vault exporter so every exported file carries the artefact's
relationship graph: an `aliases` frontmatter entry, plus a generated footer with `## References`
(explicit textual artefact mentions, E1) and `## Related` (pgvector semantic-similarity
neighbours, E2) as stem-targeted wikilinks.

**Architecture:** All changes live in `src/arb_memory/vault_export.py` and its tests. E1 is a
pure-Python scanner over the body using panel-refined lookaround boundaries; E2 is one pgvector
SQL query per artefact over the already-granted `hints`+`artefacts` tables (stored embeddings
compared server-side — no OpenAI call, no privilege change). `export_vault` becomes two-pass:
collect the export-id set first (E1 needs it), then render each file with footer.

**Tech Stack:** Python 3, `psycopg`/pgvector SQL, `pytest`, existing `scratch`/`upsert_artefact`/
`upsert_hint` fixtures/helpers.

## Global Constraints

- Read the design spec in full first — this plan implements it exactly, including every
  panel-round callout (lookaround boundaries with the round-2 sentence-final `.` refinement;
  backtick rules per id shape; `deleted_at IS NULL` in E2; stem-targeted links only; footer
  blank-line rule; env-tunable threshold).
- **Calibrated constants (measured on the real prod corpus 2026-07-06, recorded here per the
  spec's plan-stage mandate):** similarity threshold default **0.35** (distance p05=0.36,
  p10=0.41; at 0.35 → 168 raw edges, 73/91 nodes connected, max raw degree 15; at ≥0.45 the
  single-linkage hub blowup appears: maxdeg 41+), **k=5**. E1 yield under the final rule:
  **55 directed edges, 35 source files, 34 targets** — zero precision loss vs the loose scan
  (the apparent round-1 delta was self-mentions, correctly excluded).
- Body bytes above the footer marker stay verbatim; the footer is recomputed on every run
  (full-rewrite, never appended). `write_text(..., encoding="utf-8")` stays.
- Every new test uses the existing `scratch` fixture; E2 tests use explicit hand-built
  1536-dim vectors, never `fake_embed`-derived strings.

---

### Task 1: `aliases` frontmatter

**Files:**
- Modify: `src/arb_memory/vault_export.py` (`_frontmatter`)
- Test: `tests/arb_memory/test_vault_export.py`

**Interfaces:**
- Produces: frontmatter line `aliases: ["<artefact_id>"]` directly after the `artefact_id`
  line. Nothing else consumes it in-code; it's for viewer-side link autocomplete/resolution.

- [ ] **Step 1: Write the failing test**

Add to `tests/arb_memory/test_vault_export.py`:

```python
def test_frontmatter_carries_aliases_with_artefact_id(scratch, tmp_path):
    upsert_artefact(scratch, "spec-alias", content="body", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-alias")
    assert 'aliases: ["spec-alias"]' in body
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export.py::test_frontmatter_carries_aliases_with_artefact_id -v`
Expected: FAIL (`aliases` not in output).

- [ ] **Step 3: Implement**

In `_frontmatter`, insert one line after the `artefact_id` entry:

```python
        f"artefact_id: {json.dumps(artefact['artefact_id'])}",
        f"aliases: [{json.dumps(artefact['artefact_id'])}]",
```

- [ ] **Step 4: Run the full vault-export test file — all pass**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export.py -v`
Expected: all PASSED (the idempotency test is unaffected — `aliases` is deterministic).

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/vault_export.py tests/arb_memory/test_vault_export.py
git commit -m "feat(arb-memory): aliases frontmatter on vault exports

Convenience layer for viewer-side link autocomplete/alias resolution
(Quartz resolves [[artefact-id]] via aliases; Obsidian surfaces them in
autocomplete only). Emitted graph links never rely on this -- they target
filename stems. Per docs/superpowers/specs/2026-07-06-arb-memory-vault-graph-export-design.md.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp"
```

---

### Task 2: E1 reference scanner + `## References` footer

**Files:**
- Modify: `src/arb_memory/vault_export.py`
- Test: `tests/arb_memory/test_vault_export.py`

**Interfaces:**
- Produces: `_reference_targets(body: str, artefact_id: str, export_ids: set[str]) ->
  list[str]` (sorted target ids); `_stem(artefact_id: str) -> str`;
  `_footer(references: list[str], related: list[tuple[str, float]]) -> str` (empty string when
  both empty); `FOOTER_MARKER` constant. Task 3 threads `related` into `_footer` — in this
  task `export_vault` calls `_footer(references, [])`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/arb_memory/test_vault_export.py` (add `FOOTER_MARKER`, `_stem` to the existing
`from arb_memory.vault_export import ...` line):

```python
def test_references_footer_links_bare_art_hex_and_underscore_ids(scratch, tmp_path):
    upsert_artefact(scratch, "art-0123456789abcdef", content="t1", source="seat", author="mark")
    upsert_artefact(scratch, "project-a_overview", content="t2", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-src",
        content="See art-0123456789abcdef and project-a_overview for context.",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-src")
    assert "## References" in body
    assert f"[[{_stem('art-0123456789abcdef')}|art-0123456789abcdef]]" in body
    assert f"[[{_stem('project-a_overview')}|project-a_overview]]" in body


def test_references_sentence_final_period_still_links(scratch, tmp_path):
    upsert_artefact(scratch, "art-0123456789abcdef", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-sentence",
        content="Recorded as art-0123456789abcdef. Next sentence.",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    assert "## References" in _read(tmp_path, "spec-sentence")


def test_references_hyphen_only_ids_require_backticks(scratch, tmp_path):
    upsert_artefact(scratch, "spec-a", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-bare",
        content="Plain prose mentioning spec-a without intent markers.",
        source="seat", author="mark",
    )
    upsert_artefact(
        scratch, "spec-ticked",
        content="Cited with intent: `spec-a` is the target.",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    assert FOOTER_MARKER not in _read(tmp_path, "spec-bare")
    assert f"[[{_stem('spec-a')}|spec-a]]" in _read(tmp_path, "spec-ticked")


def test_references_negative_cases(scratch, tmp_path):
    upsert_artefact(scratch, "art-0123456789abcdef", content="t", source="seat", author="mark")
    upsert_artefact(scratch, "wiki_page", content="t", source="seat", author="mark")
    upsert_artefact(scratch, "wiki_page_two", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-neg",
        content=(
            "URL https://x.com/art-0123456789abcdef and path docs/art-0123456789abcdef.md "
            "and adjacent xart-0123456789abcdef and wiki_page_two only."
        ),
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-neg")
    # URL-, path-, and adjacent-embedded ids never link:
    assert f"[[{_stem('art-0123456789abcdef')}|" not in body
    # wiki_page must NOT match inside wiki_page_two (prefix collision):
    assert f"[[{_stem('wiki_page')}|wiki_page]]" not in body
    assert f"[[{_stem('wiki_page_two')}|wiki_page_two]]" in body


def test_references_self_mention_and_dedup(scratch, tmp_path):
    upsert_artefact(scratch, "art-aaaaaaaaaaaaaaaa", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-dup",
        content="art-aaaaaaaaaaaaaaaa twice: art-aaaaaaaaaaaaaaaa",
        source="seat", author="mark",
    )
    upsert_artefact(
        scratch, "art-bbbbbbbbbbbbbbbb",
        content="I am art-bbbbbbbbbbbbbbbb and mention only myself.",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    assert _read(tmp_path, "spec-dup").count("[[") == 1
    assert FOOTER_MARKER not in _read(tmp_path, "art-bbbbbbbbbbbbbbbb")


def test_footer_blank_line_separation_regardless_of_body_ending(scratch, tmp_path):
    upsert_artefact(scratch, "art-cccccccccccccccc", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-nonl", content="ends without newline art-cccccccccccccccc",
        source="seat", author="mark",
    )
    upsert_artefact(
        scratch, "spec-withnl", content="ends with newline art-cccccccccccccccc\n",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    # A true BLANK line (\n\n) must precede the marker in both cases -- a single line
    # break is not enough (markdown block parsing; round-1 panel finding, all 3 seats).
    assert f"art-cccccccccccccccc\n\n{FOOTER_MARKER}" in _read(tmp_path, "spec-nonl")
    assert f"art-cccccccccccccccc\n\n{FOOTER_MARKER}" in _read(tmp_path, "spec-withnl")


def test_body_above_footer_marker_is_verbatim_content(scratch, tmp_path):
    content = "line one art-dddddddddddddddd\n\nsome `code` and text\n"
    upsert_artefact(scratch, "art-dddddddddddddddd", content="t", source="seat", author="mark")
    upsert_artefact(scratch, "spec-verbatim", content=content, source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    text = _read(tmp_path, "spec-verbatim")
    above_marker = text.split(FOOTER_MARKER)[0]
    after_frontmatter = above_marker.split("---\n", 2)[2]
    # body bytes stay verbatim; the only addition above the marker is the footer's
    # single leading newline (content already ends with \n, so no extra is prepended)
    assert after_frontmatter == content + "\n"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export.py -v -k "references or footer_separated"`
Expected: ImportError on `FOOTER_MARKER`/`_stem` (not defined yet) — that is the red state.

- [ ] **Step 3: Implement**

In `src/arb_memory/vault_export.py`, add after `_filename`:

```python
ART_ID_RE = re.compile(r"art-[0-9a-f]{16}")
_REF_LEAD = r"(?<![A-Za-z0-9._/-])"
_REF_TRAIL = r"(?![A-Za-z0-9_/-]|\.[A-Za-z0-9._/-])"
FOOTER_MARKER = (
    "<!-- generated by vault_export: graph footer — do not edit; regenerated nightly -->"
)


def _stem(artefact_id: str) -> str:
    return _filename(artefact_id)[: -len(".md")]


def _reference_targets(body: str, artefact_id: str, export_ids: set[str]) -> list[str]:
    targets = []
    for candidate in export_ids:
        if candidate == artefact_id or candidate not in body:
            continue
        escaped = re.escape(candidate)
        patterns = [f"`{escaped}`"]
        if ART_ID_RE.fullmatch(candidate) or "_" in candidate:
            patterns.append(_REF_LEAD + escaped + _REF_TRAIL)
        if any(re.search(pattern, body) for pattern in patterns):
            targets.append(candidate)
    return sorted(targets)


def _footer(references: list[str], related: list[tuple[str, float]]) -> str:
    if not references and not related:
        return ""
    lines = ["", FOOTER_MARKER, ""]
    if references:
        lines.append("## References")
        lines.extend(f"- [[{_stem(t)}|{t}]]" for t in references)
        lines.append("")
    if related:
        lines.append("## Related")
        lines.extend(f"- [[{_stem(t)}|{t}]] (distance {d:.2f})" for t, d in related)
        lines.append("")
    return "\n".join(lines)
```

Replace `export_vault` with the two-pass version (collision check + id-set collection first,
then render; footer starts with `"\n"` via the `lines` list so a body without a trailing
newline is still separated):

```python
def export_vault(conn, vault_root: str) -> dict:
    root = Path(vault_root)
    root.mkdir(parents=True, exist_ok=True)
    exported_at = datetime.now(timezone.utc).isoformat()

    artefacts = _latest_artefacts(conn)
    seen_hashes: dict[str, str] = {}
    for artefact in artefacts:
        artefact_id = artefact["artefact_id"]
        idhash = _idhash(artefact_id)
        if idhash in seen_hashes and seen_hashes[idhash] != artefact_id:
            raise RuntimeError(
                f"idhash collision: {artefact_id!r} and {seen_hashes[idhash]!r} "
                f"both hash to {idhash!r}"
            )
        seen_hashes[idhash] = artefact_id
    export_ids = set(seen_hashes.values())

    written = 0
    for artefact in artefacts:
        artefact_id = artefact["artefact_id"]
        tags = _linked_tags(conn, artefact_id, artefact["version"])
        body = _body(artefact)
        references = _reference_targets(body, artefact_id, export_ids)
        footer = _footer(references, [])
        if footer and not body.endswith("\n"):
            # _footer's leading empty line supplies ONE newline; a body that doesn't end
            # with its own newline needs a second so the marker is preceded by a true
            # BLANK line, not just a line break (round-1 panel finding, all three seats).
            footer = "\n" + footer
        text = _frontmatter(artefact, tags, exported_at) + body + footer
        (root / _filename(artefact_id)).write_text(text, encoding="utf-8")
        written += 1

    return {"written": written}
```

- [ ] **Step 4: Run the full vault-export test file — all pass**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export.py -v`
Expected: all PASSED, including the pre-existing tests (idempotency, hostile-id, collision —
none of them mention export-set ids in their bodies, so no footers appear in them).

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/vault_export.py tests/arb_memory/test_vault_export.py
git commit -m "feat(arb-memory): E1 explicit-reference wikilinks in vault export footer

Panel-refined matching (2-round reviewed): lookaround boundaries instead
of \\b (hyphens are word constituents -- no prefix-collision FPs), URL/path
ids excluded, sentence-final '.' allowed via the refined trailing
lookahead, art-hex and underscore ids match bare, hyphen-only ids require
backticks. Links target filename stems; footer recomputed every run.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp"
```

---

### Task 3: E2 semantic-similarity `## Related` edges

**Files:**
- Modify: `src/arb_memory/vault_export.py`
- Test: `tests/arb_memory/test_vault_export.py`

**Interfaces:**
- Consumes: `_footer` (Task 2). Produces: `_related_artefacts(conn, artefact_id, version, *,
  k: int, threshold: float) -> list[tuple[str, float]]`; `export_vault(conn, vault_root, *,
  similarity_threshold: float = 0.35, related_k: int = 5)`; `VaultExportSettings` gains
  `similarity_threshold: float = 0.35`, resolved from `ARB_VAULT_EXPORT_SIMILARITY_THRESHOLD`.

- [ ] **Step 1: Write the failing tests**

```python
def _unit_vec(index, weight=1.0, second_index=None):
    vec = [0.0] * 1536
    if second_index is None:
        vec[index] = 1.0
    else:
        import math
        vec[index] = weight
        vec[second_index] = math.sqrt(1.0 - weight * weight)
    return vec


def test_related_links_near_pair_excludes_far_and_deleted(scratch, tmp_path):
    # a and b: cosine distance 1 - 0.98 = 0.02 (near). c: orthogonal, distance 1.0 (far).
    # d: identical embedding to a, but soft-deleted -- must not appear.
    _, va = upsert_artefact(scratch, "near-a", content="a", source="seat", author="mark")
    upsert_hint(scratch, "hint a", _unit_vec(0), artefact_id="near-a", artefact_version=va)
    _, vb = upsert_artefact(scratch, "near-b", content="b", source="seat", author="mark")
    upsert_hint(scratch, "hint b", _unit_vec(0, weight=0.98, second_index=1),
                artefact_id="near-b", artefact_version=vb)
    _, vc = upsert_artefact(scratch, "far-c", content="c", source="seat", author="mark")
    upsert_hint(scratch, "hint c", _unit_vec(2), artefact_id="far-c", artefact_version=vc)
    _, vd = upsert_artefact(scratch, "del-d", content="d", source="seat", author="mark")
    hid = upsert_hint(scratch, "hint d", _unit_vec(0), artefact_id="del-d", artefact_version=vd)
    scratch.execute("UPDATE hints SET deleted_at = now() WHERE id = %s", (hid,))

    export_vault(scratch, str(tmp_path))

    body_a = _read(tmp_path, "near-a")
    assert "## Related" in body_a
    assert f"[[{_stem('near-b')}|near-b]]" in body_a
    assert "far-c" not in body_a
    assert "del-d" not in body_a
    # mutuality:
    assert f"[[{_stem('near-a')}|near-a]]" in _read(tmp_path, "near-b")


def test_related_min_aggregation_over_multiple_hints(scratch, tmp_path):
    # multi's hints: one orthogonal, one near anchor -- min-linkage must connect them.
    _, va = upsert_artefact(scratch, "anchor", content="a", source="seat", author="mark")
    upsert_hint(scratch, "anchor hint", _unit_vec(0), artefact_id="anchor", artefact_version=va)
    _, vm = upsert_artefact(scratch, "multi", content="m", source="seat", author="mark")
    upsert_hint(scratch, "multi far hint", _unit_vec(3), artefact_id="multi", artefact_version=vm)
    upsert_hint(scratch, "multi near hint", _unit_vec(0, weight=0.95, second_index=1),
                artefact_id="multi", artefact_version=vm)

    export_vault(scratch, str(tmp_path))

    assert f"[[{_stem('multi')}|multi]]" in _read(tmp_path, "anchor")


def test_similarity_threshold_env_resolution():
    from arb_memory.vault_export import resolve_settings

    env = {"ARB_VAULT_EXPORT_DSN": "d", "ARB_VAULT_EXPORT_ROOT": "r"}
    assert resolve_settings(env).similarity_threshold == 0.35
    env["ARB_VAULT_EXPORT_SIMILARITY_THRESHOLD"] = "0.5"
    assert resolve_settings(env).similarity_threshold == 0.5
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export.py -v -k "related or threshold_env"`
Expected: FAIL/AttributeError (no `similarity_threshold`, no `## Related`).

- [ ] **Step 3: Implement**

Settings:

```python
@dataclass
class VaultExportSettings:
    dsn: str
    vault_root: str
    similarity_threshold: float = 0.35


def resolve_settings(env) -> VaultExportSettings:
    return VaultExportSettings(
        dsn=env["ARB_VAULT_EXPORT_DSN"],
        vault_root=env["ARB_VAULT_EXPORT_ROOT"],
        similarity_threshold=float(env.get("ARB_VAULT_EXPORT_SIMILARITY_THRESHOLD", "0.35")),
    )
```

Query (after `_linked_tags`):

```python
def _related_artefacts(conn, artefact_id: str, version: int, *, k: int, threshold: float) -> list[tuple[str, float]]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (artefact_id) artefact_id, version
            FROM artefacts ORDER BY artefact_id, version DESC
        ),
        mine AS (
            SELECT embedding FROM hints
            WHERE artefact_id = %s AND artefact_version = %s AND deleted_at IS NULL
        ),
        others AS (
            SELECT h.artefact_id AS aid, h.embedding
            FROM hints h JOIN latest l
              ON h.artefact_id = l.artefact_id AND h.artefact_version = l.version
            WHERE h.deleted_at IS NULL AND h.artefact_id <> %s
        )
        SELECT o.aid, MIN(o.embedding <=> m.embedding) AS dist
        FROM others o CROSS JOIN mine m
        GROUP BY o.aid
        HAVING MIN(o.embedding <=> m.embedding) <= %s
        ORDER BY 2 ASC, 1 ASC
        LIMIT %s
        """,
        (artefact_id, version, artefact_id, threshold, k),
    ).fetchall()
    return [(row[0], float(row[1])) for row in rows]
```

Replace `export_vault` in full with its final form (complete function, no splicing — round-1
cold-Opus nit):

```python
def export_vault(conn, vault_root: str, *, similarity_threshold: float = 0.35, related_k: int = 5) -> dict:
    root = Path(vault_root)
    root.mkdir(parents=True, exist_ok=True)
    exported_at = datetime.now(timezone.utc).isoformat()

    artefacts = _latest_artefacts(conn)
    seen_hashes: dict[str, str] = {}
    for artefact in artefacts:
        artefact_id = artefact["artefact_id"]
        idhash = _idhash(artefact_id)
        if idhash in seen_hashes and seen_hashes[idhash] != artefact_id:
            raise RuntimeError(
                f"idhash collision: {artefact_id!r} and {seen_hashes[idhash]!r} "
                f"both hash to {idhash!r}"
            )
        seen_hashes[idhash] = artefact_id
    export_ids = set(seen_hashes.values())

    written = 0
    for artefact in artefacts:
        artefact_id = artefact["artefact_id"]
        tags = _linked_tags(conn, artefact_id, artefact["version"])
        body = _body(artefact)
        references = _reference_targets(body, artefact_id, export_ids)
        related = _related_artefacts(
            conn, artefact_id, artefact["version"], k=related_k, threshold=similarity_threshold
        )
        footer = _footer(references, related)
        if footer and not body.endswith("\n"):
            # _footer's leading empty line supplies ONE newline; a body that doesn't end
            # with its own newline needs a second so the marker is preceded by a true
            # BLANK line, not just a line break (round-1 panel finding, all three seats).
            footer = "\n" + footer
        text = _frontmatter(artefact, tags, exported_at) + body + footer
        (root / _filename(artefact_id)).write_text(text, encoding="utf-8")
        written += 1

    return {"written": written}
```

`main()` threads the setting:

```python
        result = export_vault(conn, settings.vault_root, similarity_threshold=settings.similarity_threshold)
```

- [ ] **Step 4: Full file green, then whole-suite regression**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export.py -v`
Expected: all PASSED (pre-existing tests get artefacts without hints or with `fake_embed`
hints whose distances overwhelmingly exceed 0.35 — but do NOT rely on that: if any
pre-existing test breaks because two fixture artefacts land within 0.35, fix by passing
`similarity_threshold=0.0` is wrong (excludes nothing — 0.0 admits only identical); instead
pass an explicit `related_k=0` in that test's `export_vault` call and note it).
Then: `.venv/bin/python3 -m pytest tests/arb_memory/ -q` — expect same green count as base
plus the new tests.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/vault_export.py tests/arb_memory/test_vault_export.py
git commit -m "feat(arb-memory): E2 semantic Related edges in vault export footer

Min-pairwise-hint cosine distance over stored pgvector embeddings, top-5
under an env-tunable threshold (ARB_VAULT_EXPORT_SIMILARITY_THRESHOLD,
default 0.35 -- calibrated on the real corpus: 73/91 nodes connected, max
raw degree 15; >=0.45 shows single-linkage hub blowup). deleted_at
filtered like every other hint read path. No new embeddings, no OPENAI
dependency, no privilege change.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp"
```

---

## Definition of Done

- [ ] All three tasks' tests green; `.venv/bin/python3 -m pytest tests/arb_memory/ -q` shows
  no regressions.
- [ ] Body bytes above `FOOTER_MARKER` remain byte-identical to `content` (existing
  idempotency test still green).
- [ ] Commits reference the spec's panel provenance.
- [ ] Out of scope for the dispatched worker (orchestrator ops, after merge): redeploy prod
  (`git pull` + `docker compose build memory` + `up -d --force-recreate`), rerun the export,
  and spot-check a well-connected artefact's footer against the calibration numbers (≈55 E1
  edges corpus-wide; `## Related` present on ~73 of 91 files).

## Self-review notes

- Spec coverage: E1 rules (all five bullets incl. round-2 `.` refinement) → Task 2 scanner +
  its six tests; E2 (min-linkage, deleted_at, env threshold, k=5, deterministic ordering) →
  Task 3; aliases → Task 1; footer marker/blank-line/empty-omission → Task 2 `_footer`;
  calibration mandate → Global Constraints (numbers recorded).
- Longest-first scanning from the spec is satisfied vacuously: candidates are tested
  independently with lookarounds (no positional consumption), so scan order cannot change the
  result; noted here so a reviewer doesn't flag the absence of an explicit sort.
- Type consistency: `_related_artefacts` returns `list[tuple[str, float]]`, exactly what
  `_footer`'s `related` parameter formats; `_stem` is shared by tests and implementation.
