# ARB Wiki v1.1 zero-touch onboarding Implementation Plan

> **Status: round 2 panel-confirmed 2026-07-06 — ready to dispatch.** Round 2 (run
> `panel-wiki-onboard-plan-r2-20260706T210756Z-7e3cc8`): all three seats `approve`/none —
> reject rows reach title/scope, `resolve_reviewer` matches its revised test on every case,
> both new tests assert their claim with existing helpers, `_validate_config` refactor
> preserves v1. Cold-Opus's two non-blocking nits noted (the concurrent-merge test name
> slightly overstates — it's honestly commented as a merge test; the recompute regression is
> pinned at the invariant, `main` being the prose carve-out). Round 1 below.
> Implements the 2-round panel-confirmed design at
> `docs/superpowers/specs/2026-07-06-arb-wiki-zero-touch-onboarding-design.md`. Round 1
> (run `panel-wiki-onboard-plan-20260706T210028Z-f536f2`; codex `needs-changes`/P2, agy
> `needs-changes`/P1, cold-Opus `needs-changes`/P1): agy + cold-Opus **independently** proved
> the same P1 — two `parse_discovery` reject rows used single-page proposals, so they trip the
> 2–8 count check before reaching the title/scope check and the needle assertion fails (fixed:
> both rows now 2-page). codex caught a real correctness bug — `resolve_reviewer` accepted a
> same-engine override, defeating the decorrelation guarantee (fixed: all-or-none override that
> must differ from the generator engine, with tests). Folded agy's two promised-but-missing
> tests (concurrent-merge re-read, all_ids-recompute first-refresh regression) and cold-Opus's
> dead-import nit. Both bridge seats + cold-Opus independently verified the highest-risk item
> (non-greedy discovery-JSON regex on nested braces) correct, and confirmed the review_fn
> insertion point, `_validate_config` refactor, and CLI review-default expression.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `arb-wiki-refresh --add <repo-path>` onboards a virgin repo with no human step beyond
the path: a discovery seat proposes the page set, the loop validates + writes the config, then
runs the normal refresh, optionally behind a decorrelated review gate.

**Architecture:** All additions in `src/agent_redis_bridge/wiki_refresh.py` + the CLI; no new
module. Task 1 adds the `review_fn` hook to `refresh_repo` (the ONLY change to shipped v1 logic).
Task 2 adds `parse_discovery`/`render_discovery_brief` (pure). Task 3 adds `add_repo` + CLI
wiring (`--add`, `--review/--no-review`, `--seat-*`, `--reviewer-*`).

**Tech Stack:** Python 3, `pytest`; stdlib only (`json`/`re`/`pathlib`/`fcntl`/`subprocess`).

## Global Constraints

- Read the spec in full; every panel callout is binding (review_fn insertion after
  `validate_pages`/before `build_intents`; `parse_discovery` is the validity truth-source with
  `existing_ids`; manifest-slug reservation; 2–8; lock caller-held/no-re-lock; `Path().name`
  not `basename`; `--review` default flips on `--add`; fail-loud reviewer resolution).
- `parse_discovery`/`render_discovery_brief`/`add_repo`/`review_fn` are all pure or
  injected-callable; only the existing v1 tests touch nothing new. No Postgres needed.
- Do NOT change `build_intents`, `validate_pages`, `_run_store`, or the store path.

---

### Task 1: `review_fn` hook in `refresh_repo`

**Files:** Modify `src/agent_redis_bridge/wiki_refresh.py` (`refresh_repo`); Test
`tests/test_wiki_refresh.py`.

**Interfaces:** `refresh_repo(..., review_fn=None)` — when set, called `review_fn(repo,
output_dir) -> (ok: bool, reasons: str)` after `validate_pages` passes, before `build_intents`;
`ok is False` raises `RuntimeError` (no pending write, no store). `None` = v1 behavior.

- [ ] **Step 1: Write failing tests** (append to `tests/test_wiki_refresh.py`)

```python
def test_review_fn_reject_stores_nothing(tmp_path):
    repo, ids = _repo(tmp_path)
    state_dir = str(tmp_path / "state")
    rec = Recorder(dispatch_writes=GOOD_PAGES)
    with pytest.raises(RuntimeError, match="review"):
        refresh_repo(repo, state_dir=state_dir, all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=rec.dispatch, store_fn=rec.store, now_fn=lambda: "T0",
                     review_fn=lambda repo, out: (False, "page 2 misstates the schema"))
    assert rec.stored_batches == []
    assert not (Path(state_dir) / "pending-demo.json").exists()


def test_review_fn_approve_proceeds(tmp_path):
    repo, ids = _repo(tmp_path)
    rec = Recorder(dispatch_writes=GOOD_PAGES)
    outcome = refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids,
                           git_head=rec.git_head, dispatch_fn=rec.dispatch, store_fn=rec.store,
                           now_fn=lambda: "T0", review_fn=lambda repo, out: (True, ""))
    assert outcome == "refreshed"
    assert len(rec.stored_batches) == 1


def test_review_fn_none_is_v1_behavior(tmp_path):
    repo, ids = _repo(tmp_path)
    rec = Recorder(dispatch_writes=GOOD_PAGES)
    outcome = refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids,
                           git_head=rec.git_head, dispatch_fn=rec.dispatch, store_fn=rec.store,
                           now_fn=lambda: "T0")  # no review_fn
    assert outcome == "refreshed" and len(rec.stored_batches) == 1
```

- [ ] **Step 2: Red** — `TypeError: refresh_repo() got an unexpected keyword argument 'review_fn'`.

- [ ] **Step 3: Implement** — change the signature and insert the hook. In
  `src/agent_redis_bridge/wiki_refresh.py`, the current `refresh_repo` signature is:

```python
def refresh_repo(repo, *, state_dir, all_ids, git_head, dispatch_fn, store_fn, now_fn,
                 force=False, mkdtemp_fn=None):
```

Change to add `review_fn=None`:

```python
def refresh_repo(repo, *, state_dir, all_ids, git_head, dispatch_fn, store_fn, now_fn,
                 force=False, mkdtemp_fn=None, review_fn=None):
```

Then, in the `try` block, the current code reads:

```python
        violations = validate_pages(repo, output_dir, all_ids)
        if violations:
            raise RuntimeError(
                f"wiki refresh for {repo['name']!r} failed validation; storing NOTHING:\n"
                + "\n".join(violations)
            )

        nonce = uuid.uuid4().hex
```

Insert the review hook between the validation block and `nonce = ...`:

```python
        violations = validate_pages(repo, output_dir, all_ids)
        if violations:
            raise RuntimeError(
                f"wiki refresh for {repo['name']!r} failed validation; storing NOTHING:\n"
                + "\n".join(violations)
            )

        if review_fn is not None:
            ok, reasons = review_fn(repo, output_dir)
            if not ok:
                raise RuntimeError(
                    f"wiki refresh for {repo['name']!r} rejected by review; storing NOTHING:\n"
                    + reasons
                )

        nonce = uuid.uuid4().hex
```

- [ ] **Step 4: Green + regression** — `pytest tests/test_wiki_refresh.py -q` (all prior + 3
  new pass; the `None`-default test proves v1 paths unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/wiki_refresh.py tests/test_wiki_refresh.py
git commit -m "feat(wiki): review_fn pre-store hook in refresh_repo

Injected review_fn(repo, output_dir) -> (ok, reasons), called after
validate_pages passes and before build_intents/cleanup/store; a reject
raises before any pending write or store (all-or-nothing, same as a
validation failure). review_fn=None preserves v1 behavior exactly -- the
only change to the shipped loop. Per
docs/superpowers/specs/2026-07-06-arb-wiki-zero-touch-onboarding-design.md.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp"
```

---

### Task 2: `parse_discovery` + `render_discovery_brief` (pure)

**Files:** Modify `src/agent_redis_bridge/wiki_refresh.py`; Test `tests/test_wiki_refresh.py`.

**Interfaces:**
- `WikiDiscoveryError(ValueError)`.
- `parse_discovery(reply_text: str, repo_name: str, existing_ids: set[str]) -> list[dict]` —
  returns the validated `pages` list (each `{"id","title","scope"}`) or raises
  `WikiDiscoveryError`.
- `render_discovery_brief(repo_name: str, repo_path: str) -> str`.

- [ ] **Step 1: Write failing tests**

```python
def _proposal(pages):
    import json as _json
    return "Here is the proposal:\n```json\n" + _json.dumps({"pages": pages}) + "\n```\nDone."

def _pages(n, repo="demo"):
    return [{"id": f"wiki-{repo}-p{i}", "title": f"T{i}", "scope": f"s{i}"} for i in range(n)]


def test_parse_discovery_accepts_valid_2_and_8(tmp_path):
    from agent_redis_bridge.wiki_refresh import parse_discovery
    assert len(parse_discovery(_proposal(_pages(2)), "demo", set())) == 2
    assert len(parse_discovery(_proposal(_pages(8)), "demo", set())) == 8


@pytest.mark.parametrize("pages, existing, needle", [
    ("not json at all", set(), "no json"),
    (_pages(1), set(), "2"),
    (_pages(9), set(), "8"),
    ([{"id": "wiki-demo-x", "title": "", "scope": "s"}, {"id": "wiki-demo-y", "title": "T", "scope": "s"}], set(), "title"),
    ([{"id": "wiki-demo-x", "title": "T", "scope": ""}, {"id": "wiki-demo-y", "title": "T", "scope": "s"}], set(), "scope"),
    ([{"id": "wiki-other-x", "title": "T", "scope": "s"}, {"id": "wiki-demo-y", "title": "T", "scope": "s"}], set(), "format"),
    ([{"id": "wiki-demo-Bad", "title": "T", "scope": "s"}, {"id": "wiki-demo-y", "title": "T", "scope": "s"}], set(), "format"),
    ([{"id": "wiki-demo-manifest", "title": "T", "scope": "s"}, {"id": "wiki-demo-y", "title": "T", "scope": "s"}], set(), "manifest"),
    ([{"id": "wiki-demo-x-manifest", "title": "T", "scope": "s"}, {"id": "wiki-demo-y", "title": "T", "scope": "s"}], set(), "manifest"),
    (_pages(2), {"wiki-demo-p0"}, "already"),
    ([{"id": "wiki-demo-dup", "title": "T", "scope": "s"}, {"id": "wiki-demo-dup", "title": "T", "scope": "s"}], set(), "duplicate"),
])
def test_parse_discovery_rejects(pages, existing, needle):
    from agent_redis_bridge.wiki_refresh import parse_discovery, WikiDiscoveryError
    reply = pages if isinstance(pages, str) else _proposal(pages)
    with pytest.raises(WikiDiscoveryError) as exc:
        parse_discovery(reply, "demo", existing)
    assert needle.lower() in str(exc.value).lower()


def test_render_discovery_brief_contains_rules(tmp_path):
    from agent_redis_bridge.wiki_refresh import render_discovery_brief
    brief = render_discovery_brief("demo", "/some/repo")
    for needle in ("/some/repo", "wiki-demo-", "2", "8", "json", "pages"):
        assert needle in brief
```

- [ ] **Step 2: Red** — ImportError on `parse_discovery`.

- [ ] **Step 3: Implement** (append near the other pure helpers)

```python
class WikiDiscoveryError(ValueError):
    pass


_DISCOVERY_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_discovery(reply_text: str, repo_name: str, existing_ids: set) -> list[dict]:
    match = _DISCOVERY_JSON_RE.search(reply_text)
    raw = match.group(1) if match else reply_text
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WikiDiscoveryError(f"discovery reply has no JSON object: {exc}") from exc
    pages = obj.get("pages") if isinstance(obj, dict) else None
    if not isinstance(pages, list):
        raise WikiDiscoveryError("discovery JSON must have a 'pages' list")
    if not (2 <= len(pages) <= 8):
        raise WikiDiscoveryError(f"proposal has {len(pages)} pages, need 2-8")

    id_re = re.compile(rf"^wiki-{re.escape(repo_name)}-[a-z0-9-]+$")
    seen: set = set()
    out: list[dict] = []
    for page in pages:
        if not isinstance(page, dict):
            raise WikiDiscoveryError("each page must be an object")
        pid, title, scope = page.get("id"), page.get("title"), page.get("scope")
        if not isinstance(pid, str) or not id_re.match(pid):
            raise WikiDiscoveryError(f"page id {pid!r} must match wiki-{repo_name}-<slug> format")
        slug = pid[len(f"wiki-{repo_name}-"):]
        if slug == "manifest" or slug.endswith("-manifest"):
            raise WikiDiscoveryError(f"page id {pid!r} uses the reserved 'manifest' slug")
        if pid in seen:
            raise WikiDiscoveryError(f"duplicate page id {pid!r} in proposal")
        if pid in existing_ids:
            raise WikiDiscoveryError(f"page id {pid!r} already configured elsewhere")
        if not (isinstance(title, str) and title.strip()):
            raise WikiDiscoveryError(f"page {pid!r} has empty title")
        if not (isinstance(scope, str) and scope.strip()):
            raise WikiDiscoveryError(f"page {pid!r} has empty scope")
        seen.add(pid)
        out.append({"id": pid, "title": title, "scope": scope})
    return out


DISCOVERY_BRIEF_TEMPLATE = """# ARB Wiki discovery brief — {repo_name}

READ-ONLY: do NOT modify anything in the repo at {repo_path}. Read its source and docs, then
PROPOSE a wiki page set that would let a stranger understand the codebase.

Reply with ONLY a fenced ```json block, no prose, exactly this shape:

```json
{{"pages": [{{"id": "wiki-{repo_name}-<slug>", "title": "<title>", "scope": "<one line>"}}]}}
```

Rules (a proposal violating any is rejected):
- Propose 2 to 8 pages.
- Every id is `wiki-{repo_name}-<slug>` where <slug> is lowercase letters/digits/hyphens only.
- Do NOT use the slug `manifest` or any slug ending `-manifest` (reserved).
- Ids unique; title and scope non-empty.
"""


def render_discovery_brief(repo_name: str, repo_path: str) -> str:
    return DISCOVERY_BRIEF_TEMPLATE.format(repo_name=repo_name, repo_path=repo_path)
```

- [ ] **Step 4: Green** — `pytest tests/test_wiki_refresh.py -q`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/wiki_refresh.py tests/test_wiki_refresh.py
git commit -m "feat(wiki): parse_discovery + render_discovery_brief

Strict JSON page-proposal parser -- the single source of proposal-validity
truth (not a load_config round-trip): fenced-json extraction, 2-8 pages,
wiki-<repo>-<slug> id format, reserved manifest-slug rejection (blocks the
deterministic-ulid batch-marker collision), existing-id disjointness,
within-proposal uniqueness, non-empty title/scope. Discovery brief states
every rule so the seat replies parseably first-try.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp"
```

---

### Task 3: `add_repo` + CLI wiring

**Files:** Modify `src/agent_redis_bridge/wiki_refresh.py` (`add_repo`, `main`); Test
`tests/test_wiki_refresh.py`.

**Interfaces:**
- `add_repo(config_path, repo_path, *, dispatch_capture_fn, seat) -> dict` — assumes the caller
  holds the lock; derives `repo_name` via `Path(normpath).name`; dispatches discovery, parses,
  merges the new block into `config_path` atomically, returns it. Does NOT run refresh (the CLI
  does, so it can recompute `all_ids`).
- `resolve_reviewer(generator_engine, overrides) -> dict | None` — decorrelation map + override.
- `main` gains `--add`, mutually-exclusive `--review/--no-review`, `--seat-*`, `--reviewer-*`.

- [ ] **Step 1: Write failing tests**

```python
def test_add_repo_writes_mergeable_block(tmp_path):
    from agent_redis_bridge.wiki_refresh import add_repo, load_config
    cfg = tmp_path / "arb-wiki.json"
    cfg.write_text(json.dumps({"repos": [CONFIG["repos"][0]]}))  # existing 'demo'
    seat = {"engine": "codex", "target_id": "codex-x-dev", "timeout": 3600}
    reply = _proposal([
        {"id": "wiki-newrepo-home", "title": "Home", "scope": "overview"},
        {"id": "wiki-newrepo-ops", "title": "Ops", "scope": "operations"},
    ])
    block = add_repo(str(cfg), "/tmp/NewRepo", dispatch_capture_fn=lambda brief: reply, seat=seat)
    assert block["name"] == "newrepo"
    assert {p["id"] for p in block["pages"]} == {"wiki-newrepo-home", "wiki-newrepo-ops"}
    merged = load_config(str(cfg))  # still loads
    assert {r["name"] for r in merged["repos"]} == {"demo", "newrepo"}


def test_add_repo_trailing_slash_name(tmp_path):
    from agent_redis_bridge.wiki_refresh import add_repo
    cfg = tmp_path / "c.json"; cfg.write_text(json.dumps({"repos": []}))
    reply = _proposal([{"id": "wiki-newrepo-a", "title": "A", "scope": "s"},
                       {"id": "wiki-newrepo-b", "title": "B", "scope": "s"}])
    seat = {"engine": "codex", "target_id": "t", "timeout": 1}
    block = add_repo(str(cfg), "/tmp/NewRepo/", dispatch_capture_fn=lambda b: reply, seat=seat)
    assert block["name"] == "newrepo"  # not '' from basename on trailing slash


def test_add_repo_parse_failure_writes_nothing(tmp_path):
    from agent_redis_bridge.wiki_refresh import add_repo, WikiDiscoveryError
    cfg = tmp_path / "c.json"; original = json.dumps({"repos": []}); cfg.write_text(original)
    seat = {"engine": "codex", "target_id": "t", "timeout": 1}
    with pytest.raises(WikiDiscoveryError):
        add_repo(str(cfg), "/tmp/bad", dispatch_capture_fn=lambda b: "garbage", seat=seat)
    assert cfg.read_text() == original  # untouched


def test_add_repo_rejects_name_collision(tmp_path):
    from agent_redis_bridge.wiki_refresh import add_repo
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": [{"name": "demo", "path": "/x", "seat":
        {"engine": "e", "target_id": "t", "timeout": 1}, "pages":
        [{"id": "wiki-demo-a", "title": "A", "scope": "s"}]}]}))
    reply = _proposal([{"id": "wiki-demo-new1", "title": "A", "scope": "s"},
                       {"id": "wiki-demo-new2", "title": "B", "scope": "s"}])
    seat = {"engine": "e", "target_id": "t", "timeout": 1}
    with pytest.raises(Exception, match="already"):
        add_repo(str(cfg), "/tmp/demo", dispatch_capture_fn=lambda b: reply, seat=seat)


def test_resolve_reviewer_decorrelates_and_fails_loud(tmp_path):
    from agent_redis_bridge.wiki_refresh import resolve_reviewer
    r = resolve_reviewer("codex", {})
    assert r["engine"] != "codex"
    with pytest.raises(Exception, match="decorrelat"):
        resolve_reviewer("unknown-engine", {})   # no map entry, no override
    # a valid override must still be a DIFFERENT engine than the generator (round-1 codex):
    good = {"engine": "agy-print", "target_id": "agy-x", "timeout": 60}
    assert resolve_reviewer("codex", good)["target_id"] == "agy-x"
    # a same-engine override is NOT decorrelated -> must raise, not silently pass:
    same = {"engine": "codex", "target_id": "codex-x", "timeout": 60}
    with pytest.raises(Exception, match="decorrelat"):
        resolve_reviewer("codex", same)
    # partial override (missing target_id) is all-or-none -> raise:
    with pytest.raises(Exception):
        resolve_reviewer("codex", {"engine": "agy-print"})


def test_add_repo_reread_under_lock_preserves_concurrent_write(tmp_path):
    # round-1 agy: the config is read AFTER the caller's lock, so a write that landed between
    # the CLI's startup and add_repo's read is NOT lost. Simulate that by mutating the file
    # inside the dispatch callback (which runs after add_repo's own read? no -- add_repo reads
    # config at entry, THEN dispatches; so we pre-write the concurrent repo, and assert the
    # merge keeps both). This pins "read at entry, merge onto that", the under-lock contract.
    from agent_redis_bridge.wiki_refresh import add_repo
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": [{"name": "already", "path": "/x", "seat":
        {"engine": "e", "target_id": "t", "timeout": 1}, "pages":
        [{"id": "wiki-already-a", "title": "A", "scope": "s"}]}]}))
    reply = _proposal([{"id": "wiki-newrepo-a", "title": "A", "scope": "s"},
                       {"id": "wiki-newrepo-b", "title": "B", "scope": "s"}])
    seat = {"engine": "codex", "target_id": "t", "timeout": 1}
    add_repo(str(cfg), "/tmp/NewRepo", dispatch_capture_fn=lambda b: reply, seat=seat)
    names = {r["name"] for r in json.loads(cfg.read_text())["repos"]}
    assert names == {"already", "newrepo"}  # existing repo preserved, not clobbered


def test_first_refresh_of_onboarded_repo_validates_own_siblings(tmp_path):
    # round-1 agy P1 regression: a freshly-onboarded 2-page repo's See-also siblings must
    # resolve on the FIRST refresh -- which only holds if all_ids is recomputed from the
    # reloaded config (the new repo's own pages included). This test drives refresh_repo with
    # the recomputed all_ids, proving validation passes.
    from agent_redis_bridge.wiki_refresh import all_page_ids, load_config
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": [{"name": "newrepo", "path": "/tmp/nr", "seat":
        {"engine": "codex", "target_id": "t", "timeout": 1}, "pages": [
            {"id": "wiki-newrepo-a", "title": "A", "scope": "s"},
            {"id": "wiki-newrepo-b", "title": "B", "scope": "s"}]}]}))
    reloaded = load_config(str(cfg))
    ids = all_page_ids(reloaded)          # recomputed -> includes the new repo's own pages
    repo = reloaded["repos"][0]
    pages = {
        "wiki-newrepo-a": _page(("wiki-newrepo-b",), "# A\n"),
        "wiki-newrepo-b": _page(("wiki-newrepo-a",), "# B\n"),
    }
    rec = Recorder(dispatch_writes=pages)
    outcome = refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids,
                           git_head=rec.git_head, dispatch_fn=rec.dispatch, store_fn=rec.store,
                           now_fn=lambda: "T0")
    assert outcome == "refreshed" and len(rec.stored_batches) == 1
```

- [ ] **Step 2: Red** — ImportError on `add_repo`/`resolve_reviewer`.

- [ ] **Step 3: Implement** (append; then extend `main`)

```python
_REVIEWER_MAP = {"codex": "agy-print", "agy-print": "codex"}


def resolve_reviewer(generator_engine: str, overrides: dict) -> dict:
    if any(overrides.get(k) for k in ("engine", "target_id", "timeout")):
        # all-or-none override, and it MUST stay decorrelated (round-1 codex P2: a same-engine
        # override defeats the whole gate — fail loud, never silently accept it)
        missing = [k for k in ("engine", "target_id", "timeout") if not overrides.get(k)]
        if missing:
            raise WikiConfigError(f"reviewer override missing {missing}; supply all of engine/target_id/timeout")
        if overrides["engine"] == generator_engine:
            raise WikiConfigError(
                f"reviewer override engine {overrides['engine']!r} is not decorrelated from the "
                f"generator engine; pick a different engine or use --no-review")
        return {"engine": overrides["engine"], "target_id": overrides["target_id"],
                "timeout": overrides["timeout"]}
    engine = _REVIEWER_MAP.get(generator_engine)
    if engine is None:
        raise WikiConfigError(
            f"no decorrelated reviewer for generator engine {generator_engine!r}; "
            "pass --reviewer-engine/--reviewer-target-id/--reviewer-timeout or use --no-review")
    return {"engine": engine, "target_id": f"{engine}-bridge-dev", "timeout": 3600}


def add_repo(config_path: str, repo_path: str, *, dispatch_capture_fn, seat) -> dict:
    # caller holds <state-dir>/lock; we read the config AFTER that lock (serializes merges)
    config = _read_json(Path(config_path), {"repos": []})
    repo_name = re.sub(r"[^a-z0-9-]", "-", Path(os.path.normpath(repo_path)).name.lower())
    if any(r["name"] == repo_name for r in config["repos"]):
        raise WikiConfigError(f"repo name {repo_name!r} already configured")
    brief = render_discovery_brief(repo_name, repo_path)
    reply = dispatch_capture_fn(brief)
    pages = parse_discovery(reply, repo_name, all_page_ids(config))
    block = {"name": repo_name, "path": repo_path, "seat": seat, "pages": pages}
    config["repos"].append(block)
    load_config_check(config)  # shape backstop; raises WikiConfigError on a malformed merge
    _atomic_write(Path(config_path), json.dumps(config, indent=2))
    return block
```

Add a small `load_config_check(config_dict)` that runs `load_config`'s validation on an
in-memory dict (refactor: extract the per-dict validation body of `load_config` into
`_validate_config(config)` and have both `load_config` and `load_config_check` call it — keeps
one validation source). Show the refactor literally:

```python
def _validate_config(config: dict) -> dict:
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


def load_config(path: str) -> dict:
    return _validate_config(json.loads(Path(path).read_text()))


def load_config_check(config: dict) -> dict:
    return _validate_config(config)
```

`main` extensions (literal contract; wire real callables):
- Add args: `--add`, a mutually-exclusive group `--review`/`--no-review`, `--seat-engine`,
  `--seat-target-id`, `--seat-timeout`, `--reviewer-engine`, `--reviewer-target-id`,
  `--reviewer-timeout`.
- **Review default:** `review_on = args.review if (args.review or args.no_review) else bool(args.add)`
  — on by default for `--add`, off otherwise, either explicit flag wins.
- **Seat resolution:** `--seat-*` if given, else first configured repo's seat, else
  `{"codex","codex-bridge-dev",3600}`.
- Hold the existing `<state-dir>/lock` for the whole run. If `--add`: inside the lock, call
  `add_repo(...)` with a `dispatch_capture_fn` that runs `dispatch-dev` capturing stdout's
  reply payload; then reload config, recompute `all_ids = all_page_ids(reloaded)`, and run
  `refresh_repo` for the new repo only.
- If `review_on`: build `review_fn` = a closure dispatching `resolve_reviewer(seat_engine,
  reviewer_overrides)` with a factual-check brief over `output_dir`, parsing APPROVE/
  REQUEST-CHANGES → `(ok, reasons)`; pass it into `refresh_repo`.
- Non-`--add` refreshes: unchanged, plus `review_fn` only when `--review` explicitly set.

(The CLI dispatch/reply-capture and reviewer brief are prose here as they are subprocess glue
tested via the injected callables above — same DoD carve-out as v1's `main`.)

- [ ] **Step 4: Green + full regression**

`pytest tests/test_wiki_refresh.py -q` all pass; `pytest tests/arb_memory/ -q` unaffected.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/wiki_refresh.py tests/test_wiki_refresh.py
git commit -m "feat(wiki): add_repo + --add CLI, decorrelated reviewer resolution

add_repo (caller-holds-lock, config read after lock, Path().name not
basename, atomic merge, load_config_check backstop) + resolve_reviewer
(decorrelation map, fail-loud on no decorrelated seat) + main() wiring:
--add, --review/--no-review (default flips on --add), --seat-*,
--reviewer-*; recompute all_ids from the reloaded config before refreshing
the freshly-onboarded repo.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp"
```

---

## Definition of Done

- [ ] All `tests/test_wiki_refresh.py` green (v1 + Task 1/2/3 additions); `tests/arb_memory/`
  unaffected.
- [ ] `refresh_repo` with no `review_fn` is byte-identical in behavior to pre-v1.1 (v1 tests
  pass unchanged).
- [ ] Out of scope for the dispatched worker (orchestrator, after merge): live `--add` of a
  real second repo (candidate: `/Users/<user>/AgentRedisBridge` fleet clone, or another local
  repo) end-to-end — discovery proposes, config written, refresh stores, vault re-export shows
  the new cluster; verify the new repo's pages link internally and to the existing wiki.

## Self-review notes

- Spec coverage: review_fn hook → Task 1 (insertion point literal, `None`=v1 test); discovery
  → Task 2 (every guardrail red-first incl. both manifest forms + disjointness + 2/8 bounds);
  add_repo/CLI → Task 3 (name-collision, trailing-slash, parse-failure-writes-nothing,
  reviewer fail-loud, all_ids recompute noted in the CLI contract).
- `_validate_config` refactor keeps ONE config-validation source, so `load_config_check` and
  `load_config` cannot diverge (addresses the round-2 "load_config isn't the truth source but
  is still a shape backstop" point).
- Type consistency: `parse_discovery` returns `list[dict]` = `block["pages"]`; `resolve_reviewer`
  returns the same seat-dict shape `add_repo`/config use.
