"""Brief authoring for diagnose live-panel dispatch."""

from __future__ import annotations

from pathlib import Path

from skills._diagnose_common import seal

from .extraction import derive_scope, extract


class CertifierStarved(Exception):
    pass


def author_briefs(failing_test: str, repo_sha: str, recorded_traceback: dict, constants: dict) -> list[dict]:
    repo_root = Path(constants.get("_repo_root", repo_sha))
    scope = derive_scope(failing_test, recorded_traceback, repo_root, repo_sha)
    observables = [
        {"id": item["id"], "path": item["path"], "sha256": item["sha256"], "size": item["size"]}
        for item in extract(repo_root, scope, repo_sha)
    ]
    briefs = [
        _sealed_brief(
            "scribe",
            constants["scribe"]["model"],
            {
                "role": "scribe",
                "observables": observables,
                "task": "describe-observables-only",
                "system_prompt": constants["scribe"]["system_prompt"],
                "window": dict(recorded_traceback["window"]),
            },
        )
    ]
    for seat in constants["roster"]:
        role = seat["role"]
        briefs.append(
            _sealed_brief(
                role,
                seat["model"],
                {
                    "role": role,
                    "observables": observables,
                    "task": _role_task(role),
                    "system_prompt": constants["scribe"]["system_prompt"],
                    "window": dict(recorded_traceback["window"]),
                },
            )
        )
    return briefs


def author_post_briefs(constants: dict, sealed_submissions: list[dict], predicates: list[dict] | None = None) -> list[dict]:
    sealed_submissions = [s for s in sealed_submissions if s.get("role") != "scribe"]
    submissions = sorted(
        [
            {
                "role": item["role"],
                "seat": item["seat"],
                "model": item.get("model", ""),
                "seal": item["seal"],
                "bus_reply_ref": item["bus_reply_ref"],
                "bus_reply_sha256": item["bus_reply_sha256"],
            }
            for item in sealed_submissions
        ],
        key=lambda item: (item["seat"], item["role"]),
    )
    return [
        _sealed_brief(
            "certifier",
            _certifier_model(constants, predicates or []),
            {
                "role": "certifier",
                "task": constants["certifier"]["rule"],
                "sealed_submissions": submissions,
            },
        ),
        _sealed_brief(
            "collation",
            constants["scribe"]["model"],
            {
                "role": "collation",
                "task": constants["collation_order"],
                "sealed_submissions": submissions,
            },
        ),
    ]


def _sealed_brief(role: str, model: str, brief: dict) -> dict:
    return {"role": role, "model": model, "brief": brief, "seal": seal(brief)}


def _role_task(role: str) -> str:
    tasks = {
        "blind": "derive candidate causes from neutral observables only",
        "alternative": "derive strongest alternative from neutral observables only",
        "open": "inspect neutral observables without prior hypothesis",
    }
    return tasks.get(role, "describe neutral observables")


def _certifier_model(constants: dict, predicates: list[dict]) -> str:
    author_models = {predicate.get("author_model") for predicate in predicates if predicate.get("author_model")}
    candidates = sorted({seat["model"] for seat in constants["roster"]})
    for model in candidates:
        if model not in author_models:
            return model
    raise CertifierStarved("no roster model decorrelated from the predicate author")
