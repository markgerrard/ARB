from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import sqlite3
from pathlib import Path
from typing import Iterator
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TokenRecord:
    id: str
    name: str
    host: str | None
    status: str
    created_at: str
    expires_at: str
    request_id: str | None


class TokenError(ValueError):
    pass


class RegistrationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self.connect(immediate=True) as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS tokens (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT, token_hash TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              request_id TEXT UNIQUE, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests (
              id TEXT PRIMARY KEY, token_id TEXT NOT NULL UNIQUE, pubkey TEXT NOT NULL,
              signing_pubkey TEXT NOT NULL, name TEXT NOT NULL, host TEXT NOT NULL,
              channels_json TEXT NOT NULL, reply_agent_id TEXT NOT NULL, client_nonce TEXT NOT NULL,
              status TEXT NOT NULL,
              approval_event_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              decision TEXT, decision_source TEXT,
              provision_attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
              last_error TEXT, contradiction_noted INTEGER NOT NULL DEFAULT 0,
              sealing_pubkey TEXT, sealing_fingerprint TEXT, self_report_json TEXT,
              declared_roles_json TEXT,
              FOREIGN KEY(token_id) REFERENCES tokens(id)
            );
            CREATE TABLE IF NOT EXISTS host_sealing_pins (
              host TEXT PRIMARY KEY, sealing_fingerprint TEXT NOT NULL,
              request_id TEXT NOT NULL, pinned_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bus_operator_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
              host TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
            if "client_nonce" not in columns:
                conn.execute(
                    "ALTER TABLE requests ADD COLUMN client_nonce TEXT NOT NULL DEFAULT ''"
                )
            migrations = {
                "decision": "ALTER TABLE requests ADD COLUMN decision TEXT",
                "decision_source": "ALTER TABLE requests ADD COLUMN decision_source TEXT",
                "provision_attempts": (
                    "ALTER TABLE requests ADD COLUMN provision_attempts INTEGER NOT NULL DEFAULT 0"
                ),
                "next_attempt_at": "ALTER TABLE requests ADD COLUMN next_attempt_at TEXT",
                "last_error": "ALTER TABLE requests ADD COLUMN last_error TEXT",
                "contradiction_noted": (
                    "ALTER TABLE requests ADD COLUMN contradiction_noted INTEGER NOT NULL DEFAULT 0"
                ),
                "sealing_pubkey": "ALTER TABLE requests ADD COLUMN sealing_pubkey TEXT",
                "sealing_fingerprint": "ALTER TABLE requests ADD COLUMN sealing_fingerprint TEXT",
                "self_report_json": "ALTER TABLE requests ADD COLUMN self_report_json TEXT",
                "declared_roles_json": (
                    "ALTER TABLE requests ADD COLUMN declared_roles_json TEXT"
                ),
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(statement)

    def mint(self, name: str, host: str | None, ttl: timedelta) -> tuple[str, TokenRecord]:
        if not name.strip() or not host or not host.strip() or ttl.total_seconds() <= 0:
            raise TokenError("name and host must be nonblank and ttl must be positive")
        scoped_host = host.strip()
        token_id = uuid4().hex[:16]
        secret = secrets.token_urlsafe(32)
        token = f"arbseat_{token_id}_{secret}"
        now, expiry = utcnow(), utcnow() + ttl
        with self.connect(immediate=True) as conn:
            conn.execute(
                "INSERT INTO tokens "
                "(id,name,host,token_hash,status,created_at,expires_at,request_id,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (
                token_id, name.strip(), scoped_host, self._hash(token), "active",
                iso(now), iso(expiry), None, iso(now),
                ),
            )
        return token, TokenRecord(token_id, name.strip(), scoped_host, "active", iso(now), iso(expiry), None)

    def list_tokens(self) -> list[TokenRecord]:
        now = iso(utcnow())
        with self.connect(immediate=True) as conn:
            conn.execute("UPDATE tokens SET status='expired', updated_at=? WHERE status='active' AND expires_at<=?", (now, now))
            rows = conn.execute("SELECT id,name,host,status,created_at,expires_at,request_id FROM tokens ORDER BY created_at DESC").fetchall()
        return [TokenRecord(**dict(row)) for row in rows]

    def revoke(self, token_id: str) -> None:
        with self.connect(immediate=True) as conn:
            changed = conn.execute("UPDATE tokens SET status='revoked',updated_at=? WHERE id=? AND status!='revoked'", (iso(utcnow()), token_id)).rowcount
            if not changed:
                raise TokenError("unknown or already-revoked token id")

    def reserve(self, *, token: str, name: str, host: str, request: dict[str, str]) -> str:
        now = iso(utcnow())
        with self.connect(immediate=True) as conn:
            row = conn.execute("SELECT * FROM tokens WHERE token_hash=?", (self._hash(token),)).fetchone()
            if row is None or row["status"] != "active" or row["expires_at"] <= now:
                raise TokenError("invalid registration credential")
            if row["name"] != name or row["host"] != host:
                raise TokenError("invalid registration credential")
            request_id = uuid4().hex[:12]
            conn.execute("UPDATE tokens SET status='pending',request_id=?,updated_at=? WHERE id=?", (request_id, now, row["id"]))
            conn.execute(
                "INSERT INTO requests "
                "(id,token_id,pubkey,signing_pubkey,name,host,channels_json,reply_agent_id,"
                "client_nonce,status,approval_event_id,created_at,updated_at,sealing_pubkey,"
                "sealing_fingerprint,self_report_json,declared_roles_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    request_id, row["id"], request["pubkey"], request["signing_pubkey"],
                    name, host, request["channels_json"], request["reply_agent_id"],
                    request["client_nonce"], "pending", None, now, now,
                    request.get("sealing_pubkey"), request.get("sealing_fingerprint"),
                    request.get("self_report_json"),
                    request.get("declared_roles_json"),
                ),
            )
            return request_id

    def reserve_bus(
        self, *, token: str, name: str, host: str, request: dict[str, str]
    ) -> str:
        required = (
            "sealing_pubkey", "sealing_fingerprint", "self_report_json",
            "declared_roles_json",
        )
        if not all(request.get(key) for key in required):
            raise TokenError("bus registration request is incomplete")
        return self.reserve(token=token, name=name, host=host, request=request)

    def set_request(self, request_id: str, status: str, approval_event_id: str | None = None) -> None:
        token_status = {"approved": "approved", "denied": "denied", "timed_out": "denied"}.get(status)
        with self.connect(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE requests SET status=?,approval_event_id=COALESCE(?,approval_event_id),updated_at=? WHERE id=?",
                (status, approval_event_id, iso(utcnow()), request_id),
            ).rowcount
            if not changed:
                raise TokenError("unknown request id")
            if token_status:
                conn.execute("UPDATE tokens SET status=?,updated_at=? WHERE request_id=?", (token_status, iso(utcnow()), request_id))
            if status == "provisioned":
                conn.execute(
                    "UPDATE requests SET provision_attempts=0,next_attempt_at=NULL,last_error=NULL "
                    "WHERE id=?",
                    (request_id,),
                )

    def set_decision(self, request_id: str, action: str, source: str) -> bool:
        if action not in {"approve", "deny"}:
            raise TokenError("invalid decision")
        with self.connect(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE requests SET decision=?,decision_source=?,updated_at=? "
                "WHERE id=? AND decision IS NULL AND status='pending'",
                (action, source, iso(utcnow()), request_id),
            ).rowcount
        return bool(changed)

    def set_bus_decision(self, request_id: str, action: str, source: str) -> bool:
        if action not in {"approve", "deny"}:
            raise TokenError("invalid decision")
        with self.connect(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE requests SET decision=?,decision_source=?,updated_at=? "
                "WHERE id=? AND decision IS NULL AND status='pending' "
                "AND self_report_json IS NOT NULL",
                (action, source, iso(utcnow()), request_id),
            ).rowcount
        return bool(changed)

    def set_bus_status(self, request_id: str, status: str) -> None:
        if status not in {"approved", "provisioned", "denied", "provision_failed"}:
            raise TokenError("invalid bus registration status")
        token_status = status
        with self.connect(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE requests SET status=?,updated_at=? WHERE id=? "
                "AND self_report_json IS NOT NULL",
                (status, iso(utcnow()), request_id),
            ).rowcount
            if not changed:
                raise TokenError("unknown bus registration request id")
            if status == "approved":
                conn.execute(
                    "UPDATE tokens SET status=?,updated_at=? WHERE request_id=? "
                    "AND status NOT IN ('revoked','expired')",
                    (token_status, iso(utcnow()), request_id),
                )
            else:
                conn.execute(
                    "UPDATE tokens SET status=?,updated_at=? WHERE request_id=?",
                    (token_status, iso(utcnow()), request_id),
                )

    def assert_request_token_fresh(self, request_id: str) -> None:
        now = iso(utcnow())
        with self.connect() as conn:
            row = conn.execute(
                "SELECT tokens.status,tokens.expires_at FROM tokens "
                "JOIN requests ON requests.token_id=tokens.id WHERE requests.id=?",
                (request_id,),
            ).fetchone()
        if (
            row is None
            or row["status"] not in {"pending", "approved"}
            or row["expires_at"] <= now
        ):
            raise TokenError("registration credential is no longer valid")

    def host_sealing_pin(self, host: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT sealing_fingerprint FROM host_sealing_pins WHERE host=?", (host,)
            ).fetchone()
        return str(row["sealing_fingerprint"]) if row else None

    def pin_host_sealing_key(
        self, *, host: str, sealing_fingerprint: str, request_id: str
    ) -> None:
        with self.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT sealing_fingerprint FROM host_sealing_pins WHERE host=?", (host,)
            ).fetchone()
            if row is not None:
                if row["sealing_fingerprint"] != sealing_fingerprint:
                    raise TokenError("sealing key pin mismatch")
                return
            conn.execute(
                "INSERT INTO host_sealing_pins "
                "(host,sealing_fingerprint,request_id,pinned_at) VALUES (?,?,?,?)",
                (host, sealing_fingerprint, request_id, iso(utcnow())),
            )

    def unpin_host_sealing_key(self, *, host: str, source: str) -> dict[str, str]:
        if not host.strip() or not source.strip():
            raise TokenError("host and operator source must be nonblank")
        created_at = iso(utcnow())
        with self.connect(immediate=True) as conn:
            changed = conn.execute(
                "DELETE FROM host_sealing_pins WHERE host=?", (host,)
            ).rowcount
            if not changed:
                raise TokenError("host has no sealing key pin")
            conn.execute(
                "INSERT INTO bus_operator_audit (action,host,source,created_at) "
                "VALUES (?,?,?,?)",
                ("host_sealing_key_unpinned", host, source, created_at),
            )
        return {
            "action": "host_sealing_key_unpinned",
            "host": host,
            "source": source,
            "created_at": created_at,
        }

    def bus_operator_audit(self) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT action,host,source,created_at FROM bus_operator_audit ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def bus_decided(self) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE status='pending' AND decision IS NOT NULL "
                "AND self_report_json IS NOT NULL ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def bus_pending(self) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE status='pending' "
                "AND self_report_json IS NOT NULL ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def bus_approved(self) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE status='approved' "
                "AND self_report_json IS NOT NULL ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def bus_requests(self) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id,name,host,status,decision,decision_source,created_at,updated_at,"
                "self_report_json,declared_roles_json,sealing_fingerprint FROM requests "
                "WHERE self_report_json IS NOT NULL ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_attempt_failure(
        self, request_id: str, error: str, next_attempt_at: datetime, max_attempts: int
    ) -> tuple[int, bool]:
        with self.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT provision_attempts FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise TokenError("unknown request id")
            attempts = int(row["provision_attempts"]) + 1
            exhausted = attempts >= max_attempts
            status = "provision_failed" if exhausted else None
            conn.execute(
                "UPDATE requests SET provision_attempts=?,next_attempt_at=?,last_error=?,"
                "status=COALESCE(?,status),updated_at=? WHERE id=?",
                (
                    attempts, iso(next_attempt_at), error[:1000], status,
                    iso(utcnow()), request_id,
                ),
            )
            if exhausted:
                conn.execute(
                    "UPDATE tokens SET status='denied',updated_at=? WHERE request_id=?",
                    (iso(utcnow()), request_id),
                )
        return attempts, exhausted

    def mark_contradiction_noted(self, request_id: str) -> None:
        with self.connect(immediate=True) as conn:
            conn.execute(
                "UPDATE requests SET contradiction_noted=1,updated_at=? WHERE id=?",
                (iso(utcnow()), request_id),
            )

    def decided_for_contradiction_check(self) -> list[dict[str, str]]:
        cutoff = iso(utcnow() - timedelta(hours=24))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE decision IS NOT NULL "
                "AND contradiction_noted=0 AND status IN ('approved','denied','provision_failed') "
                "AND created_at>=?",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def request(self, request_id: str) -> dict[str, str] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def pending(self) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE status IN ('pending','provisioned','profile_ready') "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
