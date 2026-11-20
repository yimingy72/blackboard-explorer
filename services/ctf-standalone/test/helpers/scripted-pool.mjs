import { randomUUID } from "node:crypto";

export class ScriptedPool {
  static async create(options, script = null) {
    return new ScriptedPool(options, script);
  }

  constructor(options, script = null) {
    this.options = options;
    this.script = script;
    this.records = new Map();
    this.notifications = [];
    this.prompts = [];
    this.abortCalls = [];
    this.jobs = new Set();
    this.closed = false;
    this.errors = [];
  }

  async ensure(id, role = "worker") {
    if (this.closed) throw new Error("Pool is closed");
    if (!this.records.has(id)) {
      this.records.set(id, { id, role, sessionId: randomUUID(), idle: true, model_requests: 0,
        model_inflight: 0, tool_inflight: 0, active_processes: [], unresolved: [], error: null,
        jobs: new Set(), tail: Promise.resolve(), notices: new Set(), versions: new Map() });
    }
    return { ...this.records.get(id) };
  }

  status() { return [...this.records.values()].map((record) => ({ ...record })); }

  async tool(id, name, args, { callId = randomUUID(), context = this.options.getContext(id) } = {}) {
    const record = this.records.get(id);
    const definition = this.options.toolsForRole(record.role, id).find((entry) => entry.name === name);
    if (!definition) throw new Error(`No tool ${name} for ${id}`);
    return definition.execute(args, { agentId: id, role: record.role, context, callId, signal: new AbortController().signal });
  }

  async begin(id, context = this.options.getContext(id)) {
    const record = this.records.get(id);
    record.idle = false;
    record.model_requests += 1;
    record.model_inflight = 1;
    record.context = structuredClone(context);
    await this.options.onEvent?.({ type: "model_start", agentId: id, context });
  }

  async idle(id, context = this.records.get(id).context) {
    const record = this.records.get(id);
    record.idle = true;
    record.model_inflight = 0;
    await this.options.onEvent?.({ type: "idle", agentId: id, context, settled: true });
  }

  async error(id, message, context = this.records.get(id).context) {
    const record = this.records.get(id);
    record.idle = true;
    record.model_inflight = 0;
    record.error = message;
    await this.options.onEvent?.({ type: "error", agentId: id, error: message, context, settled: true });
  }

  async #invoke(id, message, notification) {
    if (this.closed) return;
    const context = structuredClone(this.options.getContext(id));
    await this.begin(id, context);
    try {
      await this.script?.({ id, role: this.records.get(id).role, message, notification, context, pool: this });
      await this.idle(id, context);
    } catch (error) {
      this.errors.push(error);
      await this.error(id, error.message, context);
    }
  }

  prompt(id, text) {
    this.prompts.push({ id, text });
    return this.#schedule(id, text, null);
  }

  #schedule(id, text, notice) {
    const record = this.records.get(id);
    const promise = record.tail.then(() => new Promise((resolve) => setImmediate(resolve)))
      .then(() => this.#invoke(id, text, notice));
    record.tail = promise.catch(() => {});
    this.jobs.add(promise);
    record.jobs.add(promise);
    promise.finally(() => { this.jobs.delete(promise); record.jobs.delete(promise); });
    return promise;
  }

  async notify(id, text, options = {}) {
    if (this.closed) throw new Error("Pool is closed");
    const record = this.records.get(id);
    const { eventId, version = 0, scope = "global" } = options;
    if (version < (record.versions.get(scope) ?? 0)) return { delivered: false, stale: true };
    if (eventId && record.notices.has(eventId)) return { delivered: true, duplicate: true };
    if (eventId) record.notices.add(eventId);
    record.versions.set(scope, version);
    const notice = { id, text, ...options };
    this.notifications.push(notice);
    if (this.script) this.#schedule(id, text, notice);
    return { delivered: true, eventId: options.eventId };
  }

  async abort(id) {
    this.abortCalls.push(id);
    const record = this.records.get(id);
    if (!record.idle) await this.idle(id);
    return { ...record };
  }

  async waitIdle(id) {
    const record = this.records.get(id);
    while (record.jobs.size) await Promise.allSettled([...record.jobs]);
    return { ...record };
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    await Promise.allSettled([...this.records.keys()].map((id) => this.abort(id)));
    await Promise.allSettled([...this.jobs]);
  }
}
