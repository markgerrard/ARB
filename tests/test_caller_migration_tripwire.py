"""Caller/recipe migration tripwire (Slice 1d-iv Task 5 Step 3).

Enumeration over a hardcoded list: walks production callers and recipe files
and fails for files that lack the pre-minted/authority markers (and for
enumerated caller bypasses of the single ordinary-request authority).

What this tripwire catches
--------------------------
* Direct ordinary-request enqueue construction outside ``dispatch_authority``
  in the enumerated Bash/Go/ctl/Python production callers.
* Recipe FILES that mention ``dispatch-dev`` / ``agent-dispatch`` but lack any
  ``--artefact-id`` or ``dispatch_authority`` marker (file-level presence —
  not per-recipe-block; a file with one quartet recipe and a free-form
  example still passes).
* Non-FABA production callers that *import and call* harness publish helpers
  without going through the closed FABA surfaces (or that keep
  ``ARB_MEMORY_REDIS_URL`` in the enqueue environment).

What it structurally cannot catch
---------------------------------
* Any caller (new *or* pre-existing) not named in the hardcoded path lists —
  the list catches only what it names. These three rem4 surfaces were
  in-repo ordinary-request callers that slipped because the enumeration was
  incomplete; a brand-new language/runtime outside the globs is the same
  class of gap, not a different one.
* Runtime monkey-patching of ``RedisCli.rpush`` after import.
* A recipe that lives only in an operator's head / chat (not in the repo).
* Lifecycle/control edges (``worktree_arm``/``release``, steer/cancel) — those
  are intentionally outside ordinary-request authority.
* Mentions of ``ARB_MEMORY_REDIS_URL`` for *scrubbing* / child-env deny proofs
  (engine spawn paths) — those are containment, not publish write material.
* Test fixtures under ``tests/`` that inject envelopes into the bridge receive
  path (dual-accept receive still exercises legacy strings).

Every allowlist entry names its removal stage.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Production surfaces that must not independently enqueue ordinary requests.
CALLER_PATHS = [
    "scripts/agent-dispatch",
    "scripts/dispatch-dev",
    "scripts/arb-memory-seat-e2e",
    "scripts/arb-orch-panel",
    "src/agent_redis_bridge/ctl.py",
    "src/agent_redis_bridge/wiki_refresh.py",
    "src/agent_redis_bridge/learn_intake.py",
    "skills/diagnose/panel.py",
    "tools/eval/arb_eval/pipeline.py",
    "tools/go-client/build.go",
    "tools/go-client/main.go",
    "tools/go-client/authority.go",
    "pi-extensions/arb-dispatch-monitor.ts",
]

RECIPE_PATHS = [
    "docs/fragments/dispatch-recipe.md",
    "skills/using-agent-bridge/SKILL.md",
    "README.md",
    # The canonical recipe moved out of the root README when it was split into
    # per-area front doors (docs/readme-split). The root README still names the
    # dispatchers in prose, so it stays enumerated; the file that now carries the
    # recipe has to be enumerated too, or the marker requirement stops biting.
    "src/agent_redis_bridge/README.md",
    "docs/orchestrator-patterns.md",
    "pi-extensions/README.md",
]

# Sole ordinary-request rpush site + removal stage.
ORDINARY_ENQUEUE_ALLOW = {
    "src/agent_redis_bridge/dispatch_authority.py": (
        "sole ordinary-request enqueue seam (redis_cli.rpush of request envelopes)",
        "permanent (Stage 1d-vi removes only the legacy wire branch inside it)",
    ),
}

LEGACY_TASK_CONSTRUCTION_ALLOW = {
    "src/agent_redis_bridge/dispatch_authority.py": (
        "compatibility wire branch selects legacy brief_text as payload.task",
        "Stage 1d-vi after zero-legacy proof",
    ),
}

# Surfaces allowed to *invoke* harness publish (FABA driver CLI or harness_publish).
HARNESS_PUBLISH_CALL_ALLOW = {
    "src/agent_redis_bridge/dispatch_authority.py": "permanent (FABA-only harness_publish)",
    "scripts/arb-memory-harness-publish": "permanent (FABA-driver CLI; never enqueues)",
    "src/agent_redis_bridge/dispatch_cli.py": "permanent (FABA draft CLI + enqueue_pre_minted)",
    "src/agent_redis_bridge/wiki_refresh.py": "permanent (subprocess publish then strip cred)",
    "src/agent_redis_bridge/learn_intake.py": "permanent (subprocess publish then strip cred)",
    "skills/diagnose/panel.py": "permanent (subprocess publish then strip cred)",
    "scripts/arb-memory-seat-e2e": "permanent (subprocess publish then strip cred)",
    "tools/eval/arb_eval/pipeline.py": "permanent (subprocess publish then strip cred)",
    "scripts/arb-orch-panel": "permanent (subprocess publish then strip cred)",
    "pi-extensions/arb-dispatch-monitor.ts": "permanent (subprocess publish then strip cred)",
}


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def _strip_ts_comments(src: str) -> str:
    """Strip ``//`` line and ``/* */`` block comments from TypeScript source.

    Strength enforced (rem5 F3(a) / sol comment probe): tokens that survive only
    inside comments must not satisfy live-code assertions. Naive (not a full
    lexer) — enough that commenting out the quartet ``args.push`` block REDs.
    """
    no_block = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"//[^\n]*", "", no_block)


def _strips_both_publish_creds(text: str) -> bool:
    """True if a non-FABA enqueue site strips BOTH publish-class credentials.

    ARB_MEMORY_REDIS_URL (FABA memory-writer) and ARB_AUDIT_REDIS_URL (long-lived
    audit-emitter) are co-equal members of dispatch_authority.PUBLISH_CREDENTIAL_ENV
    and MUST both be stripped from a non-FABA enqueue child (which keeps
    AGENT_REDIS_* to LPUSH). The central helpers strip the whole set by
    construction; explicit forms (shell ``env -u``, TS ``delete``) must name BOTH
    vars. A site that strips only ARB_MEMORY_REDIS_URL leaks the audit credential
    and is exactly the regression this guards.
    """
    if "filter_publish_env" in text or "pop_publish_env" in text:
        return True
    return "ARB_MEMORY_REDIS_URL" in text and "ARB_AUDIT_REDIS_URL" in text


def test_agent_dispatch_ordinary_path_delegates_to_authority():
    text = _read("scripts/agent-dispatch")
    assert "dispatch_cli" in text or "dispatch_authority" in text
    # Must not build ordinary jq payloads with free-form task=$TASK.
    assert not re.search(r"task:\$TASK\b", text)
    assert not re.search(r"--arg task \"\$TASK\"", text)
    # Ordinary path requires pre-minted flags.
    assert "--artefact-id" in text
    assert "dispatch_authority refused" in text or "dispatch_cli" in text


def test_ctl_send_task_does_not_rpush():
    text = _read("src/agent_redis_bridge/ctl.py")
    m = re.search(r"def send_task\(.*?(?=\ndef |\Z)", text, flags=re.S)
    assert m, "send_task missing"
    body = m.group(0)
    assert "enqueue_pre_minted" in body or "publish_and_enqueue" in body
    assert not re.search(r"\.rpush\s*\(", body)


def test_go_build_envelope_refuses_ordinary_independent_path():
    text = _read("tools/go-client/build.go")
    assert "dispatch_authority" in text
    assert "isOrdinaryAuthorityPath" in text
    assert not re.search(r"payload\s*:=\s*Payload\{\s*Task:\s*task\s*\}", text)


def test_go_main_routes_ordinary_to_authority():
    text = _read("tools/go-client/main.go")
    assert "authorityEnqueue" in text
    assert "isOrdinaryAuthorityPath" in text


def test_authority_is_only_python_ordinary_rpush_site():
    """Among production Python modules, only dispatch_authority rpush'es request envelopes."""
    offenders: list[str] = []
    for path in (ROOT / "src" / "agent_redis_bridge").rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        if rel.endswith("_test.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"\.rpush\s*\(", text):
            continue
        if rel in ORDINARY_ENQUEUE_ALLOW:
            continue
        # Other rpush sites (recovery scripts, notify, control) must not build
        # ordinary request task payloads in the same function — flag if they
        # clearly construct payload with task= for a request kind.
        if re.search(
            r"""kind\s*=\s*["']request["'][\s\S]{0,200}\.rpush\s*\(|"""
            r"""\.rpush\s*\([\s\S]{0,200}kind\s*=\s*["']request["']""",
            text,
        ):
            offenders.append(rel)
        # ctl historically rpush'ed; ensure send_task no longer does (covered above).
        if rel.endswith("ctl.py") and re.search(
            r"def send_task[\s\S]*?\.rpush\s*\(", text
        ):
            offenders.append(f"{rel}: send_task rpush")
    assert offenders == [], "ordinary rpush outside authority:\n  " + "\n  ".join(offenders)


def test_recipes_require_pre_minted_flags():
    offenders: list[str] = []
    for rel in RECIPE_PATHS:
        text = _read(rel)
        if "dispatch-dev" in text or "agent-dispatch" in text:
            if "--artefact-id" not in text and "dispatch_authority" not in text:
                offenders.append(rel)
    assert offenders == [], "recipes lack pre-minted/authority markers:\n  " + "\n  ".join(
        offenders
    )


def test_pipeline_dispatch_uses_authority_quartet():
    """eval BridgeDispatcher must publish-then-quartet; no positional task argv."""
    text = _read("tools/eval/arb_eval/pipeline.py")
    assert "arb-memory-harness-publish" in text
    assert "--artefact-id" in text
    assert "wrap_instructions_as_brief" in text
    # Pinned old shape: *run_id_flags, prompt] — free-form positional task.
    assert not re.search(r"\*run_id_flags,\s*prompt\s*\]", text), (
        "pipeline.py must not append free-form prompt as positional task "
        "(*run_id_flags, prompt])"
    )
    assert _strips_both_publish_creds(text), (
        "pipeline.py must strip BOTH publish credentials before enqueue"
    )


def test_pipeline_publish_passes_env_file():
    """rem5 F2: when BridgeDispatcher.env_file is set, publish argv gets --env-file.

    Publisher consumes only --env-file (scripts/arb-memory-harness-publish), never
    AGENT_ENV_FILE. Acceptance mutation: drop the --env-file append → this RED.
    """
    text = _read("tools/eval/arb_eval/pipeline.py")
    # Live passthrough: if self.env_file: … pub_cmd.extend(["--env-file", self.env_file])
    assert re.search(
        r"""if\s+self\.env_file\s*:\s*\n"""
        r"""\s*pub_cmd\.extend\(\s*\[\s*["']--env-file["']\s*,\s*self\.env_file\s*\]\s*\)""",
        text,
    ), (
        "pipeline.py must append --env-file to the harness-publish argv when "
        "self.env_file is set"
    )


def test_arb_orch_panel_dispatch_uses_authority_quartet():
    """arb-orch-panel must publish-then-quartet; no cmd.append(prompt)."""
    text = _read("scripts/arb-orch-panel")
    assert "arb-memory-harness-publish" in text
    assert "--artefact-id" in text
    assert "wrap_instructions_as_brief" in text
    # Pinned old shape: cmd.append(prompt) after building dispatch argv.
    assert "cmd.append(prompt)" not in text, (
        "arb-orch-panel must not cmd.append(prompt) as free-form positional task"
    )
    assert _strips_both_publish_creds(text), (
        "arb-orch-panel must strip BOTH publish credentials before enqueue"
    )


def test_pi_monitor_dispatch_uses_authority_quartet():
    """pi arb-dispatch-monitor must publish-then-quartet; no args.push(params.task)."""
    text = _read("pi-extensions/arb-dispatch-monitor.ts")
    live = _strip_ts_comments(text)
    assert "arb-memory-harness-publish" in live
    # Live quartet argv construction — strip comments so sol's comment probe RED
    # (tokens surviving only in // lines must not satisfy).
    assert re.search(
        r"""args\.push\(\s*"""
        r"""["']--artefact-id["'][\s\S]*?"""
        r"""["']--version["'][\s\S]*?"""
        r"""["']--receipt["'][\s\S]*?"""
        r"""["']--brief["']""",
        live,
    ), (
        "monitor must args.push quartet flags (--artefact-id/--version/--receipt/"
        "--brief) on live (non-comment) lines"
    )
    # F2: --env-file reaches the publish argv (publisher ignores AGENT_ENV_FILE).
    assert re.search(
        r"""["']--env-file["']\s*,\s*envFile""",
        live,
    ), "monitor must pass --env-file to harness-publish argv"
    # Pinned old shape: args.push(params.task) as trailing free-form positional.
    assert "args.push(params.task)" not in live, (
        "monitor must not args.push(params.task) as free-form positional task"
    )
    # Credential strip on enqueue env — BOTH publish-class creds must be deleted.
    assert "ARB_MEMORY_REDIS_URL" in live
    assert re.search(
        r"""delete\s+enqueueEnv\.ARB_MEMORY_REDIS_URL"""
        r"""|delete\s+\w+\.ARB_MEMORY_REDIS_URL""",
        live,
    ), "monitor must delete ARB_MEMORY_REDIS_URL from the enqueue spawn env"
    assert re.search(
        r"""delete\s+enqueueEnv\.ARB_AUDIT_REDIS_URL"""
        r"""|delete\s+\w+\.ARB_AUDIT_REDIS_URL""",
        live,
    ), "monitor must ALSO delete ARB_AUDIT_REDIS_URL (publish-class credential)"


def test_harness_publish_call_sites_are_allowlisted():
    """Direct harness_publish( / publish_artefact_and_gate( call sites only.

    Mentions of the CLI name in help text or recipes are not publish write material.
    """
    offenders: list[str] = []
    call_pat = re.compile(
        r"""(?x)
        (?:
            \bharness_publish\s*\(
            | \bpublish_artefact_and_gate\s*\(
        )
        """
    )
    # Also flag argv that actually invoke the CLI (not help-text mentions).
    # Includes TS join(..., "scripts/arb-memory-harness-publish") and Python list forms.
    cli_invoke = re.compile(
        r"""(?x)
        (?:
            \[["']scripts/arb-memory-harness-publish["']
            | ["']scripts/arb-memory-harness-publish["']
            | /scripts/arb-memory-harness-publish\b
            | \barb-memory-harness-publish\s+--
        )
        """
    )
    # Enumerate Task 5 production callers (not every e2e of the publish machinery).
    for rel in CALLER_PATHS + [
        "src/agent_redis_bridge/dispatch_authority.py",
        "src/agent_redis_bridge/dispatch_cli.py",
        "scripts/arb-memory-harness-publish",
    ]:
        path = ROOT / rel
        if not path.exists():
            offenders.append(f"missing: {rel}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not (call_pat.search(text) or cli_invoke.search(text)):
            continue
        if rel in HARNESS_PUBLISH_CALL_ALLOW:
            continue
        offenders.append(rel)
    assert offenders == [], (
        "harness publish call outside allowlist:\n  " + "\n  ".join(offenders)
    )


def test_non_faba_callers_strip_publish_credential_on_enqueue():
    """wiki/learn/diagnose/seat-e2e + rem4 callers must strip BOTH publish creds for enqueue.

    Guards the ARB_AUDIT_REDIS_URL split: a site that strips only
    ARB_MEMORY_REDIS_URL leaks the audit-emitter credential into the enqueue
    child. Accepts the central helpers (filter_publish_env / pop_publish_env,
    which strip the whole PUBLISH_CREDENTIAL_ENV set) or explicit both-var forms.
    """
    for rel in (
        "src/agent_redis_bridge/wiki_refresh.py",
        "src/agent_redis_bridge/learn_intake.py",
        "skills/diagnose/panel.py",
        "scripts/arb-memory-seat-e2e",
        "tools/eval/arb_eval/pipeline.py",
        "scripts/arb-orch-panel",
    ):
        text = _read(rel)
        assert "ARB_MEMORY_REDIS_URL" in text, rel
        assert _strips_both_publish_creds(text), (
            f"{rel} must strip BOTH publish credentials (memory + audit) before enqueue"
        )
    # TypeScript monitor: must delete BOTH publish-class creds from the spawn env.
    mon = _strip_ts_comments(_read("pi-extensions/arb-dispatch-monitor.ts"))
    assert re.search(r"delete\s+\w+\.ARB_MEMORY_REDIS_URL", mon), (
        "monitor must strip ARB_MEMORY_REDIS_URL before enqueue spawn"
    )
    assert re.search(r"delete\s+\w+\.ARB_AUDIT_REDIS_URL", mon), (
        "monitor must ALSO strip ARB_AUDIT_REDIS_URL before enqueue spawn"
    )


def test_allowlists_name_removal_stages():
    for path, meta in {**ORDINARY_ENQUEUE_ALLOW, **LEGACY_TASK_CONSTRUCTION_ALLOW}.items():
        reason, stage = meta
        assert reason and stage
        assert "Stage" in stage or "permanent" in stage.lower()
    for path, stage in HARNESS_PUBLISH_CALL_ALLOW.items():
        assert "permanent" in stage.lower() or "Stage" in stage, (path, stage)


def test_authority_module_marks_legacy_removal_stage():
    text = _read("src/agent_redis_bridge/dispatch_authority.py")
    assert "LEGACY COMPATIBILITY BRANCH" in text
    assert "1d-vi" in text
