import path from "node:path";

const DEFAULT_TIMEOUT_S = 30;

function timeoutSeconds() {
  const configured = Number(process.env.BRIDGE_PI_SDK_TOOL_TIMEOUT_S);
  return Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_TIMEOUT_S;
}

function isOutsideWorkspace(cwd, resolved) {
  const relative = path.relative(cwd, resolved);
  return relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative);
}

export function makeGuardedTool(innerDef, { cwd, timeoutS, label }) {
  const workspace = path.resolve(cwd);
  return {
    ...innerDef,
    __guarded: true,
    __timeoutS: timeoutS,
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const resolved = path.resolve(workspace, params?.path ?? ".");
      // This clamps lexical paths only; symlink escapes are outside this mistake-prevention threat model.
      if (isOutsideWorkspace(workspace, resolved)) {
        throw new Error(`path outside workspace: ${resolved}`);
      }

      const controller = new AbortController();
      const abort = () => controller.abort();
      if (signal?.aborted) controller.abort();
      else signal?.addEventListener("abort", abort, { once: true });
      let timer;
      try {
        const inner = innerDef.execute(toolCallId, params, controller.signal, onUpdate, ctx);
        const timedOut = new Promise((_, reject) => {
          timer = setTimeout(() => {
            controller.abort();
            reject(new Error(`${label} timed out after ${timeoutS}s`));
          }, timeoutS * 1000);
        });
        return await Promise.race([inner, timedOut]);
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
      }
    },
  };
}

export function makeGuardedFsTools({ cwd, createFind, createGrep }) {
  const timeoutS = timeoutSeconds();
  return [
    makeGuardedTool(createFind(cwd), { cwd, timeoutS, label: "find" }),
    makeGuardedTool(createGrep(cwd), { cwd, timeoutS, label: "grep" }),
  ];
}
