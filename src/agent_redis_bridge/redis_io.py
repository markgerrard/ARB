from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import redis


OWNER_FENCE_SCRIPT = (
    "if redis.call('GET', KEYS[2]) == ARGV[2] then "
    "  local n = redis.call('LREM', KEYS[1], 1, ARGV[1]); "
    "  redis.call('DEL', KEYS[2]); "
    "  return n "
    "end "
    "return 0"
)

RECOVER_VALIDATED_SCRIPT = (
    "if redis.call('LINDEX', KEYS[1], -1) == ARGV[1] then "
    "  redis.call('DEL', KEYS[3]); "
    "  redis.call('LMOVE', KEYS[1], KEYS[2], 'RIGHT', 'LEFT'); "
    "  return 1 "
    "end "
    "return 0"
)


class OwnershipLostError(RuntimeError):
    """The caller no longer owns the bus-global agent identity."""


class IdentityOwnedError(RuntimeError):
    """Another boot instance currently holds the TTL-bound identity lease."""

    def __init__(self, agent_id: str, current_owner: str, ttl: int) -> None:
        self.agent_id = agent_id
        self.current_owner = current_owner
        self.ttl = ttl
        super().__init__(f"agent_id {agent_id} already owned by {current_owner} (ttl={ttl})")


@dataclass(frozen=True)
class RedisConfig:
    host: str
    port: str
    db: str
    prefix: str
    password: str | None = None
    user: str | None = None
    tls: bool = False

    @classmethod
    def from_env_file(cls, env_file: Path, overrides: dict[str, str | None]) -> "RedisConfig":
        env = read_env_file(env_file)
        env.update({key: value for key, value in overrides.items() if value is not None})

        return cls(
            host=require(env, "AGENT_REDIS_HOST"),
            port=require(env, "AGENT_REDIS_PORT"),
            db=require(env, "AGENT_REDIS_DB"),
            prefix=require(env, "AGENT_REDIS_PREFIX"),
            password=env.get("AGENT_REDIS_PASSWORD") or None,
            user=env.get("AGENT_REDIS_USER") or None,
            tls=env.get("AGENT_REDIS_TLS", "0") in {"1", "true", "True", "yes"},
        )

    def key(self, suffix: str) -> str:
        return f"{self.prefix}{suffix}"

    def registry_key(self, agent_id: str) -> str:
        return self.key(f"registry:{agent_id}")

    def status_key(self, agent_id: str) -> str:
        return self.key(f"agent:{agent_id}:status")

    def consumer_key(self, agent_id: str) -> str:
        return self.key(f"agent:{agent_id}:consumer")

    def inbox_key(self, agent_id: str) -> str:
        return self.key(f"agent:{agent_id}:inbox")

    def control_key(self, agent_id: str) -> str:
        return self.key(f"agent:{agent_id}:control")

    def processing_key(self, agent_id: str) -> str:
        return self.key(f"agent:{agent_id}:processing")

    def processing_claim_key(self, agent_id: str, body: str) -> str:
        # Accepted residual: identical raw bodies share this SHA-256 claim key. Envelope ids make
        # bodies unique in practice; a collision causes duplicate processing, not request loss.
        import hashlib

        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
        return self.key(f"agent:{agent_id}:processing_claim:{digest}")

    def notify_inbox_key(self, agent_id: str) -> str:
        """Separate notify list, used when --notify-inbox=0 to keep the
        main :inbox reply-only and avoid BLPOP-loop churn through
        activity-stream chatter."""
        return self.key(f"agent:{agent_id}:notify_inbox")

    def task_events_key(self, task_id: str) -> str:
        return self.key(f"task:{task_id}:events")

    def task_epoch_key(self, task_id: str) -> str:
        return self.key(f"task:{task_id}:epoch")

    def task_status_key(self, task_id: str) -> str:
        return self.key(f"task:{task_id}:status")

    def task_result_key(self, task_id: str) -> str:
        return self.key(f"task:{task_id}:result")

    def announcement_key(self, agent_id: str, announcement_id: str) -> str:
        return self.key(f"announcement:{agent_id}:{announcement_id}")

    def usage_key(self, agent_id: str, day: str, metric: str) -> str:
        return self.key(f"usage:{agent_id}:{day}:{metric}")


class RedisCli:
    """Persistent redis-py client wrapper. Replaces the old subprocess-per-op
    pattern that imposed a TLS handshake + connection cost on every Redis
    command. The class name is kept for backwards compatibility with imports
    in bridge.py and ctl.py."""

    def __init__(self, config: RedisConfig) -> None:
        self.config = config
        # socket_timeout must comfortably exceed the longest blocking list pop
        # (--blpop-timeout, default 30s). If they match, redis-py
        # can close the socket as the server-side blocking pop returns nil, surfacing
        # spurious "Timeout reading from socket" errors. 120s gives 4× headroom.
        # socket_keepalive lets the kernel surface dead peers on otherwise
        # idle long-poll connections; health_check_interval pings on idle
        # connections from the pool.
        self.client = redis.Redis(
            host=config.host,
            port=int(config.port),
            db=int(config.db),
            username=config.user or None,
            password=config.password or None,
            ssl=config.tls,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=120,
            socket_keepalive=True,
            retry_on_timeout=True,
            health_check_interval=30,
        )

    def register(
        self,
        *,
        agent_id: str,
        tool: str,
        project: str,
        workspace: str,
        branch: str,
        path: str,
        registered_at: str,
        pid: int,
        owner_token: str,
        ttl: int,
        env_scrub: str = "",
        worker_vantage: str = "",
        task_wire: str = "",
        brief_hydrate: str = "",
        registration_generation: str | None = None,
        readonly_tools: str = "",
    ) -> None:
        status_key = self.config.status_key(agent_id)
        owner = f"alive:{owner_token}"
        claimed = self.client.eval(
            """
            local current = redis.call('GET', KEYS[1])
            if not current then
              redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
              return 1
            end
            if current == ARGV[1] then
              redis.call('EXPIRE', KEYS[1], ARGV[2])
              return 1
            end
            return current
            """,
            1,
            status_key,
            owner,
            str(ttl),
        )
        if claimed != 1:
            try:
                conflict_ttl = int(self.client.ttl(status_key))
            except redis.RedisError:
                conflict_ttl = -1
            raise IdentityOwnedError(agent_id, str(claimed), conflict_ttl)
        # registration_generation freezes target identity for the dispatch
        # authority (Slice 1d-iv). Default to owner_token so a boot-stable
        # heartbeat reassert does not invalidate an in-flight receipt.
        generation = (
            registration_generation if registration_generation is not None else owner_token
        )
        self.client.hset(
            self.config.registry_key(agent_id),
            mapping={
                "tool": tool,
                "project": project,
                "workspace": workspace,
                "current_branch": branch,
                "path": path,
                "registered_at": registered_at,
                "pid": str(pid),
                "owner_token": owner_token,
                # Capability advertisement consumed by tools/faba bridge_round's
                # agent-sdk author-round guard; empty when the engine family has
                # no verified child-env scrub on this code version.
                "env_scrub": env_scrub,
                # Slice 1d-iv freeze fields. Advertisement of non-empty
                # task_wire / brief_hydrate is a later-stage seat concern;
                # the registry simply stores what the supervisor wrote.
                "worker_vantage": worker_vantage,
                "task_wire": task_wire,
                "brief_hydrate": brief_hydrate,
                "registration_generation": generation,
                # SELF-DECLARED read-only allowlist (ARB-B14a): the seat's own
                # ARB_REQUIRE_READONLY_TOOLS value, empty when undeclared. Central
                # visibility of the declaration — fleet tooling can diff it against
                # expectations, so silent de-declaration becomes an observable
                # drift. NOT an enforced posture fact; the store-backed posture
                # view (bus-side gate spec §9.3) is the class fix.
                "readonly_tools": readonly_tools,
            },
        )

    def consumer_heartbeat(self, agent_id: str, owner_token: str, ttl: int) -> None:
        """Prove inbox progress only while this boot instance still owns the identity."""
        current = self.client.eval(
            """
            local current = redis.call('GET', KEYS[1])
            if current == ARGV[1] then
              redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
              return 1
            end
            return current or ''
            """,
            2,
            self.config.status_key(agent_id),
            self.config.consumer_key(agent_id),
            f"alive:{owner_token}",
            f"consuming:{owner_token}",
            str(ttl),
        )
        if current != 1:
            raise OwnershipLostError(f"agent_id {agent_id} ownership lost: current={current or 'missing'}")

    def cleanup(self, agent_id: str, owner_token: str) -> None:
        # Release must be as atomic as claim/refresh: a GET-then-DEL sequence
        # lets the lease expire and a successor claim BETWEEN the two calls,
        # after which the DEL destroys the successor's ownership keys.
        try:
            self.client.eval(
                """
                if redis.call('GET', KEYS[1]) == ARGV[1] then
                  redis.call('DEL', KEYS[1])
                end
                if redis.call('GET', KEYS[2]) == ARGV[2] then
                  redis.call('DEL', KEYS[2])
                end
                if redis.call('HGET', KEYS[3], 'owner_token') == ARGV[3] then
                  redis.call('DEL', KEYS[3])
                end
                return 1
                """,
                3,
                self.config.status_key(agent_id),
                self.config.consumer_key(agent_id),
                self.config.registry_key(agent_id),
                f"alive:{owner_token}",
                f"consuming:{owner_token}",
                owner_token,
            )
        except redis.RedisError:
            pass

    def lpush(self, agent_id: str, body: str) -> None:
        self.client.lpush(self.config.inbox_key(agent_id), body)

    def rpush(self, agent_id: str, body: str) -> None:
        self.client.rpush(self.config.inbox_key(agent_id), body)

    def lpush_control(self, agent_id: str, body: str) -> None:
        self.client.lpush(self.config.control_key(agent_id), body)

    def lpush_key(self, key: str, body: str, *, trim: int | None = None) -> None:
        self.client.lpush(key, body)
        if trim is not None:
            self.client.ltrim(key, 0, trim - 1)

    def xadd(self, key: str, fields: dict[str, str], *, maxlen: int | None = None, ttl: int | None = None) -> str:
        kwargs: dict[str, Any] = {}
        if maxlen is not None:
            kwargs["maxlen"] = maxlen
            kwargs["approximate"] = True
        result = self.client.xadd(key, fields, **kwargs)
        if ttl is not None:
            self.client.expire(key, ttl)
        return str(result)

    def expire(self, key: str, ttl: int) -> None:
        self.client.expire(key, ttl)

    def hset_key(self, key: str, fields: dict[str, str], *, ttl: int | None = None) -> None:
        self.client.hset(key, mapping=fields)
        if ttl is not None:
            self.client.expire(key, ttl)

    def hdel_key(self, key: str, *fields: str) -> None:
        if fields:
            self.client.hdel(key, *fields)

    def set_key(self, key: str, value: str, *, ttl: int | None = None) -> None:
        self.client.set(key, value, ex=ttl)

    def get_int(self, key: str) -> int:
        value = self.client.get(key)
        if not value:
            return 0
        return int(value)

    def get_str(self, key: str) -> str | None:
        value = self.client.get(key)
        return value if value else None

    def hgetall(self, key: str) -> dict[str, str]:
        return self.client.hgetall(key) or {}

    def incrby(self, key: str, amount: int, *, ttl: int | None = None) -> int:
        value = self.client.incrby(key, amount)
        if ttl is not None:
            self.client.expire(key, ttl)
        return int(value)

    def lpop(self, agent_id: str) -> str | None:
        value = self.client.lpop(self.config.inbox_key(agent_id))
        return value if value else None

    def lpop_control(self, agent_id: str) -> str | None:
        value = self.client.lpop(self.config.control_key(agent_id))
        return value if value else None

    def blpop(self, agent_id: str, timeout: int) -> str | None:
        result = self.client.blpop(self.config.inbox_key(agent_id), timeout=timeout)
        if result is None:
            return None
        # redis-py returns (key, value) tuple
        return result[1]

    def blmove_to_processing(self, agent_id: str, timeout: int) -> str | None:
        return self.client.blmove(
            self.config.inbox_key(agent_id),
            self.config.processing_key(agent_id),
            timeout,
            "LEFT",
            "RIGHT",
        )

    def peek_processing(self, agent_id: str) -> str | None:
        return self.client.lindex(self.config.processing_key(agent_id), -1)

    def recover_processing_to_inbox(self, agent_id: str) -> str | None:
        return self.client.lmove(
            self.config.processing_key(agent_id),
            self.config.inbox_key(agent_id),
            "RIGHT",
            "LEFT",
        )

    def recover_validated(self, agent_id: str, body: str) -> int:
        return int(self.client.eval(
            RECOVER_VALIDATED_SCRIPT, 3,
            self.config.processing_key(agent_id),
            self.config.inbox_key(agent_id),
            self.config.processing_claim_key(agent_id, body),
            body,
        ))

    def claim_processing(self, agent_id: str, body: str, owner_token: str, ttl: int) -> None:
        # Tag the just-parked entry with the claiming daemon's per-boot owner_token. A successor's
        # re-park overwrites this, so a stale predecessor's owner_token no longer matches (M2).
        self.client.set(self.config.processing_claim_key(agent_id, body), owner_token, ex=ttl)

    def clear_processing_claim(self, agent_id: str, body: str) -> None:
        self.client.delete(self.config.processing_claim_key(agent_id, body))

    def remove_processing(self, agent_id: str, body: str, owner_token: str) -> int:
        # Owner-fenced acknowledgement (M2): LREM the parked body only if THIS daemon still holds
        # the claim. Atomic Lua (same shape as the register/cleanup lease compare-then-act) so a
        # concurrent re-claim cannot slip between check and act. Missing/mismatched claim => no-op
        # (the entry stays parked for recovery) — NEVER an unconditional LREM.
        return int(self.client.eval(
            OWNER_FENCE_SCRIPT, 2,
            self.config.processing_key(agent_id),
            self.config.processing_claim_key(agent_id, body),
            body, owner_token,
        ))


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value

    return values


def require(values: dict[str, Any], key: str) -> str:
    value = values.get(key) or os.environ.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")

    return value
