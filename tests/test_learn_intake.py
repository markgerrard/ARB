import json
import subprocess
from pathlib import Path

import pytest

from agent_redis_bridge import learn_intake as li


def row(learn_id, status, body="Title\nbody", version=1, created_at="2026-07-07T00:00:00Z", **header):
    h = {"status": status, "target": header.pop("target", "arb"), "proposer": "mark",
         "supersedes": None, "referent": None}
    h.update(header)
    return {
        "artefact_id": learn_id,
        "version": version,
        "content": li.render_header(h) + body,
        "created_at": created_at,
    }


def reply(token, stance, severity="none"):
    return json.dumps({
        "ok": True,
        "result": f"{token}\n```vote\n"
        + json.dumps({"stance": stance, "severity": severity, "refs": [], "note": token})
        + "\n```",
    })


def test_cli_surface_help_pins_subcommands(capsys):
    with pytest.raises(SystemExit):
        li.main(["--help"])
    out = capsys.readouterr().out
    for text in (
        "propose <source-file>",
        "--target {skill,arb,project}",
        "--from-workflow HANDOFF SHA",
        "evaluate <learn-id>",
        "--panel {core,full}",
        "--seat {agy,codex-sol,fable,glm,grok}",
        "promote <learn-id>",
        "resolve <learn-id> {approve,reject}",
    ):
        assert text in out


def test_cli_help_derives_panel_choices_everywhere(monkeypatch, capsys):
    monkeypatch.setitem(li.PANEL_SEATS, "extended", ("codex-sol",))
    with pytest.raises(SystemExit):
        li.main(["--help"])
    out = capsys.readouterr().out
    assert "--panel {core,full,extended}" in out
    assert "--panel core|full|extended" in out


def test_orchestrator_skill_uses_supported_health_check_entrypoint():
    skill = Path("skills/using-arb-learn/SKILL.md").read_text()
    assert (
        "scripts/agent-dispatch --engine <engine> --target-id <target> --check"
        in skill
    )
    assert "Do not use `scripts/dispatch-dev --check`" in skill
    assert "FROM_AGENT_ID=claude-bridge-dev" in skill
    assert "BRANCH=dev" in skill


def test_id_header_and_force_ordinals_strip_header_for_dedupe():
    source = b"Hello, WORLD!!\nBody"
    assert li.mint_id(source, "Hello, WORLD!!").startswith("learn-hello-world-")
    long = li.mint_id(b"x", " ".join(["word"] * 20))
    assert len(long.split("-")[1]) <= 40
    assert li.next_force_id("learn-a-bbbb", [{"artefact_id": "learn-a-bbbb-r2"}]) == "learn-a-bbbb-r3"

    header = {"status": "proposed", "target": "arb", "proposer": "mark", "supersedes": None, "referent": None}
    rendered = li.render_header(header) + "Body\n"
    parsed, body = li.parse_header(rendered)
    assert parsed == header
    assert body == "Body\n"
    with pytest.raises(li.LearnError, match="header"):
        li.parse_header("not json\nbody")
    assert li.dedupe_key(rendered) == li.dedupe_key("Body\n")


def test_run_read_extracts_sentinel_payload_from_noisy_stdout():
    def run_fn(cmd, **kwargs):
        assert cmd[:2] == ["ssh", "arb-prod"]
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(cmd, 0, stdout='banner\nARB_JSON_BEGIN\n[{"x": 1}]\nARB_JSON_END\nnoise')

    assert li._run_read("SELECT 1", run_fn=run_fn) == [{"x": 1}]
    with pytest.raises(li.LearnError, match="sentinel"):
        li.parse_sentinel_json("ARB_JSON_BEGIN\n{}")


def test_store_version_writes_index_hint_and_visibility_barrier_checks_version():
    calls = []

    def store_fn(intents):
        calls.extend(intents)

    status = [None, {"version": 1}, {"version": 2}]
    sleeps = []
    version = li.store_version(
        "learn-x-1234",
        "new body",
        {"status": "eval-approved", "target": "arb", "proposer": "mark", "supersedes": None, "referent": None},
        current={"version": 1},
        store_fn=store_fn,
        get_status_fn=lambda _id: status.pop(0),
        sleep_fn=lambda secs: sleeps.append(secs),
    )
    assert version == 2
    assert calls[0]["artefact"]["artefact_id"] == "learn-x-1234"
    assert calls[0]["hints"][0]["metadata"]["kind"] == "artefact_index"
    assert calls[0]["hints"][0]["metadata"]["learn_proposal"] is True
    assert sleeps == [2, 2]

    with pytest.raises(li.LearnError, match="WriteLoop"):
        li.wait_visible("learn-x-1234", 9, get_status_fn=lambda _id: {"version": 8}, sleep_fn=lambda _s: None)


def test_terminal_guard_refuses_all_verbs_without_write_or_dispatch(tmp_path):
    src = tmp_path / "p.md"
    src.write_text("Title\nbody")
    writes = []
    dispatches = []
    deps = li.Deps(
        list_proposals=lambda: [row("learn-title-00000000", "rejected")],
        get_status=lambda _id: row("learn-title-00000000", "rejected"),
        store_fn=lambda intents: writes.append(intents),
        dispatch_fn=lambda *a, **k: dispatches.append(a) or reply("WORTH-BUILDING", "approve"),
        memory_search_fn=lambda *a, **k: [],
        sleep_fn=lambda _s: None,
    )
    for argv in (
        ["propose", str(src)],
        ["evaluate", "learn-title-00000000"],
        ["promote", "learn-title-00000000"],
        ["resolve", "learn-title-00000000", "approve", "--reason", "x"],
    ):
        assert li.main(argv, deps=deps) == 2
    assert writes == []
    assert dispatches == []


def test_force_on_rejected_id_mints_superseding_revision_without_touching_prior(tmp_path):
    src = tmp_path / "p.md"
    src.write_text("Title\nbody")
    source = src.read_bytes()
    base_id = li.mint_id(source, "Title")
    prior = row(base_id, "rejected", "Title\nold body", version=2)
    writes = []
    state = {base_id: prior}

    def get_status(learn_id):
        return state.get(learn_id)

    def store_fn(intents):
        writes.extend(intents)
        artefact = intents[0]["artefact"]
        state[artefact["artefact_id"]] = {
            "artefact_id": artefact["artefact_id"],
            "version": 1,
            "content": artefact["content"],
            "created_at": "now",
        }

    deps = li.Deps(
        list_proposals=lambda: list(state.values()),
        get_status=get_status,
        store_fn=store_fn,
        memory_search_fn=lambda *a, **k: [],
        sleep_fn=lambda _s: None,
    )

    assert li.main(["propose", str(src)], deps=deps) == 2
    assert writes == []
    assert state[base_id] == prior

    assert li.main(["propose", str(src), "--force"], deps=deps) == 0
    assert writes[-1]["artefact"]["artefact_id"] == f"{base_id}-r1"
    header, _body = li.parse_header(writes[-1]["artefact"]["content"])
    assert header["supersedes"] == base_id
    assert state[base_id] == prior


def test_force_without_prior_id_uses_base_id(tmp_path):
    src = tmp_path / "p.md"
    src.write_text("Unique\nbody")
    base_id = li.mint_id(src.read_bytes(), "Unique")
    writes = []
    state = {}

    def store_fn(intents):
        writes.extend(intents)
        artefact = intents[0]["artefact"]
        state[artefact["artefact_id"]] = {
            "artefact_id": artefact["artefact_id"],
            "version": 1,
            "content": artefact["content"],
            "created_at": "now",
        }

    deps = li.Deps(
        list_proposals=lambda: [],
        get_status=lambda learn_id: state.get(learn_id),
        store_fn=store_fn,
        memory_search_fn=lambda *a, **k: [],
        sleep_fn=lambda _s: None,
    )

    assert li.main(["propose", str(src), "--force"], deps=deps) == 0
    assert writes[-1]["artefact"]["artefact_id"] == base_id


@pytest.mark.parametrize(
    "stdout, expected",
    [
        (reply("REJECT", "block", "P1"), "reject"),
        (reply("WORTH-BUILDING", "approve"), "worth-building"),
        (reply("NEEDS-MARK", "abstain", "P2"), "needs-mark"),
        # Severity-less fence is VALID since 17dda19 (absent severity defaults to
        # "none"; a forgetful seat must not lose its vote) — same contract here.
        (json.dumps({"ok": True, "result": "WORTH-BUILDING\n```vote\n{\"stance\":\"approve\"}\n```"}), "worth-building"),
    ],
)
def test_parse_verdict_unwraps_real_envelope(stdout, expected):
    assert li.parse_seat_verdict(stdout).verdict == expected


def test_verdict_token_cooccurrence_reject_wins():
    assert li.parse_seat_verdict(reply("REJECT WORTH-BUILDING", "block", "P1")).verdict == "reject"


@pytest.mark.parametrize("stdout", [
    json.dumps({"ok": True}),
    "{not-json",
    reply("rejected rejection", "block", "P1"),
    reply("REJECT", "approve"),
])
def test_parse_verdict_negative_branches(stdout):
    assert li.parse_seat_verdict(stdout).verdict == "eval-error"


@pytest.mark.parametrize(
    "tokens, expected",
    [
        (["REJECT", "TIMEOUT", "WORTH-BUILDING"], "rejected"),
        (["WORTH-BUILDING", "WORTH-BUILDING", "TIMEOUT"], "eval-error"),
        (["WORTH-BUILDING", "WORTH-BUILDING", "NEEDS-MARK"], "needs-mark"),
        (["WORTH-BUILDING", "WORTH-BUILDING", "WORTH-BUILDING"], "eval-approved"),
    ],
)
def test_outcome_precedence(tokens, expected):
    verdicts = []
    for t in tokens:
        if t == "TIMEOUT":
            verdicts.append(li.SeatVerdict("codex", "eval-error", "timeout", None))
        else:
            verdicts.append(li.parse_seat_verdict(reply(t, {"REJECT": "block", "NEEDS-MARK": "abstain", "WORTH-BUILDING": "approve"}[t])))
    assert li.decide_outcome(verdicts) == expected


def test_dedupe_threshold_and_force(tmp_path, capsys):
    src = tmp_path / "p.md"
    src.write_text("Same title\n" + "alpha " * 50)
    existing = row("learn-same-title-11111111", "proposed", "Same title\n" + "alpha " * 50)
    writes = []
    deps = li.Deps(
        list_proposals=lambda: [existing],
        get_status=lambda _id: None,
        store_fn=lambda intents: writes.append(intents),
        memory_search_fn=lambda *a, **k: [],
        sleep_fn=lambda _s: None,
    )
    assert li.main(["propose", str(src)], deps=deps) == 2
    assert "learn-same-title-11111111 proposed" in capsys.readouterr().out
    assert writes == []
    assert li.similarity("abc", "xyz") < 0.6
    assert li.similarity("same title alpha beta", "same title alpha bet") >= 0.6


def test_memory_search_uses_ssh_sentinel_transport():
    def run_fn(cmd, **kwargs):
        assert cmd[:2] == ["ssh", "arb-prod"]
        assert "docker compose -f deploy/docker-compose.yml exec -T memory python3 -" in cmd[2]
        assert kwargs["capture_output"] is True
        # pin the script's VIABILITY, not just its shape (live gate caught the original
        # omitting the mandatory embed= kwarg and importing a nonexistent connect helper)
        assert "from arb_memory.store import retrieve" in kwargs["input"]
        assert "from arb_memory.embed import embed" in kwargs["input"]
        assert "retrieve(conn, 'query', k=3, embed=embed)" in kwargs["input"]
        assert "psycopg.connect" in kwargs["input"]
        assert "connect_from_env" not in kwargs["input"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='ARB_JSON_BEGIN\n[{"artefact_id": "wiki-one"}]\nARB_JSON_END\n',
        )

    assert li.memory_search("query", 3, run_fn=run_fn) == [{"artefact_id": "wiki-one"}]


def test_panel_registry_and_selection_contract():
    assert li.SEAT_REGISTRY["codex-sol"].target_id == "codex-bridge-dev-example"
    assert li.SEAT_REGISTRY["codex-sol"].effort == "high"
    assert li.SEAT_REGISTRY["fable"].target_id == "asdk-agentredisbridge-dev-example"
    assert li.SEAT_REGISTRY["grok"].target_id == "grok-agentredisbridge-dev-example"
    assert li.resolve_seats("core", []) == [
        li.SEAT_REGISTRY["codex-sol"],
        li.SEAT_REGISTRY["agy"],
        li.SEAT_REGISTRY["glm"],
    ]
    assert li.resolve_seats("full", []) == [
        li.SEAT_REGISTRY[name] for name in ("codex-sol", "agy", "glm", "fable", "grok")
    ]
    assert li.resolve_seats("core", ["grok", "codex-sol", "grok"]) == [
        li.SEAT_REGISTRY["grok"],
        li.SEAT_REGISTRY["codex-sol"],
    ]


@pytest.mark.parametrize(
    ("seat_name", "required_pairs", "absent_flags"),
    [
        ("codex-sol", [("--effort", "high")], ["--worktree"]),
        ("fable", [("--worktree-cleanup", "auto")], ["--effort"]),
        ("grok", [], ["--effort", "--worktree"]),
    ],
)
def test_dispatch_applies_seat_specific_knobs(
    monkeypatch, seat_name, required_pairs, absent_flags
):
    # Slice 1d-iv: ordinary dispatch publishes first, then enqueues without the
    # publish credential. Supply the credential and stub both subprocess stages.
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory-test/0")
    seen = {}
    publish_calls = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "scripts/arb-memory-harness-publish":
            publish_calls.append(cmd)
            target = cmd[cmd.index("--target-agent-id") + 1]
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {
                        "artefact_id": "art-learn-1",
                        "version": 1,
                        "target_agent_id": target,
                        "registration_generation": "gen-1",
                        "worker_vantage": "learn-intake",
                        "content_hash": "hash-learn-1",
                    }
                ),
            )
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=reply("WORTH-BUILDING", "approve"))

    monkeypatch.setattr(li.subprocess, "run", fake_run)

    seat = li.SEAT_REGISTRY[seat_name]
    assert li.dispatch(seat, "task", "run-1") == reply("WORTH-BUILDING", "approve")
    assert publish_calls, "harness publish must run before enqueue"
    assert "--timeout" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--timeout") + 1] == "5400"
    assert seen["kwargs"]["timeout"] == 5460
    assert seen["cmd"][seen["cmd"].index("--engine") + 1] == seat.engine
    assert seen["cmd"][seen["cmd"].index("--target-id") + 1] == seat.target_id
    # Enqueue must not carry the publish credential (store-before-send).
    assert "ARB_MEMORY_REDIS_URL" not in (seen["kwargs"].get("env") or {})
    for flag, value in required_pairs:
        assert seen["cmd"][seen["cmd"].index(flag) + 1] == value
    for flag in absent_flags:
        assert flag not in seen["cmd"]
    if seat_name == "fable":
        worktree = seen["cmd"][seen["cmd"].index("--worktree") + 1]
        assert worktree.startswith("learn-fable-")


def test_eval_brief_states_token_to_stance_mapping():
    # 2026-07-09 live incident: agy + GLM both emitted {"stance":"needs-mark"} — the
    # domain token, not a canonical stance — and were zeroed as eval-error. The brief
    # showed only an "approve" example and never stated the token→stance mapping, so a
    # NEEDS-MARK seat had to guess. The brief must state all three mappings explicitly.
    brief = li._eval_brief("learn-x", "Title\nbody", [], "digest")
    assert 'REJECT -> "block"' in brief
    assert 'WORTH-BUILDING -> "approve"' in brief
    assert 'NEEDS-MARK -> "abstain"' in brief
    # Round-2 sibling (same eval): GLM emitted severity "medium" and was zeroed —
    # severity's closed vocabulary must be stated too.
    assert '"P0", "P1", "P2", or "none"' in brief


def test_evaluate_dispatch_contract_and_drift_chronology(capsys):
    dispatches = []
    stored = []
    state = {"learn-x": row("learn-x", "proposed", "Title\nbody", version=1)}
    rows = [
        row("learn-a", "eval-approved", version=2, created_at="2026-07-07T00:03:00Z"),
        row("learn-b", "rejected", version=2, created_at="2026-07-07T00:01:00Z"),
        row("learn-c", "needs-mark", version=2, created_at="2026-07-07T00:02:00Z"),
    ]

    def dispatch(seat, task, run_id):
        dispatches.append((seat, task, run_id))
        return reply("WORTH-BUILDING", "approve")

    deps = li.Deps(
        list_proposals=lambda: rows,
        get_status=lambda _id: state.get(_id),
        store_fn=lambda intents: (
            stored.append(intents),
            state.__setitem__("learn-x", {
                "artefact_id": "learn-x",
                "version": 2,
                "content": intents[0]["artefact"]["content"],
                "created_at": "2026-07-07T00:04:00Z",
            }),
        ),
        dispatch_fn=dispatch,
        memory_search_fn=lambda *a, **k: [{"artefact_id": "wiki-one"}, {"artefact_id": "wiki-two"}, {"artefact_id": "art-no"}],
        sleep_fn=lambda _s: None,
    )
    assert li.main(["evaluate", "learn-x"], deps=deps) == 0
    assert [d[0].name for d in dispatches] == ["codex-sol", "agy", "glm"]
    assert len({d[2] for d in dispatches}) == 1
    task = dispatches[0][1]
    assert "exactly one uppercase domain token" in task
    assert "```vote" in task and "stance" in task and "severity" in task
    assert "wiki-one" in task and "wiki-two" in task and "art-no" not in task
    assert "approval-rate 1/3" in capsys.readouterr().out
    assert stored


def test_evaluate_custom_seats_dispatches_exact_selection(capsys):
    dispatches = []
    state = {"learn-x": row("learn-x", "proposed", "Title\nbody", version=1)}

    def store_fn(intents):
        state["learn-x"] = {
            "artefact_id": "learn-x",
            "version": 2,
            "content": intents[0]["artefact"]["content"],
            "created_at": "now",
        }

    deps = li.Deps(
        list_proposals=lambda: list(state.values()),
        get_status=lambda learn_id: state.get(learn_id),
        store_fn=store_fn,
        dispatch_fn=lambda seat, task, run_id: dispatches.append(seat.name) or reply("WORTH-BUILDING", "approve"),
        memory_search_fn=lambda *a, **k: [],
        sleep_fn=lambda _s: None,
    )

    assert li.main(["evaluate", "learn-x", "--seat", "fable", "--seat", "grok"], deps=deps) == 0
    assert dispatches == ["fable", "grok"]
    assert "learn-x eval-approved" in capsys.readouterr().out


def test_evaluate_full_panel_dispatches_all_five_seats(capsys):
    dispatches = []
    state = {"learn-x": row("learn-x", "proposed", "Title\nbody", version=1)}

    def store_fn(intents):
        state["learn-x"] = {
            "artefact_id": "learn-x",
            "version": 2,
            "content": intents[0]["artefact"]["content"],
            "created_at": "now",
        }

    deps = li.Deps(
        list_proposals=lambda: list(state.values()),
        get_status=lambda learn_id: state.get(learn_id),
        store_fn=store_fn,
        dispatch_fn=lambda seat, task, run_id: dispatches.append(seat.name)
        or reply("WORTH-BUILDING", "approve"),
        memory_search_fn=lambda *a, **k: [],
        sleep_fn=lambda _s: None,
    )

    assert li.main(["evaluate", "learn-x", "--panel", "full"], deps=deps) == 0
    assert dispatches == ["codex-sol", "agy", "glm", "fable", "grok"]
    assert "learn-x eval-approved" in capsys.readouterr().out


@pytest.mark.parametrize("panel", ["core", "full"])
@pytest.mark.parametrize("panel_first", [True, False])
def test_evaluate_rejects_panel_and_custom_seat_together(panel, panel_first):
    selection = ["--panel", panel, "--seat", "grok"]
    if not panel_first:
        selection = ["--seat", "grok", "--panel", panel]
    with pytest.raises(SystemExit):
        li.build_parser().parse_args(["evaluate", "learn-x", *selection])


def test_drift_rate_counts_approvals_not_rejections_and_orders_by_created_at():
    rows = [
        row("old-approved", "eval-approved", created_at="2026-07-07T00:00:00Z"),
        row("new-approved", "promoted", created_at="2026-07-07T00:04:00Z"),
        row("mid-reject", "rejected", created_at="2026-07-07T00:02:00Z"),
        row("error", "eval-error", created_at="2026-07-07T00:05:00Z"),
    ]

    approved, total, rate = li.drift_rate(rows)

    assert (approved, total, rate) == (2, 3, 2 / 3)


@pytest.mark.parametrize(
    ("statuses", "override", "expected_rc", "stderr_text"),
    [
        (["eval-approved"] * 3 + ["rejected"] * 2, False, 2, "approval-rate refusal"),
        (["eval-approved"] * 3 + ["rejected"] * 2, True, 0, ""),
        (["eval-approved"] * 2 + ["rejected"] * 3, False, 0, "warning"),
        (["eval-approved"] * 4, False, 0, ""),
    ],
)
def test_promote_drift_gate_bands(statuses, override, expected_rc, stderr_text, capsys):
    current = {"row": row("learn-x", "eval-approved", "Title\nbody", version=1)}
    stored = []
    rows = [
        row(f"learn-{idx}", status, created_at=f"2026-07-07T00:{idx:02d}:00Z")
        for idx, status in enumerate(statuses)
    ]

    def store_fn(intents):
        stored.append(intents)
        current["row"] = {
            "artefact_id": "learn-x",
            "version": current["row"]["version"] + 1,
            "content": intents[0]["artefact"]["content"],
            "created_at": "now",
        }

    deps = li.Deps(
        list_proposals=lambda: rows,
        get_status=lambda _id: current["row"],
        store_fn=store_fn,
        sleep_fn=lambda _s: None,
    )
    argv = ["promote", "learn-x"] + (["--override"] if override else [])

    assert li.main(argv, deps=deps) == expected_rc
    assert stderr_text in capsys.readouterr().err
    assert bool(stored) is (expected_rc == 0)


def test_promote_resolve_and_from_workflow(tmp_path, monkeypatch):
    handoff = tmp_path / "handoff.md"
    handoff.write_text("h")
    repo = tmp_path / "repo"
    repo.mkdir()
    seen = []
    monkeypatch.setattr(li.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    referent = li.validate_referent([str(handoff), "abc123"], str(repo), run_fn=li.subprocess.run)
    assert referent == {"workflow_handoff": str(handoff), "workflow_sha": "abc123", "workflow_repo": str(repo)}
    assert seen[-1] == ["git", "-C", str(repo), "cat-file", "-e", "abc123"]
    with pytest.raises(li.LearnError, match="handoff"):
        li.validate_referent([str(tmp_path / "missing.md"), "abc123"], str(repo), run_fn=li.subprocess.run)
    with pytest.raises(li.LearnError, match="badsha"):
        li.validate_referent(
            [str(handoff), "badsha"],
            str(repo),
            run_fn=lambda cmd, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(128, cmd)),
        )

    stored = []
    current = {"row": row("learn-x", "needs-mark", "Title\nbody", version=2)}
    deps = li.Deps(
        list_proposals=lambda: [],
        get_status=lambda _id: current["row"],
        store_fn=lambda intents: (
            stored.append(intents),
            current.__setitem__("row", {
                "artefact_id": "learn-x",
                "version": current["row"]["version"] + 1,
                "content": intents[0]["artefact"]["content"],
                "created_at": "now",
            }),
        ),
        sleep_fn=lambda _s: None,
    )
    assert li.main(["resolve", "learn-x", "approve", "--reason", "mark"], deps=deps) == 0
    assert '"status":"eval-approved"' in stored[-1][0]["artefact"]["content"]

    current["row"] = row("learn-x", "eval-approved", "Title\nbody", version=3, target="project")
    assert li.main(["promote", "learn-x"], deps=deps) == 2
    assert li.main(["promote", "learn-x", "--i-am-mark"], deps=deps) == 0


@pytest.mark.parametrize("status", ["proposed", "eval-error", "eval-approved", "rejected", "promoted"])
def test_resolve_refuses_from_every_non_needs_mark_state(status):
    deps = li.Deps(
        list_proposals=lambda: [],
        get_status=lambda _id: row("learn-x", status),
        store_fn=lambda _intents: pytest.fail("resolve should not write"),
        sleep_fn=lambda _s: None,
    )

    assert li.main(["resolve", "learn-x", "approve", "--reason", "mark"], deps=deps) == 2


def test_main_round_trips(tmp_path):
    src = tmp_path / "p.md"
    src.write_text("Title\nbody")
    state = {}
    mode = {"timeout": True}

    def get_status(learn_id):
        return state.get(learn_id)

    def store_fn(intents):
        content = intents[0]["artefact"]["content"]
        header, _ = li.parse_header(content)
        version = (state.get(intents[0]["artefact"]["artefact_id"], {}) or {}).get("version", 0) + 1
        state[intents[0]["artefact"]["artefact_id"]] = {
            "artefact_id": intents[0]["artefact"]["artefact_id"],
            "version": version,
            "content": content,
            "created_at": f"t{version}",
            "header": header,
        }

    def dispatch(_seat, _task, _run):
        if mode["timeout"]:
            mode["timeout"] = False
            raise subprocess.TimeoutExpired("dispatch", 1)
        return reply("WORTH-BUILDING", "approve")

    deps = li.Deps(
        list_proposals=lambda: list(state.values()),
        get_status=get_status,
        store_fn=store_fn,
        dispatch_fn=dispatch,
        memory_search_fn=lambda *a, **k: [],
        sleep_fn=lambda _s: None,
    )
    assert li.main(["propose", str(src)], deps=deps) == 0
    learn_id = next(iter(state))
    assert li.main(["evaluate", learn_id], deps=deps) == 0
    assert li.parse_header(state[learn_id]["content"])[0]["status"] == "eval-error"
    assert li.main(["evaluate", learn_id, "--retry"], deps=deps) == 0
    assert li.parse_header(state[learn_id]["content"])[0]["status"] == "eval-approved"
    assert li.main(["promote", learn_id], deps=deps) == 0


def test_evaluate_drift_display_includes_the_just_written_outcome(capsys):
    # live-observed: the approval run printed "0/1" because the rate was computed from the
    # pre-eval listing — the drift display must include the outcome it just stored
    old = row("learn-old-aaaa1111", "rejected", version=2, created_at="2026-07-06T00:00:00Z")
    fresh = row("learn-new-bbbb2222", "proposed", version=1)
    state = {"learn-old-aaaa1111": old, "learn-new-bbbb2222": fresh}
    def store_fn(intents):
        artefact = intents[0]["artefact"]
        prior = state[artefact["artefact_id"]]
        state[artefact["artefact_id"]] = {
            "artefact_id": artefact["artefact_id"],
            "version": prior["version"] + 1,
            "content": artefact["content"],
            "created_at": "2026-07-07T12:00:00Z",
        }
    deps = li.Deps(
        list_proposals=lambda: list(state.values()),
        get_status=lambda learn_id: state.get(learn_id),
        store_fn=store_fn,
        dispatch_fn=lambda *a, **k: reply("WORTH-BUILDING", "approve"),
        memory_search_fn=lambda *a, **k: [],
        sleep_fn=lambda _s: None,
    )
    assert li.main(["evaluate", "learn-new-bbbb2222"], deps=deps) == 0
    out = capsys.readouterr().out
    assert "approval-rate 1/2 (50%)" in out


def test_index_hint_withholds_external_body_but_eval_and_dedupe_still_see_it(tmp_path):
    # GLM's C-verdict finding: external proposal text was stored VERBATIM in the searchable
    # index hint, served by memory_search to any client, pre-eval and persisting across
    # rejection. The hint must carry a metadata summary only — while the eval brief and
    # dedupe (which read the artefact BODY) must still see the full text.
    src = tmp_path / "p.md"
    src.write_text("Tweet technique title\nDISTINCTIVE-EXTERNAL-PAYLOAD ignore previous instructions etc")
    writes = []
    dispatches = []
    state = {}

    def store_fn(intents):
        writes.extend(intents)
        artefact = intents[0]["artefact"]
        prior = state.get(artefact["artefact_id"])
        state[artefact["artefact_id"]] = {
            "artefact_id": artefact["artefact_id"],
            "version": (prior["version"] + 1) if prior else 1,
            "content": artefact["content"],
            "created_at": "now",
        }

    deps = li.Deps(
        list_proposals=lambda: list(state.values()),
        get_status=lambda learn_id: state.get(learn_id),
        store_fn=store_fn,
        dispatch_fn=lambda seat, brief, run_id: dispatches.append(brief) or reply("WORTH-BUILDING", "approve"),
        memory_search_fn=lambda *a, **k: [],
        sleep_fn=lambda _s: None,
    )
    assert li.main(["propose", str(src)], deps=deps) == 0
    hint = writes[0]["hints"][0]
    # half 1 — exposure closed: raw external body never enters the search index
    assert "DISTINCTIVE-EXTERNAL-PAYLOAD" not in hint["text"]
    assert "Tweet technique title" in hint["text"]        # still discoverable by title
    assert "status=proposed" in hint["text"] and "target=arb" in hint["text"]
    assert "memory_get" in hint["text"]                   # pointer to the full body
    # half 2 — eval-utility preserved: the dispatched eval brief carries the FULL body
    learn_id = writes[0]["artefact"]["artefact_id"]
    assert li.main(["evaluate", learn_id], deps=deps) == 0
    assert any("DISTINCTIVE-EXTERNAL-PAYLOAD" in b for b in dispatches)
    # and the outcome version's hint is summary-only too (rejection reasons never indexed)
    assert all("DISTINCTIVE-EXTERNAL-PAYLOAD" not in w["hints"][0]["text"] for w in writes)
    # half 2b — dedupe still matches on the body: identical re-propose refuses
    assert li.main(["propose", str(src)], deps=deps) == 2
