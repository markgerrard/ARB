"""Decision-record schema validation — the content half of the FABA gate.

PF1 remediation (panel-faba-proto-20260718T050210Z-39f50d): the gate must not
accept a round on a bare ingestion signal; the record itself must parse against
the contract schema and cover the prior record's open findings. Both FABA forms
gate on this module: the SDK harness validates before IT publishes the record
(the agent never publishes and never holds the bus credential); the subagent
form's SubagentStop hook validates before allowing the round agent to stop.

Deliberately regex-light and strict-on-structure, loose-on-prose: the schema is
the bootstrap contract's v0 record schema, and a record that fails here is a
failed round, never a "probably fine".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

HEADER_RE = re.compile(r"^# FABA decision record — round (?P<round>\d+)", re.MULTILINE)
SUBJECT_RE = re.compile(
    r"^Subject:\s*(?P<subject>\S+)\s*\|\s*Prior record:\s*(?P<prior>\S+)\s*\|\s*Status:\s*(?P<status>ok|failed)\s*$",
    re.MULTILINE,
)
# F10: a record naming its subject by id alone strands a zero-context successor.
# The one-line "What the subject IS" preamble was convention-only through round 4
# (rounds 2-4 survived on it); this makes it schema. The optional parenthetical
# inside the bold covers the established "(for a zero-context successor)" style.
SUBJECT_SUMMARY_RE = re.compile(r"^\*\*What the subject IS[^*\n]*\*\*:?\s*\S", re.MULTILINE)
# Auto-basis (open item #13): an OPTIONAL record-level line naming the commit the
# round verified against, so a successor can default its reopen --prior-basis to
# it. Absent or 'none' => no recorded basis (older records stay valid).
BASIS_RE = re.compile(r"^Basis:\s*(?P<basis>\S+)\s*$", re.MULTILINE)
# The evidence and reopen columns are OPTIONAL at parse time so pre-v2 3-column
# and pre-reopen 4-column records still yield their finding ids (open_finding_ids
# over an old prior record must not silently return [] — panel-faba-v2-r2
# cold-opus nit 2); validation still rejects an empty evidence cell on NEW
# records, and an empty reopen-if cell on a NEW record's CLOSED finding.
FINDING_ROW_RE = re.compile(
    r"^\|\s*(?P<id>[A-Za-z0-9_.-]+)\s*\|\s*(?P<severity>[^|]+)\|\s*(?P<status>[^|]+)\|"
    r"(?:\s*(?P<evidence>[^|]*)\|(?:\s*(?P<reopen>[^|]*)\|)?)?"
)
SKIP_ROW_IDS = {"id", "----", ":---"}


@dataclass
class RecordCheck:
    ok: bool
    status: str | None = None
    subject: str | None = None
    round_number: int | None = None
    findings: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def required_sections(text: str) -> list[str]:
    missing = []
    for section in ("## Round task", "## Findings", "## Recommendation"):
        if section not in text:
            missing.append(f"missing section {section!r}")
    return missing


def parse_findings(text: str) -> list[dict]:
    rows = []
    in_findings = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_findings = line.startswith("## Findings")
            continue
        if not in_findings:
            continue
        m = FINDING_ROW_RE.match(line.strip())
        if not m:
            continue
        fid = m.group("id").strip()
        if fid.lower() in SKIP_ROW_IDS or set(fid) <= {"-", ":"}:
            continue
        rows.append(
            {
                "id": fid,
                "severity": m.group("severity").strip(),
                "status": m.group("status").strip(),
                "evidence": (m.group("evidence") or "").strip(),
                "reopen": (m.group("reopen") or "").strip(),
            }
        )
    return rows


def _is_open(status: str) -> bool:
    return status.lower().startswith("open")


def validate_decision_record(
    text: str,
    prior_open_ids: list[str] | None = None,
    *,
    expected_round: int | None = None,
    expected_subject: str | None = None,
) -> RecordCheck:
    """Structural gate: header, subject/status line, sections, findings table,
    coverage of the prior record's open findings, non-empty evidence cells, and
    — when the caller supplies its minted expectations — BINDING to the round
    number and subject artefact (panel-faba-v2 codex F1: a structurally shaped
    record for the wrong round/subject must not pass). Returns problems rather
    than raising so callers can log every gap at once.

    Status ok|failed is schema-VALID either way (a failed round must still
    produce a publishable record — crash equivalence); whether a failed status
    fails the ROUND is the gate caller's decision, not a schema problem."""
    problems = []
    round_number = None
    hm = HEADER_RE.search(text or "")
    if not hm:
        problems.append("missing '# FABA decision record — round N' header")
    else:
        round_number = int(hm.group("round"))
        if expected_round is not None and round_number != expected_round:
            problems.append(f"record is for round {round_number}, expected round {expected_round}")
    subject = status = None
    m = SUBJECT_RE.search(text or "")
    if not m:
        problems.append("missing/malformed 'Subject: ... | Prior record: ... | Status: ok|failed' line")
    else:
        subject, status = m.group("subject"), m.group("status")
        if expected_subject is not None and subject != expected_subject:
            problems.append(f"record subject {subject!r} != expected {expected_subject!r}")
    if not SUBJECT_SUMMARY_RE.search(text or ""):
        problems.append(
            "missing '**What the subject IS:** <one line>' subject-summary line"
            " (a successor booting from this record alone must know what the subject is)"
        )
    problems.extend(required_sections(text or ""))

    findings = parse_findings(text or "")
    if "## Findings" in (text or "") and not findings:
        problems.append("findings section present but no parseable finding rows")
    for f in findings:
        if not f["evidence"]:
            problems.append(f"finding {f['id']!r} has an empty evidence cell")
        # A CLOSED finding must declare what tree change reopens it, per finding,
        # as schema (not instructional prose). Conservative default '*' = the
        # subject subtree (r28 precedent); a narrower pathspec is allowed but the
        # closer carries the evidence it is isolated. Open findings can't reopen.
        if not _is_open(f["status"]) and not f["reopen"]:
            problems.append(
                f"finding {f['id']!r} is closed but has no reopen-if scope"
                " (use '*' for the conservative subtree default, or a pathspec"
                " with evidence the finding is isolated)"
            )

    for prior_id in prior_open_ids or []:
        if not any(f["id"] == prior_id for f in findings):
            problems.append(f"prior open finding {prior_id!r} not carried in findings table")

    return RecordCheck(
        ok=not problems,
        status=status,
        subject=subject,
        round_number=round_number,
        findings=findings,
        problems=problems,
    )


def open_finding_ids(text: str) -> list[str]:
    """The ids a successor round must carry: findings whose status starts 'open'."""
    return [f["id"] for f in parse_findings(text) if _is_open(f["status"])]


def basis_ref(text: str) -> str | None:
    """The commit a record says it verified against (open item #13), or None when
    absent or explicitly 'none'. A successor's reopen consumer defaults its
    --prior-basis to this so reopen is automatic without an operator-supplied ref."""
    m = BASIS_RE.search(text or "")
    if not m:
        return None
    value = m.group("basis")
    return None if value.lower() == "none" else value


def _scope_matches(reopen_cell: str, changed_paths: list[str]) -> bool:
    """Does a reopen-if scope match any changed path? A cell is one or more
    whitespace/comma-separated tokens; a token matches a POSIX path by glob
    (`*` matches everything, incl. `/`, so the broad default reopens on any
    change) or as a directory prefix (`tools/faba/` matches anything beneath)."""
    for token in reopen_cell.replace(",", " ").split():
        prefix = token.rstrip("/") + "/"
        for path in changed_paths:
            if fnmatch(path, token) or path == token.rstrip("/") or path.startswith(prefix):
                return True
    return False


def reopened_finding_ids(text: str, changed_paths: list[str]) -> list[str]:
    """The consumer side of the reopen predicate (ADR open item #12): CLOSED
    findings whose reopen-if scope matches a path changed since the prior record.
    A successor round must carry these alongside the still-open ones, so a
    regression in a settled finding's area is re-examined rather than assumed
    fixed. Pure: `changed_paths` is supplied by the caller (git diff lives in the
    launch/driver layer). Returns sorted, unique ids."""
    paths = list(changed_paths)
    reopened = {
        f["id"]
        for f in parse_findings(text)
        if not _is_open(f["status"]) and f["reopen"] and _scope_matches(f["reopen"], paths)
    }
    return sorted(reopened)


def must_carry_ids(text: str, changed_paths: list[str] | None = None) -> list[str]:
    """The full set a successor round must carry into its findings table: the
    still-open findings plus any CLOSED finding reopened by a matching change.
    With no changed paths this is exactly the open set (backward-compatible)."""
    return sorted(set(open_finding_ids(text)) | set(reopened_finding_ids(text, changed_paths or [])))


AUTHORED_ARTEFACT_MIN_CHARS = 300

# Bare closing tag standing alone on the artefact's last non-blank line — the
# v17/v19 publish blemish: the author child stochastically closes a full-body
# Write with an XML-ish wrapper tag it invented (no such tag exists anywhere in
# its context). Matched loosely on purpose: any lone `</...>` tail is wrapper
# residue, never artefact prose.
_TRAILING_MARKUP_RE = re.compile(r"^</[A-Za-z][^>]*>$")


def validate_authored_artefact(text: str, *, allow_trailing_markup: bool = False) -> RecordCheck:
    """Light content gate for AUTHOR rounds (Workflow C, owner-directed 2026-07-19).

    Deliberately loose — quality is the review panel's job, not the gate's. The
    gate only rejects outputs that cannot function as a published artefact at
    all: stubs, headless bodies, drafts with no change summary (the cockpit's
    pointer return and the successor round both need that line), and bodies
    ending in wrapper-markup residue (the v17/v19 trailing `</content>` class).

    `allow_trailing_markup=True` is for the staged-PRIOR check only: the prior
    is the store as it IS, fetched verbatim — a historical blemish must not
    block grounding a revision that exists to remove it. Authored OUTPUT always
    gets the tail check.
    """
    problems: list[str] = []
    stripped = text.strip()
    if len(stripped) < AUTHORED_ARTEFACT_MIN_CHARS:
        problems.append(
            f"stub artefact: body too short ({len(stripped)} chars < {AUTHORED_ARTEFACT_MIN_CHARS})"
        )
    first_line = next((line for line in stripped.splitlines() if line.strip()), "")
    if not first_line.startswith("# "):
        problems.append("missing '# ' title as the first non-blank line")
    if "change summary" not in stripped.lower():
        problems.append("missing a **Change summary:** line (the pointer return and successor need it)")
    if not allow_trailing_markup:
        last_line = next((line for line in reversed(stripped.splitlines()) if line.strip()), "")
        if _TRAILING_MARKUP_RE.match(last_line.strip()):
            problems.append(
                f"trailing markup residue: last line is a bare closing tag {last_line.strip()!r} "
                "(v17/v19 publish-blemish class) — delete the final tag line, it is not part "
                "of the artefact"
            )
    return RecordCheck(ok=not problems, status="ok" if not problems else None, problems=problems)


# --- Dispatch brief (Slice 1d-iv): assumptions shape, not completeness ---

_ASSUMPTIONS_ALLOWED_TOP = frozenset({"items"})
_ASSUMPTIONS_ITEM_BASE = frozenset({"statement", "status", "vantage"})
_ASSUMPTIONS_ITEM_DEMO = _ASSUMPTIONS_ITEM_BASE | frozenset({"artefact_id", "version"})
_ASSUMPTIONS_STATUSES = frozenset({"demonstrated", "assumed"})


def _reject_duplicate_keys(pairs):
    """object_pairs_hook: refuse JSON objects with duplicate keys."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _extract_assumptions_json(text: str) -> tuple[str | None, list[str]]:
    """Return (json_text, problems). json_text is None when unrecoverable."""
    problems: list[str] = []
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "## Assumptions":
            start = i + 1
            break
    if start is None:
        problems.append("missing ## Assumptions section")
        return None, problems

    body_lines: list[str] = []
    in_fence = False
    for line in lines[start:]:
        if line.startswith("## ") and not in_fence:
            break
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_fence:
                in_fence = True
                continue
            in_fence = False
            continue
        body_lines.append(line)
    raw = "\n".join(body_lines).strip()
    if not raw:
        problems.append("## Assumptions section is empty")
        return None, problems
    return raw, problems


def validate_dispatch_brief(text: str, *, target_vantage: str) -> RecordCheck:
    """Shape gate for a dispatch brief (Slice 1d-iv).

    Proves the assumptions section is present and well-formed against the
    **selected target's registry-advertised worker vantage**. Does not claim to
    detect an omitted real-world precondition — an empty ``{"items":[]}`` is an
    explicit no-precondition claim and is valid.

    Required:
      - nonblank ``# `` title as the first non-blank line
      - ``## Assumptions`` JSON object with exact top-level key ``items``
      - each item: ``statement``, ``status`` (demonstrated|assumed), ``vantage``
      - demonstrated: also nonblank ``artefact_id`` + positive int ``version``;
        item vantage must equal ``target_vantage``
      - assumed: must not carry a demonstration ref
      - nonblank body/instructions after the assumptions section
    """
    problems: list[str] = []
    if not isinstance(target_vantage, str) or not target_vantage.strip():
        problems.append("target_vantage must be a nonblank string")

    stripped = (text or "").strip()
    first_line = next((line for line in stripped.splitlines() if line.strip()), "")
    if not first_line.startswith("# "):
        problems.append("missing '# ' title as the first non-blank line")

    raw_json, extract_problems = _extract_assumptions_json(text or "")
    problems.extend(extract_problems)

    payload = None
    if raw_json is not None:
        try:
            import json as _json

            payload = _json.loads(raw_json, object_pairs_hook=_reject_duplicate_keys)
        except ValueError as exc:
            problems.append(f"malformed assumptions JSON: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface parse class, not stack
            problems.append(f"malformed assumptions JSON: {type(exc).__name__}: {exc}")

    if isinstance(payload, dict):
        unknown_top = sorted(set(payload) - _ASSUMPTIONS_ALLOWED_TOP)
        if unknown_top:
            problems.append(f"unknown assumptions key(s): {unknown_top}")
        items = payload.get("items")
        if "items" not in payload:
            problems.append("assumptions JSON missing 'items'")
        elif not isinstance(items, list):
            problems.append("assumptions 'items' must be a list")
        else:
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    problems.append(f"items[{idx}] must be an object")
                    continue
                status = item.get("status")
                allowed = (
                    _ASSUMPTIONS_ITEM_DEMO
                    if status == "demonstrated"
                    else _ASSUMPTIONS_ITEM_BASE
                )
                # Detect unknown keys even before status is validated so
                # assumed-with-ref surfaces as both unknown-key and status rule.
                if status == "assumed":
                    ref_keys = [k for k in ("artefact_id", "version") if k in item]
                    if ref_keys:
                        problems.append(
                            f"items[{idx}]: assumed must not carry demonstration ref {ref_keys}"
                        )
                    unknown = sorted(set(item) - _ASSUMPTIONS_ITEM_BASE)
                else:
                    unknown = sorted(set(item) - _ASSUMPTIONS_ITEM_DEMO)
                if unknown:
                    problems.append(f"items[{idx}]: unknown key(s) {unknown}")
                statement = item.get("statement")
                if not isinstance(statement, str) or not statement.strip():
                    problems.append(f"items[{idx}]: blank or missing statement")
                vantage = item.get("vantage")
                if not isinstance(vantage, str) or not vantage.strip():
                    problems.append(f"items[{idx}]: blank or missing vantage")
                if status not in _ASSUMPTIONS_STATUSES:
                    problems.append(
                        f"items[{idx}]: status must be demonstrated|assumed, got {status!r}"
                    )
                    continue
                if status == "demonstrated":
                    art = item.get("artefact_id")
                    if not isinstance(art, str) or not art.strip():
                        problems.append(
                            f"items[{idx}]: demonstrated requires nonblank artefact_id"
                        )
                    version = item.get("version")
                    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                        problems.append(
                            f"items[{idx}]: demonstrated requires positive int version"
                        )
                    if (
                        isinstance(vantage, str)
                        and vantage.strip()
                        and isinstance(target_vantage, str)
                        and target_vantage.strip()
                        and vantage.strip() != target_vantage.strip()
                    ):
                        problems.append(
                            f"items[{idx}]: demonstrated vantage {vantage!r} does not "
                            f"equal target vantage {target_vantage!r}"
                        )
    elif payload is not None:
        problems.append("assumptions JSON must be an object")

    residual = _body_after_assumptions(text or "")
    if residual is not None and not residual.strip():
        problems.append("missing nonblank body/instructions after assumptions")

    return RecordCheck(
        ok=not problems,
        status="ok" if not problems else None,
        problems=problems,
    )


def _body_after_assumptions(text: str) -> str | None:
    """Return text after the assumptions JSON block, or None if no section."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "## Assumptions":
            start = i + 1
            break
    if start is None:
        return None
    i = start
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].strip().startswith("```"):
        i += 1
        while i < len(lines) and not lines[i].strip().startswith("```"):
            i += 1
        if i < len(lines):
            i += 1  # closing fence
    elif i < len(lines) and lines[i].strip().startswith("{"):
        depth = 0
        while i < len(lines):
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
            if depth <= 0:
                break
    return "\n".join(lines[i:])


def validate_revision_fold(artefact_text: str, prior_text: str) -> RecordCheck:
    """Fold-hygiene check for AUTHOR revision rounds (F14 route (c) hygiene
    tier — panel panel-f14c-design-20260720T033218Z-5ec74f, owner-scoped
    2026-07-20).

    HYGIENE, not integrity: both inputs live in the child-writable workspace,
    so this catches honest failure shapes (nothing folded), never a
    self-rewriting child — the blind-overwrite closure is the publish-time
    store read-back (fetch-by-id follow-on, scoped separately). Deliberately
    minimal: the panel rejected title/length heuristics as
    false-positive-prone (sanctioned retitles, condensing folds) and
    padding-incentivizing under the stop-gate bounce.
    """
    problems: list[str] = []
    if artefact_text == prior_text:
        problems.append(
            "revision artefact is byte-identical to the prior body — nothing was "
            "folded (the round produced no revision)"
        )
    return RecordCheck(ok=not problems, status="ok" if not problems else None, problems=problems)
