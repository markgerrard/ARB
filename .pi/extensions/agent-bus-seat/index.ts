import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import * as path from "node:path";
import { promisify } from "node:util";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const execFileAsync = promisify(execFile);

type Env = Record<string, string>;
type Envelope = {
	id?: string;
	from?: string;
	to?: string;
	kind?: string;
	branch?: string;
	sent_at?: string;
	ts?: string;
	payload?: unknown;
	body?: string;
	in_reply_to?: string;
};

const DEFAULT_INBOX_DIR = "/tmp/agent-bridge-inbox";
const DEFAULT_AGENT_ID = "pi-project-b-a";
const DEFAULT_MODEL_ID = "gpt-5.5";

function describeModel(model: unknown): string {
	const m = model as { provider?: string; id?: string; name?: string } | undefined;
	if (m?.provider && m?.id) return `${m.provider}/${m.id}`;
	return m?.id || m?.name || process.env.PI_MODEL || DEFAULT_MODEL_ID;
}

function parseEnvFile(text: string): Env {
	const out: Env = {};
	for (const rawLine of text.split(/\r?\n/)) {
		const line = rawLine.trim();
		if (!line || line.startsWith("#")) continue;
		const idx = line.indexOf("=");
		if (idx < 0) continue;
		const key = line.slice(0, idx).trim();
		let value = line.slice(idx + 1).trim();
		if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
			value = value.slice(1, -1);
		}
		out[key] = value;
	}
	return out;
}

async function loadConfig(cwd: string) {
	const envPath = process.env.AGENT_ENV_FILE || path.join(cwd, ".env");
	let fileEnv: Env = {};
	try {
		fileEnv = parseEnvFile(await fs.readFile(envPath, "utf8"));
	} catch {
		// Fall back to the known bridge env on this host. Project .env is not
		// required for this extension; the bus creds are usually in the bridge clone.
		try {
			fileEnv = parseEnvFile(await fs.readFile("/home/<user>/AgentRedisBridge/.env", "utf8"));
		} catch {
			fileEnv = {};
		}
	}
	const merged = { ...fileEnv, ...process.env } as Env;
	const agentId = process.env.AGENT_ID || process.env.PI_AGENT_ID || DEFAULT_AGENT_ID;
	const prefix = merged.AGENT_REDIS_PREFIX || "agent_scratch:";
	return {
		agentId,
		envPath,
		prefix,
		host: merged.AGENT_REDIS_HOST || "127.0.0.1",
		port: merged.AGENT_REDIS_PORT || "6379",
		db: merged.AGENT_REDIS_DB || "0",
		user: merged.AGENT_REDIS_USER || "default",
		password: merged.AGENT_REDIS_PASSWORD || "",
		tls: merged.AGENT_REDIS_TLS === "1" || merged.AGENT_REDIS_TLS === "true",
		inboxDir: merged.AGENT_BRIDGE_INBOX_DIR || DEFAULT_INBOX_DIR,
	};
}

type Config = Awaited<ReturnType<typeof loadConfig>>;

function inboxKey(config: Config) {
	return `${config.prefix}agent:${config.agentId}:inbox`;
}
function processingKey(config: Config) {
	return `${inboxKey(config)}:processing`;
}
function statusKey(config: Config) {
	return `${config.prefix}agent:${config.agentId}:status`;
}
function peerInboxKey(config: Config, peerId: string) {
	return `${config.prefix}agent:${peerId}:inbox`;
}

async function redis(config: Config, args: string[], signal?: AbortSignal): Promise<string> {
	const cliArgs = ["-h", config.host, "-p", String(config.port), "-n", String(config.db), "--no-auth-warning"];
	if (config.tls) cliArgs.push("--tls");
	if (config.user) cliArgs.push("--user", config.user);
	cliArgs.push(...args);
	const env = { ...process.env };
	if (config.password) env.REDISCLI_AUTH = config.password;
	const { stdout } = await execFileAsync("redis-cli", cliArgs, { env, signal, timeout: 35_000, maxBuffer: 1024 * 1024 });
	return stdout;
}

function parseEnvelope(raw: string): Envelope | undefined {
	try {
		const parsed = JSON.parse(raw) as Envelope;
		if (parsed && typeof parsed === "object") return parsed;
	} catch {
		return undefined;
	}
	return undefined;
}

async function writeEnvelope(config: Config, id: string, raw: string): Promise<string> {
	await fs.mkdir(config.inboxDir, { recursive: true });
	const p = path.join(config.inboxDir, `${id}.json`);
	await fs.writeFile(p, raw, "utf8");
	return p;
}

function promptForEnvelope(config: Config, env: Envelope, filePath: string, recovered: boolean): string {
	const id = env.id || "?";
	const from = env.from || "?";
	const kind = env.kind || "?";
	const taskText = typeof env.body === "string"
		? env.body
		: typeof (env.payload as any)?.task === "string"
			? (env.payload as any).task
			: "(no body/payload.task string; read the JSON file)";
	return [
		`${recovered ? "RECOVERED" : "NEW"} BUS MESSAGE for ${config.agentId}: id=${id} from=${from} kind=${kind}`,
		`Full envelope: ${filePath}`,
		"You are the team-member seat. Read the envelope if needed, act only if to == your id, then reply with evidence.",
		"When done, call agent_bus_reply with request_id, ok, result, and concise evidence. Do not leave the request unreplied.",
		"Task/body:",
		taskText,
	].join("\n");
}

function safeSendUserMessage(pi: ExtensionAPI, ctx: ExtensionContext, message: string) {
	if (ctx.isIdle()) {
		pi.sendUserMessage(message);
	} else {
		pi.sendUserMessage(message, { deliverAs: "followUp" });
		ctx.ui.notify("Bus message queued as follow-up", "info");
	}
}

export default function agentBusSeat(pi: ExtensionAPI) {
	let started = false;
	let abortController: AbortController | undefined;
	let config: Config | undefined;
	let activeCtx: ExtensionContext | undefined;
	let modelId = DEFAULT_MODEL_ID;
	const rawById = new Map<string, string>();

	async function ack(requestId: string): Promise<number> {
		if (!config) return 0;
		let raw = rawById.get(requestId);
		if (!raw) {
			try {
				raw = await fs.readFile(path.join(config.inboxDir, `${requestId}.json`), "utf8");
			} catch {
				return 0;
			}
		}
		const out = await redis(config, ["LREM", processingKey(config), "1", raw]);
		const n = Number.parseInt(out.trim() || "0", 10);
		if (n > 0) rawById.delete(requestId);
		return Number.isFinite(n) ? n : 0;
	}

	async function heartbeatLoop(signal: AbortSignal) {
		while (!signal.aborted) {
			try {
				if (config) await redis(config, ["SET", statusKey(config), `alive:${process.pid}`, "EX", "60"], signal);
			} catch (err) {
				activeCtx?.ui.notify(`agent-bus-seat heartbeat failed: ${String(err).slice(0, 120)}`, "warning");
			}
			await new Promise((resolve) => setTimeout(resolve, 25_000));
		}
	}

	async function handleRaw(raw: string, recovered: boolean) {
		if (!config || !activeCtx) return;
		const env = parseEnvelope(raw);
		if (!env?.id) {
			await redis(config, ["LREM", processingKey(config), "1", raw]).catch(() => undefined);
			activeCtx.ui.notify("agent-bus-seat dropped malformed envelope (no id)", "warning");
			return;
		}
		if (env.to !== config.agentId) {
			// Wrong recipient should never happen for our own inbox; ack it so it
			// doesn't wedge processing forever, but do not wake the model to act.
			await redis(config, ["LREM", processingKey(config), "1", raw]).catch(() => undefined);
			activeCtx.ui.notify(`agent-bus-seat ignored wrong-recipient id=${env.id} to=${env.to}`, "warning");
			return;
		}
		rawById.set(env.id, raw);
		const filePath = await writeEnvelope(config, env.id, raw);
		activeCtx.ui.notify(`${recovered ? "Recovered" : "Received"} bus ${env.kind || "?"}: ${env.id} from ${env.from || "?"}`, "info");

		if (env.kind === "request") {
			safeSendUserMessage(pi, activeCtx, promptForEnvelope(config, env, filePath, recovered));
		} else {
			// Non-request messages are informational. Persist them, notify, and ack.
			await ack(env.id);
		}
	}

	async function redrainProcessing(signal: AbortSignal) {
		if (!config || !activeCtx) return;
		const out = await redis(config, ["LRANGE", processingKey(config), "0", "-1"], signal);
		for (const raw of out.split(/\r?\n/).filter(Boolean)) {
			await handleRaw(raw, true);
		}
	}

	async function consumeLoop(signal: AbortSignal) {
		if (!config || !activeCtx) return;
		await redrainProcessing(signal);
		while (!signal.aborted) {
			try {
				const out = await redis(config, ["BLMOVE", inboxKey(config), processingKey(config), "RIGHT", "LEFT", "30"], signal);
				const raw = out.trim();
				if (!raw || raw === "(nil)") continue;
				await handleRaw(raw, false);
			} catch (err) {
				if (signal.aborted) break;
				activeCtx?.ui.notify(`agent-bus-seat consume failed: ${String(err).slice(0, 160)}`, "warning");
				await new Promise((resolve) => setTimeout(resolve, 1000));
			}
		}
	}

	pi.on("model_select", async (event) => {
		modelId = describeModel(event.model);
	});

	pi.on("session_start", async (_event, ctx) => {
		activeCtx = ctx;
		modelId = describeModel((ctx as unknown as { model?: unknown }).model);
		if (started) return;
		started = true;
		config = await loadConfig(ctx.cwd);
		abortController = new AbortController();
		ctx.ui.notify(`agent-bus-seat online as ${config.agentId}`, "info");
		void heartbeatLoop(abortController.signal);
		void consumeLoop(abortController.signal);
	});

	pi.on("session_shutdown", () => {
		abortController?.abort();
		started = false;
	});

	pi.registerTool({
		name: "agent_bus_reply",
		label: "Agent Bus Reply",
		description: "Reply to a bus request and ack it from :processing.",
		promptSnippet: "Reply to Redis/Valkey bus requests addressed to this team-member seat",
		promptGuidelines: ["Use agent_bus_reply after completing any bus request received by the team-member seat."],
		parameters: Type.Object({
			request_id: Type.String({ description: "The request id to reply to (in_reply_to)." }),
			ok: Type.Boolean({ description: "Whether the task completed successfully." }),
			result: Type.String({ description: "Concise result plus evidence." }),
			error: Type.Optional(Type.String({ description: "Error text when ok=false." })),
			evidence: Type.Optional(Type.String({ description: "Optional concise evidence block (command output, SHA, counts)." })),
		}),
		async execute(_toolCallId, params) {
			if (!config) throw new Error("agent-bus-seat not initialized");
			const raw = rawById.get(params.request_id) || await fs.readFile(path.join(config.inboxDir, `${params.request_id}.json`), "utf8");
			const req = parseEnvelope(raw);
			if (!req?.from) throw new Error(`No original sender for request ${params.request_id}`);
			const reply = {
				id: randomUUID(),
				from: config.agentId,
				to: req.from,
				kind: "reply",
				in_reply_to: params.request_id,
				branch: req.branch || "dev",
				sent_at: new Date().toISOString(),
				payload: {
					ok: params.ok,
					model: modelId,
					result: params.result,
					...(params.error ? { error: params.error } : {}),
					...(params.evidence ? { evidence: params.evidence } : {}),
				},
			};
			await redis(config, ["LPUSH", peerInboxKey(config, req.from), JSON.stringify(reply)]);
			const acked = await ack(params.request_id);
			return {
				content: [{ type: "text", text: `Replied to ${req.from}; acked=${acked}` }],
				details: { request_id: params.request_id, to: req.from, acked },
			};
		},
	});

	pi.registerTool({
		name: "agent_bus_status",
		label: "Agent Bus Status",
		description: "Show this bus seat's heartbeat, inbox depth, processing depth, and config.",
		promptSnippet: "Inspect Redis/Valkey agent bus seat liveness and queue depth",
		parameters: Type.Object({}),
		async execute() {
			if (!config) throw new Error("agent-bus-seat not initialized");
			const [status, ttl, inbox, processing] = await Promise.all([
				redis(config, ["GET", statusKey(config)]),
				redis(config, ["TTL", statusKey(config)]),
				redis(config, ["LLEN", inboxKey(config)]),
				redis(config, ["LLEN", processingKey(config)]),
			]);
			const text = [
				`agent=${config.agentId}`,
				`model=${modelId}`,
				`status=${status.trim() || "(missing)"}`,
				`ttl=${ttl.trim()}`,
				`inbox=${inbox.trim()}`,
				`processing=${processing.trim()}`,
				`inboxDir=${config.inboxDir}`,
			].join("\n");
			return { content: [{ type: "text", text }], details: { agentId: config.agentId } };
		},
	});

	pi.registerCommand("bus-seat-status", {
		description: "Show pi-project-b-a bus seat status",
		handler: async (_args, ctx) => {
			if (!config) {
				ctx.ui.notify("agent-bus-seat not initialized", "warning");
				return;
			}
			const status = await redis(config, ["GET", statusKey(config)]).catch((e) => String(e));
			const ttl = await redis(config, ["TTL", statusKey(config)]).catch((e) => String(e));
			const inbox = await redis(config, ["LLEN", inboxKey(config)]).catch((e) => String(e));
			const processing = await redis(config, ["LLEN", processingKey(config)]).catch((e) => String(e));
			ctx.ui.notify(`bus-seat ${config.agentId}: model=${modelId} status=${status.trim()} ttl=${ttl.trim()} inbox=${inbox.trim()} processing=${processing.trim()}`, "info");
		},
	});
}
