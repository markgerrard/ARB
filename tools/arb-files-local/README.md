# ARB Files Local MCP

`arb-files-local-mcp` runs the read-write local ARB Files MCP server over stdio. It exposes:

- `file_list`
- `file_head`
- `file_get`
- `file_put`
- `file_delete`
- `file_get_url`
- `file_put_url`

It uses the ARB Files Spaces credentials from the process environment and confines all object names to
the configured `ARB_FILES_PREFIX` (`agent-files/` by default). Deletes and force overwrites are
recoverable through the `.trash/` path and emit audit events when the store is configured with an
audit sink.

## Install

```sh
pip install -e '.[arb-files]'
```

## Required Environment

Load the secret environment from `envs/arb-files.env` before launching the MCP host. Do not paste the
secret values into persistent client config.

Required:

- `ARB_FILES_ENDPOINT`
- `ARB_FILES_REGION`
- `ARB_FILES_BUCKET`
- `ARB_FILES_ACCESS_KEY`
- `ARB_FILES_SECRET_KEY`

Recommended:

- `ARB_FILES_SEAT_ID` identifies this local writer in object metadata.
- `ARB_FILES_LOCAL_ROOT` confines `file_get(..., to_path=...)` and `file_put(..., from_path=...)`.
- `AGENT_WORKDIR` is used as the default local root when `ARB_FILES_LOCAL_ROOT` is unset.

Path-based local file operations fail closed when neither `ARB_FILES_LOCAL_ROOT` nor `AGENT_WORKDIR`
is set.

## Register With Claude

Launch `claude` from a shell that already has the ARB Files environment loaded:

```sh
set -a
. envs/arb-files.env
set +a
export ARB_FILES_SEAT_ID=codex-bridge-dev
export ARB_FILES_LOCAL_ROOT="$PWD"
claude
```

From that session, register the stdio MCP server:

```sh
claude mcp add --transport stdio arb-files-local -- arb-files-local-mcp
```

The MCP child inherits `ARB_FILES_*`, `PATH`, and `PYTHONPATH` from the `claude` process.

## Read-Write Notes

This local MCP can write and delete objects under `agent-files/`. That is deliberate: seats need an
automatic binary handoff path. Keep the Spaces key private, keep the local root narrow, and prefer
`file_head` before destructive actions.

`file_put_url` returns a presigned URL plus required headers. A caller performing the PUT must send the
returned headers exactly, including conditional and provenance headers.
