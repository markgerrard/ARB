from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_retention_script_pins_both_windows_and_uses_retention_dsn():
    text = (ROOT / "deploy" / "retention-purge.sh").read_text()
    assert 'RETENTION_DSN="${ARB_RETENTION_DSN:?' in text
    assert "ARB_EVAL_RETENTION_DAYS=56" in text
    assert "ARB_TRANSCRIPT_RETENTION_DAYS=56" in text
    assert "docker compose" in text
    assert "eval-purge" in text and "transcript-purge" in text


def test_retention_timer_and_units_are_nightly():
    timer = (ROOT / "deploy" / "systemd" / "arb-retention.timer").read_text()
    service = (ROOT / "deploy" / "systemd" / "arb-retention.service").read_text()
    assert "OnCalendar=*-*-* 02:15:00" in timer
    assert "arb-retention.service" in timer
    assert "retention-purge.sh" in service


def test_compose_service_set_is_unchanged():
    compose = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {
        "memory", "audit", "audit-close-consumer", "eval", "transcript",
        "mcp", "cloudflared", "writer", "visibility", "arb-tools-static",
        "hint-reads",
    }
