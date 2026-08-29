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


def test_render_brief_boundary_rule_and_sibling_repos(tmp_path):
    # cross-repo boundary awareness (claude.ai reader feedback, 2026-07-07): a page about a
    # subsystem whose code lives in ANOTHER repo must say so, or an orienting agent burns
    # context grepping this tree for code that isn't here. The brief must carry the rule
    # always, and name sibling wiki repos when given them.
    config = load_config(_write_config(tmp_path))
    brief = render_brief(config["repos"][0], "/tmp/outdir")
    assert "not in this repo" in brief.lower()          # boundary rule always present
    sibling_ids = {"wiki-other-home", "wiki-other-ops"}
    brief_with = render_brief(config["repos"][0], "/tmp/outdir", sibling_ids=sibling_ids)
    assert "other" in brief_with                        # sibling repo named
    assert "wiki-other-home" in brief_with              # citable sibling page ids listed


def test_refresh_brief_carries_sibling_ids(tmp_path):
    # refresh_repo knows all_ids; the generation brief must receive the OTHER repos' page
    # ids so pages can cite genuinely-related sibling pages (validator already accepts them)
    repo, ids = _repo(tmp_path)
    briefs = []
    rec = Recorder(dispatch_writes=GOOD_PAGES)

    def dispatch(repo_arg, brief_path, output_dir):
        briefs.append(Path(brief_path).read_text())
        rec.dispatch(repo_arg, brief_path, output_dir)

    outcome = refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids,
                           git_head=rec.git_head, dispatch_fn=dispatch, store_fn=rec.store,
                           now_fn=lambda: "T0")
    assert outcome == "refreshed"
    assert "wiki-other-home" in briefs[0]   # sibling page id from the 'other' repo
    sibling_line = next(l for l in briefs[0].splitlines() if "wiki-other-home" in l)
    assert "wiki-demo-alpha" not in sibling_line  # own pages are not their own siblings


def test_validate_requires_citation_when_sibling_repo_named(tmp_path):
    # mandatory sibling citation (Mark, 2026-07-07): naming a sibling repo in prose without
    # citing any of its wiki pages leaves orientation human-readable but not machine-followable
    # -- the project-a v3 refresh kept naming project-a-tools with zero id citations.
    config = load_config(_write_config(tmp_path))
    siblings = {"other": ["wiki-other-home", "wiki-other-ops"]}
    naming_no_cite = _page().replace(
        "word word", "the other repo owns the node side. word word", 1)
    out = _write_pages(tmp_path, {
        "wiki-demo-alpha": naming_no_cite,
        "wiki-demo-beta": _page(("wiki-demo-alpha", "wiki-demo-gamma"), "# Beta\n"),
        "wiki-demo-gamma": _page(("wiki-demo-alpha", "wiki-demo-beta"), "# Gamma\n"),
    })
    violations = validate_pages(config["repos"][0], out, all_page_ids(config),
                                sibling_repos=siblings)
    assert any("sibling" in v and "other" in v for v in violations)

    # citing one of the named sibling's pages satisfies the rule
    naming_with_cite = _page().replace(
        "word word", "the other repo owns the node side (see `wiki-other-home`). word word", 1)
    out2 = _write_pages(tmp_path, {
        "wiki-demo-alpha": naming_with_cite,
        "wiki-demo-beta": _page(("wiki-demo-alpha", "wiki-demo-gamma"), "# Beta\n"),
        "wiki-demo-gamma": _page(("wiki-demo-alpha", "wiki-demo-beta"), "# Gamma\n"),
    })
    assert validate_pages(config["repos"][0], out2, all_page_ids(config),
                          sibling_repos=siblings) == []

    # word-boundary: a page mentioning only 'other-extended' (a LONGER hyphenated name) is
    # not a mention of sibling 'other'
    boundary = _page().replace("word word", "the other-extended tool. word word", 1)
    out3 = _write_pages(tmp_path, {
        "wiki-demo-alpha": boundary,
        "wiki-demo-beta": _page(("wiki-demo-alpha", "wiki-demo-gamma"), "# Beta\n"),
        "wiki-demo-gamma": _page(("wiki-demo-alpha", "wiki-demo-beta"), "# Gamma\n"),
    })
    assert validate_pages(config["repos"][0], out3, all_page_ids(config),
                          sibling_repos=siblings) == []

    # no sibling_repos supplied -> no enforcement (back-compat)
    assert validate_pages(config["repos"][0], out, all_page_ids(config)) == []


def test_refresh_enforces_sibling_citation(tmp_path):
    # plumbing: refresh_repo passes sibling_repos into validation; a generated page naming a
    # sibling without citing it fails the run loudly (stores NOTHING)
    repo, ids = _repo(tmp_path)
    bad = dict(GOOD_PAGES)
    bad["wiki-demo-alpha"] = bad["wiki-demo-alpha"].replace(
        "word word", "the other repo does this. word word", 1)
    rec = Recorder(dispatch_writes=bad)
    with pytest.raises(RuntimeError, match="sibling"):
        refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=rec.dispatch, store_fn=rec.store, now_fn=lambda: "T0",
                     sibling_repos={"other": ["wiki-other-home", "wiki-other-ops"]})
    assert rec.stored_batches == []


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
    # pin the REAL registered seat ids (a derived target like 'agy-print-bridge-dev' does not
    # exist and times out -- the v1.1 live --add lesson). Default reviewer for codex output is
    # the launchd-persisted, read-only-tool-ceiling review seat authenticated against a paid
    # model subscription (selected after a head-to-head comparison against an alternate model
    # that made the safer fail-closed call at lower cost on the same pages).
    assert r == {"engine": "agent-sdk", "target_id": "asdk-bridge-dev-example", "timeout": 3600}
    assert resolve_reviewer("agy-print", {})["target_id"] == "codex-bridge-dev-example"
    # an agent-sdk generator must not be reviewed by the same engine family default:
    assert resolve_reviewer("agent-sdk", {})["target_id"] == "codex-bridge-dev-example"
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


def test_dispatch_reply_text_unwraps_envelope_result():
    from agent_redis_bridge.wiki_refresh import dispatch_reply_text, parse_discovery
    # the exact live-run failure shape: dispatch-dev's stdout is a JSON ENVELOPE whose
    # `result` carries the seat's fenced-JSON reply. The capture path must unwrap `result`
    # before parse_discovery, or the envelope (no 'pages' key) fails loud.
    inner = '```json\n{"pages": [{"id": "wiki-r-a", "title": "A", "scope": "s"}, ' \
            '{"id": "wiki-r-b", "title": "B", "scope": "s"}]}\n```'
    envelope = json.dumps({"result": inner, "ok": True, "error": None})
    reply = dispatch_reply_text(envelope)
    assert reply == inner
    # end-to-end: the unwrapped reply parses; the raw envelope would not
    assert len(parse_discovery(reply, "r", set())) == 2
    with pytest.raises(Exception):
        parse_discovery(envelope, "r", set())


def test_dispatch_reply_text_passthrough_for_non_envelope():
    from agent_redis_bridge.wiki_refresh import dispatch_reply_text
    plain = '```json\n{"pages": []}\n```'
    assert dispatch_reply_text(plain) == plain  # not JSON envelope -> unchanged
    assert dispatch_reply_text('{"ok": true}') == '{"ok": true}'  # JSON but no 'result'


def test_parse_review_verdict_searches_not_startswith():
    from agent_redis_bridge.wiki_refresh import _parse_review_verdict
    # mid-reply verdict (the live --add proof shape) classifies correctly, not "malformed":
    ok, reasons = _parse_review_verdict(
        "There is no filler.\nTherefore, my decision is REQUEST-CHANGES.")
    assert ok is False and "REQUEST-CHANGES" in reasons
    assert _parse_review_verdict("Looks correct. APPROVE.")[0] is True
    # fail-closed: both tokens -> reject; neither token -> reject
    assert _parse_review_verdict("I won't APPROVE; REQUEST-CHANGES")[0] is False
    assert _parse_review_verdict("hmm, unclear")[0] is False


def test_render_revision_brief_contains_reasons_and_rules(tmp_path):
    from agent_redis_bridge.wiki_refresh import render_revision_brief
    config = load_config(_write_config(tmp_path))
    reasons = "Page alpha invents envelope keys sender/recipient for the real from/to."
    brief = render_revision_brief(config["repos"][0], "/tmp/outdir", reasons)
    assert reasons in brief                      # reviewer feedback verbatim
    assert "/tmp/outdir" in brief                # where the pages to revise live
    assert "/tmp/demo-repo" in brief             # ground revisions in the real repo
    for needle in ("REJECTED", "revise", "See also:", "backticked"):
        assert needle.lower() in brief.lower()   # in-place instruction + original format rules


def test_review_reject_then_revision_approved_stores(tmp_path):
    # revise-and-resubmit: a REQUEST-CHANGES with revisions remaining re-dispatches the
    # generator with the reviewer's reasons, re-validates, re-reviews -- and an approved
    # revision stores. The reviewer is called fresh each round.
    repo, ids = _repo(tmp_path)
    rec = Recorder(dispatch_writes=GOOD_PAGES)
    briefs = []
    orig_dispatch = rec.dispatch

    def dispatch(repo_arg, brief_path, output_dir):
        briefs.append(Path(brief_path).read_text())
        orig_dispatch(repo_arg, brief_path, output_dir)

    verdicts = iter([(False, "alpha misstates the schema"), (True, "")])
    reviews = []

    def review_fn(repo_arg, output_dir):
        verdict = next(verdicts)
        reviews.append(verdict)
        return verdict

    outcome = refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids,
                           git_head=rec.git_head, dispatch_fn=dispatch, store_fn=rec.store,
                           now_fn=lambda: "T0", review_fn=review_fn, max_revisions=1)
    assert outcome == "refreshed"
    assert len(rec.stored_batches) == 1
    assert rec.dispatch_calls == 2                       # generation + one revision
    assert "alpha misstates the schema" in briefs[1]     # reasons fed back verbatim
    assert len(reviews) == 2                             # revised pages re-reviewed


def test_review_reject_exhausts_revisions_stores_nothing(tmp_path):
    repo, ids = _repo(tmp_path)
    rec = Recorder(dispatch_writes=GOOD_PAGES)
    reviews = []

    def review_fn(repo_arg, output_dir):
        reviews.append(1)
        return False, "still factually wrong"

    with pytest.raises(RuntimeError, match="review"):
        refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=rec.dispatch, store_fn=rec.store, now_fn=lambda: "T0",
                     review_fn=review_fn, max_revisions=1)
    assert rec.stored_batches == []
    assert rec.dispatch_calls == 2   # generation + exactly ONE revision, then give up
    assert len(reviews) == 2


def test_max_revisions_zero_is_discard_and_fail(tmp_path):
    # explicit opt-out: max_revisions=0 is the pre-v1.2 behavior, reject -> immediate abort
    repo, ids = _repo(tmp_path)
    rec = Recorder(dispatch_writes=GOOD_PAGES)
    with pytest.raises(RuntimeError, match="review"):
        refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=rec.dispatch, store_fn=rec.store, now_fn=lambda: "T0",
                     review_fn=lambda r, o: (False, "wrong"), max_revisions=0)
    assert rec.dispatch_calls == 1
    assert rec.stored_batches == []


def test_negative_max_revisions_fails_loud_never_skips_gates(tmp_path):
    # range(max_revisions + 1) with a negative value would skip validation AND review
    # entirely and store unvalidated content -- the worst silent failure. Must raise.
    repo, ids = _repo(tmp_path)
    rec = Recorder(dispatch_writes=GOOD_PAGES)
    with pytest.raises(Exception, match="max_revisions"):
        refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=rec.dispatch, store_fn=rec.store, now_fn=lambda: "T0",
                     review_fn=lambda r, o: (False, "wrong"), max_revisions=-1)
    assert rec.stored_batches == []


def test_revision_that_breaks_validation_fails_loud(tmp_path):
    # a revision dispatch that structurally breaks a page must abort, not store or loop
    repo, ids = _repo(tmp_path)
    calls = {"n": 0}

    def dispatch(repo_arg, brief_path, output_dir):
        calls["n"] += 1
        pages = dict(GOOD_PAGES)
        if calls["n"] > 1:   # the revision drops a required title
            pages["wiki-demo-alpha"] = pages["wiki-demo-alpha"].replace("# Alpha", "untitled")
        for page_id, text in pages.items():
            (Path(output_dir) / f"{page_id}.md").write_text(text)

    rec = Recorder()
    with pytest.raises(RuntimeError, match="validation"):
        refresh_repo(repo, state_dir=str(tmp_path / "s"), all_ids=ids, git_head=rec.git_head,
                     dispatch_fn=dispatch, store_fn=rec.store, now_fn=lambda: "T0",
                     review_fn=lambda r, o: (False, "wrong"), max_revisions=2)
    assert rec.stored_batches == []
    assert calls["n"] == 2   # no third dispatch after the broken revision


def test_rollback_add_removes_entry_when_no_pending(tmp_path):
    from agent_redis_bridge.wiki_refresh import rollback_add
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": [
        {"name": "keepme", "path": "/x", "seat": {"engine": "e", "target_id": "t", "timeout": 1},
         "pages": [{"id": "wiki-keepme-a", "title": "A", "scope": "s"}]},
        {"name": "newrepo", "path": "/y", "seat": {"engine": "e", "target_id": "t", "timeout": 1},
         "pages": [{"id": "wiki-newrepo-a", "title": "A", "scope": "s"}]},
    ]}))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    assert rollback_add(str(cfg), "newrepo", str(state_dir)) is True
    names = [r["name"] for r in json.loads(cfg.read_text())["repos"]]
    assert names == ["keepme"]  # only the added entry removed, sibling untouched


def test_rollback_add_keeps_entry_when_pending_exists(tmp_path):
    # once pending-<repo>.json exists the batch may be PARTIALLY stored; the recovery path is
    # resume-from-pending, which needs the config entry. Rollback must refuse, not orphan it.
    from agent_redis_bridge.wiki_refresh import rollback_add
    cfg = tmp_path / "c.json"
    original = json.dumps({"repos": [
        {"name": "newrepo", "path": "/y", "seat": {"engine": "e", "target_id": "t", "timeout": 1},
         "pages": [{"id": "wiki-newrepo-a", "title": "A", "scope": "s"}]},
    ]})
    cfg.write_text(original)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "pending-newrepo.json").write_text("{}")
    assert rollback_add(str(cfg), "newrepo", str(state_dir)) is False
    assert cfg.read_text() == original  # untouched


def _fake_dispatch_run(tmp_path, review_reply, calls):
    """Stand-in for subprocess.run covering every process main() shells out to:
    git rev-parse, harness-publish + dispatch-dev (discovery / generation / review), ssh store.
    review_reply: a str (same verdict every round) or a list popped per review round.

    Side-channel ``fake_run.dispatch_tasks`` snapshots instruction text while the
    temp --brief file still exists (cleaned up when _authority_dispatch returns).
    """
    review_replies = [review_reply] if isinstance(review_reply, str) else list(review_reply)
    pages = {
        "wiki-newrepo-a": _page(("wiki-newrepo-b",), "# A\n"),
        "wiki-newrepo-b": _page(("wiki-newrepo-a",), "# B\n"),
    }
    dispatch_tasks: list[str] = []

    class Result:
        def __init__(self, returncode=0, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def _task_from_cmd(cmd):
        """Slice 1d-iv: instructions live in --brief file, not a trailing free-form arg."""
        if "--brief" in cmd:
            return Path(cmd[cmd.index("--brief") + 1]).read_text(encoding="utf-8")
        return cmd[-1] if cmd else ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "git":
            return Result(stdout="sha-1\n")
        if cmd[0] == "ssh":
            return Result()
        if cmd[0] == "scripts/arb-memory-harness-publish":
            target = cmd[cmd.index("--target-agent-id") + 1]
            return Result(
                stdout=json.dumps(
                    {
                        "artefact_id": "art-wiki-1",
                        "version": 1,
                        "target_agent_id": target,
                        "registration_generation": "gen-1",
                        "worker_vantage": "wiki-refresh",
                        "content_hash": "hash-wiki-1",
                    }
                )
            )
        assert cmd[0] == "scripts/dispatch-dev"
        task = _task_from_cmd(cmd)
        dispatch_tasks.append(task)
        if "discovery brief" in task:
            return Result(stdout=json.dumps({"result": _proposal([
                {"id": "wiki-newrepo-a", "title": "A", "scope": "s"},
                {"id": "wiki-newrepo-b", "title": "B", "scope": "s"},
            ])}))
        if "Read the brief at" in task:
            out = Path(re.search(r"brief at (\S+) and", task).group(1)).parent
            for page_id, text in pages.items():
                (out / f"{page_id}.md").write_text(text)
            return Result()
        if "Review the generated" in task:
            verdict = review_replies.pop(0) if len(review_replies) > 1 else review_replies[0]
            return Result(stdout=json.dumps({"result": verdict}))
        raise AssertionError(f"unexpected dispatch task: {task[:80]}")

    fake_run.dispatch_tasks = dispatch_tasks  # type: ignore[attr-defined]
    return fake_run


def _wiki_main_env(monkeypatch):
    """Ordinary wiki dispatch requires the short-lived FABA publish credential."""
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory-test/0")


def test_main_rejected_add_rolls_back_config(tmp_path, monkeypatch):
    # the v1.2 wrinkle from the live --add proof: add_repo persists the config BEFORE the
    # review gate runs, so a rejected --add left the repo configured-but-unstored. A rejected
    # --add must now leave the config exactly as it found it.
    import agent_redis_bridge.wiki_refresh as wr
    _wiki_main_env(monkeypatch)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": []}))
    calls = []
    monkeypatch.setattr(wr.subprocess, "run",
                        _fake_dispatch_run(tmp_path, "Factually wrong. REQUEST-CHANGES.", calls))
    rc = wr.main(["--config", str(cfg), "--state-dir", str(tmp_path / "state"),
                  "--add", "/tmp/NewRepo"])
    assert rc == 1
    assert json.loads(cfg.read_text())["repos"] == []  # rolled back, not configured-but-unstored
    assert not any(c[0] == "ssh" for c in calls)       # nothing stored
    assert not (tmp_path / "state" / "pending-newrepo.json").exists()


def test_main_rejected_then_revised_add_stores(tmp_path, monkeypatch):
    # end-to-end revise-and-resubmit through the CLI glue: first review REQUEST-CHANGES,
    # the revision round's review APPROVEs -> config kept, batch stored, rc 0
    import agent_redis_bridge.wiki_refresh as wr
    _wiki_main_env(monkeypatch)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": []}))
    calls = []
    fake = _fake_dispatch_run(
        tmp_path, ["Invented keys. REQUEST-CHANGES.", "Fixed now. APPROVE."], calls)
    monkeypatch.setattr(wr.subprocess, "run", fake)
    rc = wr.main(["--config", str(cfg), "--state-dir", str(tmp_path / "state"),
                  "--add", "/tmp/NewRepo"])
    assert rc == 0
    assert [r["name"] for r in json.loads(cfg.read_text())["repos"]] == ["newrepo"]
    assert sum(1 for c in calls if c[0] == "ssh") == 1
    generation_tasks = [t for t in fake.dispatch_tasks if "Read the brief at" in t]
    assert len(generation_tasks) == 2   # generation + one revision dispatch


def test_main_review_task_names_repo_path_and_forbids_searching(tmp_path, monkeypatch):
    # the reviewer seat has no shell and a home-wide cwd; a review task that omits the repo
    # path sends the model filesystem-searching for it -- live-observed as four hung bfs
    # sweeps of /Users/<user> that wedged a review turn for 25 minutes. The task must
    # hand over BOTH the repo path and the exact page paths, and say not to search.
    import agent_redis_bridge.wiki_refresh as wr
    _wiki_main_env(monkeypatch)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": []}))
    calls = []
    fake = _fake_dispatch_run(tmp_path, "Accurate. APPROVE.", calls)
    monkeypatch.setattr(wr.subprocess, "run", fake)
    rc = wr.main(["--config", str(cfg), "--state-dir", str(tmp_path / "state"),
                  "--add", "/tmp/NewRepo"])
    assert rc == 0
    review_tasks = [t for t in fake.dispatch_tasks if "Review the generated" in t]
    assert review_tasks, "no review dispatch captured"
    for task in review_tasks:
        assert "/tmp/NewRepo" in task            # repo path handed over, not discoverable
        assert "not search" in task.lower()      # explicit no-glob instruction


def test_main_generation_output_lives_under_state_dir(tmp_path, monkeypatch):
    # the asdk reviewer seat has no shell and reads only under its workdir (home) -- pages
    # generated into the system temp dir (/var/folders/...) are unreachable for it. The CLI
    # must create generation output dirs under the state dir, which lives in home.
    import agent_redis_bridge.wiki_refresh as wr
    _wiki_main_env(monkeypatch)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": []}))
    calls = []
    fake = _fake_dispatch_run(tmp_path, "Accurate. APPROVE.", calls)
    monkeypatch.setattr(wr.subprocess, "run", fake)
    state_dir = tmp_path / "state"
    rc = wr.main(["--config", str(cfg), "--state-dir", str(state_dir),
                  "--add", "/tmp/NewRepo"])
    assert rc == 0
    briefs = [
        re.search(r"brief at (\S+) and", t).group(1)
        for t in fake.dispatch_tasks
        if "Read the brief at" in t
    ]
    assert briefs, "no generation dispatch captured"
    for brief_path in briefs:
        assert str(state_dir) in brief_path  # output dir under state dir, not system temp


def test_main_approved_add_keeps_config_and_stores(tmp_path, monkeypatch):
    # control: rollback must NOT fire on the happy path
    import agent_redis_bridge.wiki_refresh as wr
    _wiki_main_env(monkeypatch)
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"repos": []}))
    calls = []
    monkeypatch.setattr(wr.subprocess, "run",
                        _fake_dispatch_run(tmp_path, "Accurate pages. APPROVE.", calls))
    rc = wr.main(["--config", str(cfg), "--state-dir", str(tmp_path / "state"),
                  "--add", "/tmp/NewRepo"])
    assert rc == 0
    assert [r["name"] for r in json.loads(cfg.read_text())["repos"]] == ["newrepo"]
    assert sum(1 for c in calls if c[0] == "ssh") == 1  # stored exactly once
