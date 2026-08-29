# Confined eval-seat image (decision panel: Option C — absence-based confinement).
# A reviewing seat runs HERE, jailed to a container whose read-surface is ONLY this image + the
# fixture bind-mount. The scenario / seed-manifest / answer-key is NEVER mounted, so it is ABSENT
# from the namespace (not denied by a policy — absent by construction). The container IS the boundary,
# so the engine runs with its own sandbox bypassed (panel nesting answer). Portable: plain Docker, so
# the same image builds on Linux/CI as on macOS+OrbStack.
FROM node:22-slim

# Toolchain the seat needs to investigate the fixture: git (diff/log), ripgrep (search). No scenario.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Pinned reviewing engine (matches the host seat: @openai/codex).
ARG CODEX_VERSION=0.139.0
RUN npm install -g @openai/codex@${CODEX_VERSION}

# Second reviewing engine: agy (Antigravity CLI, a Go binary) — installed for this OS/arch by the
# official installer (Linux build, since the container is linux). curl is needed for the installer
# and is a reasonable seat tool besides.
RUN apt-get update && apt-get install -y --no-install-recommends curl bash \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://antigravity.google/cli/install.sh | bash \
 && (cp /root/.local/bin/agy /usr/local/bin/agy 2>/dev/null || ln -sf /root/.local/bin/agy /usr/local/bin/agy) \
 && /usr/local/bin/agy --version

# Run-time bind mounts (set by confined-review.sh / the dispatcher), all the seat ever sees:
#   /fixture   the fixture repo, READ-ONLY        (the code under review — read freely)
#   /auth      engine credentials, READ-ONLY      (~/.codex — so codex can reach the API)
#   /work      a scratch tmpfs/volume, WRITABLE   (the only writable path)
# The scenario/answer-key is deliberately NOT in this list — absence is the boundary.
ENV CODEX_HOME=/auth
WORKDIR /fixture
