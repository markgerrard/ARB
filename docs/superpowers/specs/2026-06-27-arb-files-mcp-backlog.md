# ARB Files MCP — backlog (companion to ARB Memory)

> Status: **backlog, not started** (logged 2026-06-27). Belongs with ARB. Also in ARB Memory:
> `art-1ce1e5a1975087e4`.

## Idea
A simple companion MCP server, **ARB Files**, alongside ARB Memory — lets coding agents **transfer
files between each other** (upload / download / list / delete), using the **same auth model as ARB
Memory**: the OAuth door for external connectors + a local stdio reader for the fleet
(orchestrator + seats), mirroring the ARB Memory local-read MCP split.

## Why complementary
ARB Memory carries durable *notes / artefacts* (text, searchable, pgvector). ARB Files carries
*opaque files* (binaries, larger blobs) that agents hand off — covering the "share knowledge" vs
"share a file" axes. Same auth, same fleet-distribution story.

## Backend
DigitalOcean **Spaces** (S3-compatible object storage):
- Region `lon1`, endpoint `https://<region>.digitaloceanspaces.com`
- Bucket `arb-files` (URL `https://<bucket>.<region>.digitaloceanspaces.com`)
- S3 access-key/secret stored **disk-only** in `envs/arb-files.env` (gitignored, `0600`, mac-mini) —
  NOT in repo, chat, or memory.

## Likely shape (design when picked up)
- MCP tools: `file_put` / `file_get` / `file_list` / `file_delete`, ideally **presigned-URL** variants
  so large transfers don't stream through the MCP process.
- Auth: reuse ARB Memory's OAuth (external) + local stdio reader (fleet), same split as the ARB Memory
  local-read MCP.
- Scoping: per-agent prefixes vs a shared pool — decide at design time.
- Mirror ARB Memory's **structural-containment** posture: the exposed door must not hold broad Spaces
  delete/admin rights by construction; scope the S3 policy to the bucket + the needed verbs.

## Status / next step
Not started. When picked up: design pass (auth + tool surface + S3 policy scoping) → spec → plan →
build, following the same pipeline as the ARB Memory local-read MCP.
