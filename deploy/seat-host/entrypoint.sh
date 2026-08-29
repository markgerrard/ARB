#!/usr/bin/env bash
# Seat-host entrypoint — env-driven bridge launch inside the container.
#
# The bridge is configured entirely by environment + the mounted env file, exactly
# like the launchd/systemd seat model, so this just resolves the container-specific
# bits (HOME for engine auth dirs, the baked venv, the /repos workdir convention)
# and hands off to the standard launch wrapper.
set -euo pipefail

# Engine auth dirs (codex ~/.codex, gemini/agy ~/.gemini, pi ~/.pi) live under $HOME.
# With `--user $(id -u)` the runtime UID is often absent from /etc/passwd, leaving
# HOME unset/"/" (unwritable). Pin it to a fixed, volume-mountable path.
export HOME="${SEAT_HOME:-/home/seat}"
mkdir -p "$HOME" 2>/dev/null || true

export AGENT_BRIDGE_PYTHON="${AGENT_BRIDGE_PYTHON:-/opt/venv/bin/python3}"

# AGENT_WORKDIR MUST be a container path under the /repos bind mount. A host-absolute
# path (e.g. /Users/<user>/<workspace>) does not exist in the container and worktree
# creation / file reads fail silently as "not found" — the design's one real footgun.
export AGENT_WORKDIR="${AGENT_WORKDIR:-/repos/${AGENT_PROJECT:-<workspace>}}"
case "$AGENT_WORKDIR" in
  /repos/*) : ;;
  *) echo "[seat-host] WARNING: AGENT_WORKDIR=$AGENT_WORKDIR is not under /repos — host-absolute paths do not exist in the container and fail silently as 'not found'. Set AGENT_WORKDIR=/repos/<project>." >&2 ;;
esac

echo "[seat-host] python=$AGENT_BRIDGE_PYTHON workdir=$AGENT_WORKDIR home=$HOME instance=${SEAT_INSTANCE:-<unset>}" >&2

# Allow `docker run ... <cmd>` to override (e.g. a shell, or `python -m agent_redis_bridge --help`).
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

: "${SEAT_INSTANCE:?set SEAT_INSTANCE (e.g. codex-dev, agy-print-dev, pi-sdk-dev-glm)}"
exec /app/scripts/agent-redis-bridge-systemd "$SEAT_INSTANCE"
