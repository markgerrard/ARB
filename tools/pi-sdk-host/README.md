# pi-sdk-host

Long-lived JSON-RPC stdio harness driving `@earendil-works/pi-coding-agent` via its
SDK for AgentRedisBridge's pi-sdk engine.

`@earendil-works/pi-coding-agent` (and its `@earendil-works/pi-ai` dependency) is not
published on any public package registry — it must be installed globally from
wherever your organization distributes it before running `./install.sh`, which
symlinks the global install into this package's `node_modules` (see the comment at
the top of `install.sh` for why a local `npm install` copy isn't used instead).
