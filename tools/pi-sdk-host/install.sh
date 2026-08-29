#!/usr/bin/env bash
# Symlink the globally-installed @earendil-works/pi-coding-agent into the
# local node_modules so host.mjs's ESM `import` resolves. We deliberately
# share the global install rather than `npm install`-ing a local copy:
# the pi-coding-agent dep tree is ~300 MB and the host always uses the
# same pi version as the pi-rpc engine on this box. Re-run after upgrading
# the global pi via `npm install -g @earendil-works/pi-coding-agent@X.Y.Z`.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GLOBAL_ROOT="$(npm root -g)"
PI_PKG="${GLOBAL_ROOT}/@earendil-works/pi-coding-agent"
if [[ ! -d "${PI_PKG}" ]]; then
  echo "error: @earendil-works/pi-coding-agent not installed globally." >&2
  echo "       run: npm install -g @earendil-works/pi-coding-agent" >&2
  exit 1
fi

# Locate pi-ai. Prefer pi-coding-agent's nested node_modules (npm 7+
# default for top-level installs); fall back to the global root in case
# the package manager flattened the tree (agy P2.4).
PI_AI_PKG="${PI_PKG}/node_modules/@earendil-works/pi-ai"
if [[ ! -d "${PI_AI_PKG}" ]]; then
  PI_AI_PKG="${GLOBAL_ROOT}/@earendil-works/pi-ai"
fi
if [[ ! -d "${PI_AI_PKG}" ]]; then
  echo "error: @earendil-works/pi-ai not found in nested or top-level global node_modules." >&2
  echo "       tried: ${PI_PKG}/node_modules/@earendil-works/pi-ai" >&2
  echo "       tried: ${GLOBAL_ROOT}/@earendil-works/pi-ai" >&2
  exit 1
fi

# Install direct deps before creating @earendil-works symlinks: npm prunes
# undeclared node_modules entries, so the symlinks must be recreated afterward.
( cd "${HERE}" && npm install --no-audit --no-fund --omit=dev )

mkdir -p "${HERE}/node_modules/@earendil-works"
ln -sfn "${PI_PKG}"    "${HERE}/node_modules/@earendil-works/pi-coding-agent"
ln -sfn "${PI_AI_PKG}" "${HERE}/node_modules/@earendil-works/pi-ai"

PI_VERSION="$(node -p "require('${PI_PKG}/package.json').version" 2>/dev/null || echo unknown)"
echo "pi-sdk-host: linked @earendil-works/pi-coding-agent@${PI_VERSION} from ${PI_PKG}"
