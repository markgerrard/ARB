import pytest

from arb_files.config import Settings, load_settings


BASE = {
    "ARB_FILES_ENDPOINT": "https://spaces.example.com",
    "ARB_FILES_REGION": "lon1",
    "ARB_FILES_BUCKET": "arb-files",
    "ARB_FILES_ACCESS_KEY": "AK",
    "ARB_FILES_SECRET_KEY": "SK",
}


def test_loads_required_and_defaults():
    s = load_settings(BASE)
    assert isinstance(s, Settings)
    assert s.bucket == "arb-files"
    assert s.prefix == "agent-files/"
    assert s.presign_ttl == 900
    assert s.inline_put_max == 262144
    assert s.inline_get_image_max == 3_670_016
    assert s.read_rate_per_min == 60
    assert s.write_rate_per_min == 30


def test_missing_required_fails_closed():
    with pytest.raises(ValueError):
        load_settings({"ARB_FILES_BUCKET": "arb-files"})


def test_prefix_normalised_with_trailing_slash():
    s = load_settings({**BASE, "ARB_FILES_PREFIX": "agent-files"})
    assert s.prefix == "agent-files/"


def test_env_overrides_caps_and_rates():
    s = load_settings(
        {
            **BASE,
            "ARB_FILES_PRESIGN_TTL": "60",
            "ARB_FILES_LIST_MAX": "50",
            "ARB_FILES_READ_RATE_PER_MIN": "7",
            "ARB_FILES_WRITE_RATE_PER_MIN": "3",
        }
    )
    assert s.presign_ttl == 60
    assert s.list_max == 50
    assert s.read_rate_per_min == 7
    assert s.write_rate_per_min == 3


def test_local_root_defaults_to_agent_workdir():
    s = load_settings({**BASE, "AGENT_WORKDIR": "/tmp/work"})
    assert s.local_root == "/tmp/work"


def test_explicit_local_root_wins():
    s = load_settings(
        {**BASE, "AGENT_WORKDIR": "/tmp/work", "ARB_FILES_LOCAL_ROOT": "/tmp/files"}
    )
    assert s.local_root == "/tmp/files"
