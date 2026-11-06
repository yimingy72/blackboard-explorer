import { installModelBoundary, assertSessionOpen as assertOpen, freezeSession } from "../../task-runner/src/model-boundary.mjs";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, rename } from "node:fs/promises";
import { dirname, join } from "node:path";
import { createAssistantMessageEventStream, InMemoryCredentialStore } from "@earendil-works/pi-ai";
import {
  createAgentSession, DefaultResourceLoader, ModelRuntime,
  SessionManager, SettingsManager,
} from "@earendil-works/pi-coding-agent";

export const helperModes = Object.freeze({ foreground: false, background: false });
const digest = (value) => createHash("sha256").update(value).digest("hex");

async function syncDirectory(path) {
  const file = await open(path, "r");
  try { await file.sync(); } finally { await file.close(); }
}

/** Sync Pi's journal before acknowledging its cursor; never replace Pi's history. */
export async function confirmJournal(session, acknowledge) {
  // ponytail: This P1 probe hashes the full journal; production must use incremental confirmed cursors.
  const manager = session.sessionManager;
  const path = manager.getSessionFile();
  if (!path) throw new Error("A persistent Pi journal is required");
  const leafId = manager.getLeafId();
  const file = await open(path, "r");
  let bytes;
  try {
    await file.sync();
    bytes = await file.readFile();
  } finally { await file.close(); }
  await syncDirectory(dirname(path));
  const entries = bytes.toString("utf8").trimEnd().split("\n").map(JSON.parse);
  if (entries[0]?.id !== session.sessionId
      || (leafId && !entries.some((entry) => entry.id === leafId))
      || manager.getLeafId() !== leafId) throw new Error("Pi journal changed during confirmation");
  const cursor = {
    sessionId: session.sessionId, leafId,
    seq: entries.length - 1, hash: digest(bytes),
  };
  await acknowledge(cursor);
  return cursor;
}

export function installGatewayBoundary(session, options) {
  return installModelBoundary(session, {
    createStream: createAssistantMessageEventStream,
    ...options, credential: async () => options.gatewayToken,
    confirm: () => confirmJournal(session, options.acknowledge),
  });
}

/** A real SDK session with no ambient discovery, vendor credentials or local tools. */
export async function createGatewaySession({ directory, model, customTools = [], ...boundary }) {
  const agentDir = join(directory, "agent");
  await mkdir(agentDir, { recursive: true, mode: 0o700 });
  const settingsManager = SettingsManager.inMemory({
    cacheWarming: "off", retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
  });
  const modelRuntime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(), modelsPath: null, allowModelNetwork: false, refreshOnCreate: false,
  });
  modelRuntime.registerProvider("bbx-gateway", {
    api: "openai-completions", baseUrl: boundary.gatewayUrl, apiKey: "gateway-placeholder",
    models: [{
      id: model?.id ?? "fake", name: "Gateway model", reasoning: false, input: ["text"],
      contextWindow: model?.contextWindow ?? 65536, maxTokens: 128,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    }],
  });
  await modelRuntime.refresh({ allowNetwork: false });
  const resourceLoader = new DefaultResourceLoader({
    cwd: directory, agentDir, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    systemPrompt: "Use only the approved task tools.", appendSystemPrompt: [],
  });
  await resourceLoader.reload();
  const { session } = await createAgentSession({
    cwd: directory, agentDir, modelRuntime, settingsManager, resourceLoader,
    model: modelRuntime.getModel("bbx-gateway", model?.id ?? "fake"),
    sessionManager: SessionManager.create(directory, join(directory, "journal")),
    noTools: "builtin", tools: customTools.map((tool) => tool.name), customTools,
  });
  installGatewayBoundary(session, boundary);
  return session;
}

/** Small durable fixture store; production must use its existing invocation transaction. */
export class FileReceiptStore {
  constructor(directory) { this.directory = directory; }
  async read(id) {
    try { return JSON.parse(await readFile(join(this.directory, `${digest(id)}.json`), "utf8")); }
    catch (error) { if (error.code === "ENOENT") return undefined; throw error; }
  }
  async save(id, value) {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    const path = join(this.directory, `${digest(id)}.json`);
    const temporary = `${path}.${randomUUID()}`;
    const file = await open(temporary, "wx", 0o600);
    try { await file.writeFile(JSON.stringify(value)); await file.sync(); }
    finally { await file.close(); }
    await rename(temporary, path);
    await syncDirectory(this.directory);
  }
  async claim(id, value) {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    let file;
    try { file = await open(join(this.directory, `${digest(id)}.json`), "wx", 0o600); }
    catch (error) { if (error.code === "EEXIST") return false; throw error; }
    try { await file.writeFile(JSON.stringify(value)); await file.sync(); }
    finally { await file.close(); }
    await syncDirectory(this.directory);
    return true;
  }
}

/** The host derives the operation from Pi's persisted assistant entry, never a model ID alone. */
export function toolInvocation(session, callId, { taskId, toolName }) {
  const entry = session.sessionManager.getBranch().findLast((entry) => entry.type === "message"
    && entry.message.role === "assistant" && entry.message.content.some((part) =>
      part.type === "toolCall" && part.id === callId && part.name === toolName));
  if (!taskId || !entry) throw new Error("A persisted tool invocation binding is required");
  return `${taskId}:${session.sessionId}:${entry.id}:${toolName}:${callId}`;
}

/** Awaitable tool execution is the side-effect barrier; subscriptions are display only. */
export function durableTool(tool, { store, confirm, resolveInvocation, getSession }) {
  return {
    ...tool,
    async execute(callId, args, ...rest) {
      const session = getSession();
      assertOpen(session);
      try { await confirm(); }
      catch (error) { freezeSession(session); throw error; }
      assertOpen(session);
      const invocationId = await resolveInvocation(callId);
      if (typeof invocationId !== "string" || !invocationId) throw new Error("A trusted invocation identity is required");
      const inputHash = digest(JSON.stringify(args));
      let claimed;
      let previous;
      try {
        claimed = await store.claim(invocationId, { state: "sent", inputHash });
        previous = claimed ? undefined : await store.read(invocationId);
        if (!claimed && !previous) throw new Error("The existing tool receipt could not be reconciled");
      } catch (error) { freezeSession(session); throw error; }
      if (previous) {
        if (previous.state !== "completed") {
          freezeSession(session);
          throw new Error("An unresolved tool invocation cannot be replayed");
        }
        if (previous.inputHash !== inputHash) throw new Error("The tool invocation arguments do not match");
        return previous.result;
      }
      assertOpen(session);
      try {
        const result = await tool.execute(callId, args, ...rest);
        await store.save(invocationId, { state: "completed", inputHash, result });
        return result;
      } catch (error) { freezeSession(session); throw error; }
    },
  };
}
