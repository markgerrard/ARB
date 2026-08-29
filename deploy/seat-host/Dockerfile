# Seat-host image — the engine zoo + runtimes + the bridge baked in, so a host can
# host bridge seats with `docker pull` instead of the per-host clone/venv/npm ceremony.
#
# Per docs/superpowers/specs/2026-06-27-go-client-edge-and-seat-host-container-design.md (Track 2):
#   - the REPO is a bind mount at /repos (NOT baked) — worktree-per-dispatch makes it
#     mutable shared state, so it must stay a host mount;
#   - engine AUTH dirs are named volumes (device-code login once per host);
#   - run as the host UID (`--user $(id -u)`) so the mounted tree's ownership matches.
#
# Base: node:22-bookworm-slim gives Node 22 (codex/gemini/pi need it) AND Debian
# bookworm's Python 3.11 (satisfies the bridge's requires-python >=3.11).
FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AGENT_BRIDGE_PYTHON=/opt/venv/bin/python3

# Runtimes: Python + git (worktree-per-dispatch shells out to `git worktree add`) +
# build-essential (wheels that lack a cp311 build) + ca-certificates (TLS to managed bus).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git build-essential ca-certificates bash \
    && rm -rf /var/lib/apt/lists/*

# --- the bridge (Python, in an isolated venv referenced by AGENT_BRIDGE_PYTHON) ---
WORKDIR /app
RUN python3 -m venv /opt/venv
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-cache-dir .

# --- engine zoo (Node CLIs) ---
# codex (OpenAI app-server), gemini (Google ACP), pi-coding-agent (earendil SDK host).
# agy (Antigravity CLI) has no clean in-repo install one-liner — install it separately
# at first-run and persist its dotfiles to the gemini-auth volume (see README.seat-host.md).
# Pin engine versions for reproducible builds (floating-latest would drift the image).
RUN npm install -g \
        @openai/codex@0.142.3 \
        @google/gemini-cli@0.49.0 \
        @earendil-works/pi-coding-agent@0.79.0 \
    && npm cache clean --force

# pi-sdk-host harness: symlinks the global pi modules + installs its own host deps.
# Non-fatal if the symlink layout differs at build time — the harness self-heals on
# first run via install.sh, and a failed optional engine must not fail the image.
COPY tools/pi-sdk-host /app/tools/pi-sdk-host
RUN cd /app/tools/pi-sdk-host && (bash install.sh || echo "[seat-host] pi-sdk-host install deferred to first run")

# Pre-create the engine auth dirs WORLD-WRITABLE so a fresh named volume mounted here
# inherits the mountpoint's 0777 mode — otherwise Docker initialises the volume root:root
# 0755 and the non-root `--user $(id -u)` seat cannot write it (device-code login -> EACCES).
# (The host UID is arbitrary — 501 on mac, 1000 on Linux — so 0777 not a fixed chown.)
RUN mkdir -p /home/seat/.codex /home/seat/.gemini /home/seat/.pi/agent \
    && chmod -R 0777 /home/seat

# bridge launch wrapper (engine/workspace parse + env-driven run command)
COPY scripts/agent-redis-bridge-systemd /app/scripts/agent-redis-bridge-systemd
COPY deploy/seat-host/entrypoint.sh /usr/local/bin/seat-entrypoint
RUN chmod +x /usr/local/bin/seat-entrypoint /app/scripts/agent-redis-bridge-systemd

# Engine auth/config dirs live under $HOME — mount named volumes here (see compose).
# Default HOME is /root; --user $(id -u) at runtime overrides it (entrypoint re-resolves).
ENTRYPOINT ["/usr/local/bin/seat-entrypoint"]
