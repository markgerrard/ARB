from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from agent_redis_bridge.redis_io import read_env_file

from .nip_oa import (
    OwnerKeyError, build_auth_tag, decode_secret, load_owner_secret, xonly_pubkey,
)


class BuzzError(RuntimeError):
    pass


class ProvisionError(BuzzError):
    def __init__(self, channel: str, detail: str) -> None:
        super().__init__(f"channel {channel}: {detail}")
        self.channel = channel


class BuzzOps:
    def __init__(
        self, env: dict[str, str] | None = None, buzz_env_file: Path | None = None
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        if buzz_env_file is not None:
            self.env.update(read_env_file(buzz_env_file))
        self.cli = shlex.split(self._required("ARB_REGISTRAR_BUZZ_CLI"))
        self.admin = shlex.split(self._required("ARB_REGISTRAR_BUZZ_ADMIN"))
        self.ops_channel = self._required("ARB_REGISTRAR_OPS_CHANNEL")
        self.mark_pubkey = self._required("ARB_REGISTRAR_MARK_PUBKEY").lower()
        try:
            mark_bytes = bytes.fromhex(self.mark_pubkey)
        except ValueError:
            mark_bytes = b""
        if len(mark_bytes) != 32:
            raise BuzzError("ARB_REGISTRAR_MARK_PUBKEY must be a full 64-hex pubkey")
        self.database_url = self._required("ARB_REGISTRAR_DATABASE_URL")
        self.community_id = self._required("ARB_REGISTRAR_COMMUNITY_ID")
        try:
            self.registrar_pubkey = xonly_pubkey(
                decode_secret(self._required("BUZZ_PRIVATE_KEY"))
            )
        except OwnerKeyError as exc:
            raise BuzzError("BUZZ_PRIVATE_KEY must be a hex or nsec registrar key") from exc
        self.owner_secret: bytes | None = None
        owner_key_file = self.env.get("ARB_REGISTRAR_OWNER_KEY_FILE", "").strip()
        if owner_key_file:
            try:
                self.owner_secret = load_owner_secret(
                    Path(owner_key_file).expanduser(), self.mark_pubkey
                )
            except (OSError, OwnerKeyError) as exc:
                raise BuzzError(f"invalid ARB_REGISTRAR_OWNER_KEY_FILE: {exc}") from exc

    def _required(self, key: str) -> str:
        value = self.env.get(key, "").strip()
        if not value:
            raise BuzzError(f"{key} is required")
        return value

    def _run(
        self, argv: list[str], *, stdin: str | None = None, admin: bool = False,
        expect_json: bool = True,
    ) -> Any:
        prefix = self.admin if admin else self.cli
        proc = subprocess.run(prefix + argv, input=stdin, text=True, capture_output=True, env=self.env)
        if proc.returncode:
            raise BuzzError(f"command failed ({proc.returncode}): {' '.join(argv)}: {proc.stderr.strip()}")
        output = proc.stdout.strip()
        if not output:
            return {}
        if not expect_json:
            return output
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise BuzzError(f"command returned non-JSON: {' '.join(argv)}") from exc

    def post_approval(
        self, request_id: str, name: str, host: str, pubkey: str,
        channels: list[str], standing_warnings: list[str] | None = None,
    ) -> str:
        warnings = standing_warnings or []
        warning_block = ""
        if warnings:
            warning_block = (
                "\n\n⚠ Registrar channel-admin precheck failed:\n- "
                + "\n- ".join(warnings)
                + "\nRepair registrar standing before approving this request."
            )
        content = (
            f"Seat registration `{request_id}` requests approval.\n"
            f"Name: `{name}`\nHost: `{host}`\nPubkey: `{pubkey}`\n"
            f"Channels: {', '.join(channels) if channels else '(none)'}"
            f"{warning_block}\n\n"
            f"Reply in this thread with `approve {request_id}` or `deny {request_id}`."
            f" Or react ✅ to approve / ❌ to deny. First valid signal wins."
        )
        result = self._run(["messages", "send", "--channel", self.ops_channel, "--content", "-", "--mention", self.mark_pubkey], stdin=content)
        event_id = result.get("event_id") or result.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise BuzzError("approval message response did not contain event_id")
        return event_id

    def thread(self, event_id: str) -> list[dict[str, Any]]:
        result = self._run(["messages", "thread", "--channel", self.ops_channel, "--event", event_id])
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        for key in ("messages", "events", "items"):
            if isinstance(result.get(key), list):
                return [item for item in result[key] if isinstance(item, dict)]
        return []

    def reactions(self, event_id: str) -> dict[str, set[str]]:
        result = self._run(["reactions", "get", "--event", event_id])
        items = result.get("reactions", []) if isinstance(result, dict) else []
        reactions: dict[str, set[str]] = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("emoji"), str):
                continue
            pubkeys = item.get("pubkeys", [])
            if isinstance(pubkeys, list):
                reactions[item["emoji"]] = {
                    str(pubkey).lower() for pubkey in pubkeys if isinstance(pubkey, str)
                }
        return reactions

    def react(self, event_id: str, emoji: str) -> None:
        self._run(["reactions", "add", "--event", event_id, "--emoji", emoji])

    def reply(self, event_id: str, content: str) -> None:
        self._run(
            [
                "messages", "send", "--channel", self.ops_channel, "--content", "-",
                "--reply-to", event_id,
            ],
            stdin=content,
        )

    def channel_member_role(self, channel: str, pubkey: str) -> str | None:
        result = self._run(["channels", "members", "--channel", channel])
        if isinstance(result, dict):
            result = result.get("members", result.get("items", []))
        if not isinstance(result, list):
            raise BuzzError("channel members response was not a list")
        for member in result:
            if (
                isinstance(member, dict)
                and str(member.get("pubkey", "")).lower() == pubkey.lower()
            ):
                role = member.get("role")
                return str(role).lower() if isinstance(role, str) else ""
        return None

    def channel_has_member(self, channel: str, pubkey: str) -> bool:
        return self.channel_member_role(channel, pubkey) is not None

    def registrar_standing_warnings(self, channels: list[str]) -> list[str]:
        warnings = []
        for channel in channels:
            try:
                role = self.channel_member_role(channel, self.registrar_pubkey)
            except BuzzError as exc:
                warnings.append(f"channel `{channel}`: standing unreadable ({exc})")
                continue
            if role is None:
                warnings.append(f"channel `{channel}`: registrar is not a member")
            elif role not in {"admin", "owner"}:
                warnings.append(
                    f"channel `{channel}`: registrar role `{role or '(missing)'}` "
                    "is not admin"
                )
        return warnings

    @property
    def owner_auth_enabled(self) -> bool:
        return self.owner_secret is not None

    def owner_auth_tag(self, agent_pubkey: str) -> str | None:
        if self.owner_secret is None:
            return None
        try:
            return build_auth_tag(self.owner_secret, agent_pubkey)
        except OwnerKeyError as exc:
            raise BuzzError(f"could not build owner auth tag: {exc}") from exc

    def provision(self, pubkey: str, channels: list[str]) -> None:
        self._run(
            ["add-member", "--pubkey", pubkey], admin=True, expect_json=False
        )
        for channel in channels:
            try:
                if not self.channel_has_member(channel, pubkey):
                    self._run([
                        "channels", "add-member", "--channel", channel,
                        "--pubkey", pubkey, "--role", "bot",
                    ])
                if not self.channel_has_member(channel, pubkey):
                    raise BuzzError("membership was not visible after add")
            except BuzzError as exc:
                raise ProvisionError(channel, str(exc)) from exc

    def bind_owner(self, pubkey: str) -> None:
        import psycopg

        mark_bytes = bytes.fromhex(self.mark_pubkey)
        seat_bytes = bytes.fromhex(pubkey)
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET agent_owner_pubkey=%s WHERE community_id=%s AND pubkey=%s AND agent_owner_pubkey IS NULL",
                    (mark_bytes, self.community_id, seat_bytes),
                )
                if cur.rowcount != 1:
                    cur.execute(
                        "SELECT agent_owner_pubkey FROM users "
                        "WHERE community_id=%s AND pubkey=%s",
                        (self.community_id, seat_bytes),
                    )
                    row = cur.fetchone()
                    existing_owner = bytes(row[0]) if row is not None and row[0] is not None else b""
                    if existing_owner != mark_bytes:
                        raise BuzzError(
                            "owner bind changed no row (missing user or different owner)"
                        )

    def verify_profile(self, pubkey: str, expected_name: str) -> None:
        result = self._run(["users", "get", "--pubkey", pubkey])
        profiles: list[dict[str, Any]] = []
        if isinstance(result, list):
            profiles = [item for item in result if isinstance(item, dict)]
        elif isinstance(result, dict):
            profile = result.get("profile", result)
            if isinstance(profile, dict):
                profiles = [profile]
            elif isinstance(profile, list):
                profiles = [item for item in profile if isinstance(item, dict)]
            else:
                for key in ("profiles", "users", "items"):
                    items = result.get(key)
                    if isinstance(items, list):
                        profiles = [item for item in items if isinstance(item, dict)]
                        break
        for profile in profiles:
            visible_pubkey = str(profile.get("pubkey", pubkey)).lower()
            visible_name = profile.get("display_name", profile.get("name"))
            if visible_pubkey == pubkey.lower() and visible_name == expected_name:
                return
        raise BuzzError("seat profile name was not visible after publication")
