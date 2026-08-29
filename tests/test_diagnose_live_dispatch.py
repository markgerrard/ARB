from __future__ import annotations

import shutil

import json
import pathlib
import subprocess

import pytest

import skills.diagnose.panel as panel
from skills.diagnose import briefs
from skills.diagnose.diagnose import run_diagnose


CONST = json.loads(pathlib.Path("skills/diagnose/panel_constants.json").read_text())
# Slice 1d-iv: pre-minted quartet replaces free-form positional task body.
ACCEPTED = {
    "--workspace",
    "--engine",
    "--target-id",
    "--role",
    "--timeout",
    "--fresh-context",
    "--adhoc",
    "--artefact-id",
    "--version",
    "--receipt",
    "--brief",
}
FORBIDDEN = {"--model", "--ceiling", "--work-dir"}


def test_roster_is_three_distinct_bridge_voting_seats():
    voting = CONST["roster"]
    assert [s["role"] for s in voting] == ["blind", "alternative", "open"]
    assert all(s["channel"] == "bridge" for s in voting)
    assert len({s["target_id"] for s in voting}) == 3
    assert len({s["vendor"] for s in voting}) == 3
    by_role = {s["role"]: s for s in voting}
    assert by_role["blind"]["engine"] == "codex" and by_role["blind"]["target_id"] == "codex-bridge-dev-example"
    assert by_role["alternative"]["engine"] == "agy-print" and by_role["alternative"]["target_id"] == "agy-bridge-dev"
    assert by_role["open"]["engine"] == "pi-sdk" and by_role["open"]["target_id"] == "pi-sdk-bridge-dev-minimax-m3"
    assert all(s.get("model") for s in voting)
    assert CONST["scribe"]["channel"] == "agent-tool"
    assert CONST["sender"] == "claude-bridge-dev"


def test_bridge_argv_has_positional_task_and_only_accepted_flags(monkeypatch):
    # Credential + harness-publish stub so argv_sink still exercises the enqueue argv.
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory-test/0")

    def fake_run(argv, **kwargs):
        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if argv and argv[0] == "scripts/arb-memory-harness-publish":
            target = argv[argv.index("--target-agent-id") + 1]
            return Result(
                stdout=json.dumps(
                    {
                        "artefact_id": "art-live-1",
                        "version": 1,
                        "target_agent_id": target,
                        "registration_generation": "gen-1",
                        "worker_vantage": "diagnose-panel",
                        "content_hash": "hash-live-1",
                    }
                )
            )
        return Result()

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    captured = {}

    def sink(argv):
        # Temp brief is unlinked when bridge_dispatch returns — read while live.
        captured["argv"] = list(argv)
        brief = pathlib.Path(argv[argv.index("--brief") + 1])
        captured["brief_text"] = brief.read_text(encoding="utf-8")

    d = panel.bridge_dispatch(
        "codex-bridge-dev-example",
        "codex",
        role="reviewer",
        sender="claude-bridge-dev",
        argv_sink=sink,
    )
    d({"role": "blind", "seal": "x", "brief": {"task": "find root cause"}})
    argv = captured["argv"]
    assert argv[0] == "scripts/agent-dispatch"
    flags = {a for a in argv if a.startswith("--")}
    assert flags <= ACCEPTED and not (flags & FORBIDDEN)
    # Brief body holds the sealed task (no free-form positional task string).
    assert "blind" in captured["brief_text"]
    assert "--engine" in argv and argv[argv.index("--engine") + 1] == "codex"


def test_run_panel_records_originating_seat_not_role():
    briefs = [{"role": "blind", "seal": "s1"}]

    def disp(b):
        return {"model": "codex", "from": "codex-bridge-dev-example", "reply": "obs"}

    out = panel.run_panel(briefs, disp, work_dir="/tmp/d13-rp", repo_root=".")
    sub = out["submissions"][0]
    assert sub["seat"] == "codex-bridge-dev-example"
    assert sub["role"] == "blind"


def _subs():
    return [
        {
            "role": "scribe",
            "seat": "haiku",
            "model": "claude-haiku",
            "seal": "z",
            "bus_reply_ref": "file://scribe",
            "bus_reply_sha256": "0" * 64,
        },
        {
            "role": "blind",
            "seat": "codex-bridge-dev-example",
            "model": "codex",
            "seal": "a",
            "bus_reply_ref": "file://blind",
            "bus_reply_sha256": "1" * 64,
        },
    ]


def test_scribe_unreachable_from_post_briefs():
    posts = briefs.author_post_briefs(briefs._load_constants() if hasattr(briefs, "_load_constants") else CONST, _subs(), [])
    for pb in posts:
        subs = pb["brief"].get("sealed_submissions", [])
        assert all(s["role"] != "scribe" for s in subs), "scribe leaked into a post-brief"
        assert all("scribe" not in s.get("bus_reply_ref", "") for s in subs)


def test_certifier_model_never_scribe():
    m = briefs._certifier_model(CONST, [{"author_model": "codex"}])
    assert m != CONST["scribe"]["model"]


def _make_repo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "mod.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import value\n\n"
        "def test_value():\n"
        "    assert value() == 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    return repo, {"failing_test": "tests/test_mod.py::test_value"}


requires_sandbox_exec = pytest.mark.skipif(
    shutil.which("sandbox-exec") is None,
    reason="diagnose containment uses macOS sandbox-exec; unavailable hosts fail-closed with test-containment-unavailable",
)


@requires_sandbox_exec
def test_scribe_exclusion_skill_and_gate_recompute_agree(tmp_path):
    target_ids = {
        "blind": "codex-bridge-dev-example",
        "alternative": "agy-bridge-dev",
        "open": "pi-sdk-bridge-dev-minimax-m3",
        "scribe": "scribe",
    }

    def dispatch(sealed_brief):
        return {
            "model": sealed_brief["model"],
            "from": target_ids[sealed_brief["role"]],
            "reply": f"reply for {sealed_brief['role']}",
        }

    repo, trigger = _make_repo(tmp_path)
    record, reasons = run_diagnose(repo, trigger, tmp_path / "work", dispatch=dispatch)
    assert reasons == []
    assert record["verified"] is True
    for post in record["post_briefs"]:
        subs = post["brief"]["sealed_submissions"]
        assert all(sub["role"] != "scribe" for sub in subs)
        assert all(pathlib.Path(sub["bus_reply_ref"].removeprefix("file://")).name != "scribe.txt" for sub in subs)


def test_certifier_starve_is_typed_not_bare():
    const = {
        "roster": [{"role": "blind", "model": "codex"}],
        "scribe": {"model": "claude-haiku"},
        "certifier": {"rule": "x"},
    }
    with pytest.raises(briefs.CertifierStarved):
        briefs._certifier_model(const, [{"author_model": "codex"}])


def test_certifier_selection_is_stable_order():
    const = {
        "roster": [{"role": "b", "model": "zeta"}, {"role": "a", "model": "alpha"}],
        "scribe": {"model": "claude-haiku"},
        "certifier": {"rule": "x"},
    }
    assert briefs._certifier_model(const, [{"author_model": "zeta"}]) == "alpha"


def test_run_panel_drives_real_dispatch_contract_not_blanket_mock(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory-test/0")

    def fake_run(argv, **kwargs):
        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if argv and argv[0] == "scripts/arb-memory-harness-publish":
            target = argv[argv.index("--target-agent-id") + 1]
            return Result(
                stdout=json.dumps(
                    {
                        "artefact_id": "art-live-2",
                        "version": 1,
                        "target_agent_id": target,
                        "registration_generation": "gen-1",
                        "worker_vantage": "diagnose-panel",
                        "content_hash": "hash-live-2",
                    }
                )
            )
        return Result()

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    seen = []

    def shim(sealed_brief):
        captured = {}

        def sink(argv):
            captured["argv"] = list(argv)
            brief = pathlib.Path(argv[argv.index("--brief") + 1])
            captured["brief_text"] = brief.read_text(encoding="utf-8")

        bd = panel.bridge_dispatch(
            "codex-bridge-dev-example",
            "codex",
            role="reviewer",
            sender="claude-bridge-dev",
            argv_sink=sink,
        )
        bd(sealed_brief)
        seen.append(captured)
        return {"model": "codex", "from": "codex-bridge-dev-example", "reply": "candidate: X"}

    out = panel.run_panel([{"role": "blind", "seal": "s"}], shim, work_dir="/tmp/d13-live", repo_root=".")
    assert out["blocking"] is None
    # Pre-minted path: brief file carries the sealed task body (captured while live).
    assert seen and "--brief" in seen[0]["argv"]
    assert seen[0]["brief_text"].strip()
