from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

import redis


ENGINES = ("claude", "codex", "pi")
ROLE_NAMES = (*ENGINES, "worker")
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class AclProvisionError(RuntimeError):
    pass


class AclResidueError(AclProvisionError):
    """The bus and the files are KNOWN to disagree, or a live identity is stranded.

    Raised only where the state cannot be reasoned about from the files alone. It is
    the signal that the in-flight marker must SURVIVE so the next provision refuses:
    every other failure path restores a clean state and clears it.
    """


def validate_host(host: str) -> str:
    normalized = host.strip().lower()
    if not HOST_RE.fullmatch(normalized):
        raise AclProvisionError("host must be a lower-case DNS label")
    return normalized


def validate_declared_roles(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise AclProvisionError("roles must be a non-empty list")
    if any(not isinstance(role, str) or role not in ROLE_NAMES for role in value):
        raise AclProvisionError(
            "roles must be a subset of claude,codex,pi,worker"
        )
    if len(set(value)) != len(value):
        raise AclProvisionError("roles must not contain duplicates")
    return tuple(role for role in ROLE_NAMES if role in value)


def role_username(role: str, host: str) -> str:
    host = validate_host(host)
    if role not in ROLE_NAMES:
        raise AclProvisionError(f"unsupported role: {role}")
    return f"arb-worker-{host}" if role == "worker" else f"{role}-orch-{host}"


def _selector(commands: tuple[str, ...], patterns: tuple[str, ...]) -> str:
    return "(" + " ".join((*commands, *patterns)) + ")"


def _base(db: int, commands: tuple[str, ...] = ()) -> list[str]:
    return [
        "resetchannels", "resetkeys", "clearselectors", "-@all", "+ping",
        f"+select|{db}", *commands,
    ]


def _audit_selectors() -> tuple[str, str]:
    return (
        # redis-py Redis.incr() is INCRBY on the wire.
        _selector(("+incr", "+incrby", "+expire"), ("~arbmem:audit:run:*:seq",)),
        _selector(("+xadd",), ("~arbmem:audit",)),
    )


def _task_selectors(*, seat: bool = False) -> tuple[str, str, str, str]:
    """Task-state grants. `seat=True` adds the two commands only a bridge daemon
    issues (HDEL to clear status fields, EXPIRE on the events stream); callers that
    merely read task state do not need them."""
    status_cmds = ("+hset", "+hget", "+hgetall", "+expire", "+del")
    events_cmds = ("+xadd", "+xrange", "+xlen", "+xinfo")
    if seat:
        status_cmds = ("+hset", "+hget", "+hgetall", "+hdel", "+expire", "+del")
        events_cmds = ("+xadd", "+xrange", "+xlen", "+xinfo", "+expire")
    return (
        _selector(status_cmds, ("~agent_scratch:task:*:status",)),
        _selector(("+set", "+get", "+expire"), ("~agent_scratch:task:*:result",)),
        _selector(events_cmds, ("~agent_scratch:task:*:events",)),
        # +expire is NOT optional: redis_io.incrby(key, n, ttl=...) issues INCRBY *then*
        # EXPIRE on the same key (redis_io.py ~339-343). Granting only +incrby lets the
        # counter be created and then leaves the EXPIRE to throw, which killed every turn in
        # process_request before [turn-start] — and leaked an unexpirable key per attempt,
        # because the credential could neither read nor delete what it had just created.
        # Caught by the mini canary 2026-08-09, after two earlier rounds of the same class.
        _selector(("+incrby", "+expire"), ("~agent_scratch:task:*:epoch",)),
    )


def _orch_monitoring_selectors() -> tuple[str, str, str, str]:
    """What an orchestrator needs to DRIVE and WATCH a dispatch, derived from the call sites.

    Without these an orch can dispatch and receive a reply but cannot monitor: every
    documented diagnostic (`ctl status`, `ctl result`, `ctl watch`) and the panel's live view
    NOPERM. That is the same run-vs-register blind spot that produced four worker-grant gaps.

    Call sites, verb by verb:
      scripts/agent-dispatch:511,637,646,654  HSET + EXPIRE  task:<id>:status  (the DISPATCHER
                                              writes status; it is not read-only)
      ctl.print_status                        HGETALL        task:<id>:status
      ctl.print_result                        GET            task:<id>:result
      ctl.watch_task / watch_task_until_done  XREAD          task:<id>:events
      scripts/arb-orch-panel                  XREAD, XREVRANGE  events:live
    """
    return (
        _selector(("+hset", "+hgetall", "+expire"), ("~agent_scratch:task:*:status",)),
        _selector(("+get",), ("~agent_scratch:task:*:result",)),
        _selector(("+xread",), ("~agent_scratch:task:*:events",)),
        # Read side of the stream a seat now writes with +xadd. Granting one without the
        # other leaves the visibility plane half-wired: seats emit, nobody can look.
        _selector(("+xread", "+xrevrange"), ("~agent_scratch:events:live",)),
    )


def orchestrator_rules(engine: str, host: str) -> str:
    """Rendered rules string. `orchestrator_rule_args` is the source of truth."""
    return " ".join(orchestrator_rule_args(engine, host))


def orchestrator_rule_args(engine: str, host: str) -> list[str]:
    if engine not in ENGINES:
        raise AclProvisionError(f"unsupported engine: {engine}")
    host = validate_host(host)
    # MUST be derived from role_username(), not rebuilt: this previously read
    # f"{engine}-{host}-*" and so omitted the `-orch-` segment, producing patterns
    # (agent:claude-host-b-*:...) that match NO key of the real identity
    # (claude-orch-host-b-cli). Minting from it would have left an orchestrator unable to
    # read its own inbox — a hard outage. Never existed in production only because every
    # orch line to date was hand-built; caught when codex-arbmem-prod diffed generator
    # output against live before applying, and refused (2026-08-09).
    identity = f"{role_username(engine, host)}*"
    rules = _base(12)
    rules += [
        # Own namespace is `:*`, NOT `:inbox` — an orchestrator also owns its :status and
        # :consumer keys, and a `:inbox`-only grant silently denies its own heartbeat.
        _selector(
            (
                "+blpop", "+brpop", "+lpop", "+llen", "+lrange", "+lrem",
                "+rpush", "+del", "+type", "+get", "+set", "+expire", "+ttl",
            ),
            (f"~agent_scratch:agent:{identity}:*",),
        ),
        _selector(("+lpush",), ("~agent_scratch:agent:*:inbox",)),
        # Presence read on peers — every hand-built line carries this and the generator
        # did not. Read-only: GET/TTL/PTTL/EXISTS, never SET or DEL on a foreign status.
        _selector(
            ("+get", "+ttl", "+pttl", "+exists"),
            ("~agent_scratch:agent:*:status",),
        ),
        # ARB Secrets TRANSPORT: blobs, status, outstanding. Ephemeral by design, so the
        # destructive verbs belong here — a consumed blob SHOULD be deletable.
        _selector(
            (
                "+get", "+set", "+setnx", "+getdel", "+del", "+exists",
                "+expire", "+ttl", "+pttl", "+type",
            ),
            (f"~agent_scratch:secrets:*:{identity}:*",),
        ),
        # ARB Secrets TRUST ROOT: deliberately NOT the transport command set.
        #
        # These two key families used to share one selector, so the pubkey inherited
        # transport verbs by accident of grouping — `+getdel` on a trust root is the tell:
        # a read that destroys what it read. A self-publication credential could therefore
        # UNPUBLISH its own trust root, and on 2026-08-10 claude-orch-mini-dev did exactly
        # that, sixty seconds after publishing, from a capability probe that listed DEL
        # beside SCAN and CONFIG GET as though all three were the same kind of question.
        #
        # Rotation does NOT need +del: you rotate a pubkey by SETting the new value. So the
        # destructive verbs bought nothing here and cost the key. Publication stays additive
        # and idempotent; removal is an operator action, not a credential capability.
        _selector(
            ("+get", "+set", "+exists"),
            (f"~agent_scratch:secrets:pubkey:{identity}",),
        ),
        *_orch_monitoring_selectors(),
        *_audit_selectors(),
    ]
    return rules


def worker_rules(host: str) -> str:
    """Rendered rules string. `worker_rule_args` is the source of truth."""
    return " ".join(worker_rule_args(host))


def worker_rule_args(host: str) -> list[str]:
    """Grants for a per-host shared seat credential.

    Derived from the code paths a bridge daemon actually executes, NOT from the
    spec's prose — the earlier shape was written from the grant list and could only
    confirm itself, so it omitted every startup command and no seat could run under
    it (lead review, 2026-08-09).
    """
    host = validate_host(host)
    own = f"worker-{host}-*"
    return list(
        (
            *_base(12),
            # Identity lease. register/heartbeat/cleanup claim and release the
            # daemon's own status + consumer keys and registry hash through Lua
            # (redis_io.py), so EVAL must be granted over exactly those keys.
            _selector(
                ("+eval",),
                (
                    f"~agent_scratch:agent:{own}:status",
                    f"~agent_scratch:agent:{own}:consumer",
                    f"~agent_scratch:registry:{own}",
                ),
            ),
            _selector(
                ("+get", "+set", "+expire", "+ttl", "+del"),
                (
                    f"~agent_scratch:agent:{own}:status",
                    f"~agent_scratch:agent:{own}:consumer",
                ),
            ),
            # Registry keys are `registry:<id>`, NOT `agent:<id>:registry` — an
            # `agent:...` pattern silently fails to match them.
            _selector(("+hset", "+hget", "+del"), (f"~agent_scratch:registry:{own}",)),
            # Reliable consume path: BLMOVE inbox -> processing, per-body claim key,
            # owner fence — all Lua, all over the daemon's own keys.
            _selector(
                ("+eval",),
                (
                    f"~agent_scratch:agent:{own}:inbox",
                    f"~agent_scratch:agent:{own}:processing",
                    f"~agent_scratch:agent:{own}:processing_claim:*",
                ),
            ),
            _selector(
                (
                    "+blpop", "+brpop", "+blmove", "+lmove", "+lpop", "+lindex",
                    "+llen", "+lrange", "+lrem", "+rpush", "+del", "+type",
                ),
                (
                    f"~agent_scratch:agent:{own}:inbox",
                    f"~agent_scratch:agent:{own}:processing",
                ),
            ),
            _selector(
                ("+set", "+get", "+del"),
                (f"~agent_scratch:agent:{own}:processing_claim:*",),
            ),
            _selector(("+lpop",), (f"~agent_scratch:agent:{own}:control",)),
            _selector(("+lpush",), ("~agent_scratch:agent:*:inbox",)),
            # Notify split (BRIDGE_NOTIFY_INBOX=0) writes to the CALLER's
            # :notify_inbox. `agent:*:inbox` does NOT match that key — it ends
            # `_inbox`, not `:inbox`. LTRIM enforces the maxlen cap.
            _selector(("+lpush", "+ltrim"), ("~agent_scratch:agent:*:notify_inbox",)),
            # events:live is the human-visibility plane (arb-watch and friends). A seat tees
            # four lifecycle events per turn through it, and `Bridge.live_redis` FALLS BACK to
            # the coordination connection when ARB_LIVE_REDIS_URL is unset — so on a
            # single-bus host these are coordination-plane writes under THIS credential.
            #
            # NOTE THE CONTRAST WITH AUDIT, because the two look identical and resolve
            # oppositely: audit rides a SEPARATE connection (ARB_AUDIT_REDIS_URL) under
            # audit-emitter, so its DB-12 grants were dead and were removed. The live tee has
            # no such separation by default, so its grant is required here. Do not "tidy" this
            # away by analogy with the audit removal.
            #
            # The failure is FAIL-SOFT and therefore nastier than a hard one: the turn
            # completes and replies while every lifecycle event is denied, so a migrated seat
            # is invisible in the live stream while looking perfectly healthy to its
            # dispatcher — and absence there reads as idleness, not denial.
            # +expire alongside +xadd: visibility_tee.live_tee does XADD and THEN
            # `redis.expire(key, ttl)` (visibility_tee.py:72-73). Granting only +xadd moves the
            # denial one line down instead of removing it — the entries land and the EXPIRE
            # throws. Third instance of this exact family after task:*:epoch and the identity
            # lease; the shape is "a helper issues a second command the grant does not name".
            _selector(("+xadd", "+expire"), ("~agent_scratch:events:live",)),
            # Daily usage counters: a seat started with --daily-request-limit or
            # --daily-turn-seconds-limit does GET for the limit check and then
            # incrby(usage_key(...), ttl=36h) -> INCRBY + EXPIRE, on
            # `agent_scratch:usage:<usage_identity>:<day>:<metric>`.
            #
            # The pattern is host-scoped rather than seat-scoped ON PURPOSE. usage_identity is
            # `args.usage_scope or agent_id`, and --usage-scope exists precisely so several
            # seats can share ONE account-level daily budget, so a per-seat pattern would break
            # the flag's whole reason for existing. Host-scoped keeps shared budgets working
            # while still refusing a blanket `usage:*` — a seat cannot read or spend another
            # HOST's budget. Cross-host shared budgets remain a separate, explicit decision.
            #
            # Residual worth knowing: nothing stops a seat being STARTED with a --usage-scope
            # outside `worker-<host>-*`, and the failure would be a runtime NOPERM on the first
            # limited request rather than a startup error. A start-time guard rejecting such a
            # scope is proposed separately (codex-arbmem-prod, 2026-08-09).
            _selector(
                ("+get", "+incrby", "+expire"),
                (f"~agent_scratch:usage:worker-{host}-*:*",),
            ),
            *_task_selectors(seat=True),
            # Deliberately NO audit selectors: a seat's audit emit rides a SEPARATE
            # connection (ARB_AUDIT_REDIS_URL / ARB_MEMORY_REDIS_URL, DB 5) under the
            # audit-emitter credential, so DB-12 audit grants here are dead weight.
            # What is mandatory for a vote-emitting seat is that it is CONFIGURED
            # with the audit-emitter credential — not a grant on this identity.
        )
    )


def role_rules(host: str, roles: list[str] | tuple[str, ...]) -> dict[str, str]:
    host = validate_host(host)
    declared = validate_declared_roles(roles)
    result = {}
    for role in declared:
        username = role_username(role, host)
        result[username] = (
            worker_rules(host) if role == "worker" else orchestrator_rules(role, host)
        )
    return result


def role_rule_args(host: str, roles: list[str] | tuple[str, ...]) -> dict[str, list[str]]:
    """Per-identity ACL SETUSER arguments — each selector stays ONE argument.

    `role_rules` renders these into the aclfile line. Both come from this one
    structure so the wire form and the file form cannot drift; rendering the rules
    twice from two code paths is a second parser and a defect surface.
    """
    host = validate_host(host)
    declared = validate_declared_roles(roles)
    return {
        role_username(role, host): (
            worker_rule_args(host) if role == "worker"
            else orchestrator_rule_args(role, host)
        )
        for role in declared
    }


def proof_keys(username: str, host: str) -> tuple[str, str]:
    """The (inbox, own-secret) keys a live ACL proof probes, derived from the REAL username.

    MUST NOT be rebuilt as f"{engine}-{host}-acl-proof": that drops the `-orch-` segment
    and lands outside the identity's own granted namespace. It is the same defect fixed in
    the generator at 0f47706a, which survived HERE because only the generator was swept —
    and it fails differently at the two sites, the quiet one being the dangerous one:

    * own-secret — HARD FAIL. `claude-mini-dev-acl-proof` does not match the granted
      `~agent_scratch:secrets:pubkey:claude-orch-mini-dev*`, so the proof is denied and
      `provision()` raises AFTER it has already written the aclfile and ACL LOADed.
    * inbox — SILENT PASS. The malformed key still matches the broad `agent:*:inbox` SEND
      grant, so the probe goes green while proving the wrong property: it never exercises
      the identity's OWN namespace, which is the only thing that proof exists to establish.

    Found by codex-arbmem-prod reading this code before calling the provisioner (2026-08-10).
    """
    if username.startswith("arb-worker-"):
        inbox = f"agent_scratch:agent:worker-{host}-acl-proof:inbox"
    else:
        inbox = f"agent_scratch:agent:{username}-acl-proof:inbox"
    return inbox, f"agent_scratch:secrets:pubkey:{username}-acl-proof"


def _acl_line(username: str, password: str, rules: str) -> str:
    digest = hashlib.sha256(password.encode()).hexdigest()
    return f"user {username} on #{digest} {rules}"


class RedisConnector:
    """Fresh-connection command edge used by ACL LOAD and reconnect proofs."""

    def __init__(
        self, *, host: str, port: int, tls: bool = True, ca_cert: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.tls = tls
        self.ca_cert = ca_cert

    def command(
        self, username: str, password: str, db: int, *command: str,
    ) -> Any:
        client = redis.Redis(
            host=self.host, port=self.port, db=db, username=username,
            password=password, ssl=self.tls,
            ssl_cert_reqs="required" if self.tls else None,
            ssl_ca_certs=self.ca_cert if self.tls else None,
            decode_responses=True, socket_connect_timeout=3, socket_timeout=5,
        )
        try:
            return client.execute_command(*command)
        finally:
            client.close()


class AclVerifier:
    def __init__(self, connector: RedisConnector, admin_user: str, admin_password: str) -> None:
        self.connector = connector
        self.admin_user = admin_user
        self.admin_password = admin_password

    def _allowed(self, username: str, password: str, db: int, *command: str) -> Any:
        try:
            return self.connector.command(username, password, db, *command)
        except redis.ResponseError as exc:
            raise AclProvisionError(
                f"expected allow for {username} {command[0]}, got {exc}"
            ) from exc

    def _denied(self, username: str, password: str, db: int, *command: str) -> None:
        try:
            self.connector.command(username, password, db, *command)
        except redis.ResponseError as exc:
            # redis-py 4.x turns the wire-level NOPERM reply into the exact
            # NoPermissionError subtype and removes the NOPERM token from its
            # display string; older clients retain the token in ResponseError.
            if isinstance(exc, redis.exceptions.NoPermissionError) or "NOPERM" in str(exc):
                return
            raise AclProvisionError(
                f"expected exact NOPERM for {username} {command[0]}, got {exc}"
            ) from exc
        raise AclProvisionError(f"unexpected allow for {username} {command[0]}")

    def verify(
        self, host: str, credentials: dict[str, str], roles: list[str] | tuple[str, ...]
    ) -> None:
        host = validate_host(host)
        expected_rules = role_rules(host, roles)
        cleanup_keys: list[str] = []
        audit_entries: list[str] = []
        for username in sorted(expected_rules):
            password = credentials[username]
            own_inbox, _ = proof_keys(username, host)
            foreign_inbox = "agent_scratch:agent:foreign-host-acl-proof:inbox"
            sink = "agent_scratch:agent:acl-proof-sink:inbox"
            seq = f"arbmem:audit:run:bus-reg-{host}-{username}:seq"

            if self._allowed(username, password, 12, "PING") is not True:
                raise AclProvisionError(f"fresh DB12 PING failed for {username}")
            self._allowed(username, password, 12, "RPUSH", own_inbox, "probe")
            popped = self._allowed(username, password, 12, "BLPOP", own_inbox, "1")
            if not popped or popped[1] != "probe":
                raise AclProvisionError(f"own inbox consume proof failed for {username}")
            self._allowed(username, password, 12, "LPUSH", sink, username)
            if int(self._allowed(username, password, 12, "INCRBY", seq, "1")) != 1:
                raise AclProvisionError(f"audit INCRBY proof failed for {username}")
            self._allowed(username, password, 12, "EXPIRE", seq, "60")
            audit_id = self._allowed(
                username, password, 12, "XADD", "arbmem:audit", "*",
                "source", "bus-registrar-acl-proof", "role", username,
            )
            audit_entries.append(str(audit_id))
            cleanup_keys.extend((own_inbox, sink, seq))

            self._denied(username, password, 1, "PING")
            self._denied(username, password, 12, "KEYS", "*")
            self._denied(username, password, 12, "SCAN", "0")
            self._denied(username, password, 12, "CONFIG", "GET", "maxmemory")
            self._denied(username, password, 12, "BLPOP", foreign_inbox, "1")
            self._denied(username, password, 12, "XADD", "arbmem:writes", "*", "probe", "1")

        for username in sorted(
            name for name in expected_rules if not name.startswith("arb-worker-")
        ):
            _, own_secret = proof_keys(username, host)
            self._allowed(username, credentials[username], 12, "SET", own_secret, "pubkey")
            if self._allowed(username, credentials[username], 12, "GET", own_secret) != "pubkey":
                raise AclProvisionError(f"own secret proof failed for {username}")
            cleanup_keys.append(own_secret)
            self._denied(
                username, credentials[username], 12, "GET",
                "agent_scratch:secrets:private:foreign-host:value",
            )

        if cleanup_keys:
            self.connector.command(
                self.admin_user, self.admin_password, 12, "DEL", *sorted(set(cleanup_keys))
            )
        if audit_entries:
            self.connector.command(
                self.admin_user, self.admin_password, 12,
                "XDEL", "arbmem:audit", *audit_entries,
            )


class AclProvisioner:
    def __init__(
        self, *, acl_path: Path, credentials_path: Path,
        connector: RedisConnector,
    ) -> None:
        self.acl_path = acl_path
        self.credentials_path = credentials_path
        self.connector = connector

    def provision(
        self, host: str, roles: list[str] | tuple[str, ...]
    ) -> dict[str, dict[str, str | int]]:
        host = validate_host(host)
        declared = validate_declared_roles(roles)
        if self._marker_path().exists():
            raise AclProvisionError(
                f"unresolved provision marker {self._marker_path()}: a previous provision "
                "died mid-flight and may have left a live in-memory identity absent from "
                "both files. Resolve it (compare the recorded hashes, ACL LOAD, remove the "
                "marker) before provisioning again"
            )
        credentials = json.loads(self.credentials_path.read_text())
        if not isinstance(credentials, dict) or not isinstance(credentials.get("arb-admin"), str):
            raise AclProvisionError("root credential store is missing arb-admin")
        original_credentials = dict(credentials)
        all_rules = role_rules(host, ROLE_NAMES)
        requested_rules = role_rules(host, declared)
        lines = self.acl_path.read_text().splitlines()
        line_by_user = {
            parts[1]: line for line in lines
            if len(parts := line.split(None, 2)) >= 2 and parts[0] == "user"
        }

        current_usernames: set[str] = set()
        for username, rules in all_rules.items():
            password = credentials.get(username)
            existing = line_by_user.get(username)
            if password is None and existing is None:
                continue
            if not isinstance(password, str) or existing is None:
                raise AclProvisionError(f"existing ACL identity is incomplete: {username}")
            if existing != _acl_line(username, password, rules):
                raise AclProvisionError(f"existing ACL identity differs: {username}")
            current_usernames.add(username)

        new_usernames: set[str] = set()
        for username, rules in requested_rules.items():
            if username in current_usernames:
                continue
            password = secrets.token_hex(32)
            credentials[username] = password
            lines.append(_acl_line(username, password, rules))
            current_usernames.add(username)
            new_usernames.add(username)

        admin_password = credentials["arb-admin"]
        current_roles = tuple(
            role for role in ROLE_NAMES
            if role_username(role, host) in current_usernames
        )
        new_roles = tuple(
            role for role in ROLE_NAMES
            if role_username(role, host) in new_usernames
        )
        acl_before = self.acl_path.read_text()
        credentials_before = self.credentials_path.read_text()
        acl_after = "\n".join(lines) + "\n"

        # Everything below this point mutates something, so the marker goes first —
        # including the in-memory SETUSER, which leaves a live fully-permissioned identity
        # that appears in NEITHER file if we die before DELUSER. It is written for EVERY
        # provision, not only ones adding users, because the commit block reloads the ACL
        # and a failed rollback there is just as unresolvable with nothing new to add.
        #
        # THE MARKER IS CLEARED ONLY WHEN THE STATE IS KNOWN CLEAN. Every path that ends
        # in a KNOWN-INCONSISTENT state raises AclResidueError and leaves it in place, so
        # the next provision is refused. A `finally: self._clear_marker()` is exactly wrong
        # here: it disarms the guard precisely in the cases it exists for, which is the
        # defect codex-arbmem-prod found in the first version of this method.
        self._write_marker(host, acl_before, credentials_before, sorted(new_usernames))

        if new_usernames:
            try:
                self._preflight(host, credentials, new_roles, admin_password)
            except AclResidueError:
                raise  # a live candidate could not be removed — the marker MUST survive
            except BaseException:
                self._clear_marker()  # bus untouched; files never opened
                raise

        try:
            if credentials != original_credentials:
                self._atomic_json(self.credentials_path, credentials, 0o600)
            if acl_before != acl_after:
                self._atomic_text(self.acl_path, acl_after, 0o640)
            try:
                self.connector.command("arb-admin", admin_password, 0, "ACL", "LOAD")
            except redis.RedisError as exc:
                raise AclProvisionError(f"ACL LOAD failed: {exc}") from exc
            AclVerifier(self.connector, "arb-admin", admin_password).verify(
                host, credentials, current_roles
            )
        except BaseException as exc:
            # _rollback raises AclResidueError if it cannot restore, and that propagates
            # with the marker intact.
            self._rollback(acl_before, credentials_before, admin_password, exc)
            self._clear_marker()  # restored exactly; the state is clean again
            raise

        self._clear_marker()
        return {
            username: {"username": username, "password": credentials[username], "db": 12}
            for username in sorted(current_usernames)
        }

    def _marker_path(self) -> Path:
        return self.acl_path.with_name(self.acl_path.name + ".provision-inflight")

    def _write_marker(
        self, host: str, acl_before: str, credentials_before: str, candidates: list[str]
    ) -> None:
        """Record enough to RESOLVE a crash, and nothing that leaks if the box is read.

        Paths and hashes only — never a password. The hashes let a human tell whether the
        files were replaced before the crash; the candidate names tell them which live
        in-memory identity to look for and DELUSER.
        """
        self._atomic_new(
            self._marker_path(),
            json.dumps(
                {
                    "host": host,
                    "candidates": candidates,
                    "acl_path": str(self.acl_path),
                    "acl_sha256_before": hashlib.sha256(acl_before.encode()).hexdigest(),
                    "credentials_path": str(self.credentials_path),
                    "credentials_sha256_before": hashlib.sha256(
                        credentials_before.encode()
                    ).hexdigest(),
                },
                sort_keys=True,
            )
            + "\n",
            0o600,
        )

    def _clear_marker(self) -> None:
        self._marker_path().unlink(missing_ok=True)

    def _preflight(
        self, host: str, credentials: dict[str, str], new_roles: tuple[str, ...],
        admin_password: str,
    ) -> None:
        """Prove each candidate LIVE, in memory only, before either file is touched.

        The candidate is created with the EXACT username and the EXACT rule arguments that
        will be persisted — proving an adjacent identity would repeat the defect this whole
        area keeps producing, where a check passes for reasons unrelated to the thing under
        test. It is removed again whether or not the proof succeeds, so a failed preflight
        leaves the bus byte-identical to how it was found.
        """
        rule_args = role_rule_args(host, new_roles)
        created: list[str] = []
        try:
            for username in sorted(rule_args):
                self.connector.command(
                    "arb-admin", admin_password, 0, "ACL", "SETUSER", username,
                    "on", f">{credentials[username]}", *rule_args[username],
                )
                created.append(username)
            AclVerifier(self.connector, "arb-admin", admin_password).verify(
                host, credentials, new_roles
            )
        finally:
            for username in created:
                try:
                    self.connector.command(
                        "arb-admin", admin_password, 0, "ACL", "DELUSER", username
                    )
                except redis.RedisError as exc:
                    raise AclResidueError(
                        f"preflight left live identity {username} that could not be "
                        f"removed: {exc}. It exists in memory and in NEITHER file, so no "
                        "file inspection reveals it and it survives until restart"
                    ) from exc

    def _rollback(
        self, acl_before: str, credentials_before: str, admin_password: str,
        cause: BaseException,
    ) -> None:
        """Restore both files exactly and reload. A failed rollback is never swallowed.

        Whole-file semantics: ACL LOAD replaces the entire ACL from the file, so this is
        correct only if provisioning is the sole ACL writer. The in-flight marker is what
        enforces that assumption against a second provisioner.
        """
        try:
            if self.acl_path.read_text() != acl_before:
                self._atomic_text(self.acl_path, acl_before, 0o640)
            if self.credentials_path.read_text() != credentials_before:
                self._atomic_text(self.credentials_path, credentials_before, 0o600)
            self.connector.command("arb-admin", admin_password, 0, "ACL", "LOAD")
        except BaseException as rollback_exc:
            raise AclResidueError(
                f"ROLLBACK FAILED after {type(cause).__name__}: {cause}. "
                f"Rollback error: {rollback_exc}. The bus and the files may now DISAGREE — "
                "do not retry; inspect both before any further provision"
            ) from rollback_exc

    @staticmethod
    def _atomic_new(path: Path, value: str, mode: int) -> None:
        """Atomic create for a path that does not exist yet (`_atomic_text` stats first)."""
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, str], mode: int) -> None:
        AclProvisioner._atomic_text(
            path, json.dumps(value, sort_keys=True) + "\n", mode,
        )

    @staticmethod
    def _atomic_text(path: Path, value: str, mode: int) -> None:
        stat = path.stat()
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_stat = tmp.stat()
            if (tmp_stat.st_uid, tmp_stat.st_gid) != (stat.st_uid, stat.st_gid):
                os.chown(tmp, stat.st_uid, stat.st_gid)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
