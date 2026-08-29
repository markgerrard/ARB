# Claude-tail sidecar: read-only Claude transcript tailer that publishes bridge
# visibility events. Offsets live in Redis, so the image needs no writable host mount.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Default to the daemon, but allow `docker run ... python -c ...` smoke checks or shells.
RUN printf '%s\n' \
    '#!/usr/bin/env sh' \
    'set -eu' \
    'if [ "$#" -gt 0 ]; then' \
    '  exec "$@"' \
    'fi' \
    'exec python -m agent_redis_bridge.claude_tail' \
    > /usr/local/bin/claude-tail-entrypoint \
    && chmod +x /usr/local/bin/claude-tail-entrypoint

ENTRYPOINT ["/usr/local/bin/claude-tail-entrypoint"]
