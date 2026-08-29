"""Single ordinary-request publish-and-enqueue authority (Slice 1d-iv Task 4).

Ordinary dispatch has one enqueue seam: ``publish_and_enqueue``. Caller ownership
is closed:

* **FABA draft path** — may hold ``ARB_MEMORY_REDIS_URL`` and supply a draft; the
  short-lived harness publish freezes the target, validates the brief, publishes
  via the existing ``publish_artefact_and_gate`` machinery, and returns a
  target-bound receipt. Enqueue follows only after a good receipt. Four
  production callers (wiki_refresh, learn_intake, seat-e2e, diagnose panel)
  hold the publish credential by design for that short-lived step.
* **Bash / Go / ctl / warm / other non-FABA enqueue** — no publish credential in
  the enqueue environment; only a pre-minted ``{artefact_id, version}`` plus a
  target-bound ``HarnessPublishReceipt`` and the exact original brief bytes
  (hash-verified for the legacy wire). Drafts and ambient publish material are
  refused before any enqueue. The credential is scrubbed from the child env
  (``env -u ARB_MEMORY_REDIS_URL`` / ``filterEnv``); that scrub is the enforced
  boundary for non-FABA enqueue.

``harness_publish`` itself has no identity check — it is a pure publish helper.
Identity/credential gates live in ``publish_and_enqueue`` /
``_assert_identity_and_credentials``.

Stage 1d-iv wire selection is receive-only dual-accept: the authority emits a
**legacy** task string unless the frozen target advertises both
``task_wire=legacy-or-ref-v1`` and ``brief_hydrate=v1``. No Stage 1d-iv seat
advertises hydration readiness, so production targets always select legacy.
Selection machinery for ref is present and unit-tested; emission of a ref to a
parse-only seat is a stage-boundary violation.

This module never imports/calls ``psycopg.connect``, ``memory_write``, or
``store_artefact``. The FABA publish path is either an injectable ``publish_fn``
or a subprocess/in-process call into the FABA harness.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

from .envelope import Envelope, iso_now
from .redis_io import RedisCli

# Caller identities recognised by the closed source matrix.
FABA_IDENTITY = "faba"
NON_FABA_IDENTITIES = frozenset({"bash", "go", "ctl", "warm", "other", "engine_child"})
HARNESS_PUBLISH_ENV = "ARB_MEMORY_REDIS_URL"
# The FABA memory-writer publish credential (HARNESS_PUBLISH_ENV) and the
# long-lived bridge audit-emitter credential are DIFFERENT bus identities that
# historically shared ARB_MEMORY_REDIS_URL. They are split so each can flip
# buses independently (resolve_audit_redis reads ARB_AUDIT_REDIS_URL first,
# falling back to ARB_MEMORY_REDIS_URL). Both are "publish-class" credentials
# that MUST be stripped from a non-FABA enqueue child env — but this set is
# NARROWER than engines._stdio.is_bus_credential on purpose: the enqueue child
# still needs the coordination-bus creds (AGENT_REDIS_*) to LPUSH, so only the
# publish credentials are scrubbed here. (The engine-child scrub in
# engines._stdio already covers ARB_AUDIT_REDIS_URL via its "_REDIS_URL" rule.)
AUDIT_EMITTER_ENV = "ARB_AUDIT_REDIS_URL"
PUBLISH_CREDENTIAL_ENV = frozenset({HARNESS_PUBLISH_ENV, AUDIT_EMITTER_ENV})


def filter_publish_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with every publish-class credential removed.

    The single source of truth for the non-FABA enqueue publish-scrub. Keeps
    coordination-bus creds intact (unlike the engine-child scrub); only the
    publish credentials in ``PUBLISH_CREDENTIAL_ENV`` are dropped.
    """
    return {k: v for k, v in env.items() if k not in PUBLISH_CREDENTIAL_ENV}


def pop_publish_env(env: "MutableMapping[str, str]") -> dict[str, str]:
    """Pop every publish-class credential OUT of a mutable mapping in place.

    For the FABA driver PF1-containment idiom: the credential must live only in
    a local variable, never propagate to the spawned child (which inherits
    os.environ). Returns the popped ``{name: value}`` so the caller can read the
    memory URL it consumes; the audit URL, if present, is popped and discarded.
    """
    return {k: env.pop(k) for k in PUBLISH_CREDENTIAL_ENV if k in env}
# Typed payload keys reserved from extra_payload even at default values.
_TYPED_PAYLOAD_KEYS = frozenset(
    {"task", "operation", "worktree", "worktree_lease", "lane"}
)


class DispatchAuthorityError(RuntimeError):
    """Refuse before ordinary-request enqueue. ``reason`` is the stable token."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class HarnessPublishReceipt:
    """Pointer-only target-bound receipt produced by harness publish.

    Binds ref, target id, registration generation, vantage, and the
    domain-separated content hash of the published brief. Never carries body
    bytes or credentials.
    """

    artefact_id: str
    version: int
    target_agent_id: str
    registration_generation: str
    worker_vantage: str
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HarnessPublishReceipt":
        try:
            version = data["version"]
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise DispatchAuthorityError("receipt version must be a positive int")
            return cls(
                artefact_id=str(data["artefact_id"]),
                version=version,
                target_agent_id=str(data["target_agent_id"]),
                registration_generation=str(data["registration_generation"]),
                worker_vantage=str(data["worker_vantage"]),
                content_hash=str(data["content_hash"]),
            )
        except KeyError as exc:
            raise DispatchAuthorityError(f"receipt missing field: {exc}") from exc


@dataclass(frozen=True)
class DraftSource:
    """FABA-driver-owned draft. Published inside the harness before enqueue."""

    brief_text: str
    artefact_id: str | None = None


@dataclass(frozen=True)
class PreMintedSource:
    """Non-FABA source: pre-minted ref + target-bound receipt + original bytes."""

    artefact_id: str
    version: int
    receipt: HarnessPublishReceipt
    legacy_brief_bytes: bytes


def content_hash_for_brief(text: str) -> str:
    """Domain-separated artefact hash for brief bytes (mime text/markdown)."""
    from arb_memory.hash import artefact_hash

    return artefact_hash(text, None, "text/markdown")


def select_wire(frozen: Mapping[str, str]) -> str:
    """Choose legacy vs ref from the frozen target record.

    Ref only when both dual-parse and hydration readiness are advertised.
    Stage 1d-iv seats lack ``brief_hydrate=v1``, so this returns ``legacy``.
    """
    if (
        frozen.get("task_wire") == "legacy-or-ref-v1"
        and frozen.get("brief_hydrate") == "v1"
    ):
        return "ref"
    return "legacy"


def freeze_target(redis_cli: RedisCli, target_agent_id: str) -> dict[str, str]:
    """Read and freeze the target registry record for validation + recheck."""
    if not target_agent_id or not str(target_agent_id).strip():
        raise DispatchAuthorityError("missing target")
    fields = redis_cli.hgetall(redis_cli.config.registry_key(target_agent_id))
    if not fields:
        raise DispatchAuthorityError("missing target")
    status = redis_cli.get_str(redis_cli.config.status_key(target_agent_id))
    if not status or not str(status).startswith("alive:"):
        raise DispatchAuthorityError("stale target")
    vantage = (fields.get("worker_vantage") or "").strip()
    if not vantage:
        raise DispatchAuthorityError("blank/missing worker_vantage")
    generation = (
        fields.get("registration_generation") or fields.get("owner_token") or ""
    ).strip()
    if not generation:
        raise DispatchAuthorityError("missing registration_generation")
    return {
        "agent_id": target_agent_id,
        "owner_token": fields.get("owner_token") or "",
        "registration_generation": generation,
        "worker_vantage": vantage,
        "task_wire": fields.get("task_wire") or "",
        "brief_hydrate": fields.get("brief_hydrate") or "",
    }


def _change_summary(brief_text: str) -> str:
    match = re.search(
        r"^\*\*Change summary:\*\*\s*(.+)$",
        brief_text,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()[:200]
    # Fall back to first non-title non-heading prose line.
    for line in brief_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("```"):
            continue
        return s[:200]
    return ""


def _parse_source(source: Any) -> DraftSource | PreMintedSource:
    if isinstance(source, (DraftSource, PreMintedSource)):
        return source
    if not isinstance(source, Mapping):
        raise DispatchAuthorityError("source must be DraftSource, PreMintedSource, or mapping")
    kind = source.get("kind")
    if kind == "draft":
        text = source.get("brief_text")
        if not isinstance(text, str):
            raise DispatchAuthorityError("draft source requires brief_text")
        return DraftSource(brief_text=text, artefact_id=source.get("artefact_id"))
    if kind == "pre_minted" or (
        "artefact_id" in source and "receipt" in source and "legacy_brief_bytes" in source
    ):
        receipt_raw = source.get("receipt")
        if receipt_raw is None:
            raise DispatchAuthorityError("ref without receipt")
        if isinstance(receipt_raw, HarnessPublishReceipt):
            receipt = receipt_raw
        elif isinstance(receipt_raw, Mapping):
            receipt = HarnessPublishReceipt.from_dict(receipt_raw)
        else:
            raise DispatchAuthorityError("receipt must be HarnessPublishReceipt or mapping")
        bytes_raw = source.get("legacy_brief_bytes")
        if isinstance(bytes_raw, str):
            legacy = bytes_raw.encode("utf-8")
        elif isinstance(bytes_raw, (bytes, bytearray)):
            legacy = bytes(bytes_raw)
        else:
            raise DispatchAuthorityError("legacy_brief_bytes required for pre-minted source")
        version = source.get("version", receipt.version)
        if isinstance(version, bool) or not isinstance(version, int):
            raise DispatchAuthorityError("version must be a positive int")
        return PreMintedSource(
            artefact_id=str(source.get("artefact_id") or receipt.artefact_id),
            version=version,
            receipt=receipt,
            legacy_brief_bytes=legacy,
        )
    if "artefact_id" in source and "version" in source and "receipt" not in source:
        raise DispatchAuthorityError("ref without receipt")
    raise DispatchAuthorityError(f"unrecognised source kind: {kind!r}")


def _assert_identity_and_credentials(
    *,
    caller_identity: str,
    source: DraftSource | PreMintedSource,
    env: Mapping[str, str],
    redis_url: str | None,
) -> None:
    is_faba = caller_identity == FABA_IDENTITY
    ambient = (env.get(HARNESS_PUBLISH_ENV) or "").strip()
    supplied = (redis_url or "").strip()

    if not is_faba:
        if ambient or supplied:
            raise DispatchAuthorityError(
                "non-faba harness publish credential forbidden "
                f"(identity={caller_identity})"
            )
        if isinstance(source, DraftSource):
            raise DispatchAuthorityError(
                f"non-faba draft forbidden (identity={caller_identity})"
            )
        return

    # FABA draft requires an explicit or ambient publish credential.
    if isinstance(source, DraftSource) and not (ambient or supplied):
        raise DispatchAuthorityError("faba draft requires publish credential")


def _validate_brief(brief_text: str, *, target_vantage: str) -> None:
    # Import from tools/faba — add path if needed (same pattern as harness scripts).
    faba_dir = Path(__file__).resolve().parents[2] / "tools" / "faba"
    if str(faba_dir) not in sys.path:
        sys.path.insert(0, str(faba_dir))
    from faba_schema import validate_dispatch_brief  # noqa: WPS433

    check = validate_dispatch_brief(brief_text, target_vantage=target_vantage)
    if not check.ok:
        raise DispatchAuthorityError(
            "invalid brief: " + "; ".join(check.problems)
        )


def _verify_receipt(
    receipt: HarnessPublishReceipt,
    *,
    artefact_id: str,
    version: int,
    frozen: Mapping[str, str],
    legacy_bytes: bytes,
) -> str:
    if receipt.artefact_id != artefact_id:
        raise DispatchAuthorityError("receipt artefact_id mismatch")
    if receipt.version != version:
        raise DispatchAuthorityError("receipt version mismatch")
    if receipt.target_agent_id != frozen["agent_id"]:
        raise DispatchAuthorityError("receipt target mismatch")
    if receipt.registration_generation != frozen["registration_generation"]:
        raise DispatchAuthorityError("receipt registration_generation mismatch")
    if receipt.worker_vantage != frozen["worker_vantage"]:
        raise DispatchAuthorityError("receipt vantage mismatch")
    try:
        brief_text = legacy_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DispatchAuthorityError("legacy_brief_bytes are not utf-8") from exc
    expected = content_hash_for_brief(brief_text)
    if receipt.content_hash != expected:
        raise DispatchAuthorityError("legacy bytes content hash mismatch")
    return brief_text


def _recheck_frozen(redis_cli: RedisCli, frozen: Mapping[str, str]) -> dict[str, str]:
    current = freeze_target(redis_cli, frozen["agent_id"])
    for key in (
        "registration_generation",
        "worker_vantage",
        "task_wire",
        "brief_hydrate",
    ):
        if current.get(key) != frozen.get(key):
            raise DispatchAuthorityError(
                f"target {key} changed since freeze "
                f"({frozen.get(key)!r} -> {current.get(key)!r})"
            )
    return current


def harness_publish(
    *,
    brief_text: str,
    target_agent_id: str,
    redis_cli: RedisCli,
    redis_url: str,
    artefact_id: str | None = None,
    author: str = "faba-dispatch",
    receipt_timeout: float = 60.0,
    publish_fn: Callable[..., HarnessPublishReceipt] | None = None,
    request_id: str | None = None,
) -> HarnessPublishReceipt:
    """Freeze target, validate, publish, return a target-bound receipt.

    Never enqueues an ordinary request. No identity check here — callers that
    must refuse non-FABA drafts/credentials gate that in
    ``publish_and_enqueue`` / ``_assert_identity_and_credentials``. ``publish_fn``
    is the test seam; when omitted, the real ``publish_artefact_and_gate`` path
    runs in a temp workspace with the dispatch-brief validator bound to the
    frozen vantage.
    """
    frozen = freeze_target(redis_cli, target_agent_id)
    _validate_brief(brief_text, target_vantage=frozen["worker_vantage"])
    art_id = artefact_id or f"art-brief-{uuid.uuid4().hex[:12]}"
    req_id = request_id or f"pub-{uuid.uuid4().hex}"

    if publish_fn is not None:
        receipt = publish_fn(
            brief_text=brief_text,
            target_agent_id=target_agent_id,
            redis_url=redis_url,
            artefact_id=art_id,
            author=author,
            receipt_timeout=receipt_timeout,
            request_id=req_id,
            frozen=frozen,
        )
        if not isinstance(receipt, HarnessPublishReceipt):
            raise DispatchAuthorityError("publish_fn must return HarnessPublishReceipt")
        return receipt

    # Real path: stage artefact.md and call publish_artefact_and_gate with the
    # dispatch-brief validator. Extends the existing gate; does not duplicate it.
    import tempfile

    faba_dir = Path(__file__).resolve().parents[2] / "tools" / "faba"
    if str(faba_dir) not in sys.path:
        sys.path.insert(0, str(faba_dir))
    from faba_launch import publish_artefact_and_gate  # noqa: WPS433
    from faba_schema import validate_dispatch_brief  # noqa: WPS433

    vantage = frozen["worker_vantage"]

    def _validate(text: str):
        return validate_dispatch_brief(text, target_vantage=vantage)

    with tempfile.TemporaryDirectory(prefix="arb-harness-publish-") as tmp:
        workspace = Path(tmp)
        (workspace / "artefact.md").write_text(brief_text, encoding="utf-8")
        result = publish_artefact_and_gate(
            redis_url,
            workspace=workspace,
            artefact_id=art_id,
            request_id=req_id,
            author=author,
            receipt_timeout=receipt_timeout,
            validate=_validate,
        )
        if not result.passed or not result.receipt:
            raise DispatchAuthorityError(
                f"failed/wrong receipt: {result.reason}"
            )
        store_receipt = result.receipt
        version = store_receipt.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise DispatchAuthorityError("failed/wrong receipt: missing version")
        if store_receipt.get("artefact_id") not in {art_id, None}:
            # gate_decision already checks id; be defensive
            if store_receipt.get("artefact_id") != art_id:
                raise DispatchAuthorityError("failed/wrong receipt: artefact_id mismatch")
        return HarnessPublishReceipt(
            artefact_id=art_id,
            version=version,
            target_agent_id=target_agent_id,
            registration_generation=frozen["registration_generation"],
            worker_vantage=frozen["worker_vantage"],
            content_hash=content_hash_for_brief(brief_text),
        )


def publish_and_enqueue(
    source: Any,
    *,
    target_agent_id: str,
    operation: str = "request",
    run_id: str,
    worktree_lease: str | None = None,
    caller_identity: str,
    redis_cli: RedisCli,
    sender: str,
    branch: str,
    redis_url: str | None = None,
    worktree: dict[str, Any] | None = None,
    extra_payload: dict[str, Any] | None = None,
    lane: str | None = None,
    publish_fn: Callable[..., HarnessPublishReceipt] | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Validate, (optionally) publish, select wire, enqueue once.

    Returns pointer-only metadata:
    ``{request_id, artefact_id, version, target_agent_id, change_summary}``.
    When ``dry_run=True``, also includes ``envelope`` (the exact JSON that would
    be enqueued) and ``wire`` (``legacy`` or ``ref``), and never calls ``rpush``.

    On any failure before a successful wire build, the Redis ``rpush`` primitive
    is never called — gated and exempt lanes stop identically.

    Stage 1d-iv: the only permitted ordinary-request enqueue. Legacy task-string
    construction lives solely in this function's wire-selection branch and is
    removed in Stage 1d-vi after zero-legacy proof.
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    parsed = _parse_source(source)
    _assert_identity_and_credentials(
        caller_identity=caller_identity,
        source=parsed,
        env=env_map,
        redis_url=redis_url,
    )

    frozen = freeze_target(redis_cli, target_agent_id)

    if isinstance(parsed, DraftSource):
        url = (redis_url or env_map.get(HARNESS_PUBLISH_ENV) or "").strip()
        if not url:
            raise DispatchAuthorityError("faba draft requires publish credential")
        try:
            receipt = harness_publish(
                brief_text=parsed.brief_text,
                target_agent_id=target_agent_id,
                redis_cli=redis_cli,
                redis_url=url,
                artefact_id=parsed.artefact_id,
                publish_fn=publish_fn,
            )
        except DispatchAuthorityError:
            raise
        except Exception as exc:
            raise DispatchAuthorityError(
                f"publish exception: {type(exc).__name__}: {exc}"
            ) from exc
        brief_text = parsed.brief_text
        # Re-validate receipt against freeze (harness_publish already froze, but
        # identity of returned receipt must still bind this target).
        # Note: artefact_id/version comparisons below are vacuous on the draft
        # path — we pass the receipt's own fields as the expectation. The real
        # checks are target / registration_generation / vantage / content_hash.
        brief_text = _verify_receipt(
            receipt,
            artefact_id=receipt.artefact_id,
            version=receipt.version,
            frozen=frozen,
            legacy_bytes=brief_text.encode("utf-8"),
        )
        artefact_id = receipt.artefact_id
        version = receipt.version
    else:
        # Pre-minted: zero publish calls.
        brief_text = _verify_receipt(
            parsed.receipt,
            artefact_id=parsed.artefact_id,
            version=parsed.version,
            frozen=frozen,
            legacy_bytes=parsed.legacy_brief_bytes,
        )
        _validate_brief(brief_text, target_vantage=frozen["worker_vantage"])
        artefact_id = parsed.artefact_id
        version = parsed.version
        receipt = parsed.receipt

    # Recheck freeze immediately before enqueue.
    current = _recheck_frozen(redis_cli, frozen)
    wire = select_wire(current)

    if wire == "ref":
        # Ref emission: pointer only — never include body bytes.
        task: Any = {"artefact_id": artefact_id, "version": version}
    else:
        # LEGACY COMPATIBILITY BRANCH — Stage 1d-iv only.
        # Removal stage: 1d-vi after fleet zero-legacy proof.
        task = brief_text

    payload: dict[str, Any] = {"task": task}
    if operation and operation != "request":
        payload["operation"] = operation
    if worktree_lease:
        payload["worktree_lease"] = worktree_lease
    if worktree:
        payload["worktree"] = worktree
    if lane:
        payload["lane"] = lane
    if extra_payload:
        # Typed key NAMES are reserved even when left at defaults (so
        # extra_payload cannot inject operation/lane/worktree* and bypass
        # the CLI --operation choices gate). Keys already placed in payload
        # are also skipped.
        for key, value in extra_payload.items():
            if key in payload or key in _TYPED_PAYLOAD_KEYS:
                continue
            payload[key] = value

    req_id = request_id or str(uuid.uuid4())
    envelope = Envelope(
        id=req_id,
        sender=sender,
        branch=branch,
        recipient=target_agent_id,
        kind="request",
        sent_at=iso_now(),
        payload=payload,
        run_id=run_id if run_id else None,
    )
    envelope_json = envelope.to_json()

    if dry_run:
        return {
            "request_id": req_id,
            "artefact_id": artefact_id,
            "version": version,
            "target_agent_id": target_agent_id,
            "change_summary": _change_summary(brief_text),
            "wire": wire,
            "envelope": envelope_json,
        }

    # The only ordinary-request enqueue.
    redis_cli.rpush(target_agent_id, envelope_json)

    return {
        "request_id": req_id,
        "artefact_id": artefact_id,
        "version": version,
        "target_agent_id": target_agent_id,
        "change_summary": _change_summary(brief_text),
    }
