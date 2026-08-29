# PI-1 — scope + timeout guard on the pi host's builtin find/grep

**Spec:** `docs/BACKLOG.md` § PI-1. The incident: a diligent GLM reviewer issued
`find` with `path: '/'`; the crawl blocked in the OS for 40+ min and the turn died only
at the 3600s bridge timeout. Structural fix per
[[structural-not-configurational-containment]]: the host must be UNABLE to crawl
outside the workspace, and a wedged crawl must become an in-band tool error the model
can react to — never a hang.

**World (verified 2026-07-11 against dev `a9af21a`, mapped from the live tree):**
- find/grep are **pi-coding-agent builtins**, not our code. host.mjs passes tool NAME
  strings only; the SDK constructs tools via `createAllToolDefinitions(this._cwd, ...)`.
- The SDK exports `createFindToolDefinition(cwd, {operations})` and
  `createGrepToolDefinition(cwd, {operations})` from
  `node_modules/@earendil-works/pi-coding-agent/dist/core/tools/{find,grep}.js`.
- Chosen seam (D-B2, lowest-friction, our-code): in `buildSessionToolArgs`
  (`tools/pi-sdk-host/host.mjs:275`) build OUR guarded find/grep definitions and push
  them into `customTools`; add `excludeTools: ["find","grep"]` to `baseSessionArgs`
  (`host.mjs:389-397`) so the builtins are displaced (same-name customTools override,
  `agent-session.js:1877-1881`; `excludeTools` is a first-class
  `createAgentSession` option applied at `sdk.js:133`).
- Session cwd: `host.mjs:329` (validated existing dir at `:330-338`), flows to
  `baseSessionArgs.cwd` (`:390`); rotation reuses `state.thread.toolArgs` + cwd
  (`:421`, `:454-470`) — guarded tools must survive rotation (they will if produced in
  `buildSessionToolArgs`, but a rotate test must prove it).
- Tool `execute` signature carries an AbortSignal:
  `execute(toolCallId, params, signal, onUpdate, ctx)` (SDK `types.d.ts:361`).
- Test harness: `node:test` + DI fakes (`createHost(deps)` accepts
  `{createSession, startBridge}`, `host.mjs:813-819`; exemplar
  `host.rotate.test.mjs:22-60`). Run with `npm test` in `tools/pi-sdk-host/`.
- No AbortController/timeout precedent in host.mjs today; the only local idiom is the
  race-against-setTimeout at `host.mjs:810`.

**Behaviour contract:**
1. **Scope clamp (unconditional):** any find/grep call whose `path` (after
   `path.resolve` against session cwd) falls outside the cwd subtree returns an in-band
   tool ERROR `path outside workspace: <resolved>` — the inner tool is never invoked.
   Prefix check must be separator-safe (`/repo` vs `/repo-evil`). Symlink escape is an
   accepted residual (threat model is mistakes, not malice — note it in a comment).
2. **Wall-clock timeout:** inner execute raced against
   `BRIDGE_PI_SDK_TOOL_TIMEOUT_S` (env, default 30, read via `process.env` at guard
   construction) — on expiry, abort the inner call via an `AbortController` chained to
   the passed-through signal and return in-band tool error
   `find timed out after <N>s` / `grep timed out after <N>s`.
3. **Error shape:** match the SDK's own tool-error convention — inspect how
   `find.js`/`grep.js` return error results (isError-style result vs throw) and mirror
   it EXACTLY so the model sees the same shape as any other tool error. Name the chosen
   shape in the reply.
4. Calls with no `path` arg default to cwd (today's behaviour) and pass through.

```python fixture-smoke
# World-claims of this plan, executed against the actual host tree via node.
# Proves: SDK exports the two factory functions; a created definition exposes an
# execute(...) callable and a name; excludeTools is a real CreateAgentSessionOptions key.
import subprocess, json, pathlib
host_dir = (TREE if "TREE" in dir() else pathlib.Path(".")) / "tools/pi-sdk-host"
node_code = r"""
const m = await import("@earendil-works/pi-coding-agent");
const finddef = m.createFindToolDefinition ? m.createFindToolDefinition(process.cwd(), {}) : null;
const grepdef = m.createGrepToolDefinition ? m.createGrepToolDefinition(process.cwd(), {}) : null;
const fs = await import("node:fs");
const sdkTxt = fs.readFileSync(
  "node_modules/@earendil-works/pi-coding-agent/dist/core/sdk.d.ts", "utf8");
console.log(JSON.stringify({
  hasFindFactory: !!m.createFindToolDefinition,
  hasGrepFactory: !!m.createGrepToolDefinition,
  findShape: finddef ? {name: finddef.name, exec: typeof finddef.execute} : null,
  grepShape: grepdef ? {name: grepdef.name, exec: typeof grepdef.execute} : null,
  excludeTools: sdkTxt.includes("excludeTools"),
}));
"""
out = subprocess.run(["node", "--input-type=module", "-e", node_code],
                     cwd=str(host_dir), capture_output=True, text=True, timeout=60)
assert out.returncode == 0, f"node probe failed:\n{out.stderr}"
data = json.loads(out.stdout.strip().splitlines()[-1])
# If the factories are NOT top-level exports, the implementor imports them by dist path
# instead — that is a DECLARED deviation, not a blocker; but at least one route must exist:
if not (data["hasFindFactory"] and data["hasGrepFactory"]):
    probe = subprocess.run(["node", "--input-type=module", "-e",
        'const f=await import("@earendil-works/pi-coding-agent/dist/core/tools/find.js");'
        'const g=await import("@earendil-works/pi-coding-agent/dist/core/tools/grep.js");'
        'console.log(JSON.stringify({f: !!f.createFindToolDefinition, g: !!g.createGrepToolDefinition}))'],
        cwd=str(host_dir), capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0 and '"f":true' in probe.stdout and '"g":true' in probe.stdout, \
        f"neither import route exposes the factories: {out.stdout} / {probe.stdout} {probe.stderr}"
assert data["excludeTools"], "excludeTools not found in SDK entry — displacement claim is stale"
print("fixture-smoke OK: factories + excludeTools confirmed on the real SDK")
```

---

## Task 1 — guard module (`tools/pi-sdk-host/tool-guard.mjs`) + unit tests

**RED:** `tools/pi-sdk-host/tool-guard.test.mjs` (`node:test`), driving
`makeGuardedTool(innerDef, {cwd, timeoutS, label})` with FAKE inner defs
(`{name, execute: async (...)=>...}`):
- outside path (`{path: "/"}`, `{path: "../x"}`, `{path: cwd + "-evil"}`) → error
  result containing `path outside workspace:`; inner execute NOT called (spy count 0).
- inside path (absolute under cwd, relative, missing) → inner called, result passed
  through verbatim.
- never-resolving inner + `timeoutS: 0.05` → error result containing
  `timed out after`, and the AbortSignal handed to inner has `aborted === true`.
- inner that itself errors → error passes through unchanged (guard adds nothing).
- `makeGuardedFsTools({cwd, createFind, createGrep})` returns both, names preserved,
  timeout read from `BRIDGE_PI_SDK_TOOL_TIMEOUT_S` (test via env set/unset around call).

**GREEN:** implement `tool-guard.mjs`. Commit
`feat(pi-sdk-host): PI-1 T1 — scope+timeout tool guard module`.

## Task 2 — host.mjs wiring + rotation survival

**RED:** extend the DI-harness tests (new file `host.guard.test.mjs`, patterned on
`host.rotate.test.mjs`): fake `createSession` captures args;
- `threadStart` with `tools: ["find","grep","read"]` → captured `excludeTools`
  includes `find`+`grep`; `customTools` contains guarded find+grep (identify via a
  marker property, e.g. `def.__guarded === true`); `read` untouched in `tools`.
- `threadStart` with `tools` not including find/grep → NO excludeTools entry for
  them, no guarded customTools (don't displace what wasn't requested — the SDK default
  active set omits find/grep; displacing unconditionally would ADD tools the seat
  didn't have).
- rotation (`thread/rotate` or the rotate handler in host.rotate.test.mjs style) →
  second `createSession` call ALSO carries the guarded customTools + excludeTools.

**GREEN:** wire `buildSessionToolArgs`/`startSessionWithBridge` (real
`createFindToolDefinition`/`createGrepToolDefinition` imported at module top with the
route the smoke confirmed; DI seam `deps.createFindTool`/`deps.createGrepTool` optional
overrides for tests). Full host suite: `npm test` in `tools/pi-sdk-host/` must be
green. Commit `feat(pi-sdk-host): PI-1 T2 — displace builtin find/grep with guarded defs`.

## Task 3 — docs

CHANGELOG (what + why: the 40-min root-crawl wedge, in-band self-correction);
BACKLOG § PI-1 → SHIPPED + SHA. Commit `docs(pi1): find/grep guard shipped`.

**Evidence contract (reply MUST contain):** per task — SHA, `npm test` pass/fail counts,
the SDK import route used, the tool-error shape chosen (with the find.js cite), and any
deviation from this plan named explicitly.
