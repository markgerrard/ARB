from __future__ import annotations

import json
import pytest
import sys
from types import SimpleNamespace

import sys as _sys
import pytest as _pytest
if _sys.version_info >= (3, 14):
    # coincurve publishes no wheels for Python >= 3.14 and pyproject.toml scopes the
    # dependency to < 3.14. On those interpreters this module is a stated platform gap,
    # not a silent skip; on <= 3.13 the hard import below still fails loudly.
    _pytest.importorskip("coincurve", reason="coincurve has no wheel for Python >= 3.14 (see pyproject.toml)")

from arb_registration.buzz import BuzzError, BuzzOps
from arb_registration.crypto import public_identity


def base_env() -> dict[str, str]:
    return {
        "ARB_REGISTRAR_BUZZ_CLI": "/bin/true",
        "ARB_REGISTRAR_BUZZ_ADMIN": "/bin/true",
        "ARB_REGISTRAR_OPS_CHANNEL": "channel",
        "ARB_REGISTRAR_MARK_PUBKEY": "aa" * 32,
        "ARB_REGISTRAR_DATABASE_URL": "postgresql://example",
        "ARB_REGISTRAR_COMMUNITY_ID": "community",
        "BUZZ_PRIVATE_KEY": "01" * 32,
    }


def test_buzz_identity_env_file_is_merged_without_replacing_service_env(tmp_path):
    identity = tmp_path / "identity.env"
    identity.write_text(
        f"BUZZ_RELAY_URL=https://relay\nBUZZ_PRIVATE_KEY={'02' * 32}\n"
    )
    identity.chmod(0o600)
    ops = BuzzOps(base_env(), buzz_env_file=identity)
    assert ops.env["BUZZ_RELAY_URL"] == "https://relay"
    assert ops.env["BUZZ_PRIVATE_KEY"] == "02" * 32
    assert ops.ops_channel == "channel"


def test_registrar_identity_accepts_live_nsec_shape():
    env = base_env()
    env["BUZZ_PRIVATE_KEY"] = (
        "nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqps52s3re"
    )
    assert BuzzOps(env).registrar_pubkey == public_identity(f"{3:064x}")[0]


@pytest.mark.parametrize("pubkey", ["ae0954e4", "zz" * 32, "aa" * 31])
def test_mark_pubkey_requires_full_hex_identity(pubkey):
    env = base_env()
    env["ARB_REGISTRAR_MARK_PUBKEY"] = pubkey
    with pytest.raises(BuzzError, match="full 64-hex"):
        BuzzOps(env)


def test_admin_plain_text_success_is_accepted(monkeypatch):
    monkeypatch.setattr(
        "arb_registration.buzz.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="added abc as member\n", stderr=""
        ),
    )
    ops = BuzzOps(base_env())
    assert ops._run(
        ["add-member", "--pubkey", "abc"], admin=True, expect_json=False
    ) == "added abc as member"
    with pytest.raises(BuzzError, match="non-JSON"):
        ops._run(["messages", "send"])


def test_reaction_output_is_normalized_to_pubkey_sets(monkeypatch):
    output = '{"reactions":[{"emoji":"✅","count":1,"pubkeys":["AA"]}]}'
    monkeypatch.setattr(
        "arb_registration.buzz.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr=""
        ),
    )
    assert BuzzOps(base_env()).reactions("event") == {"✅": {"aa"}}


@pytest.mark.parametrize(
    "output,expected",
    [
        ('[{"pubkey":"AA","role":"bot"}]', True),
        ('{"members":[{"pubkey":"bb","role":"member"}]}', False),
    ],
)
def test_channel_membership_readback_accepts_list_or_wrapped_shape(
    monkeypatch, output, expected
):
    monkeypatch.setattr(
        "arb_registration.buzz.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr=""
        ),
    )
    assert BuzzOps(base_env()).channel_has_member("channel", "aa") is expected


@pytest.mark.parametrize(
    "output,expected",
    [
        ('[{"pubkey":"AA","role":"admin"}]', "admin"),
        ('{"members":[{"pubkey":"AA","role":"owner"}]}', "owner"),
        ('{"items":[{"pubkey":"bb","role":"member"}]}', None),
    ],
)
def test_channel_role_readback_accepts_live_shapes(monkeypatch, output, expected):
    monkeypatch.setattr(
        "arb_registration.buzz.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr=""
        ),
    )
    assert BuzzOps(base_env()).channel_member_role("channel", "aa") == expected


def test_registrar_standing_requires_admin_or_owner(monkeypatch):
    ops = BuzzOps(base_env())
    roles = {"admin-channel": "admin", "member-channel": "member", "missing": None}
    monkeypatch.setattr(
        ops, "channel_member_role", lambda channel, pubkey: roles[channel]
    )

    assert ops.registrar_standing_warnings(list(roles)) == [
        "channel `member-channel`: registrar role `member` is not admin",
        "channel `missing`: registrar is not a member",
    ]


def test_approval_message_surfaces_precheck_warning(monkeypatch):
    captured = {}
    ops = BuzzOps(base_env())

    def run(argv, *, stdin=None, **kwargs):
        captured["content"] = stdin
        return {"event_id": "event-1"}

    monkeypatch.setattr(ops, "_run", run)
    ops.post_approval(
        "request", "seat", "host", "bb" * 32, ["databases"],
        ["channel `databases`: registrar is not a member"],
    )

    assert "⚠ Registrar channel-admin precheck failed" in captured["content"]
    assert "channel `databases`: registrar is not a member" in captured["content"]
    assert "Repair registrar standing before approving" in captured["content"]


@pytest.mark.parametrize(
    "output",
    [
        '[{"pubkey":"AA","display_name":"new-seat"}]',
        '{"profile":{"pubkey":"aa","name":"new-seat"}}',
    ],
)
def test_profile_readback_accepts_live_list_and_legacy_wrapped_shapes(
    monkeypatch, output
):
    monkeypatch.setattr(
        "arb_registration.buzz.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr=""
        ),
    )
    BuzzOps(base_env()).verify_profile("aa", "new-seat")


def test_profile_readback_rejects_wrong_identity_or_name(monkeypatch):
    output = '[{"pubkey":"bb","display_name":"new-seat"}]'
    monkeypatch.setattr(
        "arb_registration.buzz.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr=""
        ),
    )
    with pytest.raises(BuzzError, match="not visible after publication"):
        BuzzOps(base_env()).verify_profile("aa", "new-seat")


def test_owner_bind_uses_bytes_for_postgres_bytea_pubkeys(monkeypatch):
    calls = []

    class Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement, params):
            calls.append((statement, params))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return Cursor()

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda database_url: Connection()),
    )
    BuzzOps(base_env()).bind_owner("bb" * 32)

    assert calls[0][1] == (
        bytes.fromhex("aa" * 32),
        "community",
        bytes.fromhex("bb" * 32),
    )
    assert isinstance(calls[0][1][0], bytes)
    assert isinstance(calls[0][1][2], bytes)


def test_owner_auth_is_default_off():
    ops = BuzzOps(base_env())
    assert ops.owner_auth_enabled is False
    assert ops.owner_auth_tag(public_identity(f"{4:064x}")[0]) is None


def test_configured_owner_key_builds_kind_zero_auth_tag(tmp_path):
    owner_secret = f"{3:064x}"
    owner_pubkey = public_identity(owner_secret)[0]
    owner_file = tmp_path / "owner.key"
    owner_file.write_text(owner_secret + "\n")
    owner_file.chmod(0o600)
    env = base_env()
    env["ARB_REGISTRAR_MARK_PUBKEY"] = owner_pubkey
    env["ARB_REGISTRAR_OWNER_KEY_FILE"] = str(owner_file)

    ops = BuzzOps(env)
    tag = json.loads(ops.owner_auth_tag(public_identity(f"{4:064x}")[0]))

    assert ops.owner_auth_enabled is True
    assert tag[:3] == ["auth", owner_pubkey, "kind=0"]
    assert len(tag[3]) == 128


def test_configured_owner_key_must_be_private(tmp_path):
    owner_file = tmp_path / "owner.key"
    owner_file.write_text(f"{3:064x}\n")
    owner_file.chmod(0o644)
    env = base_env()
    env["ARB_REGISTRAR_OWNER_KEY_FILE"] = str(owner_file)
    with pytest.raises(BuzzError, match="group or other"):
        BuzzOps(env)
