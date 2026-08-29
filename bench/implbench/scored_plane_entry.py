#!/usr/bin/env python3
"""Frozen-cell entry point for the core scored control/tool daemons."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench"))

from agent_redis_bridge.scored_plane import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
