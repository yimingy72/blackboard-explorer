import { join } from "node:path";
import { mkdir } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";
import { createAssistantMessageEventStream, getCurrentSystemMessage, InMemoryCredentialStore, normalizeContext } from "@earendil-works/pi-ai";
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SettingsManager } from "@earendil-works/pi-coding-agent";
import { McpClient, StreamableHttpTransport, toLlmContent } from "@earendil-works/pi-mcp";
import { assertSessionOpen, freezeSession, installModelBoundary } from "./model-boundary.mjs";
import { IncrementalJournal, openNativeJournal } from "./journal.mjs";
import { PROTOCOL, digest, jsonText, safeUrl, sameFence, sameValue, validate } from "./protocol.mjs";

const BASE_PROMPT = "Use the published bbx_role instructions and only the declared tools.";
const CREDENTIAL_PROJECTION_WAIT_MS = 120000;

export class RunnerSession {
  constructor(config, spec, specDigest, lease, platform, credentials, onApplied) {
    this.config = config;
    this.spec = spec;
    this.specDigest = specDigest;
    this.binding = spec.binding;
    this.lease = lease;
    this.platform = platform;
    this.credentials = credentials;
    this.onApplied = onApplied;
    this.jobs = new Set();
    this.invocations = new Map();
    this.unresolvedAttempts = new Set();
    this.unresolvedInvocations = new Set();
    this.pendingInputs = new Set();
    this.promptRevision = spec.prompt_revision;
    this.promptText = spec.instructions;
    this.roleInstalled = false;
    this.stopController = new AbortController();
    this.scheduleLeaseTimer();
  }
  assertLease() {
    this.stopController.signal.throwIfAborted();
    if (this.expired || this.lease.lease_expires_at <= Date.now() / 1000) {
      this.expired = true;
      if (this.session) freezeSession(this.session);
      throw new Error("The session lease expired");
    }
  }
  assertActive() { this.assertLease(); assertSessionOpen(this.session); }
  scheduleLeaseTimer() {
    clearTimeout(this.leaseTimer);
    this.leaseTimer = setTimeout(() => {
      this.expired = true;
      if (this.session) { freezeSession(this.session); this.session.clearQueue(); void this.session.abort().catch(() => {}); }
    }, Math.min(2147483647, Math.max(0, this.lease.lease_expires_at * 1000 - Date.now())));
    this.leaseTimer.unref?.();
  }
  renew(receipt) {
    validate("SessionLeaseReceipt", receipt);
    if (!sameFence(receipt.fence, this.config.fence) || receipt.binding_id !== this.binding.binding_id
        || !sameValue(receipt.binding, this.binding)
        || receipt.request_id !== this.lease.request_id) throw new Error("Lease renewal identity did not match");
    const sdkSessionId = this.session?.sessionId ?? this.lease.sdk_session_id ?? null;
    if (receipt.sdk_session_id == null && sdkSessionId !== null) return false;
    if ((receipt.sdk_session_id ?? null) !== sdkSessionId) throw new Error("Lease renewal SDK identity did not match");
    if (this.closed || this.expired) return false;
    if (this.lease.lease_expires_at <= Date.now() / 1000) {
      this.expired = true;
      if (this.session) { freezeSession(this.session); this.session.clearQueue(); void this.session.abort().catch(() => {}); }
      return false;
    }
    if (this.session) {
      try { assertSessionOpen(this.session); } catch { return false; }
    }
    if (receipt.lease_revision < this.lease.lease_revision) return;
    if (receipt.lease_revision === this.lease.lease_revision) {
      if (receipt.lease_expires_at !== this.lease.lease_expires_at) throw new Error("Lease renewal revision conflicts");
      return;
    }
    if (receipt.lease_expires_at <= this.lease.lease_expires_at || receipt.lease_expires_at <= Date.now() / 1000) throw new Error("Lease renewal did not extend a live permit");
    this.lease = { ...this.lease, lease_revision: receipt.lease_revision, lease_expires_at: receipt.lease_expires_at };
    this.scheduleLeaseTimer();
  }
  async initialize() {
    this.assertLease();
    const aliases = this.spec.mcp_tool_aliases;
    if (!aliases || new Set(this.spec.allowed_tools).size !== this.spec.allowed_tools.length
        || Object.keys(aliases).length !== this.spec.allowed_tools.length
        || this.spec.allowed_tools.some((name) => !Object.hasOwn(aliases, name))
        || new Set(Object.values(aliases)).size !== this.spec.allowed_tools.length) {
      throw new Error("MCP aliases must map every declared tool to a distinct wire name");
    }
    this.toolNames = new Map(Object.entries(aliases));
    if (Boolean(this.lease.cursor) !== Boolean(this.lease.sdk_session_id)) throw new Error("The session lease is incomplete");
    const model = this.spec.model;
    if (!model.input.includes("text") || new Set(model.input).size !== model.input.length
        || (!model.reasoning && model.thinking_level !== "off") || (model.thinking_budgets && Object.keys(model.thinking_budgets).length)
        || model.thinking_level_map?.[model.thinking_level] === null) throw Object.assign(new Error("The effective model settings are unsupported by this route"), { code: "unsupported_model" });
    // A running Runner can receive a new binding before its Secret projection arrives.
    const projectionDeadline = performance.now() + CREDENTIAL_PROJECTION_WAIT_MS;
    for (;;) {
      this.assertLease();
      try {
        await this.credentials.requireBinding(this.spec);
        this.assertLease();
        break;
      } catch (error) {
        const remaining = projectionDeadline - performance.now();
        if (error.code !== "credential_pending" || remaining <= 0) throw error;
        await delay(Math.min(100, remaining), undefined, { signal: this.stopController.signal });
      }
    }
    const directory = join(this.config.journal_root, this.binding.binding_id);
    const agentDir = join(directory, "agent");
    await mkdir(agentDir, { recursive: true, mode: 0o700 });
    const manager = await openNativeJournal(join(directory, "session.jsonl"), directory, this.binding, this.specDigest, {
      sdkSessionId: this.lease.sdk_session_id, minimumSize: this.lease.cursor?.offset ?? 0, cursor: this.lease.cursor,
    });
    const sdkSessionId = manager.getSessionId();
    this.sdkSessionId = sdkSessionId;
    this.assertLease();
    if (this.lease.sdk_session_id && this.lease.sdk_session_id !== sdkSessionId) throw new Error("The granted SDK session is absent from this PVC");
    if (!this.lease.cursor && manager.getEntries().some((entry) => entry.type === "message" || entry.type === "custom_message")) {
      throw new Error("An unregistered persisted conversation requires reconciliation");
    }
    const settings = SettingsManager.inMemory({ cacheWarming: "off", compaction: { enabled: true },
      retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } } });
    const runtime = await ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null,
      allowModelNetwork: false, refreshOnCreate: false });
    const sdkProvider = "openai";
    runtime.registerProvider(sdkProvider, { api: "openai-completions", baseUrl: this.config.model_proxy_url,
      apiKey: "scoped-gateway-identity", models: [{ id: model.id, name: model.id,
        reasoning: model.reasoning, input: [...model.input], contextWindow: model.context_window, maxTokens: model.max_output_tokens,
        ...(model.thinking_level_map ? { thinkingLevelMap: model.thinking_level_map } : {}),
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }] });
    await runtime.refresh({ allowNetwork: false });
    const resourceLoader = new DefaultResourceLoader({ cwd: directory, agentDir, settingsManager: settings,
      noExtensions: true, noSkills: true, noContextFiles: true, noPromptTemplates: true, noThemes: true,
      systemPrompt: BASE_PROMPT, appendSystemPrompt: [] });
    await resourceLoader.reload();
    this.mcp = new McpClient({ name: "bbx-task-runner", version: "0.1.0",
      requestTimeoutMs: this.config.mcp_metadata_timeout_ms ?? 15000, capabilities: {} });
    this.transport = new StreamableHttpTransport({ url: safeUrl(this.config.mcp_gateway_url), openGetStream: false,
      reconnect: { maxRetries: 0 }, authProvider: { token: () => this.credentials.resolve(this.spec.mcp_credential_ref, "mcp", this.binding.binding_id) },
      headers: { "x-bbx-task-id": this.config.fence.task_id, "x-bbx-binding-id": this.binding.binding_id,
        "x-bbx-agent-id": this.binding.agent_id, "x-bbx-runner-id": this.config.fence.runner_id,
        "x-bbx-pod-uid": this.config.fence.pod_uid, "x-bbx-sdk-session-id": sdkSessionId,
        "x-bbx-runtime-epoch": String(this.config.fence.epoch) },
      fetch: (url, init) => fetch(url, { ...init, redirect: "error",
        signal: AbortSignal.any([this.stopController.signal, ...(init.signal ? [init.signal] : [])]) }) });
    await this.mcp.connect(this.transport);
    const available = await this.mcp.listTools();
    const tools = this.spec.allowed_tools.map((name) => {
      const matches = available.filter((tool) => tool.name === this.toolNames.get(name));
      if (matches.length !== 1 || ["bash", "read", "write", "edit"].includes(name)) throw new Error("An approved remote tool is unavailable, ambiguous or conflicts with a local tool");
      const [remote] = matches;
      return { name, label: remote.title ?? name, description: remote.description ?? name, parameters: remote.inputSchema,
        execute: (callId, args, signal) => this.callTool(name, callId, args, signal) };
    });
    this.assertLease();
    const created = await createAgentSession({ cwd: directory, agentDir, modelRuntime: runtime, settingsManager: settings,
      resourceLoader, model: runtime.getModel(sdkProvider, model.id), thinkingLevel: model.thinking_level, sessionManager: manager,
      noTools: "builtin", tools: tools.map((tool) => tool.name), customTools: tools });
    this.session = created.session;
    if (this.session.thinkingLevel !== model.thinking_level) throw Object.assign(new Error("Pi cannot honor the effective thinking level"), { code: "unsupported_model" });
    const promptEntry = manager.getEntries().findLast((entry) => entry.type === "custom" && entry.customType === "bbx.prompt");
    if (promptEntry) {
      const system = manager.getEntry(promptEntry.data.system_entry_id)?.message;
      const text = system?.role === "system" ? system.sections?.bbx_role : undefined;
      if (typeof text !== "string" || digest(text) !== promptEntry.data.prompt_digest) throw new Error("The persisted prompt version is inconsistent");
      this.promptText = text;
      this.promptRevision = promptEntry.data.prompt_revision;
      this.roleInstalled = true;
    }
    this.session.agent.toolExecution = "sequential";
    this.journal = new IncrementalJournal(this.session, { fence: this.config.fence, binding: this.binding,
      lease: this.lease, platform: this.platform });
    await this.journal.initialize();
    installModelBoundary(this.session, { gatewayUrl: this.config.model_proxy_url,
      createStream: createAssistantMessageEventStream,
      prepareContext: (context, options) => this.refreshPrompt(context, options),
      binding: { taskId: this.config.fence.task_id, agentId: this.binding.agent_id, bindingId: this.binding.binding_id,
        epoch: this.config.fence.epoch, runnerId: this.config.fence.runner_id, podUid: this.config.fence.pod_uid },
      credential: () => this.credentials.resolve(this.spec.model_credential_ref, "model", this.binding.binding_id),
      confirm: (signal) => this.confirm(signal), assertActive: () => this.assertActive(),
      admit: async (request) => {
        const ticket = await this.platform.admitModel(this, request);
        this.unresolvedAttempts.add(request.attemptId);
        return ticket;
      },
      settle: async (receipt) => {
        await this.platform.settleModel(this, receipt);
        if (receipt.outcome !== "unknown") this.unresolvedAttempts.delete(receipt.attemptId);
      },
    });
    const after = this.session.agent.afterToolCall;
    this.session.agent.afterToolCall = async (context) => ({ ...await after?.(context),
      ...(context.result.details?.bbx_is_error ? { isError: true } : {}) });
    await this.confirm(this.stopController.signal);
    this.assertActive();
  }
  async refreshPrompt(context, options) {
    const published = await this.platform.publishedPrompt(this, options.signal);
    this.assertActive();
    if (!published && !this.roleInstalled) throw new Error("The initial published prompt is unavailable");
    if (published) {
      if (this.roleInstalled && published.prompt_revision === this.promptRevision && published.instructions !== this.promptText) throw new Error("A published prompt revision changed content");
      if (!this.roleInstalled || published.prompt_revision > this.promptRevision) {
        const message = { role: "system", content: "", sections: { bbx_role: published.instructions }, timestamp: Date.now() };
        const entryId = this.session.sessionManager.appendMessage(message);
        this.session.agent.state.messages = [...this.session.agent.state.messages, message];
        this.session.sessionManager.appendCustomEntry("bbx.prompt", { prompt_revision: published.prompt_revision,
          prompt_digest: digest(published.instructions), system_entry_id: entryId });
        this.promptRevision = published.prompt_revision;
        this.promptText = published.instructions;
        this.roleInstalled = true;
      }
    }
    if (options.sessionId !== this.session.sessionId) return context;
    const system = getCurrentSystemMessage(context.messages);
    return normalizeContext({ messages: [{ ...system, role: "system", content: system?.content ?? BASE_PROMPT,
      sections: { ...system?.sections, bbx_role: this.promptText }, timestamp: system?.timestamp ?? Date.now() },
    ...context.messages.filter((message) => message.role !== "system")] });
  }
  async confirm(signal) {
    try {
      const cursor = await this.journal.confirm(signal);
      this.onApplied?.(this, cursor);
      return cursor;
    } catch (error) { if (this.session) freezeSession(this.session); throw error; }
  }
  remember(command) {
    const previous = this.journal.commands.get(command.request_id);
    if (previous && previous.payload_digest !== command.payload_digest) throw new Error("request_conflict");
    if (!previous) this.session.sessionManager.appendCustomEntry("bbx.control", {
      request_id: command.request_id, payload_digest: command.payload_digest, kind: command.payload.kind,
      ...(command.payload.kind === "input" ? { input_id: command.payload.input_id, content_digest: digest(command.payload.content) } : {}),
    });
  }
  input(command) {
    this.assertActive();
    const known = this.journal.inputs.get(command.payload.input_id);
    const intent = [...this.journal.commands.values()].find((item) => item.kind === "input" && item.input_id === command.payload.input_id);
    if (intent && intent.content_digest !== digest(command.payload.content)) throw new Error("request_conflict");
    if (known || this.pendingInputs.has(command.payload.input_id)) return;
    this.pendingInputs.add(command.payload.input_id);
    const work = this.session.sendCustomMessage({ customType: "bbx.input", content: command.payload.content, display: true,
      details: { input_id: command.payload.input_id, content_digest: digest(command.payload.content), request_id: command.request_id } }, {
      triggerTurn: true, deliverAs: command.payload.mode === "steer" ? "steer" : "followUp",
    });
    const settled = work.catch(() => freezeSession(this.session)).then(async () => {
      try { await this.confirm(AbortSignal.timeout(2000)); } catch { /* The platform retains the unconfirmed cursor. */ }
    });
    this.jobs.add(settled);
    void settled.finally(() => this.jobs.delete(settled));
  }
  async callTool(name, callId, args, signal) {
    this.assertActive();
    if (!this.toolNames.has(name)) throw new Error("The tool is not declared in this session");
    const entry = this.session.sessionManager.getBranch().findLast((entry) => entry.type === "message"
      && entry.message.role === "assistant" && Array.isArray(entry.message.content)
      && entry.message.content.some((part) => part.type === "toolCall" && part.id === callId && part.name === name));
    if (!entry) throw new Error("A persisted native tool invocation is required");
    const invocationId = digest(jsonText([this.config.fence.task_id, this.binding.binding_id, this.session.sessionId, entry.id, callId, name]));
    const argumentsJson = jsonText(args);
    this.invocations.set(`${entry.id}:${callId}`, invocationId);
    const invocation = { protocol: PROTOCOL, fence: this.config.fence, binding: this.binding,
      sdk_session_id: this.session.sessionId, invocation_id: invocationId, tool_call_id: callId,
      assistant_entry_id: entry.id, tool_name: name, arguments_json: argumentsJson,
      arguments_digest: digest(argumentsJson), journal: await this.confirm(signal) };
    try {
      const permit = await this.platform.admitTool(this, invocation, signal);
      this.assertActive();
      if (permit.disposition === "unknown") throw new Error("The tool invocation is unresolved");
      let result;
      if (permit.disposition === "completed") {
        if (typeof permit.result_json !== "string" || digest(permit.result_json) !== permit.result_digest) throw new Error("The saved MCP result does not match its receipt");
        result = JSON.parse(permit.result_json);
      }
      if (permit.disposition === "admitted") {
        const remaining = permit.authorized_wait_until_ms - Date.now();
        if (!Number.isSafeInteger(permit.authorized_wait_until_ms) || remaining <= 0) throw new Error("The admitted tool wait deadline is unavailable or expired");
        this.unresolvedInvocations.add(invocationId);
        try {
          await this.mcp.request("tools/call", { name: this.toolNames.get(name), arguments: JSON.parse(argumentsJson),
            _meta: { bbx: { ...invocation, permit_ref: invocationId } } }, { signal, timeoutMs: Math.min(2147483647, remaining) });
        } catch { /* A lost transport reply can still have a durable host result. */ }
        const saved = await this.platform.completeTool(this, invocation, signal);
        result = JSON.parse(saved.result_json);
        this.unresolvedInvocations.delete(invocationId);
      }
      if (!result || !Array.isArray(result.content)
          || result.content.some((part) => !part || typeof part.type !== "string")) throw new Error("The saved remote tool result is invalid");
      return { content: toLlmContent(result), details: { invocation_id: invocationId, bbx_is_error: result.isError === true } };
    } catch (error) {
      freezeSession(this.session);
      if (this.unresolvedInvocations.has(invocationId)) await this.platform.unknownTool(this, invocation).catch(() => {});
      throw error;
    }
  }
  stop() {
    this.stopController.abort();
    if (this.session) { freezeSession(this.session); this.session.clearQueue(); }
  }
  async abort() {
    this.stop();
    await this.session?.abort();
    await Promise.allSettled([...this.jobs]);
    if (!this.journal) return this.lease.cursor;
    try { const cursor = await this.confirm(AbortSignal.timeout(2000)); this.journalPending = false; return cursor; }
    catch { this.journalPending = true; return this.journal.cursor; }
  }
  close() {
    if (this.closing) return this.closing;
    this.closed = true;
    clearTimeout(this.leaseTimer);
    this.closing = (async () => {
      await this.abort();
      await this.journal?.queue;
      const cleanup = await Promise.allSettled([
        this.mcp?.close(),
        Promise.resolve().then(() => this.session?.dispose()),
        (async () => { try { await this.journal?.file?.sync(); } finally { await this.journal?.close(); } })(),
      ]);
      const summary = { sdk_session_id: this.session?.sessionId ?? this.sdkSessionId ?? this.lease.sdk_session_id ?? null,
        cursor: this.journal?.cursor ?? this.lease.cursor ?? null, sdk_idle: !this.session?.isStreaming,
        journal_confirmed: Boolean(this.journal) && !this.journalPending,
        pending_confirmation: this.journal?.pendingConfirmation ?? null,
        unresolved_attempts: [...this.unresolvedAttempts], unresolved_invocations: [...this.unresolvedInvocations] };
      this.session = this.mcp = this.transport = this.journal = this.spec = this.onApplied = undefined;
      this.promptText = undefined;
      this.toolNames?.clear();
      this.jobs.clear(); this.invocations.clear(); this.pendingInputs.clear();
      if (cleanup.some((result) => result.status === "rejected")) {
        throw Object.assign(new Error("Local session cleanup failed"), { closeSummary: summary });
      }
      return summary;
    })();
    return this.closing;
  }
}
