import { createHash, randomUUID } from "node:crypto";
import { stream as streamPiMessages } from "@earendil-works/pi-ai/api/pi-messages";

const frozenSessions = new WeakSet();

function stableCallId(parts) {
  const value = createHash("sha256").update(JSON.stringify(parts)).digest();
  value[6] = (value[6] & 0x0f) | 0x50;
  value[8] = (value[8] & 0x3f) | 0x80;
  const hex = value.subarray(0, 16).toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function freezeSession(session) { frozenSessions.add(session); }
export function assertSessionOpen(session) {
  if (!session || frozenSessions.has(session)) throw new Error("The session admission boundary is frozen");
}

const zeroUsage = () => ({
  input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
});

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
export function installModelBoundary(session, {
  gatewayUrl, credential, binding, confirm, admit, settle, createStream, prepareContext = async (context) => context,
  assertActive = () => {}, fetch: transport = fetch, nativeModelRevision, nativeProof,
}) {
  const base = new URL(gatewayUrl.endsWith("/") ? gatewayUrl : `${gatewayUrl}/`);
  if (!(base.protocol === "https:" || (base.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(base.hostname)))
      || base.username || base.password || base.search || base.hash) throw new Error("A restricted gateway URL is required");
  if (typeof credential !== "function" || typeof createStream !== "function" || !binding.taskId || !binding.agentId) throw new Error("A gateway identity and native stream factory are required");
  const direct = nativeModelRevision !== undefined && nativeModelRevision !== null;
  if (direct && (!Number.isSafeInteger(nativeModelRevision) || nativeModelRevision < 1 || typeof nativeProof !== "function")) {
    throw new Error("The frozen direct Pi model scope is incomplete");
  }
  const nativeEndpoint = new URL("messages", base);
  const original = session.agent.streamFunction;
  const selected = session.model;
  if (!selected || selected.api !== "openai-completions") throw new Error("The Runner requires its approved gateway model");
  const beforeToolCall = session.agent.beforeToolCall;
  session.agent.beforeToolCall = async (context) => {
    if (frozenSessions.has(session)) return { block: true, terminate: true, reason: "The session admission boundary is frozen" };
    const result = await beforeToolCall?.(context);
    return frozenSessions.has(session) ? { block: true, terminate: true, reason: "The session admission boundary is frozen" } : result;
  };
  session.settingsManager.setCacheWarmingMode("off");
  session.settingsManager.applyOverrides({ retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } } });

  session.agent.streamFunction = async (model, context, options = {}) => {
    const stream = createStream();
    const callId = randomUUID();
    let attempt;
    let settlementStarted = false;
    const finish = async (message) => {
      if (!attempt?.admitted || settlementStarted) return;
      settlementStarted = true;
      // This reports SDK behavior only. Native gateway records own fees and unknown accounting.
      const completed = !["error", "aborted"].includes(message.stopReason);
      await settle({
        ...attempt, binding, callId,
        outcome: completed ? "completed" : attempt.sent ? "unknown" : "not_sent",
        modelOutcome: completed ? "completed" : message.stopReason,
        usage: completed ? message.usage : null,
      });
    };
    try {
      assertSessionOpen(session);
      assertActive();
      if (options.signal?.aborted || model.provider !== selected.provider
          || model.id !== selected.id || model.api !== selected.api
          || new URL(model.baseUrl).href.replace(/\/$/, "") !== base.href.replace(/\/$/, "")) {
        throw new Error("The requested model is outside the gateway binding");
      }
      const preparedContext = await prepareContext(context, options);
      const journal = await confirm(options.signal);
      assertSessionOpen(session);
      assertActive();
      if (options.signal?.aborted) throw new Error("The request was aborted");
      const fetchOnce = async (input, init = {}) => {
        assertSessionOpen(session);
        assertActive();
        const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url);
        if (attempt || url.origin !== base.origin || !url.pathname.startsWith(base.pathname)) {
          throw new Error("Implicit retry or a gateway escape was refused");
        }
        attempt = { attemptId: randomUUID(), sent: false, admitted: false, httpStatus: null };
        const bodyJson = init.body;
        if (typeof bodyJson !== "string" || Buffer.byteLength(bodyJson) > 8 * 1024 * 1024
            || Buffer.from(bodyJson, "utf8").toString("utf8") !== bodyJson) throw new Error("The native model body is unavailable or exceeds its byte bound");
        const bodyDigest = createHash("sha256").update(bodyJson, "utf8").digest("hex");
        const request = {
          binding, callId, attemptId: attempt.attemptId, journal, model: selected.id,
          purpose: options.sessionId === session.sessionId ? "turn" : "summary", bodyJson, bodyDigest,
        };
        const ticket = await admit(request);
        if (ticket?.callId !== callId || ticket?.attemptId !== attempt.attemptId) throw new Error("Admission identity did not match");
        attempt.admitted = true;
        assertSessionOpen(session);
        assertActive();
        if (init.signal?.aborted || options.signal?.aborted) throw new Error("The request was aborted");
        const headers = new Headers({ "content-type": "application/json", accept: "text/event-stream" });
        const token = await credential();
        assertSessionOpen(session);
        assertActive();
        if (init.signal?.aborted || options.signal?.aborted) throw new Error("The request was aborted");
        headers.set("authorization", `Bearer ${token}`);
        headers.set("x-bbx-task-id", binding.taskId);
        headers.set("x-bbx-agent-id", binding.agentId);
        for (const [key, value] of Object.entries({ "binding-id": binding.bindingId, "runtime-epoch": binding.epoch, "runner-id": binding.runnerId, "pod-uid": binding.podUid, "sdk-session-id": session.sessionId })) {
          if (value !== undefined) headers.set(`x-bbx-${key}`, String(value));
        }
        headers.set("x-bbx-call-id", callId);
        headers.set("x-bbx-attempt-id", attempt.attemptId);
        headers.set("x-bbx-journal-hash", journal.hash);
        headers.set("x-bbx-body-digest", bodyDigest);
        attempt.sent = true;
        const response = await transport(url, { ...init, body: bodyJson, headers, redirect: "error" });
        attempt.httpStatus = response.status;
        return response;
      };
      let source;
      if (direct) {
        const token = await credential();
        const sdkModel = { ...selected, api: "pi-messages", baseUrl: gatewayUrl };
        const action = session.isCompacting ? "summary" : "turn";
        const proof = nativeProof(journal);
        const logicalCallId = createHash("sha256").update(JSON.stringify([
          "bbx-native-pi-v1", binding.taskId, proof.binding.platform_session_id,
          session.sessionId, journal.leaf_id ?? journal.hash, journal.seq, action,
        ])).digest("hex");
        const directCallId = stableCallId([logicalCallId]);
        const attemptId = stableCallId([directCallId, "provider-attempt-1"]);
        let used = false;
        source = streamPiMessages(sdkModel, preparedContext, {
          signal: options.signal,
          apiKey: token,
          env: {},
          reasoning: options.reasoning,
          maxTokens: Math.min(options.maxTokens ?? selected.maxTokens, selected.maxTokens),
          onPayload: (payload) => ({ ...payload, bbx: {
            task_id: binding.taskId, call_id: directCallId, attempt_id: attemptId,
            model_id: selected.id, model_revision: nativeModelRevision,
            logical_call_id: logicalCallId, action, proof,
          } }),
          fetch: async (input, init) => {
            assertSessionOpen(session);
            assertActive();
            init.signal?.throwIfAborted();
            if (used || new URL(input).href !== nativeEndpoint.href || init.method !== "POST" ||
                typeof init.body !== "string" || Buffer.byteLength(init.body) > 8 * 1024 * 1024) {
              throw new Error("A duplicate or unapproved private Pi request was refused");
            }
            used = true;
            return transport(nativeEndpoint, { ...init, redirect: "error" });
          },
        });
      } else {
        // The old frozen task still requires its original one-shot transport.
        source = await original({ ...selected, baseUrl: gatewayUrl }, preparedContext, {
          ...options, apiKey: undefined, headers: undefined, env: {}, maxRetries: 0,
          transport: "sse", fetch: fetchOnce, maxTokens: Math.min(options.maxTokens ?? selected.maxTokens, selected.maxTokens),
        });
      }
      void (async () => {
        try {
          let terminal = false;
          for await (const event of source) {
            if (direct) {
              assertSessionOpen(session);
              assertActive();
              const logical = event.partial ?? event.message ?? event.error;
              if (logical) {
                // The session journal keeps its frozen logical model; pi-messages is only private transport.
                Object.assign(logical, { api: selected.api, provider: selected.provider, model: selected.id });
                if (event.type === "error") logical.errorMessage = "The trusted Pi model result was not confirmed";
              }
              if (event.type === "error") freezeSession(session);
            }
            if (event.type === "done" || event.type === "error") {
              const message = event.type === "done" ? event.message : event.error;
              if (!direct) await finish(message);
              terminal = true;
            }
            stream.push(event);
          }
          const message = await source.result();
          if (!terminal) {
            if (direct) throw new Error("The trusted Pi model stream has no terminal event");
            await finish(message);
          }
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
