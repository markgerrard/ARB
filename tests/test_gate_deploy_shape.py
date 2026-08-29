"""Static deploy-shape assertions for the bus-side gate reader (Slice 1c).

No database. Keeps the documented role/DSN/isolation/default-off shape executable.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ENV = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
DEPLOY_README = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")


def test_deploy_env_documents_gate_reader_credentials():
    assert "ARB_GATE_READER_ROLE" in DEPLOY_ENV
    assert "ARB_GATE_READER_DSN" in DEPLOY_ENV


def test_deploy_readme_documents_isolation_and_default_off():
    assert "assert_gate_role_isolation" in DEPLOY_README
    assert "BRIDGE_CLAIM_GATE=0" in DEPLOY_README
    assert "ARB_GATE_READER_DSN" in DEPLOY_README


def test_deploy_readme_documents_postgres_17_floor_for_maintain():
    """MAINTAIN privilege (readiness probe) exists only on PostgreSQL ≥17."""
    assert "PostgreSQL ≥ 17" in DEPLOY_README or "PostgreSQL >= 17" in DEPLOY_README
    assert "MAINTAIN" in DEPLOY_README


# --- Slice 1d-vi / Task 7: rollout keys, checklists, E25 deploy findings ---

# Keys that must appear in deploy/.env.example (enforced by looping).
REQUIRED_DEPLOY_ENV_KEYS = (
    "ARB_GATE_LANE_WRITER_ROLE",
    "ARB_GATE_LANE_WRITER_DSN",
    "BRIDGE_WORKTREE_LANE",
    "ARB_MEMORY_LOCAL_DSN",
    "ARB_MEMORY_LOCAL_MCP",
    "BRIDGE_WORKER_VANTAGE",
    "BRIDGE_CLAIM_GATE=0",
    "BRIDGE_TASK_REF_REQUIRED=0",
)
# Documented in deploy/README.md only (not env-file keys; do not assert in .env.example).
REQUIRED_DEPLOY_README_ONLY = (
    "BRIDGE_EXEMPT_GIT_SSH_COMMAND",
    "bus-and-gate-daemon-creds-v2",
    "legacy-or-ref-v1",
    "brief_hydrate",
)


def test_deploy_env_documents_lane_writer_vantage_flags_and_local_dsn():
    for key in REQUIRED_DEPLOY_ENV_KEYS:
        assert key in DEPLOY_ENV, f"missing {key} in deploy/.env.example"


def test_deploy_readme_documents_readme_only_rollout_tokens():
    for token in REQUIRED_DEPLOY_README_ONLY:
        assert token in DEPLOY_README, f"missing {token} in deploy/README.md"


def test_deploy_readme_documents_rollout_checklists_and_order():
    assert "ref-required" in DEPLOY_README.lower() or "BRIDGE_TASK_REF_REQUIRED" in DEPLOY_README
    assert "legacy-removal" in DEPLOY_README.lower() or "legacy removal" in DEPLOY_README.lower()
    assert "seat-preflight" in DEPLOY_README
    assert "--checklist" in DEPLOY_README
    # Fifteen-item ordered rollout (plan Step 3)
    assert "Record a Slice 1d base SHA" in DEPLOY_README or "base SHA" in DEPLOY_README
    assert "BRIDGE_TASK_REF_REQUIRED=1" in DEPLOY_README
    assert "BRIDGE_CLAIM_GATE=1" in DEPLOY_README
    # Prerequisites map
    assert "Delivered by" in DEPLOY_README or "delivered by" in DEPLOY_README.lower()
    assert "IdentitiesOnly=yes" in DEPLOY_README
    assert "BRIDGE_EXEMPT_GIT_REMOTE_URL" in DEPLOY_README
    assert "task_wire=legacy-or-ref-v1" in DEPLOY_README or "legacy-or-ref-v1" in DEPLOY_README
    assert "brief_hydrate=v1" in DEPLOY_README or "brief_hydrate" in DEPLOY_README


def test_deploy_readme_documents_e25_interpreter_and_publish_credential():
    """E25 findings: redis-capable interpreter on supervisor PATH; publish cred is process-env."""
    assert "python3" in DEPLOY_README.lower() or "interpreter" in DEPLOY_README.lower()
    assert "redis" in DEPLOY_README.lower()
    # Publish credential must be process environment, not --env-file alone —
    # require the URL and "process environment" as a co-located claim (E25 section).
    marker = "Publish credential is process-env"
    assert marker in DEPLOY_README
    idx = DEPLOY_README.index(marker)
    window = " ".join(DEPLOY_README[idx : idx + 400].lower().split())
    assert "arb_memory_redis_url" in window
    assert "process environment" in window, (
        "ARB_MEMORY_REDIS_URL must be tied to 'process environment' nearby"
    )
    assert "--env-file" in DEPLOY_README


def test_deploy_readme_documents_evidence_trust_and_window_bounds():
    assert "operator-authored" in DEPLOY_README.lower() or "attestation" in DEPLOY_README.lower()
    # Exact phrases at the observation-window bounds paragraph (not "Task 7" / "≥ 17").
    assert "24 hours" in DEPLOY_README
    assert "7 days" in DEPLOY_README
    assert "exempt-git" in DEPLOY_README.lower()
