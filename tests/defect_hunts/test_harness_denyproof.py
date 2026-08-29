from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from skills.defect_hunts.eval.runner import EvalResult, _sealed_scenario, run_eval
from skills.defect_hunts.h1_config_drift import hunt as h1_hunt
from skills.defect_hunts.types import Finding, Scenario, Verdict


FIXTURE_DIR = Path(__file__).parent / "eval"
POSITIVES_PATH = FIXTURE_DIR / "positives.json"
NEGATIVES_PATH = FIXTURE_DIR / "negatives.json"


def test_real_hunt_passes_before_deny_proof_adversaries_are_interpreted():
    result = _real_result()

    assert result.passed
    assert result.recall == 1.0
    assert result.precision == 1.0


def test_no_op_detector_goes_red():
    result = _run(_no_op)

    assert not result.passed
    assert _case(result, "audit-prefix").actual_decision == "CLEAR"
    assert _case(result, "h1-divergence-trap").actual_decision == "CLEAR"


def test_catch_all_detector_goes_red():
    result = _run(_catch_all)

    assert not result.passed
    assert _case(result, "arb-memory-prefix-post-fix").actual_decision == "FLAG"
    assert _case(result, "h1-alias-trap").actual_decision == "FLAG"


def test_class_name_label_matcher_goes_red_because_sealing_removed_names():
    result = _run(_class_name_label_matcher)

    assert not result.passed
    assert _case(result, "audit-prefix").actual_decision == "CLEAR"


def test_mutation_selective_hollow_detector_goes_red_on_clean_surface_feature():
    result = _run(_surface_env_get_matcher)

    assert not result.passed
    mutant = _case(result, "arb-memory-prefix-post-fix")
    assert mutant.expected_decision == "CLEAR"
    assert mutant.actual_decision == "FLAG"


def test_fixture_inspector_goes_red_and_fixture_read_is_denied():
    result = _run(_fixture_inspector)

    assert not result.passed
    assert all(case.sandbox_violation is None for case in result.per_case)
    assert _case(result, "audit-prefix").actual_decision == "CLEAR"
    assert "fixture not visible" in _case(result, "audit-prefix").evidence


def test_environment_leaker_goes_red_and_parent_context_is_denied():
    result = _run(_environment_leaker)

    assert not result.passed
    assert all(case.sandbox_violation is None for case in result.per_case)
    assert _case(result, "audit-prefix").actual_decision == "CLEAR"
    assert "parent context not visible" in _case(result, "audit-prefix").evidence


def test_import_graph_matcher_goes_red_because_import_targets_are_sealed():
    result = _run(_import_graph_matcher)

    assert not result.passed
    assert _case(result, "audit-prefix").actual_decision == "CLEAR"


def test_order_matcher_goes_red_with_fresh_process_per_case():
    result = _run(_order_matcher)

    assert not result.passed
    assert _case(result, "arb-memory-prefix-post-fix").actual_decision == "FLAG"


def test_value_ignoring_same_named_literal_detector_goes_red():
    result = _run(_value_ignoring_same_named_literal_detector)

    assert not result.passed
    assert _case(result, "h1-diverging-literal-clean").expected_decision == "CLEAR"
    assert _case(result, "h1-diverging-literal-clean").actual_decision == "FLAG"


def test_could_not_analyze_spammer_goes_red():
    result = _run(_could_not_analyze_spammer)

    assert not result.passed
    assert all(case.could_not_analyze for case in result.per_case)
    assert not _case(result, "arb-memory-prefix-post-fix").passed


def test_negative_could_not_analyze_spammer_goes_red():
    result = _run(_negative_could_not_analyze_spammer)

    assert not result.passed
    assert _case(result, "audit-prefix").actual_decision == "FLAG"
    assert _case(result, "arb-memory-prefix-post-fix").could_not_analyze
    assert not _case(result, "arb-memory-prefix-post-fix").passed


def test_diff_ignoring_env_literal_detector_goes_red_on_out_of_diff_negative():
    result = _run(_diff_ignoring_env_literal_detector)

    assert not result.passed
    mutant = _case(result, "h1-env-literal-outside-diff")
    assert mutant.expected_decision == "CLEAR"
    assert mutant.actual_decision == "FLAG"


def test_eval_positive_sealing_removes_audit_filename_and_prefix_anchor():
    scenario = _sealed_scenario(_load(POSITIVES_PATH)["cases"][0])
    joined = "\n".join([*scenario.files.keys(), *scenario.files.values(), scenario.diff])

    assert "audit" not in joined.lower()
    assert "audit.py" not in joined
    assert "PREFIX" not in joined


def test_sealed_paths_are_not_class_correlated():
    positives = _load(POSITIVES_PATH)
    negatives = _load(NEGATIVES_PATH)
    path_sets = {
        case["id"]: tuple(sorted(_sealed_scenario(case).files))
        for manifest in (positives, negatives)
        for case in manifest["cases"]
        if case.get("kind") == "H1"
    }

    assert all(all(path.startswith("neutral_pkg/") for path in paths) for paths in path_sets.values())
    assert not any(path.startswith("src/") or path.startswith("pkg/") for paths in path_sets.values() for path in paths)


def test_deny_proof_adversary_red_list_has_at_least_ten_members():
    adversaries = {
        "no-op": _no_op,
        "catch-all": _catch_all,
        "class-name-label-matcher": _class_name_label_matcher,
        "surface-env-get-matcher": _surface_env_get_matcher,
        "fixture-inspector": _fixture_inspector,
        "environment-leaker": _environment_leaker,
        "import-graph-matcher": _import_graph_matcher,
        "order-matcher": _order_matcher,
        "value-ignoring-same-named-literal": _value_ignoring_same_named_literal_detector,
        "cna-spammer": _could_not_analyze_spammer,
        "negative-cna-spammer": _negative_could_not_analyze_spammer,
        "diff-ignoring-env-literal": _diff_ignoring_env_literal_detector,
        "changed-file-only-env-literal": _changed_file_only_env_literal_detector,
    }

    red = [name for name, detector in adversaries.items() if not _run(detector).passed]

    assert len(adversaries) >= 13
    assert red == list(adversaries)


def _real_result() -> EvalResult:
    return _run(h1_hunt)


def _run(hunt) -> EvalResult:
    return run_eval(hunt, _load(POSITIVES_PATH), _load(NEGATIVES_PATH))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _case(result: EvalResult, case_id: str):
    matches = [case for case in result.per_case if case.case_id == case_id]
    assert len(matches) == 1
    return matches[0]


def _no_op(_scenario: Scenario) -> Verdict:
    return Verdict([Finding("adversary", "H1", "CLEAR", "always clear")])


def _catch_all(_scenario: Scenario) -> Verdict:
    return Verdict([Finding("adversary", "H1", "FLAG", "always flag")])


def _class_name_label_matcher(scenario: Scenario) -> Verdict:
    joined = "\n".join([*scenario.files.keys(), *scenario.files.values(), scenario.diff])
    decision = "FLAG" if "ArbMemory" in joined or "audit-prefix" in joined else "CLEAR"
    return Verdict([Finding("adversary", "H1", decision, "class-name label matcher")])


def _surface_env_get_matcher(scenario: Scenario) -> Verdict:
    # The post-fix co-move negative is clean by construction: both sibling
    # readers derive the same value from the same env key. This hollow detector
    # still flags because it keys only on the added surface syntax.
    decision = "FLAG" if "os.environ.get" in scenario.diff else "CLEAR"
    return Verdict([Finding("adversary", "H1", decision, "surface env-get matcher")])


def _fixture_inspector(_scenario: Scenario) -> Verdict:
    import os

    visible = []
    for entry in os.scandir("."):
        visible.append(entry.name)
    decision = "FLAG" if "positives.json" in visible or "tests" in visible else "CLEAR"
    evidence = "fixture visible" if decision == "FLAG" else "fixture not visible"
    return Verdict([Finding("adversary", "H1", decision, evidence)])


def _environment_leaker(_scenario: Scenario) -> Verdict:
    import os

    has_git = os.system("test -d .git >/dev/null 2>&1") == 0
    has_user = bool(os.environ.get("USER"))
    decision = "FLAG" if has_git or has_user else "CLEAR"
    evidence = "parent context visible" if decision == "FLAG" else "parent context not visible"
    return Verdict([Finding("adversary", "H1", decision, evidence)])


def _import_graph_matcher(scenario: Scenario) -> Verdict:
    import ast

    for path, content in scenario.files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "bus" in node.module.split("."):
                    return Verdict([Finding("adversary", "H1", "FLAG", "module bus")])
                for alias in node.names:
                    if alias.name == "ensure_group":
                        return Verdict([Finding("adversary", "H1", "FLAG", "symbol ensure_group")])
    return Verdict([Finding("adversary", "H1", "CLEAR", "sealed import graph")])


def _order_matcher(_scenario: Scenario) -> Verdict:
    sequence = ["FLAG", "CLEAR", "CLEAR", "CLEAR", "FLAG", "CLEAR", "CLEAR", "CLEAR", "CLEAR", "CLEAR"]
    index = getattr(_order_matcher, "_index", 0)
    _order_matcher._index = index + 1
    decision = sequence[index % len(sequence)]
    return Verdict([Finding("adversary", "H1", decision, "fixed manifest-order sequence")])


def _value_ignoring_same_named_literal_detector(scenario: Scenario) -> Verdict:
    import ast

    env_names = set()
    literal_names = set()
    for path, content in scenario.files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content, filename=path)
        for statement in tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            if isinstance(statement.value, ast.Call):
                text = ast.unparse(statement.value)
                if "os.environ.get" in text:
                    env_names.update(names)
            elif isinstance(statement.value, ast.Constant):
                literal_names.update(names)
    decision = "FLAG" if env_names & literal_names else "CLEAR"
    return Verdict([Finding("adversary", "H1", decision, "same-named literal ignoring value")])


def _could_not_analyze_spammer(_scenario: Scenario) -> Verdict:
    return Verdict(
        [
            Finding(
                "adversary",
                "H1",
                "FLAG",
                "could-not-analyze: detector declined the case",
            )
        ]
    )


def _negative_could_not_analyze_spammer(scenario: Scenario) -> Verdict:
    import ast

    env_defaults = {}
    literals = {}
    for path, content in scenario.files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content, filename=path)
        for statement in tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            if isinstance(statement.value, ast.Call):
                text = ast.unparse(statement.value)
                if "os.environ.get" in text and len(statement.value.args) >= 2:
                    default = statement.value.args[1]
                    if isinstance(default, ast.Constant):
                        for name in names:
                            env_defaults[name] = default.value
            elif isinstance(statement.value, ast.Constant):
                for name in names:
                    literals[name] = statement.value.value
    if any(name in literals and literals[name] == default for name, default in env_defaults.items()):
        return Verdict([Finding("adversary", "H1", "FLAG", "positive-shape flag")])
    return Verdict(
        [
            Finding(
                "adversary",
                "H1",
                "FLAG",
                "could-not-analyze: negative case declined",
            )
        ]
    )


def _diff_ignoring_env_literal_detector(scenario: Scenario) -> Verdict:
    import ast
    from pathlib import Path

    by_parent = {}
    for path, content in scenario.files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content, filename=path)
        parent = str(Path(path).parent)
        env_defaults, literals = by_parent.setdefault(parent, ({}, {}))
        for statement in tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            if isinstance(statement.value, ast.Call):
                text = ast.unparse(statement.value)
                if "os.environ.get" in text and len(statement.value.args) >= 2:
                    default = statement.value.args[1]
                    if isinstance(default, ast.Constant):
                        for name in names:
                            env_defaults[name] = default.value
            elif isinstance(statement.value, ast.Constant):
                for name in names:
                    literals[name] = statement.value.value
    decision = "CLEAR"
    for env_defaults, literals in by_parent.values():
        if any(name in literals and literals[name] == default for name, default in env_defaults.items()):
            decision = "FLAG"
            break
    return Verdict([Finding("adversary", "H1", decision, "diff-ignoring env/literal match")])


def _changed_file_only_env_literal_detector(scenario: Scenario) -> Verdict:
    import ast
    from pathlib import Path

    changed_paths = {
        line.removeprefix("+++ b/")
        for line in scenario.diff.splitlines()
        if line.startswith("+++ b/")
    }
    by_parent = {}
    for path, content in scenario.files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content, filename=path)
        parent = str(Path(path).parent)
        env_defaults, literals = by_parent.setdefault(parent, ({}, {}))
        for statement in tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            if isinstance(statement.value, ast.Call):
                text = ast.unparse(statement.value)
                if "os.environ.get" in text and len(statement.value.args) >= 2:
                    default = statement.value.args[1]
                    if isinstance(default, ast.Constant):
                        for name in names:
                            env_defaults[name] = (default.value, path)
            elif isinstance(statement.value, ast.Constant):
                for name in names:
                    literals[name] = (statement.value.value, path)
    decision = "CLEAR"
    for env_defaults, literals in by_parent.values():
        for name, (default, env_path) in env_defaults.items():
            literal = literals.get(name)
            if literal is None:
                continue
            literal_value, literal_path = literal
            if literal_value != default:
                continue
            if env_path in changed_paths or literal_path in changed_paths:
                decision = "FLAG"
                break
        if decision == "FLAG":
            break
    return Verdict([Finding("adversary", "H1", decision, "changed-file-only env/literal match")])
