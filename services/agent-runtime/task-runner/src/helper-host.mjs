import { AsyncLocalStorage } from "node:async_hooks";
import { mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { mcpScope } from "./mcp-binding.mjs";
import { RunnerSession } from "./session.mjs";
import { freezeSession } from "./model-boundary.mjs";
import { PROTOCOL, decodeCommand, digest, sameFence, sameValue, validate } from "./protocol.mjs";

const name = "delegate_helper";
const backgroundName = "delegate_helper_background";
const statusName = "background_helper_status";
const cancelName = "cancel_helper";
const agent = "bbx-helper";
const tools = new Set(["read_file", "exec_command"]);
const self = fileURLToPath(import.meta.url);
const registryKey = Symbol.for("bbx.task-runner.helper-launch.v1");
const launches = globalThis[registryKey] ??= new AsyncLocalStorage();
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
function bounded(work, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
    work.then((value) => { signal.removeEventListener("abort", abort); resolve(value); },
      (error) => { signal.removeEventListener("abort", abort); reject(error); });
  });
}

class ChildCredentials {
  constructor(spec, config, records) {
    this.records = records;
    this.spec = spec;
    this.taskId = config.fence.task_id;
    if (Object.keys(records ?? {}).length !== 2) throw new Error("The helper requires two child credentials");
  }
  async resolve(reference, audience, binding) {
    const sandbox = mcpScope(this.spec, "sandbox");
    const expected = audience === "model" ? this.spec.model_credential_ref :
      audience === sandbox.audience ? sandbox.reference : undefined;
    const value = this.records?.[reference];
    if (!expected || reference !== expected || binding !== this.spec.binding.binding_id ||
        value?.audience !== audience || value.task_id !== this.taskId || value.binding_id !== binding ||
        !Number.isFinite(value.expires_at) || value.expires_at <= Date.now() / 1000 ||
        typeof value.token !== "string" || !value.token) throw new Error("The helper credential is unavailable");
    return value.token;
  }
  async requireBinding(spec) {
    await this.resolve(spec.model_credential_ref, "model", spec.binding.binding_id);
    const sandbox = mcpScope(spec, "sandbox");
    await this.resolve(sandbox.reference, sandbox.audience, spec.binding.binding_id);
  }
  clear() { this.records = undefined; }
}

function ownedLaunch(run) {
  const launch = launches.getStore();
  if (!launch || run.agent !== agent || run.childIndex !== 0 || typeof run.runId !== "string" || !run.runId)
    throw new Error("The plugin did not present a claimed foreground helper");
  return launch;
}

export async function prepareChildSessionHost(run, signal) {
  const launch = ownedLaunch(run);
  if (launch.started) throw new Error("The helper launch cannot be repeated");
  launch.started = true;
  launch.runId = run.runId;
  const active = signal ? AbortSignal.any([signal, launch.signal]) : launch.signal;
  let slot;
  const fail = async () => {
    try { await slot?.hostedPreparationFailed(); }
    finally { launch.credentials.clear(); launch.done.resolve(); }
  };
  try {
    active.throwIfAborted();
    launch.assertParent?.();
    const lease = await launch.platform.acquire(launch.command, active);
    active.throwIfAborted();
    if (lease.sdk_session_id != null || lease.cursor != null) throw new Error("A helper cannot attach an existing session");
    const spec = launch.command.payload.spec;
    slot = launch.slot = new RunnerSession(launch.config, spec, launch.command.payload_digest,
      lease, launch.platform, launch.credentials);
    slot.assertParent = launch.assertParent;
    const stop = () => slot.stop();
    active.addEventListener("abort", stop, { once: true });
    const plan = await slot.prepareSession();
    active.throwIfAborted();
    return { ...plan, withoutSession: fail,
      beforeDispose: async () => slot.stop(),
      afterDispose: async (_session, idle) => {
        try {
          await launch.cancelRequest;
          const summary = await slot.hostedTerminal(idle);
          launch.localEnded = true;
          launch.summary = summary;
          const proof = { child_id: spec.helper_child.child_id, binding_id: spec.binding.binding_id,
            plugin_run_id: run.runId, sdk_session_id: summary.sdk_session_id, cursor: summary.cursor,
            sdk_idle: true, disposed: true, journal_confirmed: true };
          try { await launch.platform.helper(launch.parent, launch.invocation, "terminal", proof); }
          catch (error) {
            if (error.status && error.status < 500) throw error;
            await launch.platform.helper(launch.parent, launch.invocation, "terminal", proof);
          }
        } finally {
          active.removeEventListener("abort", stop);
          launch.credentials.clear();
          launch.done.resolve();
        }
      },
    };
  } catch (error) { await fail(); throw error; }
}

export async function installChildSessionHost({ session, run }) {
  const launch = ownedLaunch(run);
  if (!launch.slot || launch.runId !== run.runId) throw new Error("The helper session host changed");
  launch.signal.throwIfAborted();
  await launch.slot.installSession(session);
  launch.slot.remember(launch.command);
  const cursor = await launch.slot.confirm(launch.signal);
  const receipt = { protocol: PROTOCOL, request_id: launch.command.request_id,
    payload_digest: launch.command.payload_digest, fence: launch.config.fence, status: "prepared",
    binding_id: launch.slot.binding.binding_id, sdk_session_id: session.sessionId,
    helper_run_id: run.runId, cursor, journal_confirmed: true, sdk_idle: !session.isStreaming,
    supported_business_contracts: ["challenge-board-v1"] };
  for (;;) {
    launch.signal.throwIfAborted();
    try { await launch.platform.acknowledge(receipt, launch.signal); break; }
    catch (error) {
      if (error.status && error.status < 500) throw error;
      await delay(100, undefined, { signal: launch.signal });
    }
  }
  launch.slot.assertActive();
}

export default async function parentPolicy(pi) {
  const { registerAgentViaEvents } = await import("pi-subagents/agents");
  const { registerRequiredChildExtensions } = await import("pi-subagents/required-child-extensions");
  const { registerSubagentCapabilityCeiling } = await import("pi-subagents/capability-ceiling");
  let host, ceiling, definition;
  pi.on("session_start", (_event, context) => {
    definition = registerAgentViaEvents({ pi, name: agent, definition: {
      description: "The platform's foreground helper", systemPrompt: "Follow the platform's scoped helper instructions.",
      tools: [], inheritSkills: false, inheritGlobalContext: false, inheritProjectContext: false,
      extensions: [], allowNestedSubagents: false, defaultContext: "fresh", defaultAsync: false,
    } });
    const sessionId = context.sessionManager.getSessionId();
    host = registerRequiredChildExtensions({ sessionId, requireForAllRunners: true,
      extensions: [{ id: "bbx.host", path: self, sessionHost: true, hosted: true }] });
    ceiling = registerSubagentCapabilityCeiling({ sessionId, source: "bbx-host",
      ceiling: { allowedAgents: [agent], allowedTools: [...tools], denyExtensions: false } });
  });
  pi.on("session_shutdown", () => { host?.dispose(); ceiling?.dispose(); definition?.dispose(); });
}

let sterileHome;
async function pluginPaths(config) {
  const home = join(config.journal_root, ".helper-home");
  if (sterileHome && sterileHome !== home) throw new Error("The helper host is scoped to one journal");
  if (!sterileHome) {
    const agentDir = join(home, "agent");
    const configDir = join(agentDir, "extensions", "subagent");
    const temp = join(home, "tmp");
    await Promise.all([mkdir(configDir, { recursive: true, mode: 0o700 }), mkdir(temp, { recursive: true, mode: 0o700 })]);
    await writeFile(join(agentDir, "settings.json"), JSON.stringify({
      subagents: { disableBuiltins: true, defaultExtensions: [] },
    }), { mode: 0o600 });
    await writeFile(join(configDir, "config.json"), JSON.stringify({
      toolActivation: "eager", defaultSubagentContext: "fresh", asyncByDefault: false, enableSlashCommands: false,
      scheduledRuns: { enabled: false }, autoResume: false, resultScanLogging: "off",
      asyncWidget: false, fleetView: false, programStatus: false, completionBatch: { enabled: false },
      intercomBridge: { mode: "off" },
    }), { mode: 0o600 });
    Object.assign(process.env, { HOME: home, USERPROFILE: home, PI_CODING_AGENT_DIR: agentDir, TMPDIR: temp,
      XDG_CONFIG_HOME: home, XDG_DATA_HOME: home, XDG_CACHE_HOME: home, PI_OFFLINE: "1" });
    sterileHome = home;
  }
  return [createRequire(import.meta.url).resolve("pi-subagents"), self];
}

export class ForegroundHelpers {
  constructor(config, platform) {
    this.config = config;
    this.platform = platform;
    this.active = new Map();
  }
  parentPaths() {
    if (this.config.helper_foreground !== true && this.config.helper_background !== true)
      throw new Error("The Task has no trusted helper profile");
    return this.paths ??= pluginPaths(this.config);
  }
  checkParentTools(parent) {
    const expected = [...parent.toolNames.keys(), ...(parent.spec.helper_foreground ? [name] : []),
      ...(parent.spec.helper_background ? [backgroundName, statusName, cancelName] : [])].sort();
    if (parent.session.getActiveToolNames().sort().join("\0") !== expected.join("\0"))
      throw new Error("The Worker exposed tools outside its frozen helper contract");
  }
  tool(parent, background = false) {
    const toolName = background ? backgroundName : name;
    return { name: toolName, label: background ? "Delegate one supervised background helper" : "Delegate one foreground helper",
      description: background ?
        "Register one helper for a local goal. It can continue in this Task Runner after your response, for up to 180 seconds." :
        "Use a spare seat for one local goal; runs in the foreground for at most 60 seconds.",
      parameters: { type: "object", additionalProperties: false, required: ["goal", "expected_output"],
        properties: { goal: { type: "string", minLength: 1, maxLength: 2000 },
          input_refs: { type: "array", maxItems: 8, items: { type: "string" } },
          expected_output: { type: "string", minLength: 1, maxLength: 1000 } } },
      execute: (callId, args, signal) => this.execute(parent, callId, args, signal, toolName) };
  }
  backgroundTools(parent) {
    return [this.tool(parent, true), ...[statusName, cancelName].map((toolName) => ({
      name: toolName, label: toolName === statusName ? "Read original background helper status" : "Request original helper cancellation",
      description: toolName === statusName ? "Read the confirmed status or saved result of your original helper." :
        "Request cancellation of your original helper. This does not confirm that it has stopped.",
      parameters: { type: "object", additionalProperties: false, required: ["child_run_id"],
        properties: { child_run_id: { type: "string" } } },
      execute: (callId, args, signal) => this.execute(parent, callId, args, signal, toolName),
    }))];
  }
  readResult(parent, invocation, receipt) {
    if (typeof receipt.result_json !== "string" || digest(receipt.result_json) !== receipt.result_digest)
      throw new Error("Original helper result is unresolved");
    const result = JSON.parse(receipt.result_json);
    if (!Array.isArray(result.content) || result.content.some((item) => item?.type !== "text" || typeof item.text !== "string"))
      throw new Error("Invalid saved helper result");
    parent.assertActive();
    parent.unresolvedInvocations.delete(invocation.invocation_id);
    return { content: result.content, details: { invocation_id: invocation.invocation_id, bbx_is_error: result.isError === true } };
  }
  async execute(parent, callId, args, toolSignal, toolName = name) {
    this.checkParentTools(parent);
    const invocation = await parent.toolInvocation(toolName, callId, args, toolSignal);
    parent.unresolvedInvocations.add(invocation.invocation_id);
    try {
      if (toolName === statusName || toolName === cancelName) {
        const action = toolName === statusName ? "model-status" : "model-cancel";
        const request = () => this.platform.helper(parent, invocation, action, undefined, toolSignal);
        let receipt;
        try { receipt = await request(); }
        catch (error) {
          if (toolSignal?.aborted || (error.status && error.status < 500)) throw error;
          receipt = await request();
        }
        validate("ToolResultReceipt", receipt);
        if (receipt.invocation_id !== invocation.invocation_id) throw new Error("Helper tool receipt changed invocation");
        return this.readResult(parent, invocation, receipt);
      }
      return await this.runClaim(parent, callId, invocation, toolSignal, toolName === backgroundName);
    }
    catch (error) { freezeSession(parent.session); throw error; }
  }
  async runClaim(parent, callId, invocation, toolSignal, background) {
    const send = (action, body, signal) => this.platform.helper(parent, invocation, action, body, signal);
    const read = (status) => {
      if (![background ? "accepted" : "saved", "unavailable"].includes(status.state) ||
          status.invocation_id !== invocation.invocation_id) throw new Error("Original helper result is unresolved");
      return this.readResult(parent, invocation, status);
    };
    const parentSignal = AbortSignal.any([parent.stopController.signal, ...(toolSignal ? [toolSignal] : [])]);
    let reserved;
    try { reserved = await send("reserve", undefined, parentSignal); }
    catch { return read(await send("status", undefined, parentSignal)); }
    if (reserved.state !== "reserved") return read(reserved);
    parentSignal.throwIfAborted();
    let grant;
    try { grant = await send("launch", undefined, parentSignal); }
    catch { return read(await send("status", undefined, parentSignal)); }
    if (grant.launch_allowed !== true) return read(grant);
    const command = decodeCommand(grant.command);
    const spec = command.payload.spec;
    const deadline = background ? grant.child_deadline_ms : grant.deadline_ms;
    if (this.stopping || grant.invocation_id !== invocation.invocation_id || !sameFence(command.fence, this.config.fence) ||
        command.payload.kind !== "prepare" || spec?.helper_foreground || spec?.helper_background || !spec?.helper_child ||
        (spec.helper_child.mode ?? "foreground") !== (background ? "background" : "foreground") ||
        spec.helper_child.parent_binding_id !== parent.binding.binding_id ||
        spec.helper_child.child_id !== grant.child_id || spec.binding.binding_id !== grant.binding_id ||
        spec.binding.binding_id === parent.binding.binding_id || spec.helper_child.deadline_ms !== deadline ||
        !spec.allowed_tools.every((tool) => tools.has(tool) && parent.toolNames.has(tool)) ||
        !sameValue(spec.model, parent.spec.model) ||
        this.active.has(grant.binding_id)) throw new Error("The helper launch grant did not match its parent");
    const now = Date.now();
    const remaining = deadline - now;
    const parentRemaining = grant.deadline_ms - now;
    if (!Number.isSafeInteger(deadline) || remaining <= 0 || remaining > (background ? 180000 : 60000) ||
        !Number.isSafeInteger(grant.deadline_ms) || parentRemaining <= 0 || parentRemaining > 60000)
      throw new Error("The helper launch deadline is invalid");
    const controller = new AbortController();
    const active = AbortSignal.any([controller.signal, background ? parent.stopController.signal : parentSignal,
      AbortSignal.timeout(remaining)]);
    const launch = { config: this.config, platform: this.platform, parent, invocation, command,
      credentials: new ChildCredentials(spec, this.config, grant.credentials),
      done: deferred(), controller, signal: active, background };
    if (background) launch.assertParent = () => {
      try { parent.assertActive(); }
      catch (error) { controller.abort(); throw error; }
    };
    this.active.set(grant.binding_id, launch);
    const cancel = () => {
      launch.slot?.stop();
      launch.cancelRequest ??= send("cancel", {}).catch(() => undefined);
    };
    active.addEventListener("abort", cancel, { once: true });
    try {
      parentSignal.throwIfAborted();
      active.throwIfAborted();
      const plugin = parent.session.getToolDefinition("subagent");
      if (!plugin) throw new Error("The required plugin tool is unavailable");
      let accepted;
      if (background) {
        const handoff = AbortSignal.any([parentSignal, AbortSignal.timeout(parentRemaining)]);
        const proof = { child_id: grant.child_id, binding_id: grant.binding_id, payload_digest: command.payload_digest };
        let status;
        try { status = await send("accepted", proof, handoff); }
        catch { status = await send("status", undefined, handoff); }
        if (status.state !== "accepted" || status.child_id !== grant.child_id || status.binding_id !== grant.binding_id)
          throw new Error("The original background helper acceptance is unconfirmed");
        accepted = read(status);
        active.throwIfAborted();
        launch.assertParent();
      }
      const toolContext = parent.session.extensionRunner.createToolContext(callId, undefined);
      launch.pluginWork = Promise.resolve().then(() => {
        active.throwIfAborted();
        return launches.run(launch, () => plugin.execute(callId, {
          agent, task: spec.instructions, context: "fresh", async: false, artifacts: false,
        }, active, undefined, toolContext));
      });
      const pluginFinished = () => {
        if (!launch.started) { launch.credentials.clear(); launch.done.resolve(); }
      };
      void launch.pluginWork.then(pluginFinished, pluginFinished);
      const save = async (output) => {
        await launch.done.promise;
        if (!launch.summary) throw new Error("The foreground helper exit is unresolved");
        const text = [...(output.content ?? []).filter((item) => item.type === "text").map((item) => item.text).join("\n")]
          .slice(0, 4000).join("").replace(/[\u0000-\u0008\u000b-\u001f]/g, " ");
        const result_json = JSON.stringify({ content: [{ type: "text", text }],
          ...(output.isError || active.aborted ? { isError: true } : {}) });
        try { return await send("result", { result_json }); }
        catch { return send("status"); }
      };
      launch.resultWork = launch.pluginWork.then(save, () => save({
        isError: true, content: [{ type: "text", text: "The helper ended without a successful result." }],
      }));
      if (background) {
        const cleanup = () => active.removeEventListener("abort", cancel);
        void launch.resultWork.then(cleanup, () => {
          if (!launch.summary) controller.abort();
          cleanup();
        });
        return accepted;
      }
      let status = await bounded(launch.resultWork, active);
      while (!["saved", "unavailable", "unknown"].includes(status.state)) {
        await delay(100, undefined, { signal: active });
        status = await send("status", undefined, active);
      }
      active.throwIfAborted();
      parent.assertActive();
      return read(status);
    } catch (error) {
      controller.abort();
      throw error;
    } finally {
      if (!background || !launch.resultWork) active.removeEventListener("abort", cancel);
      if (!launch.pluginWork) {
        if (active.aborted) cancel();
        launch.credentials.clear();
        launch.done.resolve();
      }
    }
  }
  renewal(bindingId) { return this.active.get(bindingId)?.slot; }
  supervise() {
    for (const launch of this.active.values()) {
      if (launch.localEnded || launch.signal.aborted || !launch.assertParent) continue;
      try { launch.assertParent(); } catch { /* Cancels the original background child. */ }
    }
  }
  async closeBinding(bindingId) {
    const launch = this.active.get(bindingId);
    if (!launch) return undefined;
    if (!launch.localEnded) launch.controller.abort();
    await launch.done.promise;
    if (launch.background) await Promise.allSettled([launch.pluginWork, launch.resultWork]);
    if (!launch.summary) {
      if (!launch.background) this.active.delete(bindingId);
      throw new Error("The original helper exit is unresolved");
    }
    this.active.delete(bindingId);
    return launch.summary;
  }
  async stopParent(bindingId) {
    const children = [...this.active.values()].filter((entry) => entry.parent.binding.binding_id === bindingId);
    for (const child of children) child.controller.abort();
    await Promise.allSettled(children.flatMap((child) => [child.done.promise, child.pluginWork, child.resultWork]));
  }
  cancelParent(bindingId) {
    for (const child of this.active.values()) {
      if (child.parent.binding.binding_id === bindingId) child.controller.abort();
    }
  }
  async stopAll() {
    this.stopping = true;
    const children = [...this.active.values()];
    for (const child of children) if (!child.localEnded) child.controller.abort();
    const background = children.filter((child) => child.background);
    await Promise.allSettled(background.flatMap((child) => [child.done.promise, child.pluginWork, child.resultWork]));
    return background.every((child) => Boolean(child.summary));
  }
}
