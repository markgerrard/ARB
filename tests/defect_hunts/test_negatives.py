from __future__ import annotations

import ast
import importlib
import json
import tempfile
import subprocess
import sys
import types
from pathlib import Path

from skills.defect_hunts.h1_config_drift import hunt as h1_hunt
from skills.defect_hunts.h2_assumptions import validate_h2_section
from skills.defect_hunts.scenarios import seal_files
from skills.defect_hunts.types import Verdict


NEGATIVES_PATH = Path(__file__).parent / "eval" / "negatives.json"
MANIFEST_PATH = Path(__file__).parents[2] / "docs" / "defect-classes" / "eval-manifest.md"
REPO_ROOT = Path(__file__).parents[2]


def test_negatives_manifest_drives_h1_precision_cases():
    negatives = _load_negatives()
    cases = {case["id"]: case for case in negatives["cases"]}

    required_ids = {
        "arb-memory-prefix-post-fix",
        "bridge-notify-inbox",
        "bridge-pi-thinking-level",
        "h1-alias-trap",
        "h1-divergence-trap",
        "h1-defaultless-env-trap",
        "h2-safe-pinned-assumption",
        "arb-memory-dsn",
        "arb-memory-redis-url",
    }
    assert required_ids <= set(cases)
    assert "bridge-role-profile-file" not in cases

    for case in cases.values():
        if case["id"].endswith("-trap") or case["id"] == "h2-safe-pinned-assumption":
            assert case.get("baits")

    for case_id, expected in {
        "arb-memory-prefix-post-fix": "CLEAR",
        "bridge-notify-inbox": "CLEAR",
        "bridge-pi-thinking-level": "CLEAR",
        "h1-alias-trap": "CLEAR",
        "h1-divergence-trap": "FLAG",
        "h1-defaultless-env-trap": "CLEAR",
        "h2-safe-pinned-assumption": "CLEAR",
        "arb-memory-dsn": "CLEAR",
        "arb-memory-redis-url": "CLEAR",
    }.items():
        verdict = h1_hunt(_scenario_from_case(cases[case_id]))
        assert _decisions(verdict) == [expected], case_id
        if case_id == "h1-divergence-trap":
            assert "literal sibling equals env default" in verdict.findings[0].evidence


def test_h2_safe_case_is_a_complete_brief_that_flags_nothing():
    case = _case_by_id(_load_negatives(), "h2-safe-pinned-assumption")

    ok, reason = validate_h2_section(case["h2_section"], repo_root=REPO_ROOT)

    assert ok, reason
    rows = case["h2_section"]["rows"] + case["h2_section"]["coverage_acknowledgment"]["additional_assumptions"]
    assert all(row.get("disposition") != "flag" for row in rows)


def test_arb_memory_dsn_and_redis_url_readers_co_move_with_env(monkeypatch):
    readers = _grep_env_readers("ARB_MEMORY_DSN") | _grep_env_readers("ARB_MEMORY_REDIS_URL")
    assert readers == {
        "src/agent_redis_bridge/README.md",  # DOCUMENTATION mention only: the comms-plane README names the credential when it explains harness-publish (holds ARB_MEMORY_REDIS_URL) vs dispatch (env -u). No env is read; the grep-guard flags the literal wherever it occurs, so it is classified here.
        "src/agent_redis_bridge/bridge.py",  # audit-bus tee; Bridge builds redis from the env/env-file URL at init.
        # ctl.py dropped from this set when the non-FABA enqueue publish-scrub was
        # centralized: it now calls dispatch_authority.filter_publish_env (which
        # strips the whole PUBLISH_CREDENTIAL_ENV set) instead of naming the literal,
        # so it no longer directly reads ARB_MEMORY_REDIS_URL. The credential-handling
        # responsibility moved to dispatch_authority.py (below), still tracked here.
        "src/agent_redis_bridge/dispatch_authority.py",  # Slice 1d-iv: FABA-only harness publish reads the credential; non-FABA path refuses ambient URL. Also home of PUBLISH_CREDENTIAL_ENV + filter_publish_env/pop_publish_env (the central publish-scrub).
        "src/agent_redis_bridge/dispatch_cli.py",  # Slice 1d-iv: strips ARB_MEMORY_REDIS_URL for non-FABA enqueue_pre_minted; FABA draft may pass redis_url.
        "src/agent_redis_bridge/engines/agent_sdk.py",  # scrub-VERIFIER only (Slice 1d-i): reads REDIS_URL/gate vars solely to build the secrets-to-redact + var-names-to-blank lists for the SDK child; constructs no client, and the value never leaves the redaction path.
        "src/agent_redis_bridge/engines/_stdio.py",  # DOCSTRING mention only (Slice 1d-i): the literal appears in prose explaining the _REDIS_URL-suffix scrub; the predicate matches by suffix and reads no env. Grep-guard flags the string wherever it occurs, so it must be classified here.
        "src/agent_redis_bridge/learn_intake.py",  # advisory-search inline script only; the DSN is read inside the memory container, never locally. Also short-lived FABA publish gate (1d-iv).
        "src/agent_redis_bridge/wiki_refresh.py",  # STORE_SCRIPT_TEMPLATE only; the env is read on the droplet inside the memory container, never by the local process. Also short-lived FABA publish gate (1d-iv).
        "src/arb_warm_orch/subprocess_dispatch.py",  # warm-orch live dispatcher: strips ARB_MEMORY_REDIS_URL from the agent-dispatch env (publish keeps it as the credential); constructs no memory client.
        "src/arb_memory/audit.py",  # resolve_audit_env (a79f14bf): ARB_AUDIT_* wins, ARB_MEMORY_* is the fallback so daemon and vote-emitting CLIs resolve the audit plane IDENTICALLY — a mid-migration host with the two pointed at different buses otherwise splits its audit plane and only finds out at verdict close (refused_reconcile). NOT new surface: arb-panel-vote and arb-audit-emit each read ARB_MEMORY_REDIS_URL directly before that commit; centralizing here took two direct readers to one. They live under scripts/, which this guard does not scan, so the read only now became visible to it.
        "src/arb_memory/client.py",
        "src/arb_memory/journey_export.py",  # export job runs in the memory container under its own DSN env; legitimate direct reader.
        "src/arb_memory/local_read_policy.py",  # local-read match-check only; compares env DSNs, no client constructed.
        "src/arb_memory/mcp/config.py",
        "src/arb_memory/run.py",
        "src/arb_memory/writer.py",  # writer proxy service builds its async bus client from its own container env at app construction (928d18aa); injected client wins, env is the deploy-time fallback.
        "src/arb_registration/bus_acl.py",  # COMMENT mention only (97528c74): the literal appears in prose explaining why worker rules carry NO audit selectors — a seat's audit emit rides a separate connection under the audit-emitter credential, so DB-12 audit grants would be dead weight. The module reads no environment at all (zero os.environ/getenv/env.get in the file) and constructs no client; it emits ACL rule strings. Grep-guard flags the string wherever it occurs, so it must be classified here.
        "src/arb_registration/bus_registrar.py",  # NAME-as-data only (8f0aaaa0): the literal is a member of ALLOWED_PRODUCER_ENVS, the allowlist a registrant's self-report producer entry is validated AGAINST (env_var not in the set -> ValueError). The name is compared, never resolved to a value; the module reads no environment and builds no client from it.
    }

    redis_calls: list[tuple[str, bool]] = []
    psycopg_calls: list[tuple[str, bool | None]] = []
    bridge = importlib.import_module("agent_redis_bridge.bridge")

    fake_redis = types.SimpleNamespace(
        from_url=lambda url, decode_responses=False, **_kwargs: redis_calls.append((url, decode_responses))
        or {"url": url}
    )
    fake_psycopg = types.SimpleNamespace(
        connect=lambda dsn, autocommit=None: psycopg_calls.append((dsn, autocommit)) or {"dsn": dsn}
    )
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    dsn = "postgresql://arb_memory:changed@db.example/arb_memory"
    redis_url = "redis://cache.example:6380/15"
    monkeypatch.setenv("ARB_MEMORY_DSN", dsn)
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", redis_url)
    monkeypatch.delenv("ARB_MEMORY_MCP_DSN", raising=False)
    monkeypatch.setenv("ARB_MEMORY_MCP_PUBLIC_BASE_URL", "https://memory.example")
    monkeypatch.setenv("ARB_MEMORY_MCP_LOGIN_SECRET", "login-secret")
    monkeypatch.setenv("ARB_MEMORY_MCP_TOTP_SECRET", "totp-secret")

    client = importlib.reload(importlib.import_module("arb_memory.client"))
    run = importlib.reload(importlib.import_module("arb_memory.run"))
    config = importlib.reload(importlib.import_module("arb_memory.mcp.config"))

    client._redis_client()
    run._redis_client()
    run._memory_conn()
    settings = config.load_settings()
    config.mcp_connect(settings)

    # bridge.py is a sanctioned reader: the audit-bus tee resolves ARB_MEMORY_REDIS_URL
    # from process env (or the parsed env-file fallback) and constructs its audit Redis
    # client from that value during Bridge init. local_read_policy.py is exempt from a
    # client exercise because it performs a DSN fingerprint comparison only.
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "workdir"
        workdir.mkdir()
        env_file = Path(tmp) / ".env"
        env_file.write_text(
            "AGENT_REDIS_HOST=127.0.0.1\n"
            "AGENT_REDIS_PORT=6390\n"
            "AGENT_REDIS_DB=12\n"
            "AGENT_REDIS_PREFIX=agent_scratch:\n"
            "AGENT_WORKSPACE=dev\n"
            "AGENT_PROJECT=project-c\n"
        )
        monkeypatch.setattr(bridge, "RedisCli", lambda config: {"config": config})
        args = bridge.build_parser().parse_args(["--env-file", str(env_file), "--workdir", str(workdir)])
        instance = bridge.Bridge(args)
        assert instance.audit_redis == {"url": redis_url}

    assert redis_calls == [
        (redis_url, True),
        (redis_url, True),
        (redis_url, True),
    ]
    assert psycopg_calls == [
        (dsn, None),
        ("postgresql://arbmem_mcp:changed@db.example/arb_memory", True),
    ]
    assert settings.mcp_dsn == "postgresql://arbmem_mcp:changed@db.example/arb_memory"


def test_eval_manifest_logs_bridge_max_parallel_as_finding_not_negative():
    manifest = MANIFEST_PATH.read_text(encoding="utf-8")

    assert "BRIDGE_MAX_PARALLEL" in manifest
    assert "finding" in _section_for(manifest, "BRIDGE_MAX_PARALLEL").lower()
    assert "negative" not in _section_for(manifest, "BRIDGE_MAX_PARALLEL").lower()
    assert "deferred-class roadmap" in manifest.lower()


def _load_negatives() -> dict:
    return json.loads(NEGATIVES_PATH.read_text(encoding="utf-8"))


def _case_by_id(negatives: dict, case_id: str) -> dict:
    matches = [case for case in negatives["cases"] if case["id"] == case_id]
    assert len(matches) == 1
    return matches[0]


def _scenario_from_case(case: dict):
    return seal_files(
        case["files"],
        case["diff"],
        anchors={
            "filenames": list(case.get("anchors", {}).get("filenames", [])),
            "symbols": _top_level_symbols(case["files"]),
            "scenario_ids": [case["id"]],
        },
    )


def _decisions(verdict: Verdict) -> list[str]:
    return [finding.decision for finding in verdict.findings]


def _top_level_symbols(files: dict[str, str]) -> list[str]:
    symbols: set[str] = set()
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content, filename=path)
        for statement in tree.body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.add(statement.name)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        symbols.add(target.id)
    return sorted(symbols)


def _grep_env_readers(env_name: str) -> set[str]:
    # Pure-Python search (no external `rg`/`grep` binary): portable across hosts and
    # subprocess PATHs. Matches `rg -l <env> src` semantics: every text file under src/
    # that contains the literal env name, as a repo-relative posix path.
    readers: set[str] = set()
    for path in (REPO_ROOT / "src").rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if env_name in text:
            readers.add(path.relative_to(REPO_ROOT).as_posix())
    return readers


def _section_for(manifest: str, needle: str) -> str:
    index = manifest.index(needle)
    previous_heading = manifest.rfind("\n##", 0, index)
    next_heading = manifest.find("\n##", index)
    start = previous_heading if previous_heading != -1 else 0
    end = next_heading if next_heading != -1 else len(manifest)
    return manifest[start:end]
