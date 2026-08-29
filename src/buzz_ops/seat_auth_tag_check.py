"""Flag agent-owned seats whose kind:0 cannot open the observer gate.

WHAT THIS CATCHES, and why it is worth a check rather than a runbook line.
The web-channel "Agent activity" panel renders nothing when a seat's profile
fails `profile_agent_owner()` — and NOTHING is logged at any level. The seat
stays online, answers mentions, and looks healthy; only the thinking panel is
dark. Both times this has happened the seat was renamed with
`buzz users set-profile`, which replaces kind:0 wholesale and drops the auth tag
unless BUZZ_AUTH_TAG is supplied. The first incident was found by a human noticing
silence, days after the rename; the second was the same failure class on a
different seat, caught and repaired within the hour.

The check answers one question per seat: would the proxy admit this profile?

SCOPE / WHAT IT DOES NOT COVER. This is the PROXY gate (gate 2 of four). A seat
can pass every check here and still show an empty panel because:
  - the seat is not publishing observer frames at all (BUZZ_ACP_RELAY_OBSERVER),
  - the relay's users.agent_owner_pubkey is unset (checked here as a WARNING —
    see below — but it is the relay's gate, not the profile's),
  - the agent emits no agent_thought_chunk for the turn in question, which is
    normal for trivial prompts and reads identically to a fault.
Reporting a clean run as "agent activity works" would be exactly the
adjacent-property mistake this repo keeps paying for. It means "no seat is
blocked by its profile".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .nip_oa import parse_tags, verify_auth_tag

# Relay-side query. LEFT JOIN LATERAL so a seat with NO kind:0 at all still
# appears as a row — an absent profile is a finding, and an inner join would
# silently drop exactly the worst case.
SEAT_QUERY = """
SELECT encode(u.pubkey, 'hex')                                AS agent_pubkey,
       encode(u.agent_owner_pubkey, 'hex')                    AS relay_owner,
       e.kind                                                 AS kind,
       COALESCE(EXTRACT(EPOCH FROM e.created_at)::bigint, 0)   AS created_at,
       e.tags::text                                           AS tags,
       e.content                                              AS content
FROM users u
LEFT JOIN LATERAL (
    SELECT kind, created_at, tags, content
    FROM events
    WHERE pubkey = u.pubkey AND kind = 0
    ORDER BY created_at DESC
    LIMIT 1
) e ON TRUE
WHERE u.agent_owner_pubkey IS NOT NULL
ORDER BY agent_pubkey
"""


@dataclass(frozen=True)
class SeatFinding:
    agent_pubkey: str
    status: str
    detail: str
    display_name: str | None = None
    relay_owner: str | None = None
    tag_owner: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def blocking(self) -> bool:
        """Blocking = the proxy will refuse this profile. Warnings are real but
        do not by themselves blank the panel."""
        return self.status not in {"ok", "owner_mismatch_warning"}


def _display_name(content: str | None) -> str | None:
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("display_name") or parsed.get("name")
    return name if isinstance(name, str) else None


def check_seat(row: dict) -> SeatFinding:
    """Classify ONE seat. Pure — no DB, no clock — so every branch is testable."""
    agent = row["agent_pubkey"]
    relay_owner = row.get("relay_owner")
    name = _display_name(row.get("content"))

    if row.get("kind") is None:
        return SeatFinding(agent, "no_profile",
                           "relay treats this pubkey as an agent but it has published no kind:0",
                           name, relay_owner)

    tags = parse_tags(row.get("tags"))
    auth_tags = [t for t in tags
                 if isinstance(t, list) and t and t[0] == "auth"]

    if not auth_tags:
        return SeatFinding(agent, "no_auth_tag",
                           "kind:0 carries no auth tag — profile_agent_owner returns None, "
                           "so the seat is absent from owned_agents and its frames are never "
                           "subscribed. Usual cause: set-profile without BUZZ_AUTH_TAG.",
                           name, relay_owner)

    if len(auth_tags) > 1:
        # server.rs: `let [auth_tag] = auth_tags.as_slice() else { return None }`
        return SeatFinding(agent, "multiple_auth_tags",
                           f"{len(auth_tags)} auth tags present; the proxy destructures exactly "
                           "one and returns None otherwise, so two tags fail like zero",
                           name, relay_owner)

    verdict = verify_auth_tag(auth_tags[0], agent,
                              kind=int(row["kind"]), created_at=int(row["created_at"]))
    if not verdict.ok:
        return SeatFinding(agent, verdict.reason,
                           f"auth tag present but rejected ({verdict.reason}); "
                           f"conditions={verdict.conditions!r}",
                           name, relay_owner, verdict.owner_pubkey)

    if relay_owner and verdict.owner_pubkey and relay_owner != verdict.owner_pubkey:
        # Not proxy-blocking on its own: the proxy checks the tag, the relay
        # checks its column, and they can disagree. Surfaced because a seat in
        # that state is one write away from a confusing half-failure.
        return SeatFinding(agent, "owner_mismatch_warning",
                           f"tag owner {verdict.owner_pubkey[:12]}… != relay column "
                           f"{relay_owner[:12]}…",
                           name, relay_owner, verdict.owner_pubkey)

    return SeatFinding(agent, "ok", "profile would be admitted by the proxy",
                       name, relay_owner, verdict.owner_pubkey)


@dataclass
class CheckReport:
    findings: list[SeatFinding] = field(default_factory=list)

    @property
    def blocking(self) -> list[SeatFinding]:
        return [f for f in self.findings if f.blocking]

    @property
    def warnings(self) -> list[SeatFinding]:
        return [f for f in self.findings if f.status == "owner_mismatch_warning"]

    def as_json(self) -> str:
        return json.dumps(
            {
                "checked": len(self.findings),
                "blocking": len(self.blocking),
                "warnings": len(self.warnings),
                "seats": [
                    {
                        "agent_pubkey": f.agent_pubkey,
                        "display_name": f.display_name,
                        "status": f.status,
                        "detail": f.detail,
                    }
                    for f in self.findings
                ],
            },
            indent=2,
        )


def check_rows(rows) -> CheckReport:
    return CheckReport([check_seat(dict(r)) for r in rows])


def fetch_rows(dsn: str) -> list[dict]:
    import psycopg  # imported here so the pure logic is importable without a driver

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(SEAT_QUERY)
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, values)) for values in cur.fetchall()]
