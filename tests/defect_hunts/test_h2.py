from __future__ import annotations

from skills.defect_hunts.h2_assumptions import validate_h2_section


def test_new_h2_section_object_with_answered_disposition_validates(tmp_path):
    anchor = tmp_path / "tests" / "test_import_contract.py"
    anchor.parent.mkdir()
    anchor.write_text("def test_seat_import_contract():\n    pass\n")
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "answered",
                "violating_run": "PYTHONPATH=. python -m pytest tests/test_import_contract.py",
                "evidence": "tests/test_import_contract.py::test_seat_import_contract",
            }
        ],
    }

    ok, reason = validate_h2_section(section, repo_root=tmp_path)

    assert (ok, reason) == (True, "ok")


def test_not_load_bearing_requires_anchored_evidence(tmp_path):
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "redis:pkg/a.py:redis.from_url#1",
                "disposition": "not_load_bearing",
                "reason": "test-only",
                "evidence": "does/not/exist.py",
            }
        ],
    }

    ok, reason = validate_h2_section(section, repo_root=tmp_path)

    assert ok is False
    assert "evidence anchor" in reason


def test_flag_disposition_validates_with_assumption_and_anchored_evidence(tmp_path):
    anchor = tmp_path / "docs" / "review-notes.md"
    anchor.parent.mkdir()
    anchor.write_text("single process owns the shared git index\n")
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "flag",
                "assumption": "single process owns the shared git index",
                "evidence": "docs/review-notes.md",
            }
        ],
    }

    ok, reason = validate_h2_section(section, repo_root=tmp_path)

    assert (ok, reason) == (True, "ok")


def test_additional_assumptions_are_validated_like_rows(tmp_path):
    section = {
        "coverage_acknowledgment": {
            "acknowledged": True,
            "additional_assumptions": [
                {
                    "candidate_id": "external:pkg/a.py:subprocess.run#1",
                    "disposition": "flag",
                    "assumption": "single process owns the shared git index",
                    "evidence": "docs/missing-review-notes.md",
                }
            ],
        },
        "rows": [],
    }

    ok, reason = validate_h2_section(section, repo_root=tmp_path)

    assert ok is False
    assert "evidence anchor" in reason


def test_rejects_load_bearing_assumption_missing_violating_run(tmp_path):
    anchor = tmp_path / "logs" / "import-check.log"
    anchor.parent.mkdir()
    anchor.write_text("ModuleNotFoundError reproduced\n")
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "answered",
                "evidence": str(anchor.relative_to(tmp_path)),
            }
        ],
    }

    ok, reason = validate_h2_section(section, repo_root=tmp_path)

    assert ok is False
    assert "violating_run" in reason


def test_complete_assumption_with_real_evidence_anchor_passes(tmp_path):
    anchor = tmp_path / "tests" / "test_import_contract.py"
    anchor.parent.mkdir()
    anchor.write_text("def test_seat_import_contract():\n    pass\n")
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "answered",
                "violating_run": "PYTHONPATH=. python -m pytest tests/test_import_contract.py",
                "evidence": "tests/test_import_contract.py::test_seat_import_contract",
            }
        ],
    }

    ok, reason = validate_h2_section(section, repo_root=tmp_path)

    assert (ok, reason) == (True, "ok")


def test_rejects_plausible_evidence_without_existing_anchor(tmp_path):
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "redis:pkg/a.py:redis.from_url#1",
                "disposition": "answered",
                "violating_run": "redis-cli ping with no redis service",
                "evidence": "verified redis startup behavior in the logs",
            }
        ],
    }

    ok, reason = validate_h2_section(section, repo_root=tmp_path)

    assert ok is False
    assert "evidence anchor" in reason


def test_rejects_evidence_anchor_pointing_at_nonexistent_path(tmp_path):
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "answered",
                "violating_run": "cd /tmp/empty && python run_worker.py",
                "evidence": "logs/missing-workdir-check.log",
            }
        ],
    }

    ok, reason = validate_h2_section(section, repo_root=tmp_path)

    assert ok is False
    assert "does not exist" in reason


def test_rejects_non_object_h2_section(tmp_path):
    ok, reason = validate_h2_section("FLAG", repo_root=tmp_path)

    assert ok is False
    assert "object" in reason


def test_rejects_flag_disposition_without_assumption(tmp_path):
    missing_assumption = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [{"candidate_id": "external:pkg/a.py:subprocess.run#1", "disposition": "flag"}],
    }

    ok, reason = validate_h2_section(missing_assumption, repo_root=tmp_path)

    assert ok is False
    assert "assumption" in reason


def test_rejects_flag_disposition_without_evidence(tmp_path):
    missing_evidence = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "flag",
                "assumption": "single process owns the shared git index",
            }
        ],
    }

    ok, reason = validate_h2_section(missing_evidence, repo_root=tmp_path)

    assert ok is False
    assert "evidence" in reason


def test_flag_disposition_blocks(tmp_path):
    anchor = tmp_path / "docs" / "review-notes.md"
    anchor.parent.mkdir()
    anchor.write_text("single process owns the shared git index\n")
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "flag",
                "assumption": "single process owns the shared git index",
                "evidence": "docs/review-notes.md",
            }
        ],
    }

    ok, reason = validate_h2_section(section, repo_root=tmp_path)

    assert (ok, reason) == (True, "ok")


def test_validation_is_deterministic(tmp_path):
    anchor = tmp_path / "tests" / "test_default_config.py"
    anchor.parent.mkdir()
    anchor.write_text("def test_default_config_contract():\n    pass\n")
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": "external:pkg/a.py:subprocess.run#1",
                "disposition": "answered",
                "violating_run": "PYTHONPATH=. python -m pytest tests/test_default_config.py",
                "evidence": "tests/test_default_config.py::test_default_config_contract",
            }
        ],
    }

    first = validate_h2_section(section, repo_root=tmp_path)
    second = validate_h2_section(section, repo_root=tmp_path)

    assert first == second
