from __future__ import annotations

import argparse
import base64
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


INDEX_HINT_CHARS = 4000
MIN_PAGE_CHARS = 500
MAX_PAGE_CHARS = 6000
BACKTICKED_WIKI_RE = re.compile(r"`(wiki-[A-Za-z0-9._-]+)`")
BARE_WIKI_RE = re.compile(r"(?<![`A-Za-z0-9._/-])(wiki-[A-Za-z0-9._-]+)(?![`A-Za-z0-9._/-])")


class WikiConfigError(ValueError):
    pass


class WikiDiscoveryError(ValueError):
    pass


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


def all_page_ids(config: dict) -> set[str]:
    return {page["id"] for repo in config["repos"] for page in repo["pages"]}


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


# Maps a generator engine to a decorrelated reviewer SEAT (full engine+target_id): the target
# is NOT `{engine}-bridge-dev` -- agy-print's real registered seat is `agy-bridge-dev`, not
# `agy-print-bridge-dev`. Derived-target guessing timed out the review dispatch on the live
# --add proof; pin the real seat ids here.
# Default reviewer for codex output is a persistent, read-only-tool-ceiling review seat
# authenticated against a paid model subscription. Selected after a head-to-head comparison
# against an alternate model that showed it made the safer fail-closed call at lower cost.
# Terms-change fallback: swap target to an alternate model-backed seat.
_REVIEWER_MAP = {
    "codex": {"engine": "agent-sdk", "target_id": "asdk-bridge-dev-example", "timeout": 3600},
    "agent-sdk": {"engine": "codex", "target_id": "codex-bridge-dev-example", "timeout": 3600},
    "agy-print": {"engine": "codex", "target_id": "codex-bridge-dev-example", "timeout": 3600},
}


def resolve_reviewer(generator_engine: str, overrides: dict) -> dict:
    if any(overrides.get(k) for k in ("engine", "target_id", "timeout")):
        # all-or-none override, and it MUST stay decorrelated (round-1 codex P2: a same-engine
        # override defeats the whole gate -- fail loud, never silently accept it)
        missing = [k for k in ("engine", "target_id", "timeout") if not overrides.get(k)]
        if missing:
            raise WikiConfigError(f"reviewer override missing {missing}; supply all of engine/target_id/timeout")
        if overrides["engine"] == generator_engine:
            raise WikiConfigError(
                f"reviewer override engine {overrides['engine']!r} is not decorrelated from the "
                f"generator engine; pick a different engine or use --no-review")
        return {"engine": overrides["engine"], "target_id": overrides["target_id"],
                "timeout": overrides["timeout"]}
    reviewer = _REVIEWER_MAP.get(generator_engine)
    if reviewer is None:
        raise WikiConfigError(
            f"no decorrelated reviewer for generator engine {generator_engine!r}; "
            "pass --reviewer-engine/--reviewer-target-id/--reviewer-timeout or use --no-review")
    return dict(reviewer)


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


def rollback_add(config_path: str, repo_name: str, state_dir: str) -> bool:
    """Compensate a failed --add: remove the config block add_repo just persisted, so a
    rejected/failed first refresh does not leave the repo configured-but-unstored (the live
    --add proof left exactly that and needed a manual revert).

    Refuses (returns False) when pending-<repo>.json exists: the intent batch is durable and
    may be PARTIALLY stored, and the recovery path -- resume-from-pending on the next run --
    needs the config entry. Caller holds <state-dir>/lock, same as add_repo.
    """
    if (Path(state_dir).expanduser() / f"pending-{repo_name}.json").is_file():
        return False
    config = _read_json(Path(config_path), {"repos": []})
    kept = [r for r in config["repos"] if r["name"] != repo_name]
    if len(kept) == len(config["repos"]):
        return False
    config["repos"] = kept
    _atomic_write(Path(config_path), json.dumps(config, indent=2))
    return True


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

## Boundaries (load-bearing for readers orienting in this repo)

- Where a page touches a system whose code is NOT in this repo — a separate service, a
  sibling repo, an external node — SAY SO explicitly and name where it lives if the source
  or docs reveal it. A reader must never be left to search this tree for code that lives
  elsewhere.
{sibling_section}
Reply with the list of files written.
"""

SIBLING_SECTION_TEMPLATE = """\
- Other repos already on this wiki: {sibling_ids}
  Whenever a page NAMES one of these sibling repos, it MUST also cite at least one of that
  repo's page ids (backticked) at the mention or on the See also line — naming a sibling
  without a citation fails validation. Do not name siblings gratuitously to force links.
"""


REVISION_BRIEF_TEMPLATE = """# ARB Wiki revision brief — {repo_name}

Your previously generated wiki pages for the repo at {repo_path} were reviewed and REJECTED.
The pages are still in {output_dir}. Revise them IN PLACE (same filenames) to fix every point
below — grounded in the repo's actual source, never by inventing facts. READ-ONLY on the repo
itself: do NOT modify anything at {repo_path}.

## Reviewer's reasons (verbatim)

{reasons}

## Original brief — every format rule still applies

{brief}
"""


def render_brief(repo: dict, output_dir: str, sibling_ids=None) -> str:
    page_list = "\n".join(
        f"{i}. `{page['id']}.md` — {page['title']}: {page['scope']}"
        for i, page in enumerate(repo["pages"], 1)
    )
    own = {page["id"] for page in repo["pages"]}
    siblings = sorted(set(sibling_ids or ()) - own)
    sibling_section = (
        SIBLING_SECTION_TEMPLATE.format(sibling_ids=", ".join(f"`{s}`" for s in siblings))
        if siblings else ""
    )
    return BRIEF_TEMPLATE.format(
        repo_name=repo["name"], repo_path=repo["path"],
        output_dir=output_dir, page_list=page_list, sibling_section=sibling_section,
    )


def render_revision_brief(repo: dict, output_dir: str, reasons: str, sibling_ids=None) -> str:
    return REVISION_BRIEF_TEMPLATE.format(
        repo_name=repo["name"], repo_path=repo["path"], output_dir=output_dir,
        reasons=reasons, brief=render_brief(repo, output_dir, sibling_ids=sibling_ids),
    )


def validate_pages(repo: dict, output_dir: str, all_ids: set[str], sibling_repos=None) -> list[str]:
    violations: list[str] = []
    page_ids = [page["id"] for page in repo["pages"]]
    sibling_min = min(2, len(page_ids) - 1)
    out = Path(output_dir)
    # mandatory sibling citation: naming a sibling repo in prose without citing any of its
    # wiki pages leaves the boundary human-readable but not machine-followable (a real refresh
    # once named a sibling repo "project-a-tools" with zero id citations). Word-boundary match so
    # 'project-a' inside 'project-a-tools' does not false-trigger.
    sibling_patterns = [
        (name, re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", re.I), set(ids))
        for name, ids in (sibling_repos or {}).items()
        if name != repo["name"]
    ]

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

        cited = set(BACKTICKED_WIKI_RE.findall(text))
        for token in cited:
            if token not in all_ids and token != f"wiki-{repo['name']}-manifest":
                violations.append(f"{page_id}: backticked reference to unknown id {token!r}")
        body_wo_backticked = BACKTICKED_WIKI_RE.sub("", text)
        if BARE_WIKI_RE.search(body_wo_backticked):
            violations.append(f"{page_id}: bare (unbackticked) wiki-* token in body")

        for name, pattern, sibling_ids in sibling_patterns:
            # match against text with backticked ids stripped: citing `wiki-<name>-x` is a
            # citation, not a prose mention that itself demands one
            if pattern.search(body_wo_backticked) and not (cited & sibling_ids):
                violations.append(
                    f"{page_id}: names sibling repo {name!r} but cites none of its pages")
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


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def refresh_repo(repo, *, state_dir, all_ids, git_head, dispatch_fn, store_fn, now_fn,
                 force=False, mkdtemp_fn=None, review_fn=None, max_revisions=1,
                 sibling_repos=None):
    if max_revisions < 0:
        # a negative value would make the validate/review loop body never run and store
        # UNVALIDATED content -- fail loud, never clamp silently
        raise WikiConfigError(f"max_revisions must be >= 0, got {max_revisions}")
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
        brief_path.write_text(render_brief(repo, output_dir, sibling_ids=all_ids))
        dispatch_fn(repo, str(brief_path), output_dir)

        # revise-and-resubmit: a REQUEST-CHANGES with revisions remaining feeds the reviewer's
        # reasons back into a revision dispatch, then re-validates and re-reviews the revised
        # pages. The re-review PROMPT is identical each round, but a session-continuous
        # reviewer seat still remembers round 1 (live-observed: "previously-flagged issues
        # have been corrected") -- accepted deliberately (Mark, 2026-07-07): the reviewer who
        # wrote the objections verifies its own fixes against source, and round-1 coverage of
        # unchanged pages stands. Flip the review dispatch to fresh_context for cold rounds.
        # Structural validation failures abort immediately: those are contract bugs, not the
        # factual disagreements the feedback loop exists to fix.
        for attempt in range(max_revisions + 1):
            violations = validate_pages(repo, output_dir, all_ids, sibling_repos=sibling_repos)
            if violations:
                raise RuntimeError(
                    f"wiki refresh for {repo['name']!r} failed validation; storing NOTHING:\n"
                    + "\n".join(violations)
                )

            if review_fn is None:
                break
            ok, reasons = review_fn(repo, output_dir)
            if ok:
                break
            if attempt == max_revisions:
                raise RuntimeError(
                    f"wiki refresh for {repo['name']!r} rejected by review "
                    f"(after {max_revisions} revision(s)); storing NOTHING:\n" + reasons
                )
            revision_path = Path(output_dir) / f"_revision-brief-{attempt + 1}.md"
            revision_path.write_text(
                render_revision_brief(repo, output_dir, reasons, sibling_ids=all_ids))
            dispatch_fn(repo, str(revision_path), output_dir)

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


STORE_SCRIPT_TEMPLATE = '''\
import base64, json, os
from arb_memory import redis_conn
from arb_memory.bus import memory_write

intents = json.loads(base64.b64decode("{payload_b64}"))
client = redis_conn.from_url(os.environ["ARB_MEMORY_REDIS_URL"])
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


def dispatch_reply_text(dispatch_stdout: str) -> str:
    """Extract the seat's reply text from scripts/dispatch-dev's stdout JSON envelope.

    dispatch-dev prints a payload envelope `{"result": "<reply>", "ok": ..., ...}` on stdout;
    the discovery/review capture path needs the inner `result`, not the envelope (a raw
    envelope handed to parse_discovery has no 'pages' key and fails loud). Found by the live
    --add proof: the CLI's capture glue was the untested prose carve-out.
    """
    try:
        payload = json.loads(dispatch_stdout)
    except json.JSONDecodeError:
        return dispatch_stdout  # not an envelope (e.g. a test stub) -> pass through
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return dispatch_stdout


def _parse_review_verdict(reply: str) -> tuple:
    """Classify a reviewer reply into (ok, reasons). Fail-CLOSED for a quality gate: search
    the whole reply (a reviewer routinely states its verdict mid-text, not at position 0 --
    the live --add proof mislabeled a valid mid-reply REQUEST-CHANGES as malformed under the
    old startswith check), REQUEST-CHANGES wins if both tokens appear, and an unrecognized
    reply is treated as a rejection (never store on an ambiguous review)."""
    if "REQUEST-CHANGES" in reply:
        return False, reply
    if "APPROVE" in reply:
        return True, reply
    return False, f"reviewer reply contained neither APPROVE nor REQUEST-CHANGES:\n{reply}"


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


def main(argv) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/arb-wiki.json")
    parser.add_argument("--state-dir", default="~/.arb-wiki")
    parser.add_argument("--repo")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--add")
    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument("--review", action="store_true")
    review_group.add_argument("--no-review", action="store_true")
    parser.add_argument("--max-revisions", type=int, default=1,
                        help="revision rounds after a review rejection (0 = reject aborts)")
    parser.add_argument("--seat-engine")
    parser.add_argument("--seat-target-id")
    parser.add_argument("--seat-timeout", type=int)
    parser.add_argument("--reviewer-engine")
    parser.add_argument("--reviewer-target-id")
    parser.add_argument("--reviewer-timeout", type=int)
    args = parser.parse_args(argv)

    state_root = Path(args.state_dir).expanduser()
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root / "lock"

    def _output_root() -> Path:
        root = state_root / "tmp"
        root.mkdir(exist_ok=True)
        return root
    review_on = args.review if (args.review or args.no_review) else bool(args.add)

    def _env():
        env = os.environ.copy()
        env.update({
            "FROM_AGENT_ID": "claude-bridge-dev",
            "BRANCH": "dev",
            "AGENT_ENV_FILE": "envs/agent-redis-bridge-dev.env",
        })
        return env

    def _seat_from_args(config: dict) -> dict:
        if any((args.seat_engine, args.seat_target_id, args.seat_timeout)):
            missing = [
                name for name, value in (
                    ("--seat-engine", args.seat_engine),
                    ("--seat-target-id", args.seat_target_id),
                    ("--seat-timeout", args.seat_timeout),
                )
                if not value
            ]
            if missing:
                raise WikiConfigError(f"seat override missing {missing}; supply all seat fields")
            return {
                "engine": args.seat_engine,
                "target_id": args.seat_target_id,
                "timeout": args.seat_timeout,
            }
        if config.get("repos"):
            return config["repos"][0]["seat"]
        return {"engine": "codex", "target_id": "codex-bridge-dev-example", "timeout": 3600}

    def _reviewer_overrides() -> dict:
        return {
            "engine": args.reviewer_engine,
            "target_id": args.reviewer_target_id,
            "timeout": args.reviewer_timeout,
        }

    def git_head(repo_path):
        res = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()

    def _authority_dispatch(*, target_id, engine, timeout, brief_text, run_id):
        """Slice 1d-iv: pre-mint via harness publish, enqueue via dispatch-dev authority path."""
        import tempfile
        from pathlib import Path as _P

        redis_url = os.environ.get("ARB_MEMORY_REDIS_URL", "").strip()
        if not redis_url:
            raise RuntimeError(
                "wiki_refresh ordinary dispatch requires ARB_MEMORY_REDIS_URL "
                "for the short-lived FABA harness publish step (Slice 1d-iv)"
            )
        with tempfile.TemporaryDirectory(prefix="wiki-dispatch-") as tmp:
            tmp_p = _P(tmp)
            brief_file = tmp_p / "brief.md"
            # Wrap free-form body into a valid dispatch brief (assumptions empty).
            from agent_redis_bridge.dispatch_cli import wrap_instructions_as_brief

            brief_file.write_text(
                wrap_instructions_as_brief(brief_text, vantage="wiki-refresh"),
                encoding="utf-8",
            )
            pub = subprocess.run(
                [
                    "scripts/arb-memory-harness-publish",
                    "--target-agent-id", target_id,
                    "--brief", str(brief_file),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=_env(),
            )
            receipt = json.loads(pub.stdout)
            receipt_file = tmp_p / "receipt.json"
            receipt_file.write_text(json.dumps(receipt), encoding="utf-8")
            # Enqueue without publish credentials (non-FABA identity).
            from .dispatch_authority import filter_publish_env

            enq_env = filter_publish_env(_env())
            return subprocess.run(
                [
                    "scripts/dispatch-dev",
                    "--engine", engine,
                    "--target-id", target_id,
                    "--timeout", str(timeout),
                    "--run-id", run_id,
                    "--artefact-id", str(receipt["artefact_id"]),
                    "--version", str(receipt["version"]),
                    "--receipt", str(receipt_file),
                    "--brief", str(brief_file),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=enq_env,
            )

    def dispatch_fn(repo, brief_path, output_dir):
        task = (
            "READ-ONLY documentation-generation task, no repo modifications. "
            f"Read the brief at {brief_path} and execute it."
        )
        _authority_dispatch(
            target_id=repo["seat"]["target_id"],
            engine=repo["seat"]["engine"],
            timeout=repo["seat"]["timeout"],
            brief_text=task,
            run_id=f"wiki-{repo.get('id', 'repo')}",
        )

    def dispatch_capture_fn(brief):
        res = _authority_dispatch(
            target_id=_active_seat["target_id"],
            engine=_active_seat["engine"],
            timeout=_active_seat["timeout"],
            brief_text=brief,
            run_id="wiki-capture",
        )
        return dispatch_reply_text(res.stdout)

    def review_fn(repo, output_dir):
        reviewer = resolve_reviewer(repo["seat"]["engine"], _reviewer_overrides())
        # only the configured page files, never the generated _brief.md (cold-Opus P2:
        # a reviewer shown the brief is reviewing against the brief, not the code)
        page_list = "\n".join(
            str(Path(output_dir) / f"{page['id']}.md") for page in repo["pages"]
        )
        task = (
            f"Review the generated ARB Wiki pages for {repo['name']} in {output_dir}.\n"
            f"Files:\n{page_list}\n\n"
            f"The repo these pages document is at {repo['path']} — read its real source to "
            "check every checkable claim. Use ONLY the exact paths above and paths under the "
            "repo; do NOT search or glob the filesystem outside the repo.\n"
            "Flag any statement factually wrong about the code, or any page that is generic "
            "filler. Reply APPROVE or REQUEST-CHANGES with reasons."
        )
        res = _authority_dispatch(
            target_id=reviewer["target_id"],
            engine=reviewer["engine"],
            timeout=reviewer["timeout"],
            brief_text=task,
            run_id=f"wiki-review-{repo.get('id', 'repo')}",
        )
        reply = dispatch_reply_text(res.stdout).strip()
        return _parse_review_verdict(reply)

    failures = 0
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw_config = _read_json(Path(args.config), {"repos": []})
        added_name = None
        if args.add:
            try:
                # seat resolution inside the try (cold-Opus P2: a partial --seat-* override
                # should surface as a clean `add: ERROR`, not an escaping traceback)
                _active_seat = _seat_from_args(raw_config)
                new_block = add_repo(
                    args.config,
                    args.add,
                    dispatch_capture_fn=dispatch_capture_fn,
                    seat=_active_seat,
                )
                added_name = new_block["name"]
                print(f"{added_name}: config added")
                config = load_config(args.config)
                ids = all_page_ids(config)
                sibling_map = {r["name"]: [p["id"] for p in r["pages"]] for r in config["repos"]}
                repos = [repo for repo in config["repos"] if repo["name"] == new_block["name"]]
            except Exception as exc:
                failures += 1
                print(f"add: ERROR {exc}", file=sys.stderr)
                repos = []
        else:
            config = load_config(args.config)
            ids = all_page_ids(config)
            sibling_map = {r["name"]: [p["id"] for p in r["pages"]] for r in config["repos"]}
            repos = config["repos"]

        for repo in repos:
            if args.repo and repo["name"] != args.repo:
                continue
            try:
                outcome = refresh_repo(
                    repo,
                    state_dir=str(state_root),
                    all_ids=ids,
                    git_head=git_head,
                    dispatch_fn=dispatch_fn,
                    store_fn=_run_store,
                    now_fn=lambda: datetime.datetime.now(
                        datetime.UTC
                    ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    force=args.force,
                    review_fn=review_fn if review_on else None,
                    max_revisions=args.max_revisions,
                    sibling_repos=sibling_map,
                    # generation output goes under the state dir (in home), NOT the system
                    # temp dir: the asdk reviewer seat has no shell and can only read under
                    # its workdir, and /var/folders/... is outside any home-scoped workdir
                    mkdtemp_fn=lambda: tempfile.mkdtemp(dir=str(_output_root())),
                )
                print(f"{repo['name']}: {outcome}")
            except Exception as exc:
                failures += 1
                print(f"{repo['name']}: ERROR {exc}", file=sys.stderr)
                if repo["name"] == added_name and rollback_add(
                        args.config, added_name, str(state_root)):
                    print(f"{added_name}: config entry rolled back (add stored nothing)",
                          file=sys.stderr)
        fcntl.flock(lock, fcntl.LOCK_UN)
    return 1 if failures else 0
