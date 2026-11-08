import { PROTOCOL, SUPPORTED_BUSINESS_CONTRACTS, decodeCommand, sameFence, sameValue, validate } from "./protocol.mjs";
import { RunnerSession } from "./session.mjs";
import { ForegroundHelpers } from "./helper-host.mjs";

const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

/** A finite command consumer. Pi alone owns the model/tool loop. */
export class TaskRunner {
  constructor(config, platform, credentials) {
    this.config = config;
    this.platform = platform;
    this.credentials = credentials;
    this.helpers = new ForegroundHelpers(config, platform);
    this.sessions = new Map();
    this.preparing = new Map();
    this.preparingSlots = new Map();
    this.closedBindings = new Map();
    this.operations = new Map();
    this.pendingReceipts = new Map();
    this.abortedBindings = new Set();
    this.work = new Set();
    this.controller = new AbortController();
    this.afterSeq = 0;
  }
  receipt(command, status, extra = {}) {
    return { protocol: PROTOCOL, request_id: command.request_id, payload_digest: command.payload_digest,
      fence: this.config.fence, status, ...extra };
  }
  enqueue(receipt) {
    validate("CommandReceipt", receipt);
    if (receipt.status === "accepted" && this.operations.get(receipt.request_id)?.receipt?.status === "applied") return;
    this.pendingReceipts.set(receipt.request_id, receipt);
    const operation = this.operations.get(receipt.request_id);
    if (operation) operation.receipt = receipt;
  }
  applications(slot, cursor) {
    for (const applied of slot.journal.inputs.values()) {
      if (applied.seq > cursor.seq) continue;
      for (const intent of slot.journal.commands.values()) if (intent.kind === "input" && intent.input_id === applied.input_id) {
        const known = this.operations.get(intent.request_id);
        if (known?.receipt?.status === "applied") continue;
        if (!known) this.operations.set(intent.request_id, { digest: intent.payload_digest });
        this.enqueue({ protocol: PROTOCOL, request_id: intent.request_id, payload_digest: intent.payload_digest,
          fence: this.config.fence, status: "applied", binding_id: slot.binding.binding_id,
          sdk_session_id: slot.session.sessionId, input_id: applied.input_id, cursor });
      }
    }
  }
  dispatch(command) {
    command = decodeCommand(command);
    if (!sameFence(command.fence, this.config.fence)) {
      this.pendingReceipts.set(command.request_id, this.receipt(command, "rejected", { error_code: "stale_fence" }));
      return;
    }
    const bindingId = command.payload.kind === "prepare" ? command.payload.spec.binding.binding_id : command.payload.binding_id;
    const known = this.operations.get(command.request_id);
    if (known) {
      if (known.digest !== command.payload_digest) {
        this.pendingReceipts.set(command.request_id, this.receipt(command, "rejected", { error_code: "request_conflict" }));
      } else if (command.payload.kind !== "close" && this.closedBindings.has(bindingId)) {
        this.enqueue(this.receipt(command, "rejected", { binding_id: bindingId, error_code: "binding_unavailable" }));
      } else if (known.receipt) this.enqueue(known.receipt);
      return;
    }
    this.operations.set(command.request_id, { digest: command.payload_digest });
    const pending = this.handle(command).catch((error) => {
      this.enqueue(this.receipt(command, "rejected", { error_code: error.code === "unsupported_model" ? "unsupported_model" : "boundary_frozen" }));
    });
    this.work.add(pending);
    void pending.finally(() => this.work.delete(pending));
  }
  async handle(command) {
    const payload = command.payload;
    if (payload.kind === "prepare") {
      if (payload.spec.helper_child) throw new Error("The plugin owns helper preparation");
      if (payload.spec.helper_foreground && this.config.helper_foreground !== true)
        throw new Error("The Task has no trusted foreground helper profile");
      if (payload.spec.helper_background && this.config.helper_background !== true)
        throw new Error("The Task has no trusted background helper profile");
      const id = payload.spec.binding.binding_id;
      this.controller.signal.throwIfAborted();
      if (this.closedBindings.has(id) || this.abortedBindings.has(id)) throw new Error("The binding is closed");
      let slot = this.sessions.get(id);
      let preparing = this.preparing.get(id);
      if (!slot && !preparing) {
        preparing = (async () => {
          const lease = await this.platform.acquire(command, this.controller.signal);
          const marker = this.closedBindings.get(id);
          if (marker) {
            marker.identity = { binding: payload.spec.binding, request_id: lease.request_id, sdk_session_id: lease.sdk_session_id ?? null };
            marker.summary = { ...marker.summary, sdk_session_id: lease.sdk_session_id ?? null, cursor: lease.cursor ?? null };
            throw new Error("The binding was closed during prepare");
          }
          this.controller.signal.throwIfAborted();
          if (this.abortedBindings.has(id)) throw new Error("The binding was aborted during prepare");
          const slot = new RunnerSession(this.config, payload.spec, command.payload_digest, lease, this.platform, this.credentials,
            (slot, cursor) => this.applications(slot, cursor), this.helpers);
          this.preparingSlots.set(id, slot);
          try {
            await slot.initialize();
            if (this.closedBindings.has(id) || this.abortedBindings.has(id) || this.controller.signal.aborted) throw new Error("The binding was cancelled during prepare");
            this.sessions.set(id, slot);
            return slot;
          } catch (error) {
            const marker = this.closedBindings.get(id);
            try {
              const summary = await slot.close();
              if (marker) Object.assign(marker, { summary, identity: { binding: slot.binding,
                request_id: slot.lease.request_id, sdk_session_id: summary.sdk_session_id } });
            } catch (cleanupError) {
              if (marker) Object.assign(marker, { summary: cleanupError.closeSummary ?? marker.summary, failed: true });
            }
            throw error;
          }
          finally { this.preparingSlots.delete(id); }
        })();
        this.preparing.set(id, preparing);
      }
      if (!slot) {
        try { slot = await preparing; }
        finally { if (this.preparing.get(id) === preparing) this.preparing.delete(id); }
      }
      if (this.closedBindings.has(id)) throw new Error("The binding is closed");
      if (slot.specDigest !== command.payload_digest) throw new Error("request_conflict");
      slot.remember(command);
      const cursor = await slot.confirm();
      slot.assertActive();
      this.enqueue(this.receipt(command, "prepared", { binding_id: id, sdk_session_id: slot.session.sessionId, cursor,
        journal_confirmed: true, sdk_idle: !slot.session.isStreaming, supported_business_contracts: [...SUPPORTED_BUSINESS_CONTRACTS] }));
      return;
    }
    if (payload.kind === "shutdown") {
      const known = await this.stop();
      this.enqueue(known ? this.receipt(command, "closed") :
        this.receipt(command, "rejected", { error_code: "boundary_frozen" }));
      return;
    }
    if (payload.kind === "close") {
      const id = payload.binding_id;
      let marker = this.closedBindings.get(id);
      if (!marker) {
        marker = { summary: { sdk_idle: true, journal_confirmed: false } };
        this.closedBindings.set(id, marker);
        const preparing = this.preparing.get(id);
        const slot = this.sessions.get(id) ?? this.preparingSlots.get(id);
        slot?.stop();
        if (slot) marker.identity = { binding: slot.binding, request_id: slot.lease.request_id,
          sdk_session_id: slot.session?.sessionId ?? slot.lease.sdk_session_id ?? null };
        marker.pending = (async () => {
          try {
            const helper = await this.helpers.closeBinding(id);
            if (helper) marker.summary = helper;
            if (preparing) await preparing.catch(() => {});
            if (slot) {
              marker.summary = await slot.close();
              marker.identity.sdk_session_id = marker.summary.sdk_session_id;
            }
          } catch (error) { marker.summary = error.closeSummary ?? marker.summary; marker.failed = true; }
          finally {
            this.sessions.delete(id); this.preparing.delete(id); this.preparingSlots.delete(id);
            this.abortedBindings.delete(id);
          }
        })();
      }
      try { await marker.pending; } finally { delete marker.pending; }
      this.enqueue(this.receipt(command, marker.failed ? "rejected" : "closed", { binding_id: id, ...marker.summary,
        ...(marker.failed ? { error_code: "boundary_frozen" } : {}) }));
      return;
    }
    if (this.closedBindings.has(payload.binding_id)) {
      this.enqueue(this.receipt(command, "rejected", { binding_id: payload.binding_id, error_code: "binding_unavailable" }));
      return;
    }
    if (payload.kind === "abort") {
      this.abortedBindings.add(payload.binding_id);
      this.preparingSlots.get(payload.binding_id)?.stop();
      const helper = await this.helpers.closeBinding(payload.binding_id);
      if (helper) {
        this.enqueue(this.receipt(command, "aborted", { binding_id: payload.binding_id, ...helper }));
        return;
      }
    }
    const slot = this.sessions.get(payload.binding_id) ?? await this.preparing.get(payload.binding_id);
    if (!slot) {
      this.enqueue(this.receipt(command, "rejected", { binding_id: payload.binding_id, error_code: "binding_unavailable" }));
      return;
    }
    if (payload.kind === "abort") {
      const cursor = await slot.abort();
      this.enqueue(this.receipt(command, "aborted", { binding_id: payload.binding_id, sdk_session_id: slot.session.sessionId,
        sdk_idle: !slot.session.isStreaming, journal_confirmed: !slot.journalPending, cursor,
        unresolved_attempts: [...slot.unresolvedAttempts], unresolved_invocations: [...slot.unresolvedInvocations] }));
      return;
    }
    if (this.abortedBindings.has(payload.binding_id)) throw new Error("The binding is frozen");
    slot.assertActive();
    slot.remember(command);
    await slot.confirm();
    slot.input(command);
    const native = slot.journal.inputs.get(payload.input_id);
    this.enqueue(this.receipt(command, native && native.seq <= slot.journal.cursor.seq ? "applied" : "accepted", {
      binding_id: payload.binding_id, sdk_session_id: slot.session.sessionId, input_id: payload.input_id,
      ...(native ? { cursor: slot.journal.cursor } : {}),
    }));
  }
  async flushReceipts(signal = this.controller.signal) {
    for (const [id, receipt] of this.pendingReceipts) {
      if (signal.aborted) break;
      try {
        await this.platform.acknowledge(receipt, signal);
        if (this.pendingReceipts.get(id) === receipt) this.pendingReceipts.delete(id);
      } catch { /* Explicitly resend this same idempotent receipt while control polling continues. */ }
    }
  }
  async run() {
    if (this.config.helper_foreground === true || this.config.helper_background === true) await this.helpers.parentPaths();
    const flusher = (async () => {
      while (!this.controller.signal.aborted) { await this.flushReceipts(); await pause(50); }
    })();
    try {
      while (!this.controller.signal.aborted) {
        try {
          this.helpers.supervise();
          const batch = await this.platform.poll(this.afterSeq, this.controller.signal);
          for (const receipt of batch.lease_renewals ?? []) {
            if (!sameFence(receipt.fence, this.config.fence)) throw Object.assign(new Error("Lease renewal fence did not match"), { code: "invalid_protocol" });
            const closed = this.closedBindings.get(receipt.binding_id);
            if (closed) {
              const identity = closed.identity;
              if (identity && (!sameValue(receipt.binding, identity.binding) || receipt.request_id !== identity.request_id
                  || (receipt.sdk_session_id != null && receipt.sdk_session_id !== identity.sdk_session_id))) {
                throw Object.assign(new Error("Closed binding renewal identity did not match"), { code: "invalid_protocol" });
              }
              continue;
            }
            const slot = this.sessions.get(receipt.binding_id) ?? this.preparingSlots.get(receipt.binding_id) ??
              this.helpers.renewal(receipt.binding_id);
            if (slot) {
              try { slot.renew(receipt); }
              catch (error) { throw Object.assign(error, { code: "invalid_protocol" }); }
            }
          }
          this.helpers.supervise();
          for (const command of batch.commands) {
            this.dispatch(command);
            this.afterSeq = Math.max(this.afterSeq, command.command_seq);
          }
        } catch (error) {
          if ([401, 403, 409].includes(error.status) || error.code === "invalid_protocol") { await this.stop(); throw error; }
          if (!this.controller.signal.aborted) await pause(100);
        }
      }
    } finally {
      await flusher;
      await Promise.allSettled([...this.work]);
      await this.flushReceipts(AbortSignal.timeout(2000));
      await Promise.allSettled([...this.sessions.values()].map((slot) => slot.close()));
    }
  }
  stop() {
    return this.stopping ??= (async () => {
      this.controller.abort();
      const children = this.helpers.stopAll();
      for (const slot of this.preparingSlots.values()) slot.stop();
      for (const slot of this.sessions.values()) await slot.abort().catch(() => {});
      return children;
    })();
  }
}
