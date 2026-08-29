# ARB Files local MCP — provisioning runbook

This runbook wires a seat host to the shared ARB Files object-storage backend.

## Goal

Each seat can run `arb-files-local-mcp` over stdio with:

- credentials loaded from `envs/arb-files.env`
- object access confined by code to `agent-files/`
- host filesystem reads/writes confined to `ARB_FILES_LOCAL_ROOT` or `AGENT_WORKDIR`
- seat provenance recorded through `ARB_FILES_SEAT_ID`

## Step 1 — Create the local secret env file

Create `envs/arb-files.env` with mode `0600`. Do not commit it.

```sh
mkdir -p envs
chmod 700 envs
touch envs/arb-files.env
chmod 600 envs/arb-files.env
```

The file must export:

```sh
ARB_FILES_ENDPOINT=https://<region>.<your-object-storage-provider>.example
ARB_FILES_REGION=<region>
ARB_FILES_BUCKET=<bucket-name>
ARB_FILES_ACCESS_KEY=...
ARB_FILES_SECRET_KEY=...
ARB_FILES_PREFIX=agent-files/
```

Optional caps:

```sh
ARB_FILES_PRESIGN_TTL=900
ARB_FILES_INLINE_PUT_MAX=262144
ARB_FILES_INLINE_GET_MAX=5242880
ARB_FILES_INLINE_GET_IMAGE_MAX=3670016
ARB_FILES_LIST_MAX=1000
```

## Step 2 — Install the Python extra

```sh
pip install -e '.[arb-files]'
```

## Step 3 — Launch the MCP host with explicit local identity

```sh
set -a
. envs/arb-files.env
set +a
export ARB_FILES_SEAT_ID=<seat-id>
export ARB_FILES_LOCAL_ROOT=<absolute-workspace-path>
claude
```

If `ARB_FILES_LOCAL_ROOT` is not set, `AGENT_WORKDIR` is used. If neither is set, path-based
`file_get`/`file_put` operations fail closed.

## Step 4 — Register the stdio server

```sh
claude mcp add --transport stdio arb-files-local -- arb-files-local-mcp
```

Do not use `--env` for secrets; let the child inherit from the launch environment.

## Step 5 — Smoke check

Use the MCP client to:

1. `file_put` a small base64 payload under a disposable name.
2. `file_head` the same name and confirm `uploaded_by` matches `ARB_FILES_SEAT_ID`.
3. `file_get` it back.
4. `file_delete` it and confirm the returned recovery key is under `.trash/`.

## Operational Notes

The current object-storage key has bucket-level access. ARB Files code confines intended tool use to
`agent-files/`, but the key itself is not prefix-scoped. Treat `envs/arb-files.env` as a high-value
secret and keep the host trust boundary narrow.
