import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, rename } from "node:fs/promises";
import { dirname, join } from "node:path";
import { createAssistantMessageEventStream, InMemoryCredentialStore } from "@earendil-works/pi-ai";
import {
  createAgentSession, DefaultResourceLoader, ModelRuntime,
  SessionManager, SettingsManager,
} from "@earendil-works/pi-coding-agent";

export const helperModes = Object.freeze({ foreground: false, background: false });
const frozenSessions = new WeakSet();
function assertOpen(session) {
  if (!session || frozenSessions.has(session)) throw new Error("The session admission boundary is frozen");
}

const digest = (value) => createHash("sha256").update(value).digest("hex");
const zeroUsage = () => ({
  input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
});

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

function failedMessage(model, aborted) {
  return {
    role: "assistant", content: [], api: model.api, provider: model.provider,
    model: model.id, usage: zeroUsage(), stopReason: aborted ? "aborted" : "error",
    errorMessage: "BBX journal or model boundary refused the request", timestamp: Date.now(),
  };
}

function pushFailure(stream, model, signal) {
  const message = failedMessage(model, signal?.aborted);
  stream.push({ type: "error", reason: message.stopReason, error: message });
  stream.end(message);
}

/** Install only on host-owned sessions. Provider credentials never cross this boundary. */
export function installGatewayBoundary(session, {
  gatewayUrl, gatewayToken, binding, acknowledge, admit, settle, fetch: transport = fetch,
}) {
  const base = new URL(gatewayUrl.endsWith("/") ? gatewayUrl : `${gatewayUrl}/`);
  if (!(base.protocol === "https:" || (base.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(base.hostname)))
      || base.username || base.password || base.search || base.hash) throw new Error("A restricted gateway URL is required");
  if (!gatewayToken || !binding.taskId || !binding.agentId) throw new Error("A gateway identity is required");
  const original = session.agent.streamFunction;
  const selected = session.model;
  if (!selected || selected.api !== "openai-completions") throw new Error("The seam requires its approved gateway model");
  const beforeToolCall = session.agent.beforeToolCall;
  session.agent.beforeToolCall = async (context) => {
    if (frozenSessions.has(session)) return { block: true, terminate: true, reason: "The session admission boundary is frozen" };
    const result = await beforeToolCall?.(context);
    return frozenSessions.has(session) ? { block: true, terminate: true, reason: "The session admission boundary is frozen" } : result;
  };
  session.settingsManager.setCacheWarmingMode("off");
  session.settingsManager.applyOverrides({ retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } } });

  session.agent.streamFunction = async (model, context, options = {}) => {
    const stream = createAssistantMessageEventStream();
    const callId = randomUUID();
    let attempt;
    let usageObserved = false;
    let settlementStarted = false;
    const finish = async (message) => {
      if (!attempt?.admitted || settlementStarted) return;
      settlementStarted = true;
      // A local error, HTTP error or abort is not proof that upstream stopped or billed zero.
      const completed = !["error", "aborted"].includes(message.stopReason);
      await settle({
        ...attempt, binding, callId,
        outcome: completed && usageObserved ? "completed" : attempt.sent ? "unknown" : "not_sent",
        modelOutcome: completed ? "completed" : message.stopReason,
        usage: completed && usageObserved ? message.usage : null,
      });
    };
    try {
      assertOpen(session);
      if (options.signal?.aborted || model.provider !== selected.provider
          || model.id !== selected.id || model.api !== selected.api
          || new URL(model.baseUrl).href.replace(/\/$/, "") !== base.href.replace(/\/$/, "")) {
        throw new Error("The requested model is outside the gateway binding");
      }
      const journal = await confirmJournal(session, acknowledge);
      assertOpen(session);
      if (options.signal?.aborted) throw new Error("The request was aborted");
      const fetchOnce = async (input, init = {}) => {
        assertOpen(session);
        const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url);
        if (attempt || url.origin !== base.origin || !url.pathname.startsWith(base.pathname)) {
          throw new Error("Implicit retry or a gateway escape was refused");
        }
        attempt = { attemptId: randomUUID(), sent: false, admitted: false, httpStatus: null };
        const request = {
          binding, callId, attemptId: attempt.attemptId, journal, model: selected.id,
          purpose: options.sessionId === session.sessionId ? "turn" : "summary", body: JSON.parse(init.body),
        };
        const ticket = await admit(request);
        if (ticket?.callId !== callId || ticket?.attemptId !== attempt.attemptId) throw new Error("Admission identity did not match");
        attempt.admitted = true;
        assertOpen(session);
        if (init.signal?.aborted || options.signal?.aborted) throw new Error("The request was aborted");
        const headers = new Headers({ "content-type": "application/json", accept: "text/event-stream" });
        headers.set("authorization", `Bearer ${gatewayToken}`);
        headers.set("x-bbx-task-id", binding.taskId);
        headers.set("x-bbx-agent-id", binding.agentId);
        headers.set("x-bbx-call-id", callId);
        headers.set("x-bbx-attempt-id", attempt.attemptId);
        headers.set("x-bbx-journal-hash", journal.hash);
        attempt.sent = true;
        const response = await transport(url, { ...init, headers, redirect: "error" });
        attempt.httpStatus = response.status;
        return response;
      };
      // A replacement StreamFn does not inherit Pi's private ModelRuntime auth marker.
      // Drop pre-resolved summary auth and pin the final transport instead of importing it.
      const source = await original({ ...selected, baseUrl: gatewayUrl }, context, {
        ...options, apiKey: undefined, headers: undefined, env: {}, maxRetries: 0,
        transport: "sse", fetch: fetchOnce,
        onProviderStreamEvent: async (data, requestModel) => {
          const usage = data?.usage ?? data?.choices?.find((choice) => choice.usage)?.usage;
          if (usage && [usage.prompt_tokens, usage.completion_tokens].every((count) => Number.isInteger(count) && count >= 0)) usageObserved = true;
          await options.onProviderStreamEvent?.(data, requestModel);
        },
      });
      void (async () => {
        try {
          let terminal = false;
          for await (const event of source) {
            if (event.type === "done" || event.type === "error") {
              const message = event.type === "done" ? event.message : event.error;
              await finish(message);
              terminal = true;
            }
            stream.push(event);
          }
          const message = await source.result();
          if (!terminal) await finish(message);
          stream.end(message);
        } catch {
          frozenSessions.add(session);
          try { await finish(failedMessage(selected, options.signal?.aborted)); } catch { /* The reservation remains unresolved. */ }
          pushFailure(stream, selected, options.signal);
        }
      })();
    } catch {
      frozenSessions.add(session);
      try { await finish(failedMessage(selected, options.signal?.aborted)); }
      catch { frozenSessions.add(session); }
      pushFailure(stream, selected, options.signal);
    }
    return stream;
  };
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
      catch (error) { frozenSessions.add(session); throw error; }
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
      } catch (error) { frozenSessions.add(session); throw error; }
      if (previous) {
        if (previous.state !== "completed") {
          frozenSessions.add(session);
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
      } catch (error) { frozenSessions.add(session); throw error; }
    },
  };
}
