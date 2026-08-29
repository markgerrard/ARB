// Smoke test for @earendil-works/pi-coding-agent SDK.
//
// Goal: confirm the SDK works for the same shape of use the pi-rpc engine
// drives today (createAgentSession + subscribe + prompt + harvest assistant
// text) without building any of the JSON-RPC harness around it yet. Logs the
// raw typed event stream to stderr (one JSON object per line) so we can see
// what shapes pi's SDK actually emits, vs the NDJSON the pi-rpc engine
// currently parses out of `pi --mode rpc`.
//
// Usage:
//   OPENROUTER_API_KEY=... NODE_PATH=/home/<user>/.npm-global/lib/node_modules \
//     node tools/pi-sdk-host/smoke.mjs "What is 2+2? Reply in one short line."
//
// Stdout: a single JSON object with {ok, finalText, sessionId, eventTypes}.
// Stderr: one JSON event per line (event_type + a summarised payload).

import {
  AuthStorage,
  ModelRegistry,
  SessionManager,
  createAgentSession,
} from "@earendil-works/pi-coding-agent";
import { getModel } from "@earendil-works/pi-ai";

const PROVIDER = process.env.SMOKE_PROVIDER || "openrouter";
const MODEL_ID = process.env.SMOKE_MODEL_ID || "qwen/qwen3-coder-next";
const PROMPT = process.argv.slice(2).join(" ") || "Reply with exactly: PI_SDK_SMOKE_OK";
const TURN_TIMEOUT_MS = Number(process.env.SMOKE_TIMEOUT_MS || 60_000);

function logEvent(label, payload) {
  process.stderr.write(JSON.stringify({ t: label, ...payload }) + "\n");
}

function summariseEvent(event) {
  // Return a compact JSON-safe summary so the stderr stream stays readable.
  // Keep type + the small set of fields we expect to care about in the
  // future Python engine; drop large payloads (full message arrays, etc).
  const out = { type: event.type };
  if (event.type === "message_update") {
    const inner = event.assistantMessageEvent;
    out.inner = inner?.type;
    if (inner?.type === "text_delta" && typeof inner.delta === "string") {
      out.delta_len = inner.delta.length;
    }
    if (inner?.type === "thinking_delta" && typeof inner.delta === "string") {
      out.delta_len = inner.delta.length;
    }
  } else if (event.type === "tool_execution_start") {
    out.toolName = event.toolName;
    out.toolCallId = event.toolCallId;
  } else if (event.type === "tool_execution_end") {
    out.toolName = event.toolName;
    out.toolCallId = event.toolCallId;
    out.isError = event.isError === true;
  } else if (event.type === "turn_end" || event.type === "agent_end") {
    out.messageCount = Array.isArray(event.messages) ? event.messages.length : undefined;
  }
  return out;
}

async function main() {
  logEvent("smoke_start", { provider: PROVIDER, modelId: MODEL_ID, prompt: PROMPT });

  const model = getModel(PROVIDER, MODEL_ID);
  if (!model) {
    logEvent("model_resolve_failed", { provider: PROVIDER, modelId: MODEL_ID });
    process.stdout.write(JSON.stringify({ ok: false, error: "model_not_found" }) + "\n");
    process.exit(2);
  }
  logEvent("model_resolved", { provider: model.provider, id: model.id, name: model.name });

  const authStorage = AuthStorage.create();
  const modelRegistry = ModelRegistry.create(authStorage);

  const { session } = await createAgentSession({
    cwd: process.cwd(),
    model,
    thinkingLevel: "off",
    tools: ["read", "grep", "find", "ls"],
    sessionManager: SessionManager.inMemory(),
    authStorage,
    modelRegistry,
  });
  logEvent("session_created", { sessionId: session.sessionId });

  const finalTextChunks = [];
  const eventTypes = new Set();
  let agentEndSeen = false;

  const unsubscribe = session.subscribe((event) => {
    eventTypes.add(event.type);
    logEvent("ev", summariseEvent(event));
    if (
      event.type === "message_update" &&
      event.assistantMessageEvent?.type === "text_delta" &&
      typeof event.assistantMessageEvent.delta === "string"
    ) {
      finalTextChunks.push(event.assistantMessageEvent.delta);
    }
    if (event.type === "agent_end") {
      agentEndSeen = true;
    }
  });

  const watchdog = setTimeout(() => {
    logEvent("watchdog_fired", { timeoutMs: TURN_TIMEOUT_MS });
    session.abort().catch(() => {});
  }, TURN_TIMEOUT_MS);

  try {
    await session.prompt(PROMPT);
    clearTimeout(watchdog);
    unsubscribe();
    const finalText = finalTextChunks.join("");
    process.stdout.write(
      JSON.stringify({
        ok: true,
        agentEndSeen,
        sessionId: session.sessionId,
        finalText,
        finalTextLen: finalText.length,
        eventTypes: [...eventTypes].sort(),
      }) + "\n",
    );
  } catch (err) {
    clearTimeout(watchdog);
    unsubscribe();
    logEvent("prompt_failed", { error: String(err?.message || err) });
    process.stdout.write(
      JSON.stringify({ ok: false, error: String(err?.message || err) }) + "\n",
    );
    process.exit(3);
  } finally {
    session.dispose();
  }
}

main().catch((err) => {
  logEvent("fatal", { error: String(err?.stack || err?.message || err) });
  process.stdout.write(JSON.stringify({ ok: false, error: "fatal" }) + "\n");
  process.exit(1);
});
