"""Unit tests for run_author_round's revision arm-time staging guard.

Incident (art-81438f2f5a5c4955 store v16, 2026-07-19): an --artefact-id
revision was invoked without a staged prior. The tool-bounded author cannot
read ARB Memory, so it authored patch instructions and the harness published
them over the ADR. The driver now fetches and validates HEAD before any
workspace, pointer, child, or write-intent state exists.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
for p in (str(FABA), str(FABA / "subagent")):
    if p not in sys.path:
        sys.path.insert(0, p)

import run_author_round
import bridge_round


@pytest.fixture(autouse=True)
def _admit_existing_driver_tests(monkeypatch):
    monkeypatch.setattr(run_author_round, "gate_hook_wired", lambda repo: None)


BASE_ARGS = [
    "--stage", "changelog",
    "--subject-summary", "guard test",
    "--task", "irrelevant",
]

# A body that passes validate_authored_artefact — what a store-fetched current
# artefact body necessarily looks like, since it was published through the gate.
VALID_PRIOR_BODY = (
    "# prior artefact\n\n**Change summary:** baseline for the guard test.\n\n"
    + "body " * 80
    + "\n"
)


def _arm_args(tmp_path, *, prior=None):
    args = [
        *BASE_ARGS,
        "--artefact-id", "art-existing",
        "--env-file", str(tmp_path / "absent.env"),
    ]
    if prior is not None:
        args.extend(["--prior-record-file", str(prior)])
    return args


def _run_to_child(tmp_path, monkeypatch, fetched, *, prior=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pointer = tmp_path / "pointer.json"
    client = object()
    calls = []
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")
    monkeypatch.setattr(run_author_round, "POINTER", pointer)
    monkeypatch.setattr(run_author_round.redis, "from_url", lambda *a, **kw: client)
    monkeypatch.setattr(run_author_round.tempfile, "mkdtemp", lambda **kw: str(workspace))
    monkeypatch.setattr(
        run_author_round.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )

    def fetch(seen_client, artefact_id, timeout=None):
        calls.append((seen_client, artefact_id, timeout))
        return fetched

    code = run_author_round.main(_arm_args(tmp_path, prior=prior), fetch_by_id=fetch)
    return code, workspace, pointer, client, calls


def test_brief_stage_selects_dispatch_brief_validator(monkeypatch):
    """Stage 'brief' routes content validation through validate_dispatch_brief
    (bound to the selected target vantage), not the generic authored check."""
    from faba_schema import validate_authored_artefact, validate_dispatch_brief

    chosen = run_author_round.content_validator_for_stage(
        "brief", target_vantage="mac-host-dev"
    )
    assert chosen is not validate_authored_artefact
    # Bound partial / lambda must invoke validate_dispatch_brief semantics.
    sample = (
        "# Dispatch brief\n\n## Assumptions\n```json\n{\"items\":[]}\n```\n\n"
        "## Instructions\n\nDo it.\n"
    )
    assert chosen(sample).ok
    # Wrong-vantage demonstrated item must fail under the bound target.
    bad = (
        "# Dispatch brief\n\n## Assumptions\n```json\n"
        '{"items":[{"statement":"x","status":"demonstrated","vantage":"other",'
        '"artefact_id":"art-1","version":1}]}\n'
        "```\n\n## Instructions\n\nDo it.\n"
    )
    assert not chosen(bad).ok
    # Non-brief stages keep the authored artefact validator.
    authored = run_author_round.content_validator_for_stage("design", target_vantage=None)
    assert authored is validate_authored_artefact


def test_native_lock_is_released_when_round_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")
    monkeypatch.setattr(run_author_round, "POINTER", tmp_path / "pointer.json")
    monkeypatch.setattr(run_author_round.redis, "from_url", lambda *a, **kw: object())
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path / "locks")
    monkeypatch.setattr(
        run_author_round, "materialise_author_workspace",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("injected failure")),
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        run_author_round.main([
            *BASE_ARGS, "--run-id", "lock-test", "--env-file", str(tmp_path / "absent.env"),
        ])
    lock = bridge_round.acquire_local_lock("art-faba-au-lock-test")
    bridge_round.release_local_lock(lock)


@pytest.mark.parametrize(
    ("fetched", "failure_class"),
    [
        (None, "timeout"),
        ({"outcome": "not_found"}, "not_found"),
        ({"outcome": "malformed"}, "malformed"),
        ({"outcome": "infra_exhausted"}, "infra_exhausted"),
        # Client-side transport legs refuse too, but under their own names.
        ({"outcome": "request_unsent"}, "request_unsent"),
        ({"outcome": "result_unreadable"}, "result_unreadable"),
        ({"outcome": "driver_error"}, "driver_error"),
        ({"outcome": "binary_unsupported"}, "binary_unsupported"),
        ({"outcome": "ok", "version": 1, "artefact_id": "art-existing"}, "malformed"),
        ({"outcome": "ok", "content": VALID_PRIOR_BODY, "artefact_id": "art-existing"}, "malformed"),
        ({"outcome": "ok", "content": VALID_PRIOR_BODY, "version": 1,
          "artefact_id": "art-other"}, "malformed"),
    ],
)
def test_revision_fetch_refusals_leave_no_workspace_or_pointer(
    tmp_path, monkeypatch, capsys, fetched, failure_class
):
    created = []
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")
    monkeypatch.setattr(run_author_round, "POINTER", tmp_path / "pointer.json")
    monkeypatch.setattr(run_author_round.redis, "from_url", lambda *a, **kw: object())
    monkeypatch.setattr(run_author_round.tempfile, "mkdtemp", lambda **kw: created.append(kw))

    code = run_author_round.main(
        _arm_args(tmp_path), fetch_by_id=lambda client, artefact_id, timeout=None: fetched
    )

    assert code == 2
    assert failure_class in capsys.readouterr().err
    assert created == []
    assert not run_author_round.POINTER.exists()


def test_revision_fetch_stages_crlf_bytes_and_all_provenance(tmp_path, monkeypatch):
    body = VALID_PRIOR_BODY.replace("\n", "\r\n")
    fetched = {
        "outcome": "ok",
        "artefact_id": "art-existing",
        "version": 7,
        "content": body,
        "request_id": "fetch-req-7",
    }
    code, workspace, pointer, client, calls = _run_to_child(tmp_path, monkeypatch, fetched)

    assert code == 1  # child did not fire the content gate; arming itself succeeded
    prior_bytes = body.encode("utf-8")
    assert (workspace / "prior-record.md").read_bytes() == prior_bytes
    author_input = json.loads((workspace / "author-input.json").read_text(encoding="utf-8"))
    expected_provenance = {
        "prior_source": "store-fetch",
        "store_version": 7,
        "fetch_request_id": "fetch-req-7",
        "prior_sha256": hashlib.sha256(prior_bytes).hexdigest(),
    }
    assert expected_provenance.items() <= author_input.items()
    manifest = run_author_round.hash_staged_inputs(workspace, None)
    assert run_author_round.verify_staged_inputs(manifest, workspace) == []
    assert calls == [
        (client, "art-existing", 10.0),
        (client, "art-existing", 10.0),
    ]
    assert not pointer.exists()


def test_revision_override_wins_and_warns_on_reachable_divergent_head(
    tmp_path, monkeypatch, capsys
):
    prior = tmp_path / "override.md"
    prior.write_bytes(VALID_PRIOR_BODY.encode("utf-8"))
    fetched = {
        "outcome": "ok", "artefact_id": "art-existing", "version": 8,
        "content": VALID_PRIOR_BODY + "store moved\n", "request_id": "fetch-8",
    }
    code, workspace, _, client, calls = _run_to_child(
        tmp_path, monkeypatch, fetched, prior=prior
    )

    assert code == 1
    assert (workspace / "prior-record.md").read_bytes() == prior.read_bytes()
    author_input = json.loads((workspace / "author-input.json").read_text(encoding="utf-8"))
    assert author_input["prior_source"] == "operator-file"
    assert author_input["prior_sha256"] == hashlib.sha256(prior.read_bytes()).hexdigest()
    assert "store_version" not in author_input and "fetch_request_id" not in author_input
    err = capsys.readouterr().err
    assert "WARNING" in err and "store_version=8" in err
    assert "prior_sha256=" in err and "store_sha256=" in err
    assert calls == [
        (client, "art-existing", 10.0),
        (client, "art-existing", 10.0),
    ]


def test_revision_override_bus_unreachable_is_silent(tmp_path, monkeypatch, capsys):
    prior = tmp_path / "override.md"
    prior.write_text(VALID_PRIOR_BODY, encoding="utf-8")
    code, _, _, _, _ = _run_to_child(tmp_path, monkeypatch, None, prior=prior)

    assert code == 1
    assert "WARNING" not in capsys.readouterr().err


def test_revision_with_materialised_prior_passes_guard(tmp_path, monkeypatch, capsys):
    """With the prior body materialised, the guard admits the invocation; the
    run then stops at the next check (no bus credential), proving the refusal
    above came from the guard and not from argparse or the env."""
    prior = tmp_path / "prior.md"
    prior.write_text(VALID_PRIOR_BODY, encoding="utf-8")
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    code = run_author_round.main(
        [
            *BASE_ARGS,
            "--artefact-id", "art-existing",
            "--prior-record-file", str(prior),
            "--env-file", str(tmp_path / "absent.env"),
        ]
    )
    assert code == 2
    assert "ARB_MEMORY_REDIS_URL" in capsys.readouterr().err


def test_revision_with_dev_null_prior_is_refused(capsys):
    """F14 route (a): /dev/null passed the presence-only guard and would have
    materialised an EMPTY prior-record.md. Content is now checked."""
    with pytest.raises(SystemExit) as exc:
        run_author_round.main(
            [*BASE_ARGS, "--artefact-id", "art-existing", "--prior-record-file", "/dev/null"]
        )
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "empty" in err
    assert "F14" in err


def test_revision_with_non_artefact_prior_is_refused(tmp_path, capsys):
    """F14: a stray document (too short / headless / no change summary) cannot
    be the current body of an artefact that was published through the gate."""
    prior = tmp_path / "stray.md"
    prior.write_text("just a note\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        run_author_round.main(
            [*BASE_ARGS, "--artefact-id", "art-existing", "--prior-record-file", str(prior)]
        )
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "F14" in err
    assert "publishable artefact body" in err


def test_revision_with_tail_blemished_prior_is_admitted(tmp_path, monkeypatch, capsys):
    """The staged prior is the store as it IS: a v17/v19-class trailing-tag
    blemish in the CURRENT body must not refuse the revision that exists to
    remove it. (Authored OUTPUT keeps the tail check — see faba_schema tests.)
    Same stop-at-bus-credential shape as the valid-prior test above."""
    prior = tmp_path / "blemished.md"
    prior.write_text(VALID_PRIOR_BODY + "</content>\n", encoding="utf-8")
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    code = run_author_round.main(
        [
            *BASE_ARGS,
            "--artefact-id", "art-existing",
            "--prior-record-file", str(prior),
            "--env-file", str(tmp_path / "absent.env"),
        ]
    )
    assert code == 2
    assert "ARB_MEMORY_REDIS_URL" in capsys.readouterr().err


def test_staged_input_mutation_is_detected_with_scopes(tmp_path):
    """Input-mutation incident (2026-07-19 v18 fold): the author child
    overwrote its staged source file. The manifest catches mutation of both
    the workspace copies (provenance-breaking) and the source (baseline
    poisoning), with the right scope on each."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = tmp_path / "source.md"
    source.write_text(VALID_PRIOR_BODY, encoding="utf-8")
    run_author_round.materialise_author_workspace(
        workspace,
        stage="changelog",
        subject_summary="integrity test",
        artefact_id="art-x",
        prior_artefact_id="none",
        prior_record_id="none",
        task="t",
        prior_record_file=source,
    )
    manifest = run_author_round.hash_staged_inputs(workspace, source)
    assert run_author_round.verify_staged_inputs(manifest, workspace) == []

    source.write_text("child output overwrote me\n", encoding="utf-8")
    (workspace / "prior-record.md").write_text("also mutated\n", encoding="utf-8")
    mutations = run_author_round.verify_staged_inputs(manifest, workspace)
    scopes = {m["scope"] for m in mutations}
    assert scopes == {"source", "workspace"}
    assert all(m["staged_sha256"] != m["post_round_sha256"] for m in mutations)


def test_fresh_artefact_needs_no_prior(tmp_path, monkeypatch, capsys):
    """Minting a fresh artefact (no --artefact-id) is not a revision — the
    guard must not demand a prior body there."""
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    code = run_author_round.main(
        [*BASE_ARGS, "--env-file", str(tmp_path / "absent.env")]
    )
    assert code == 2
    assert "ARB_MEMORY_REDIS_URL" in capsys.readouterr().err


# --- FABA-1: an undispatched author must not read as an author that wrote nothing ---
#
# A round whose child orchestrator never dispatches the `faba-author` agent
# failed IDENTICALLY to one where the author ran and produced nothing: both
# surfaced as "content gate never-fired — nothing published". The gate log
# already holds the discriminator (hook_input.agent_type). Cost of that
# ambiguity, measured 2026-07-28: five ~15-minute rounds, four wrong
# hypotheses, all aimed at the author's inputs while the author had never run.


def _gate_log(workspace, agent_types):
    (workspace / "gate-log.jsonl").write_text(
        "".join(
            json.dumps({"hook_input": {"agent_type": t}, "attempts": 0}) + "\n"
            for t in agent_types
        ),
        encoding="utf-8",
    )


def test_author_dispatch_observed_true_when_faba_author_stopped(tmp_path):
    """The healthy shape: a faba-author entry means the author really ran."""
    _gate_log(tmp_path, ["", "faba-author"])
    assert run_author_round.author_dispatch_observed(tmp_path) is True


def test_author_dispatch_observed_false_when_only_untyped_subagents_stopped(tmp_path):
    """The FABA-1 shape: something stopped, but never the author."""
    _gate_log(tmp_path, ["", ""])
    assert run_author_round.author_dispatch_observed(tmp_path) is False


def test_author_dispatch_observed_false_when_no_gate_log_at_all(tmp_path):
    """No SubagentStop ever fired — also not an author that wrote nothing."""
    assert run_author_round.author_dispatch_observed(tmp_path) is False


def test_author_dispatch_observed_survives_malformed_lines(tmp_path):
    """A truncated or partially-written log must not crash the diagnosis."""
    (tmp_path / "gate-log.jsonl").write_text(
        'not json\n{"hook_input": {"agent_type": "faba-author"}}\n{"broken":\n',
        encoding="utf-8",
    )
    assert run_author_round.author_dispatch_observed(tmp_path) is True


def test_never_fired_reason_names_the_undispatched_author(tmp_path):
    """The whole point of FABA-1: the reason must say WHICH failure this was."""
    _gate_log(tmp_path, ["", ""])
    reason = run_author_round.never_fired_reason(None, tmp_path)
    assert "author-never-dispatched" in reason
    assert "--task" in reason, "must point at the actual trigger"


def test_never_fired_reason_keeps_generic_wording_when_author_did_run(tmp_path):
    """Author ran and produced nothing publishable — the ORIGINAL diagnosis,
    which must not be overwritten by the new one."""
    _gate_log(tmp_path, ["faba-author"])
    reason = run_author_round.never_fired_reason(None, tmp_path)
    assert "author-never-dispatched" not in reason
    assert "never-fired" in reason


def test_author_dispatch_observed_true_when_artefact_written_but_no_stop_event(tmp_path):
    """An author killed by the turn timeout mid-write emits no SubagentStop.
    Real shape: the 2026-07-28 fold that hit the then-hardcoded 900s ceiling had
    written a complete 98KB draft. Calling that 'never dispatched' points the
    next reader at the orchestrator instead of the timeout."""
    _gate_log(tmp_path, [""])
    (tmp_path / "artefact.md").write_text("# a real draft\n", encoding="utf-8")
    assert run_author_round.author_dispatch_observed(tmp_path) is True
    assert "author-never-dispatched" not in run_author_round.never_fired_reason(None, tmp_path)
