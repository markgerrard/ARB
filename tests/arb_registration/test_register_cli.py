from __future__ import annotations

import pytest
from types import SimpleNamespace

from arb_registration.register_cli import _buzz_command, _profile, main, read_token


def test_token_file_must_be_private(tmp_path):
    path = tmp_path / "token"
    path.write_text("one-time-secret\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        read_token(None, path)
    path.chmod(0o600)
    assert read_token(None, path) == "one-time-secret"


def test_inline_token_remains_supported():
    assert read_token(" token ", None) == "token"
    with pytest.raises(ValueError, match="empty"):
        read_token("", None)


def test_token_can_be_read_from_stdin(monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO("sealed-token\n"))
    assert read_token(None, None, True) == "sealed-token"


def test_buzz_cli_is_preflighted_before_one_time_token_stdin_is_read(
    monkeypatch, tmp_path
):
    class UnreadableStdin:
        def read(self):
            raise AssertionError("one-time token was read before local preflight")

    monkeypatch.delenv("ARB_SEAT_BUZZ_CLI", raising=False)
    monkeypatch.setattr("sys.stdin", UnreadableStdin())
    monkeypatch.setattr(
        "sys.argv",
        [
            "seat-register", "--name", "new-seat", "--token-stdin",
            "--key-file", str(tmp_path / "seat.key"),
            "--env-file", str(tmp_path / "bridge.env"),
        ],
    )

    with pytest.raises(SystemExit, match="ARB_SEAT_BUZZ_CLI is required"):
        main()


def test_buzz_cli_preflight_returns_shell_split_argv(monkeypatch):
    monkeypatch.setenv("ARB_SEAT_BUZZ_CLI", "/opt/buzz --quiet")
    assert _buzz_command() == ["/opt/buzz", "--quiet"]


def test_profile_publication_passes_provisioned_auth_tag(monkeypatch):
    captured = {}

    def run(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("arb_registration.register_cli.subprocess.run", run)
    _profile(
        "seat", "04" * 32, "https://relay", ["buzz"],
        '["auth","owner","kind=0","signature"]',
    )

    assert captured["env"]["BUZZ_AUTH_TAG"] == '["auth","owner","kind=0","signature"]'
    assert captured["argv"] == [
        "buzz", "users", "set-profile", "--name", "seat", "--about",
        "ARB registered seat",
    ]


def test_profile_publication_without_tag_preserves_default_absence(monkeypatch):
    captured = {}
    monkeypatch.delenv("BUZZ_AUTH_TAG", raising=False)
    monkeypatch.setattr(
        "arb_registration.register_cli.subprocess.run",
        lambda *args, **kwargs: (
            captured.update(kwargs) or SimpleNamespace(returncode=0, stderr="")
        ),
    )

    _profile("seat", "04" * 32, "https://relay", ["buzz"])

    assert "BUZZ_AUTH_TAG" not in captured["env"]
