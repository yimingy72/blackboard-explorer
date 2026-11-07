import { readFile } from "node:fs/promises";
import { PROTOCOL, digest, safeUrl, sameValue, validate } from "./protocol.mjs";

const pendingCredential = () => Object.assign(new Error("The scoped credential has not been projected"), { code: "credential_pending" });

/** The mount contains only this task's scoped credentials, never a platform service key. */
export class MountedCredentials {
  constructor(path, taskId) { this.path = path; this.taskId = taskId; }
  async #read() {
    let text;
    try { text = await readFile(this.path, "utf8"); }
    catch (error) { if (error.code === "ENOENT") throw pendingCredential(); throw error; }
    let records;
    try { records = JSON.parse(text); }
    catch { throw new Error("The credential mount is invalid"); }
    if (!records || typeof records !== "object" || Array.isArray(records)
        || Object.values(records).some((item) => !item || !["runner-control", "model", "mcp"].includes(item.audience)
          || item.task_id !== this.taskId)) throw new Error("The credential mount must contain only this task's scoped identities");
    return records;
  }
  #token(records, reference, audience, bindingId) {
    if (!["runner-control", "model", "mcp"].includes(audience)) throw new Error("The scoped credential is unavailable");
    if (!Object.hasOwn(records, reference)) return undefined;
    const credential = records[reference];
    if (credential.audience !== audience || credential.task_id !== this.taskId
        || (credential.binding_id ?? null) !== bindingId || typeof credential.expires_at !== "number"
        || !Number.isFinite(credential.expires_at) || credential.expires_at <= Date.now() / 1000
        || typeof credential.token !== "string" || !credential.token) throw new Error("The scoped credential is unavailable");
    return credential.token;
  }
  async resolve(reference, audience, bindingId = null) {
    const token = this.#token(await this.#read(), reference, audience, bindingId);
    if (token === undefined) throw pendingCredential();
    return token;
  }
  async requireBinding(spec) {
    const records = await this.#read();
    const id = spec.binding.binding_id;
    const model = this.#token(records, spec.model_credential_ref, "model", id);
    const mcp = this.#token(records, spec.mcp_credential_ref, "mcp", id);
    if (model === undefined || mcp === undefined) throw pendingCredential();
  }
}

/** Blackboard owns command/journal/tool receipts; the Runtime proxy owns model admission. */
export class HttpPlatform {
  constructor(config, credentials, transport = fetch) {
    this.config = config;
    this.credentials = credentials;
    this.transport = transport;
    this.control = safeUrl(config.control_url).href.replace(/\/$/, "");
    this.model = safeUrl(config.model_proxy_url).href.replace(/\/$/, "");
  }
  async request(base, path, { method = "POST", body, audience = "runner-control", reference = this.config.control_credential_ref,
    bindingId = null, signal, timeoutMs = 20000, missing = false, notModified = false } = {}) {
    const token = await this.credentials.resolve(reference, audience, bindingId);
    const response = await this.transport(`${base}${path}`, {
      method, redirect: "error", signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs),
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (missing && response.status === 404) return undefined;
    if (notModified && response.status === 304) return undefined;
    if (!response.ok) throw Object.assign(new Error(`Platform receipt refused (${response.status})`), { status: response.status });
    return response.json();
  }
  async poll(afterSeq, signal) {
    const query = new URLSearchParams({ after_seq: String(afterSeq), runner_id: this.config.fence.runner_id,
      epoch: String(this.config.fence.epoch), pod_uid: this.config.fence.pod_uid });
    return validate("RunnerPoll", await this.request(this.control, `/commands?${query}`, { method: "GET", signal }));
  }
  async acknowledge(receipt, signal) {
    validate("CommandReceipt", receipt);
    const ack = validate("OperationAck", await this.request(this.control, `/commands/${receipt.request_id}/receipts`, { body: receipt, signal }));
    if (ack.request_id !== receipt.request_id || ack.status !== receipt.status
        || ack.receipt_digest !== digest(JSON.stringify(receipt))) throw new Error("Command acknowledgement did not match");
  }
  async acquire(command, signal) {
    const body = { protocol: PROTOCOL, request_id: command.request_id, payload_digest: command.payload_digest,
      fence: command.fence, binding: command.payload.spec.binding };
    validate("SessionLeaseRequest", body);
    const receipt = validate("SessionLeaseReceipt", await this.request(this.control,
      `/bindings/${body.binding.binding_id}/acquire`, { body, signal }));
    if (receipt.request_id !== body.request_id || receipt.binding_id !== body.binding.binding_id
        || !sameValue(receipt.binding, body.binding)
        || !sameValue(receipt.fence, body.fence)) throw new Error("Session lease identity did not match");
    return receipt;
  }
  async confirmJournal(body, signal) {
    validate("JournalConfirmation", body);
    const receipt = validate("JournalReceipt", await this.request(this.control,
      `/bindings/${body.binding.binding_id}/journal/confirm`, { body, signal }));
    if (receipt.request_id !== body.request_id || receipt.binding_id !== body.binding.binding_id
        || receipt.sdk_session_id !== body.sdk_session_id || !sameValue(receipt.cursor, body.cursor)) {
      throw new Error("Journal confirmation did not match");
    }
    return receipt;
  }
  async publishedPrompt(slot, signal) {
    const known = slot.roleInstalled ? slot.promptRevision : 0;
    const raw = await this.request(this.control,
      `/bindings/${slot.binding.binding_id}/prompt?known_revision=${known}`, { method: "GET", signal, notModified: true });
    if (raw === undefined) return undefined;
    const prompt = validate("PublishedPrompt", raw);
    if (prompt.binding_id !== slot.binding.binding_id || !sameValue(prompt.fence, this.config.fence)
        || prompt.prompt_revision < slot.promptRevision) throw new Error("Published prompt identity or revision did not match");
    return prompt;
  }
  async admitTool(slot, body, signal) {
    validate("ToolInvocation", body);
    const receipt = validate("ToolPermit", await this.request(this.control,
      `/bindings/${body.binding.binding_id}/tools/admit`, { body, signal, audience: "mcp",
        reference: slot.spec.mcp_credential_ref, bindingId: body.binding.binding_id }));
    if (receipt.invocation_id !== body.invocation_id || receipt.arguments_digest !== body.arguments_digest) throw new Error("Tool admission identity did not match");
    return receipt;
  }
  async completeTool(slot, invocation, signal) {
    const body = validate("ToolResultConfirmation", { protocol: PROTOCOL, invocation });
    const receipt = validate("ToolResultReceipt", await this.request(this.control,
      `/bindings/${invocation.binding.binding_id}/tools/${invocation.invocation_id}/result`, {
        body, signal, audience: "mcp", reference: slot.spec.mcp_credential_ref, bindingId: invocation.binding.binding_id,
      }));
    if (receipt.invocation_id !== invocation.invocation_id || digest(receipt.result_json) !== receipt.result_digest) throw new Error("Tool result receipt did not match");
    return receipt;
  }
  async unknownTool(slot, invocation) {
    const body = validate("ToolUnknownSubmission", { protocol: PROTOCOL, invocation });
    const receipt = validate("ToolPermit", await this.request(this.control,
      `/bindings/${invocation.binding.binding_id}/tools/${invocation.invocation_id}/unknown`, {
        body, timeoutMs: 2000, audience: "mcp", reference: slot.spec.mcp_credential_ref, bindingId: invocation.binding.binding_id,
      }));
    if (receipt.invocation_id !== invocation.invocation_id || receipt.arguments_digest !== invocation.arguments_digest
        || receipt.disposition === "admitted") throw new Error("The unknown tool receipt did not match");
  }
  async admitModel(slot, request) {
    const body = validate("ModelAttemptRequest", { protocol: PROTOCOL, fence: this.config.fence, binding: slot.binding,
      sdk_session_id: slot.session.sessionId, call_id: request.callId, attempt_id: request.attemptId,
      journal: request.journal, purpose: request.purpose, profile_revision: slot.spec.profile_revision,
      prompt_revision: slot.promptRevision, model_snapshot: slot.spec.model,
      body_json: request.bodyJson, body_digest: request.bodyDigest });
    const receipt = validate("ModelAdmissionReceipt", await this.request(this.model, "/attempts/admit", {
      audience: "model", reference: slot.spec.model_credential_ref, bindingId: slot.binding.binding_id,
      body,
    }));
    if (receipt.call_id !== request.callId || receipt.attempt_id !== request.attemptId
        || receipt.request_digest !== digest(JSON.stringify(body))) throw new Error("Model admission receipt did not match");
    return { callId: request.callId, attemptId: request.attemptId };
  }
  async settleModel(slot, receipt) {
    const body = validate("ModelOutcomeSubmission", { protocol: PROTOCOL, fence: this.config.fence,
      binding_id: slot.binding.binding_id, sdk_session_id: slot.session.sessionId, call_id: receipt.callId,
      attempt_id: receipt.attemptId, outcome: receipt.outcome, model_outcome: receipt.modelOutcome,
      usage: receipt.usage, http_status: receipt.httpStatus });
    const ack = validate("ModelOutcomeReceipt", await this.request(this.model, `/attempts/${receipt.attemptId}/receipt`, {
      audience: "model", reference: slot.spec.model_credential_ref, bindingId: slot.binding.binding_id,
      body,
    }));
    if (ack.attempt_id !== receipt.attemptId || ack.request_digest !== digest(JSON.stringify(body))) throw new Error("Model settlement receipt did not match");
  }
}
