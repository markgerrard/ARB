"""roster-preflight: rosterability decisions from live seat facts.

The decision layer (`evaluate`) is pure, so every axis is asserted on its own
specific failure code rather than on a bare "not rosterable" — on a layered
default-deny path a bare refusal is ambient and would stay green with the
mechanism under test deleted (docs/defect-classes/refusal-is-ambient-assert-the-code.md).
"""
import importlib.machinery
import importlib.util
import json
import pathlib

import pytest


_path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "roster-preflight"
_spec = importlib.util.spec_from_loader(
    "roster_preflight",
    importlib.machinery.SourceFileLoader("roster_preflight", str(_path)),
)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


def healthy_registry(**overrides) -> dict[str, str]:
    reg = {
        "worker_vantage": "bridge-dev-mac",
        "registration_generation": "7",
        "brief_hydrate": "v1",
    }
    reg.update(overrides)
    return reg


def row(**overrides) -> dict:
    """A rosterable codex seat, with named fields overridden per test."""
    kwargs = {
        "agent_id": "codex-bridge-dev-example",
        "reg": healthy_registry(),
        "status": "alive:7cd3590cf6b34e7c",
        "engine": "codex",
        "tool_ceiling": "",
        "sender_policy_text": "--sender-policy claude-bridge-dev=trusted",
        "sender": "claude-bridge-dev",
        "running": True,
    }
    kwargs.update(overrides)
    return rp.evaluate(**kwargs)


def codes(result: dict) -> list[str]:
    """The axis code of each failure, e.g. ['A3']."""
    return [f.split()[0] for f in result["failures"]]


# --- the baseline must be clean, or every negative test below proves nothing ---


def test_healthy_codex_seat_is_rosterable():
    result = row()
    assert result["rosterable"], result["failures"]
    assert result["failures"] == []


# --- Axis A: the publish path's own refusals ---


def test_missing_registry_record_fails_a1():
    result = row(reg={})
    assert "A1" in codes(result)
    assert not result["rosterable"]


def test_absent_status_fails_a2_and_names_it_absent():
    result = row(status="")
    assert "A2" in codes(result)
    assert "<absent>" in result["failures"][codes(result).index("A2")]


def test_non_alive_status_fails_a2():
    result = row(status="stopping:7cd3590cf6b34e7c")
    assert "A2" in codes(result)


def test_blank_worker_vantage_fails_a3():
    result = row(reg=healthy_registry(worker_vantage="   "))
    assert "A3" in codes(result)
    assert not result["rosterable"]


def test_missing_worker_vantage_key_fails_a3():
    reg = healthy_registry()
    del reg["worker_vantage"]
    assert "A3" in codes(row(reg=reg))


def test_missing_registration_generation_fails_a4():
    reg = healthy_registry()
    del reg["registration_generation"]
    assert "A4" in codes(row(reg=reg))


def test_owner_token_satisfies_a4_when_generation_absent():
    reg = healthy_registry()
    del reg["registration_generation"]
    reg["owner_token"] = "5cf91f6c1a304f72"
    assert "A4" not in codes(row(reg=reg))


# --- Axis B: hydration, the axis no refusal-set reading gives you ---


def test_brief_hydrate_not_v1_fails_b1():
    result = row(reg=healthy_registry(brief_hydrate="v0"))
    assert "B1" in codes(result)


def test_absent_brief_hydrate_fails_b1():
    reg = healthy_registry()
    del reg["brief_hydrate"]
    assert "B1" in codes(row(reg=reg))


def test_pi_sdk_fails_b2_as_demonstrated_non_hydrator():
    result = row(engine="pi-sdk")
    assert "B2" in codes(result)
    assert "demonstrated" in result["failures"][codes(result).index("B2")]


def test_asdk_without_shell_in_ceiling_fails_b2():
    result = row(engine="asdk", tool_ceiling="Read,Write,Grep")
    assert "B2" in codes(result)
    assert not result["rosterable"]


def test_asdk_with_shell_in_ceiling_passes_b2_but_is_flagged_undemonstrated():
    result = row(engine="asdk", tool_ceiling="Read,Bash,Write")
    assert result["rosterable"], result["failures"]
    assert "NOT yet demonstrated" in result["note"]


def test_b1_v1_alone_does_not_clear_b2():
    """The whole point of Axis B: registry v1 is necessary, not sufficient.

    A pi-sdk seat advertising brief_hydrate=v1 must still fail — this is the
    exact shape that cost two rosters.
    """
    result = row(engine="pi-sdk", reg=healthy_registry(brief_hydrate="v1"))
    assert "B1" not in codes(result)
    assert "B2" in codes(result)
    assert not result["rosterable"]


def test_shell_capable_but_undemonstrated_engine_is_noted_not_failed():
    result = row(engine="grok-acp")
    assert result["rosterable"], result["failures"]
    assert "not yet demonstrated" in result["note"]


def test_unknown_engine_is_noted_not_failed():
    result = row(engine="some-new-engine")
    assert result["rosterable"], result["failures"]
    assert "UNKNOWN" in result["note"]


# --- Axis C: trust ---


def test_untrusted_sender_fails_c1():
    result = row(sender_policy_text="--sender-policy someone-else=trusted")
    assert "C1" in codes(result)


def test_sender_named_but_not_trusted_fails_c1():
    result = row(sender_policy_text="--sender-policy claude-bridge-dev=denied")
    assert "C1" in codes(result)


def test_trusts_sender_requires_the_trusted_marker_not_a_bare_mention():
    assert rp.trusts_sender("claude-bridge-dev=trusted", "claude-bridge-dev")
    assert not rp.trusts_sender("claude-bridge-dev", "claude-bridge-dev")
    assert not rp.trusts_sender("", "claude-bridge-dev")


def test_trust_is_not_widened_to_a_prefix_match():
    """A seat trusting `claude-bridge-dev-two` does not thereby trust us."""
    assert not rp.trusts_sender("claude-bridge-dev-two=trusted", "claude-bridge-dev-x")


# --- not running: argv and trust are unreadable, so nothing can be attested ---


def test_not_running_refuses_and_does_not_claim_engine_facts():
    result = row(running=False, engine="?")
    assert not result["rosterable"]
    assert any("not running" in f for f in result["failures"])
    assert not any(f.startswith("B2") for f in result["failures"])


# --- argv/env precedence ---


def test_engine_prefers_argv_over_env():
    assert rp.engine_of("--engine agy-print", "AGENT_BRIDGE_ENGINE=codex") == "agy-print"


def test_engine_falls_back_to_env_then_default():
    assert rp.engine_of("", "AGENT_BRIDGE_ENGINE=pi-sdk") == "pi-sdk"
    assert rp.engine_of("", "") == "codex"


def test_tool_ceiling_prefers_argv_over_env():
    assert rp.tool_ceiling_of(
        "--agent-sdk-tools Read,Bash", "BRIDGE_AGENT_SDK_TOOLS=Read"
    ) == "Read,Bash"


def test_tool_ceiling_falls_back_to_env():
    assert rp.tool_ceiling_of("", "BRIDGE_AGENT_SDK_TOOLS=Read,Grep") == "Read,Grep"


# --- roster parsing ---


def test_roster_ids_skips_inline_targets(tmp_path):
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({
        "implementor": {"targetId": "inline"},
        "panel": [
            {"targetId": "codex-bridge-dev-example"},
            {"targetId": "inline"},
            {"targetId": "agy-bridge-dev"},
        ],
    }))
    assert rp.roster_ids(roster) == ["codex-bridge-dev-example", "agy-bridge-dev"]


# --- exit codes ---


def _stub_io(monkeypatch, registry_by_id, status_by_id):
    monkeypatch.setattr(rp, "build_proc_map", lambda: {
        "6363": ("codex-bridge-dev-example", "--engine codex --sender-policy claude-bridge-dev=trusted", ""),
        "21656": ("agy-arb-codex-dev", "--engine agy-print --sender-policy claude-bridge-dev=trusted", ""),
    })
    monkeypatch.setattr(rp, "registry", lambda a: registry_by_id.get(a, {}))
    monkeypatch.setattr(rp, "agent_status", lambda a: status_by_id.get(a, ""))


def test_exit_zero_when_every_seat_is_rosterable(monkeypatch, capsys):
    _stub_io(
        monkeypatch,
        {"codex-bridge-dev-example": healthy_registry()},
        {"codex-bridge-dev-example": "alive:7cd3"},
    )
    assert rp.main(["--sender", "claude-bridge-dev", "codex-bridge-dev-example"]) == 0
    assert "rosterable: 1/1" in capsys.readouterr().out


def test_exit_one_when_a_seat_is_not_rosterable(monkeypatch, capsys):
    _stub_io(
        monkeypatch,
        {
            "codex-bridge-dev-example": healthy_registry(),
            "agy-arb-codex-dev": healthy_registry(worker_vantage=""),
        },
        {"codex-bridge-dev-example": "alive:7cd3", "agy-arb-codex-dev": "alive:5cf9"},
    )
    code = rp.main([
        "--sender", "claude-bridge-dev",
        "codex-bridge-dev-example", "agy-arb-codex-dev",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "A3 blank/missing worker_vantage" in out
    assert "rosterable: 1/2" in out


def test_json_output_carries_the_failure_codes(monkeypatch, capsys):
    _stub_io(
        monkeypatch,
        {"agy-arb-codex-dev": healthy_registry(worker_vantage="")},
        {"agy-arb-codex-dev": "alive:5cf9"},
    )
    assert rp.main(["--sender", "claude-bridge-dev", "--json", "agy-arb-codex-dev"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["sender"] == "claude-bridge-dev"
    assert any(f.startswith("A3") for f in payload["seats"][0]["failures"])


def test_no_seats_is_a_usage_error(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        rp.main(["--sender", "claude-bridge-dev"])
    assert excinfo.value.code == 2


# --- the script must resolve ITS OWN checkout, not a fixed clone ---


def test_src_path_is_resolved_from_this_checkout():
    expected = pathlib.Path(__file__).resolve().parent.parent / "src"
    assert rp._SRC == expected
