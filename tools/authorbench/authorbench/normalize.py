from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


@dataclass(frozen=True)
class Hit:
    token: str
    token_class: str
    span: tuple[int, int]


@dataclass(frozen=True)
class EditLog:
    token_class: str
    from_text: str
    to_text: str


@dataclass(frozen=True)
class StagingResult:
    staged: bool
    quarantined: bool
    aborted: bool
    token_class: str | None = None
    redaction_log: list[EditLog] = field(default_factory=list)


class BlindingAbort(RuntimeError):
    def __init__(self, token_class: str):
        self.token_class = token_class
        super().__init__(f"blinding marker survived redaction: {token_class}")


IDENTITY_HEADER_RE = re.compile(
    r"^(author|status|model|session|run)[ \t]*:.*$",
    re.IGNORECASE | re.MULTILINE,
)
MODEL_SELF_REF_RE = re.compile(r"\b(GPT-5 Codex|Codex GPT-5|Claude|Anthropic|OpenAI model)\b", re.IGNORECASE)
SESSION_RE = re.compile(r"\b(?:run|session)-?\d{4}-\d{2}-\d{2}\b", re.IGNORECASE)

DENY_PATTERNS = [
    re.compile(r"\bAUTHOR_IDENTITY_TOKEN\b"),
    re.compile(r"\bSTUBBORN_AUTHOR_IDENTITY\b"),
    re.compile(r"\bauthor(?:ing)?[-_ ]seat[ \t:]+[A-Za-z0-9_.-]+\b", re.IGNORECASE),
    re.compile(r"\bmodel[ \t:]+(?:gpt|claude|codex|anthropic)[A-Za-z0-9_. -]*\b", re.IGNORECASE),
]


def normalize(raw, *, version: int) -> str:
    if version != 1:
        raise ValueError(f"unsupported normalizer version: {version}")
    text = str(raw)
    text = IDENTITY_HEADER_RE.sub("", text)
    text = MODEL_SELF_REF_RE.sub("[model]", text)
    text = SESSION_RE.sub("[session]", text)
    text = re.sub(r"^\s*[-*]\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + ("\n" if text.strip() else "")


def _literal_identity_patterns(tokens: Iterable[str] | None) -> list[re.Pattern[str]]:
    return [re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(str(token))}(?![A-Za-z0-9_-])") for token in tokens or () if token]


def denylist_scan(text, *, author_identity_tokens: Iterable[str] | None = None) -> list[Hit]:
    out: list[Hit] = []
    for pat in [*DENY_PATTERNS, *_literal_identity_patterns(author_identity_tokens)]:
        for match in pat.finditer(str(text)):
            out.append(Hit(match.group(0), "author-identity", match.span()))
    out.sort(key=lambda hit: hit.span)
    return out


def redact_once(text, hits: list[Hit]) -> tuple[str, list[EditLog]]:
    redacted = str(text)
    edits: list[EditLog] = []
    for hit in hits:
        redacted = redacted.replace(hit.token, "[redacted-author-identity]", 1)
        edits.append(EditLog(hit.token_class, hit.token, "[redacted-author-identity]"))
    return redacted, edits


def stage_for_judges(
    draft,
    staging_dir,
    *,
    raise_on_abort: bool = False,
    author_identity_tokens: Iterable[str] | None = None,
    redactor: Callable[[str, list[Hit]], tuple[str, list[EditLog]]] = redact_once,
) -> StagingResult:
    staging_dir = Path(staging_dir)
    raw = str(draft)
    normalized = normalize(raw, version=1)
    hits = denylist_scan(normalized, author_identity_tokens=author_identity_tokens)
    if not hits:
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "draft.md").write_text(normalized, encoding="utf-8")
        return StagingResult(staged=True, quarantined=False, aborted=False)

    redacted, edits = redactor(normalized, hits)
    rescan = denylist_scan(redacted, author_identity_tokens=author_identity_tokens)
    if rescan:
        token_class = rescan[0].token_class
        if raise_on_abort:
            raise BlindingAbort(token_class)
        return StagingResult(
            staged=False,
            quarantined=True,
            aborted=True,
            token_class=token_class,
            redaction_log=edits,
        )

    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "draft.md").write_text(redacted, encoding="utf-8")
    return StagingResult(
        staged=True,
        quarantined=True,
        aborted=False,
        token_class=hits[0].token_class,
        redaction_log=edits,
    )
