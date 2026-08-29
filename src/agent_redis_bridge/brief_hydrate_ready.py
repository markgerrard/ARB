"""Executed readiness predicate for brief_hydrate=v1 advertisement.

One predicate shared by bridge construction and registry advertisement
(``Bridge.__init__`` and ``reassert_liveness``). A declarative registry value
without this execution is refused. Verifies: console-script executability
via the same bare-name helper the pointer prompt instructs
(``shutil.which`` + exit-0 on ``--readiness-probe``; no venv-bin fallback on
the advertisement path), local-read policy against the configured store,
pointer-prompt construction (token presence), and receipt parser/cleanup.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

HELPER_CONSOLE_SCRIPT = "arb-memory-brief-hydrate"
READINESS_PROBE_FLAG = "--readiness-probe"


def build_pointer_prompt(
    *,
    artefact_id: str,
    version: int,
    output_path: str | Path,
    receipt_path: str | Path,
    helper_cmd: str = HELPER_CONSOLE_SCRIPT,
) -> str:
    """Pointer-only engine prompt: ref + helper + paths. No body bytes."""
    out = str(output_path)
    rcpt = str(receipt_path)
    return (
        "You are executing a bridge task whose brief is a pinned memory artefact.\n"
        "Do NOT expect the brief body on the bus. Hydrate it yourself, then follow it.\n\n"
        "1. Run exactly:\n"
        f"   {helper_cmd} --artefact-id {artefact_id} --version {version} "
        f"--output {out} --receipt {rcpt}\n"
        "2. Read the staged brief at the --output path.\n"
        "3. Leave the receipt JSON at the --receipt path (the helper writes it).\n"
        "4. Execute the brief instructions and report the outcome.\n\n"
        f"Pinned ref: artefact_id={artefact_id} version={version}\n"
    )


def parse_hydration_receipt(receipt_path: str | Path) -> dict:
    """Parse worker receipt. Raises ValueError on malformed content."""
    path = Path(receipt_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    raw = path.read_text(encoding="utf-8")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("receipt is not an object")
    for key in ("artefact_id", "version", "content_hash"):
        if key not in obj:
            raise ValueError(f"receipt missing {key}")
    if not isinstance(obj["artefact_id"], str) or not obj["artefact_id"].strip():
        raise ValueError("receipt artefact_id invalid")
    if not isinstance(obj["version"], int) or isinstance(obj["version"], bool) or obj["version"] < 1:
        raise ValueError("receipt version invalid")
    h = obj["content_hash"]
    if not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h.lower()):
        raise ValueError("receipt content_hash not well-formed hex sha256")
    return obj


def cleanup_stage_dir(stage_dir: str | Path) -> bool:
    """Remove staged body/receipt and the mode-0700 directory.

    Returns True when the path is gone (or never existed). Returns False when
    removal fails and the path still exists — callers may log or fail closed.
    """
    path = Path(stage_dir)
    if not path.exists():
        return True
    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger.warning("brief-hydrate stage cleanup failed for %s: %s", path, exc)
    if path.exists():
        logger.warning("brief-hydrate stage dir still present after cleanup: %s", path)
        return False
    return True


def allocate_stage_dir(runtime_root: str | Path) -> Path:
    """Mode-0700 mkdtemp under operator-owned runtime root; constant child names later."""
    root = Path(runtime_root)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    stage = Path(tempfile.mkdtemp(prefix="brief-stage-", dir=str(root)))
    os.chmod(stage, 0o700)
    return stage


def locate_brief_hydrate_console_script() -> str | None:
    """Locate the bare console-script name on PATH (advertisement contract).

    Returns the path ``shutil.which`` resolves for ``HELPER_CONSOLE_SCRIPT``, or
    None when the bare name is not on PATH. Deliberately does **not** fall back
    to the active interpreter's venv ``bin/``: readiness True must mean the
    engine child can resolve the same bare name the pointer prompt instructs.
    For diagnostics-only venv-bin lookup (never consulted by advertisement),
    see ``locate_brief_hydrate_console_script_in_venv_bin``.
    """
    return shutil.which(HELPER_CONSOLE_SCRIPT)


def locate_brief_hydrate_console_script_in_venv_bin() -> str | None:
    """Diagnostics-only: look for the helper next to ``sys.executable``.

    **Not** part of the advertisement path — ``prove_console_script_executable``
    and ``prove_brief_hydrate_readiness`` never call this. Uses the unresolved
    parent of ``sys.executable`` (no ``.resolve()``) so a symlinked mirror venv
    is not escaped into the real install tree; operators inspecting a seat can
    still see whether a local bin/ copy exists.
    """
    candidate = Path(sys.executable).parent / HELPER_CONSOLE_SCRIPT
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def prove_console_script_executable() -> bool:
    """Bare-name on PATH and exit 0 on ``--readiness-probe``.

    Enforces exactly the prompt contract: the helper is the bare console-script
    name (``HELPER_CONSOLE_SCRIPT``), resolved via PATH, and that same resolved
    path exits 0 when invoked with ``--readiness-probe``. A venv-bin copy that
    is not on PATH does not count — the engine child would fail the same way.
    """
    script = locate_brief_hydrate_console_script()
    if script is None:
        return False
    try:
        completed = subprocess.run(
            [script, READINESS_PROBE_FLAG],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def prove_brief_hydrate_readiness(
    *,
    env: Mapping[str, str] | None = None,
    runtime_root: str | Path | None = None,
    allow_missing_dsn: bool = False,
) -> bool:
    """Return True only after executed console-script/policy/prompt/receipt/cleanup checks.

    Does not advertise; callers set brief_hydrate=v1 only when this returns True.
    Step 1 proves the console script is locatable and executes (exit 0), not
    merely that arb_memory.brief_hydrate is importable.
    """
    environ = dict(env if env is not None else os.environ)

    # 1. Console script locatable + executable (the command the pointer prompt runs).
    if not prove_console_script_executable():
        return False

    # Module still needed for policy import path; failure here is also refuse.
    try:
        from arb_memory import brief_hydrate as _helper  # noqa: F401
        from arb_memory.brief_hydrate import hydrate, validate_artefact_id  # noqa: F401
    except Exception:
        return False

    # 2. Local-read policy resolvable when DSN configured.
    local_dsn = (environ.get("ARB_MEMORY_LOCAL_DSN") or "").strip()
    if not local_dsn:
        if not allow_missing_dsn:
            return False
    else:
        try:
            from arb_memory.local_read_policy import local_read_dsn

            local_read_dsn(environ)  # type: ignore[arg-type]
        except Exception:
            return False

    # 3. Pointer-prompt construction — token presence for the flags the prompt
    # instructs (not a full structural parse of argv order).
    try:
        prompt = build_pointer_prompt(
            artefact_id="art-readiness-probe",
            version=1,
            output_path="/tmp/brief-readiness/brief",
            receipt_path="/tmp/brief-readiness/receipt",
        )
        if "art-readiness-probe" not in prompt:
            return False
        if HELPER_CONSOLE_SCRIPT not in prompt:
            return False
        for token in ("--artefact-id", "--version", "--output", "--receipt"):
            if token not in prompt:
                return False
        # No accidental body injection surface.
        if "BODY_SENTINEL" in prompt:
            return False
    except Exception:
        return False

    # 4. Receipt parser + cleanup.
    root = Path(runtime_root) if runtime_root is not None else Path(
        tempfile.mkdtemp(prefix="brief-ready-root-")
    )
    own_root = runtime_root is None
    try:
        stage = allocate_stage_dir(root)
        receipt = stage / "receipt"
        receipt.write_text(
            json.dumps(
                {
                    "artefact_id": "art-readiness-probe",
                    "version": 1,
                    "content_hash": "a" * 64,
                    "path": str(stage / "brief"),
                }
            ),
            encoding="utf-8",
        )
        parsed = parse_hydration_receipt(receipt)
        if parsed["artefact_id"] != "art-readiness-probe":
            return False
        if not cleanup_stage_dir(stage):
            return False
        if stage.exists():
            return False
    except Exception:
        return False
    finally:
        if own_root:
            cleanup_stage_dir(root)

    return True
