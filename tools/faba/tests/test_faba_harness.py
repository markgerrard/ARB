"""Unit tests for the FABA launch harness — the deterministic pieces.

The SDK session itself is exercised by the live smoke run, not here; these cover
the properties the architecture leans on: cache-stable template prefix,
harness-side gating that never trusts the agent, and receipt polling.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
for path in (str(FABA), str(FABA.parents[1] / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from faba_launch import gate_decision, load_env_file, parse_exit_line, render_bootstrap  # noqa: E402
from faba_record import poll_receipt  # noqa: E402

TEMPLATE = (FABA / "bootstrap_template.md").read_text(encoding="utf-8")


def _variables(**overrides):
    base = {
        "workspace": "/tmp/ws",
        "round": "1",
        "artefact_id": "art-x",
        "subject_summary": "art-x is the toy subject",
        "prior_record_id": "none",
        "record_artefact_id": "art-faba-1",
        "task": "do the thing",
    }
    base.update(overrides)
    return base


class TestRenderBootstrap:
    def test_invariant_prefix_is_byte_stable_across_variable_changes(self):
        prompt_a, sha_a = render_bootstrap(TEMPLATE, _variables())
        prompt_b, sha_b = render_bootstrap(TEMPLATE, _variables(task="a different round", round="7"))
        assert sha_a == sha_b
        marker_a = prompt_a.index("<!-- ROUND VARIABLES BELOW")
        marker_b = prompt_b.index("<!-- ROUND VARIABLES BELOW")
        assert prompt_a[:marker_a] == prompt_b[:marker_b]
        assert prompt_a[marker_a:] != prompt_b[marker_b:]

    def test_variables_land_only_in_the_tail(self):
        prompt, _ = render_bootstrap(TEMPLATE, _variables(task="UNIQUE-TASK-SENTINEL"))
        marker = prompt.index("<!-- ROUND VARIABLES BELOW")
        assert "UNIQUE-TASK-SENTINEL" not in prompt[:marker]
        assert "UNIQUE-TASK-SENTINEL" in prompt[marker:]

    def test_unresolved_placeholder_raises(self):
        variables = _variables()
        del variables["task"]
        with pytest.raises(ValueError, match="unresolved"):
            render_bootstrap(TEMPLATE, variables)

    def test_missing_marker_raises(self):
        with pytest.raises(ValueError, match="marker"):
            render_bootstrap("no marker here {{task}}", _variables())

    def test_placeholder_in_head_raises_not_leaks(self):
        """PF5: a placeholder above the marker is a template bug, never emitted."""
        broken = "head {{oops}}\n" + TEMPLATE
        with pytest.raises(ValueError, match="invariant head"):
            render_bootstrap(broken, _variables())

    def test_braces_inside_variable_value_do_not_abort(self):
        """PF6: task text citing a placeholder (e.g. '{{workspace}}') is data,
        not an unresolved variable."""
        prompt, _ = render_bootstrap(TEMPLATE, _variables(task="inject {{workspace}} literally"))
        assert "inject {{workspace}} literally" in prompt


class TestParseExitLine:
    def test_parses_final_exit_line(self):
        text = 'Work done.\nFABA_EXIT {"record_artefact_id": "art-faba-1", "status": "ok", "recommendation": "merge"}'
        parsed = parse_exit_line(text)
        assert parsed == {"record_artefact_id": "art-faba-1", "status": "ok", "recommendation": "merge"}

    def test_absent_line_is_none(self):
        assert parse_exit_line("no exit line at all") is None
        assert parse_exit_line("") is None

    def test_malformed_json_is_none_not_crash(self):
        assert parse_exit_line("FABA_EXIT {not json") is None

    def test_last_exit_line_wins(self):
        text = 'FABA_EXIT {"status": "failed"}\nrevised...\nFABA_EXIT {"status": "ok"}'
        assert parse_exit_line(text) == {"status": "ok"}


class TestGateDecision:
    def test_stored_receipt_passes(self):
        passed, reason = gate_decision(
            {"artefact_outcome": "stored", "artefact_id": "art-faba-1", "version": 1}, "art-faba-1"
        )
        assert passed
        assert "art-faba-1" in reason

    def test_deduped_receipt_passes(self):
        passed, _ = gate_decision(
            {"artefact_outcome": "deduped", "artefact_id": "art-faba-1", "version": 2}, "art-faba-1"
        )
        assert passed

    def test_no_receipt_fails(self):
        passed, reason = gate_decision(None, "art-faba-1")
        assert not passed
        assert "no receipt" in reason

    def test_failed_outcome_fails(self):
        passed, _ = gate_decision({"artefact_outcome": "failed", "reason": "deadlettered"}, "art-faba-1")
        assert not passed

    def test_wrong_artefact_id_fails(self):
        # An agent publishing under a different id than the harness assigned must not pass.
        passed, _ = gate_decision(
            {"artefact_outcome": "stored", "artefact_id": "art-other", "version": 1}, "art-faba-1"
        )
        assert not passed


class TestLoadEnvFile:
    def test_bridge_bus_keys_never_enter_the_environment(self, tmp_path, monkeypatch):
        # AGENT_REDIS_* leakage reroutes child dispatches to the wrong bus (r3 incident):
        # process env outranks --env-file in the dispatch layer, so the harness must
        # only ever admit Memory-bus keys.
        env = tmp_path / "seed.env"
        env.write_text(
            "AGENT_REDIS_HOST=wrong-bus.example.com\n"
            "AGENT_REDIS_PORT=25061\n"
            "AGENT_REDIS_PASSWORD=secret\n"
            "ARB_MEMORY_REDIS_URL=rediss://right/12\n"
            "# comment\n",
            encoding="utf-8",
        )
        for key in ("AGENT_REDIS_HOST", "AGENT_REDIS_PORT", "AGENT_REDIS_PASSWORD", "ARB_MEMORY_REDIS_URL"):
            monkeypatch.delenv(key, raising=False)
        applied = load_env_file(env)
        assert applied == {"ARB_MEMORY_REDIS_URL": "rediss://right/12"}
        import os
        assert "AGENT_REDIS_HOST" not in os.environ
        assert os.environ["ARB_MEMORY_REDIS_URL"] == "rediss://right/12"

    def test_explicit_env_still_wins(self, tmp_path, monkeypatch):
        env = tmp_path / "seed.env"
        env.write_text("ARB_MEMORY_REDIS_URL=rediss://from-file/12\n", encoding="utf-8")
        monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "rediss://explicit/12")
        applied = load_env_file(env)
        assert applied == {}
        import os
        assert os.environ["ARB_MEMORY_REDIS_URL"] == "rediss://explicit/12"


class FakeRedis:
    def __init__(self, responses):
        self.responses = list(responses)
        self.keys_seen = []

    def lrange(self, key, start, end):
        self.keys_seen.append(key)
        return self.responses.pop(0) if self.responses else []


class TestPollReceipt:
    def test_returns_parsed_receipt_when_present(self):
        envelope = json.dumps({"artefact_outcome": "stored", "artefact_id": "a", "version": 1})
        fake = FakeRedis([[envelope]])
        receipt = poll_receipt("req-1", timeout=5, client=fake, prefix="")
        assert receipt["artefact_outcome"] == "stored"
        assert fake.keys_seen == ["arbmem:write_result:req-1"]

    def test_times_out_to_none(self):
        fake = FakeRedis([[]])
        assert poll_receipt("req-1", timeout=0, client=fake, prefix="") is None


# --- compose_contract: the one-surface composition (panel-faba-sa P2: production
# hashes the COMPOSED bytes, so the composed path is what needs the coverage) ---

from faba_launch import CONTRACT_MARKER, VARIABLES_MARKER, compose_contract  # noqa: E402

CONTRACT = (FABA / "round-contract.md").read_text(encoding="utf-8")


def test_compose_contract_replaces_marker_in_head():
    composed = compose_contract(TEMPLATE, CONTRACT)
    assert CONTRACT_MARKER not in composed
    head, _, _ = composed.partition(VARIABLES_MARKER)
    assert "## Contract" in head and "## Rails" in head


def test_compose_contract_missing_marker_raises():
    stripped = TEMPLATE.replace(CONTRACT_MARKER, "")
    with pytest.raises(ValueError, match="contract marker"):
        compose_contract(stripped, CONTRACT)


def test_compose_contract_marker_below_variables_raises():
    moved = TEMPLATE.replace(CONTRACT_MARKER, "") + "\n" + CONTRACT_MARKER + "\n"
    with pytest.raises(ValueError, match="contract marker"):
        compose_contract(moved, CONTRACT)


def test_composed_template_renders_and_sha_covers_contract():
    """Production parity: compose(real template, real contract) -> render succeeds,
    and the invariant sha moves when the CONTRACT changes (one-surface property)."""
    _, sha_a = render_bootstrap(compose_contract(TEMPLATE, CONTRACT), _variables())
    _, sha_b = render_bootstrap(
        compose_contract(TEMPLATE, CONTRACT + "\n- extra rail\n"), _variables()
    )
    assert sha_a != sha_b


def test_contract_carries_no_placeholders():
    """A {{placeholder}} in the contract would land in the invariant head and leak
    unrendered (PF5 shape) — the shared surface must stay placeholder-free."""
    assert "{{" not in CONTRACT


def test_main_stages_operator_prior_crlf_bytes_exactly(tmp_path, monkeypatch):
    import faba_launch

    prior = tmp_path / "prior.md"
    prior_bytes = (
        "# FABA decision record — round 1\r\n"
        "Subject: art-x | Prior record: none | Status: ok\r\n\r\n"
        "## Round task\r\nt\r\n\r\n## Findings\r\n"
        "| id | severity | status | evidence (command, exit code, ref) |\r\n"
        "|----|----|----|----|\r\n\r\n"
        "## Recommendation\r\nr\r\n\r\n## Open items\r\nnone\r\n"
    ).encode("utf-8")
    prior.write_bytes(prior_bytes)
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")

    async def fake_run_session(*args, **kwargs):
        return {"text": "", "is_error": False}

    monkeypatch.setattr(faba_launch, "run_session", fake_run_session)
    monkeypatch.setattr(
        faba_launch,
        "publish_and_gate",
        lambda *args, **kwargs: (False, "test stop", None, None),
    )

    code = faba_launch.main(
        [
            "--artefact-id", "art-x",
            "--subject-summary", "CRLF staging test",
            "--round", "2",
            "--prior-record-id", "art-prior",
            "--prior-record-file", str(prior),
            "--task", "test byte-exact staging",
            "--workspace", str(workspace),
        ]
    )

    assert code == 1
    assert (workspace / "prior-record.md").read_bytes() == prior_bytes


class TestPublishArtefactAndGate:
    """Validation-first refusal paths — no bus access before content passes
    (same posture as publish_and_gate; the redis import sits below validation)."""

    def test_missing_artefact_refuses_without_bus(self, tmp_path):
        from faba_launch import publish_artefact_and_gate

        passed, reason, receipt, check = publish_artefact_and_gate(
            "redis://unreachable.invalid:1/0",
            workspace=tmp_path,
            artefact_id="art-au-x",
            request_id="req-x",
            author="faba-au-test",
            receipt_timeout=0.1,
        )
        assert not passed
        assert "no artefact.md" in reason
        assert receipt is None and check is None

    def test_invalid_artefact_refuses_without_bus(self, tmp_path):
        from faba_launch import publish_artefact_and_gate

        (tmp_path / "artefact.md").write_text("too short, no title", encoding="utf-8")
        passed, reason, receipt, check = publish_artefact_and_gate(
            "redis://unreachable.invalid:1/0",
            workspace=tmp_path,
            artefact_id="art-au-x",
            request_id="req-x",
            author="faba-au-test",
            receipt_timeout=0.1,
        )
        assert not passed
        assert "authored-artefact check" in reason
        assert check is not None and not check.ok

    # F14(c) hygiene tier (panel panel-f14c-design-20260720T033218Z-5ec74f):
    # revision publishes refuse pre-bus on a missing/invalid prior or a
    # nothing-folded artefact. Same validation-first posture — no redis needed
    # for any refusal below (unreachable URL proves it).

    VALID_BODY = (
        "# Design — frobnicator\n\n**Change summary:** first draft.\n\n"
        + "body paragraph with enough substance to clear the stub floor. " * 8
        + "\n"
    )

    def test_revision_without_prior_refuses_without_bus(self, tmp_path):
        from faba_launch import publish_artefact_and_gate

        (tmp_path / "artefact.md").write_text(self.VALID_BODY, encoding="utf-8")
        passed, reason, receipt, check = publish_artefact_and_gate(
            "redis://unreachable.invalid:1/0",
            workspace=tmp_path,
            artefact_id="art-au-x",
            request_id="req-x",
            author="faba-au-test",
            receipt_timeout=0.1,
            revision=True,
        )
        assert not passed
        assert "prior-record.md" in reason

    def test_revision_nothing_folded_refuses_without_bus(self, tmp_path):
        from faba_launch import publish_artefact_and_gate

        (tmp_path / "artefact.md").write_text(self.VALID_BODY, encoding="utf-8")
        (tmp_path / "prior-record.md").write_text(self.VALID_BODY, encoding="utf-8")
        passed, reason, receipt, check = publish_artefact_and_gate(
            "redis://unreachable.invalid:1/0",
            workspace=tmp_path,
            artefact_id="art-au-x",
            request_id="req-x",
            author="faba-au-test",
            receipt_timeout=0.1,
            revision=True,
        )
        assert not passed
        assert "revision-fold" in reason
        assert check is not None and not check.ok

    def test_revision_blemished_prior_admitted_fold_differs(self, tmp_path):
        """Tail-blemished prior (store as it IS) must not refuse the revision
        that removes the blemish; refusal here must come from the BUS being
        unreachable, i.e. validation passed."""
        from faba_launch import publish_artefact_and_gate

        (tmp_path / "artefact.md").write_text(self.VALID_BODY, encoding="utf-8")
        (tmp_path / "prior-record.md").write_text(self.VALID_BODY + "</content>\n", encoding="utf-8")
        try:
            passed, reason, receipt, check = publish_artefact_and_gate(
                "redis://unreachable.invalid:1/0",
                workspace=tmp_path,
                artefact_id="art-au-x",
                request_id="req-x",
                author="faba-au-test",
                receipt_timeout=0.1,
                revision=True,
            )
        except Exception:
            return  # reached the bus layer — validation admitted the fold
        assert "prior" not in reason and "revision-fold" not in reason
