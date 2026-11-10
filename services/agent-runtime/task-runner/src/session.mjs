import { join } from "node:path";
import { mkdir } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";
import { createAssistantMessageEventStream, getCurrentSystemMessage, InMemoryCredentialStore, normalizeContext } from "@earendil-works/pi-ai";
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SettingsManager } from "@earendil-works/pi-coding-agent";
import { McpClient, StreamableHttpTransport, toLlmContent } from "@earendil-works/pi-mcp";
import { assertSessionOpen, freezeSession, installModelBoundary } from "./model-boundary.mjs";
import { mcpScope } from "./mcp-binding.mjs";
import { IncrementalJournal, openNativeJournal } from "./journal.mjs";
import { PROTOCOL, digest, jsonText, safeUrl, sameFence, sameValue, validate } from "./protocol.mjs";

const BASE_PROMPT = "Use the published bbx_role instructions and only the declared tools.";
const CREDENTIAL_PROJECTION_WAIT_MS = 120000;

export class RunnerSession {
  constructor(config, spec, specDigest, lease, platform, credentials, onApplied, helpers) {
    this.config = config;
    this.spec = spec;
    this.specDigest = specDigest;
    this.binding = spec.binding;
    this.lease = lease;
    this.platform = platform;
    this.credentials = credentials;
    this.onApplied = onApplied;
    this.helpers = helpers;
    this.jobs = new Set();
    this.invocations = new Map();
    this.unresolvedAttempts = new Set();
    this.unresolvedInvocations = new Set();
    this.pendingInputs = new Set();
    this.mcpClients = new Map();
    this.promptRevision = spec.prompt_revision;
    this.promptText = spec.instructions;
    this.roleInstalled = false;
    this.stopController = new AbortController();
    this.scheduleLeaseTimer();
  }
  assertLease() {
    this.stopController.signal.throwIfAborted();
    this.assertParent?.();
    if (this.expired || this.lease.lease_expires_at <= Date.now() / 1000 ||
        (this.spec?.helper_child && this.spec.helper_child.deadline_ms <= Date.now())) {
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
  async prepareSession() {
    this.assertLease();
    const routes = this.spec.mcp_tool_routes;
    const aliases = Object.fromEntries(Object.entries(routes).map(([name, route]) => [name, route.wire_name]));
    const keys = Object.values(routes).map((route) => `${route.target}/${route.wire_name}`);
    if (new Set(this.spec.allowed_tools).size !== this.spec.allowed_tools.length ||
        Object.keys(aliases).length !== this.spec.allowed_tools.length ||
        this.spec.allowed_tools.some((name) => !Object.hasOwn(aliases, name)) ||
        new Set(keys).size !== this.spec.allowed_tools.length) {
      throw new Error("MCP routes must map each declared tool to one frozen target and wire name");
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
      retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } } },
      { projectTrusted: false });
    const runtime = await ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null,
      allowModelNetwork: false, refreshOnCreate: false });
    const sdkProvider = "openai";
    runtime.registerProvider(sdkProvider, { api: "openai-completions", baseUrl: this.config.model_proxy_url,
      apiKey: "scoped-pi-identity", models: [{ id: model.id, name: model.id,
        reasoning: model.reasoning, input: [...model.input], contextWindow: model.context_window, maxTokens: model.max_output_tokens,
        ...(model.thinking_level_map ? { thinkingLevelMap: model.thinking_level_map } : {}),
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }] });
    await runtime.refresh({ allowNetwork: false });
    const timeout = this.config.mcp_metadata_timeout_ms ?? 15000;
    const targets = Object.keys(this.spec.mcp_target_credential_refs);
    if (!targets.length || this.spec.helper_child && (targets.length !== 1 || targets[0] !== "sandbox")) {
      throw new Error("Invalid frozen MCP role targets");
    }
    const available = new Map();
    for (const target of targets) {
      const { audience, reference } = mcpScope(this.spec, target);
      const url = safeUrl(this.config.mcp_targets[target]);
      if (target === "sandbox" && url.href !== "http://127.0.0.1:8766/mcp/") {
        throw new Error("The Sandbox MCP target must use the frozen loopback endpoint");
      }
      if (target === "business" &&
          (url.origin !== safeUrl(this.config.control_url).origin ||
           !url.pathname.endsWith(`/api/tasks/${this.config.fence.task_id}/business-mcp-v2/`))) {
        throw new Error("The Business MCP URL did not match the Task");
      }
      const client = new McpClient({ name: "bbx-task-runner", version: "0.1.0", requestTimeoutMs: timeout, capabilities: {} });
      this.mcpClients.set(target, client);
      const transport = new StreamableHttpTransport({ url, openGetStream: false, reconnect: { maxRetries: 0 },
        authProvider: { token: () => this.credentials.resolve(reference, audience, this.binding.binding_id) },
        headers: { "x-bbx-task-id": this.config.fence.task_id, "x-bbx-binding-id": this.binding.binding_id,
          "x-bbx-agent-id": this.binding.agent_id, "x-bbx-runner-id": this.config.fence.runner_id,
          "x-bbx-pod-uid": this.config.fence.pod_uid, "x-bbx-sdk-session-id": sdkSessionId,
          "x-bbx-runtime-epoch": String(this.config.fence.epoch) },
        fetch: (input, init) => {
          if (new URL(input).href !== url.href) throw new Error("The original MCP destination changed");
          return fetch(url, { ...init, redirect: "error",
            signal: AbortSignal.any([this.stopController.signal, ...(init.signal ? [init.signal] : [])]) });
        } });
      await client.connect(transport);
      available.set(target, await client.listTools({ signal: AbortSignal.any([
        this.stopController.signal, AbortSignal.timeout(timeout),
      ]) }));
    }
    const tools = this.spec.allowed_tools.map((name) => {
      const matches = (available.get(this.mcpTarget(name)) ?? []).filter((tool) => tool.name === this.toolNames.get(name));
      if (matches.length !== 1 || ["bash", "read", "write", "edit"].includes(name)) throw new Error("An approved remote tool is unavailable, ambiguous or conflicts with a local tool");
      const [remote] = matches;
      return { name, label: remote.title ?? name, description: remote.description ?? name, parameters: remote.inputSchema,
        execute: (callId, args, signal) => this.callTool(name, callId, args, signal) };
    });
    this.assertLease();
    return { cwd: directory, agentDir, modelRuntime: runtime, settingsManager: settings,
      model: runtime.getModel(sdkProvider, model.id), thinkingLevel: model.thinking_level,
      sessionManager: manager, customTools: tools };
  }
  async initialize() {
    if (this.spec.helper_child) throw new Error("A helper can only open through its claimed plugin launch");
    const plan = await this.prepareSession();
    const paths = this.spec.helper_foreground || this.spec.helper_background ? await this.helpers.parentPaths() : [];
    if (paths.length) plan.customTools.push(...(this.spec.helper_foreground ? [this.helpers.tool(this)] : []),
      ...(this.spec.helper_background ? this.helpers.backgroundTools(this) : []));
    const resourceLoader = new DefaultResourceLoader({ cwd: plan.cwd, agentDir: plan.agentDir,
      settingsManager: plan.settingsManager, noExtensions: true, noSkills: true, noContextFiles: true,
      noPromptTemplates: true, noThemes: true, projectTrusted: false, additionalExtensionPaths: paths,
      systemPrompt: BASE_PROMPT, appendSystemPrompt: [] });
    await resourceLoader.reload();
    if (resourceLoader.getExtensions().errors.length) throw new Error("A trusted Runner extension could not load");
    const names = plan.customTools.map((tool) => tool.name);
    const created = await createAgentSession({ ...plan, resourceLoader, noTools: "builtin",
      tools: paths.length ? [...names, "subagent"] : names, projectTrusted: false });
    await this.installSession(created.session);
    if (paths.length) {
      this.helperShutdownNeeded = true;
      const errors = [];
      await this.session.bindExtensions({ mode: "print", onError: (error) => errors.push(error) });
      if (errors.length || !this.session.getToolDefinition("subagent")) throw new Error("Trusted helper plugin startup failed");
      this.session.setActiveToolsByName(names);
      this.helperParent = true;
    }
  }
  async installSession(session) {
    this.session = session;
    const manager = session.sessionManager;
    if (session.sessionId !== this.sdkSessionId || session.thinkingLevel !== this.spec.model.thinking_level)
      throw Object.assign(new Error("Pi cannot honor the effective session identity or thinking level"), { code: "unsupported_model" });
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
    installModelBoundary(this.session, { runtimeUrl: this.config.model_proxy_url,
      nativeModelRevision: this.spec.model.model_revision,
      nativeProof: (journal) => ({ kind: "native", fence: this.config.fence, binding: this.binding,
        sdk_session_id: this.session.sessionId, journal, profile_revision: this.spec.profile_revision,
        prompt_revision: this.promptRevision }),
      createStream: createAssistantMessageEventStream,
      prepareContext: (context, options) => this.refreshPrompt(context, options),
      binding: { taskId: this.config.fence.task_id, agentId: this.binding.agent_id, bindingId: this.binding.binding_id,
        epoch: this.config.fence.epoch, runnerId: this.config.fence.runner_id, podUid: this.config.fence.pod_uid },
      credential: () => this.credentials.resolve(this.spec.model_credential_ref, "model", this.binding.binding_id),
      confirm: (signal) => this.confirm(signal), assertActive: () => this.assertActive(),
    });
    const after = this.session.agent.afterToolCall;
    this.session.agent.afterToolCall = async (context) => ({ ...await after?.(context),
      ...(context.result.details?.bbx_is_error ? { isError: true } : {}) });
    await this.confirm(this.stopController.signal);
    this.assertActive();
  }
  async refreshPrompt(context, options) {
    if (this.helperParent) this.helpers.checkParentTools(this);
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
  async toolInvocation(name, callId, args, signal) {
    this.assertActive();
    const entry = this.session.sessionManager.getBranch().findLast((entry) => entry.type === "message"
      && entry.message.role === "assistant" && Array.isArray(entry.message.content)
      && entry.message.content.some((part) => part.type === "toolCall" && part.id === callId && part.name === name));
    if (!entry) throw new Error("A persisted native tool invocation is required");
    const invocationId = digest(jsonText([this.config.fence.task_id, this.binding.binding_id, this.session.sessionId, entry.id, callId, name]));
    const argumentsJson = jsonText(args);
    this.invocations.set(`${entry.id}:${callId}`, invocationId);
    return { protocol: PROTOCOL, fence: this.config.fence, binding: this.binding,
      sdk_session_id: this.session.sessionId, invocation_id: invocationId, tool_call_id: callId,
      assistant_entry_id: entry.id, tool_name: name, arguments_json: argumentsJson,
      arguments_digest: digest(argumentsJson), journal: await this.confirm(signal) };
  }
  mcpTarget(name) {
    if (!Object.hasOwn(this.spec.mcp_tool_routes, name)) throw new Error("The tool is not bound to a frozen MCP target");
    return this.spec.mcp_tool_routes[name].target;
  }
  async closeMcp() {
    const clients = [...this.mcpClients.values()];
    this.mcpClients.clear();
    await Promise.all(clients.map((client) => client.close()));
  }
  async callTool(name, callId, args, signal) {
    if (!this.toolNames.has(name)) throw new Error("The tool is not declared in this session");
    const invocation = await this.toolInvocation(name, callId, args, signal);
    const { invocation_id: invocationId, arguments_json: argumentsJson } = invocation;
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
          await this.mcpClients.get(this.mcpTarget(name)).request("tools/call", { name: this.toolNames.get(name), arguments: JSON.parse(argumentsJson),
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
    if (this.helperParent) this.helpers.cancelParent(this.binding.binding_id);
    await this.session?.abort();
    await Promise.allSettled([...this.jobs]);
    if (!this.journal) return this.lease.cursor;
    try { const cursor = await this.confirm(AbortSignal.timeout(2000)); this.journalPending = false; return cursor; }
    catch { this.journalPending = true; return this.journal.cursor; }
  }
  async hostedTerminal(idle) {
    this.closed = true;
    clearTimeout(this.leaseTimer);
    try {
      this.stop();
      if (!idle || !this.journal || this.session?.isStreaming) throw new Error("The helper did not become idle");
      const cursor = await this.confirm(AbortSignal.timeout(2000));
      if ((await this.journal.assertFile(cursor.offset)).size !== cursor.offset || this.journal.pendingConfirmation)
        throw new Error("The disposed helper journal does not end at its confirmed cursor");
      return { sdk_session_id: this.session.sessionId, cursor, sdk_idle: true, journal_confirmed: true,
        unresolved_attempts: [...this.unresolvedAttempts], unresolved_invocations: [...this.unresolvedInvocations] };
    } finally {
      const closed = await Promise.allSettled([this.closeMcp(), this.journal?.close()]);
      if (closed.some((outcome) => outcome.status === "rejected")) throw new Error("The helper resources did not close");
    }
  }
  async hostedPreparationFailed() {
    this.closed = true;
    clearTimeout(this.leaseTimer);
    this.stop();
    await this.closeMcp();
  }
  close() {
    if (this.closing) return this.closing;
    this.closed = true;
    clearTimeout(this.leaseTimer);
    this.closing = (async () => {
      await this.abort();
      if (this.helperShutdownNeeded) {
        await this.helpers.stopParent(this.binding.binding_id);
        await this.session.extensionRunner.emit({ type: "session_shutdown", reason: "quit" });
        try {
          const cursor = await this.confirm(AbortSignal.timeout(2000));
          this.journalPending = (await this.journal.assertFile(cursor.offset)).size !== cursor.offset;
        } catch { this.journalPending = true; }
      }
      await this.journal?.queue;
      const cleanup = await Promise.allSettled([
        this.closeMcp(),
        Promise.resolve().then(() => this.session?.dispose()),
        (async () => { try { await this.journal?.file?.sync(); } finally { await this.journal?.close(); } })(),
      ]);
      const summary = { sdk_session_id: this.session?.sessionId ?? this.sdkSessionId ?? this.lease.sdk_session_id ?? null,
        cursor: this.journal?.cursor ?? this.lease.cursor ?? null, sdk_idle: !this.session?.isStreaming,
        journal_confirmed: Boolean(this.journal) && !this.journalPending,
        pending_confirmation: this.journal?.pendingConfirmation ?? null,
        unresolved_attempts: [...this.unresolvedAttempts], unresolved_invocations: [...this.unresolvedInvocations] };
      this.session = this.journal = this.spec = this.onApplied = undefined;
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
