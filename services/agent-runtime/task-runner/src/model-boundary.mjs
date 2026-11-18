import { createHash } from "node:crypto";
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
  runtimeUrl, credential, binding, confirm, createStream, prepareContext = async (context) => context,
  assertActive = () => {}, fetch: transport = fetch, nativeModelRevision, nativeProof,
}) {
  const base = new URL(runtimeUrl.endsWith("/") ? runtimeUrl : `${runtimeUrl}/`);
  if (!(base.protocol === "https:" || (base.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(base.hostname)))
      || base.username || base.password || base.search || base.hash) throw new Error("A restricted Runtime URL is required");
  if (typeof credential !== "function" || typeof createStream !== "function" || !binding.taskId || !binding.agentId) throw new Error("A Runtime identity and native stream factory are required");
  if (!Number.isSafeInteger(nativeModelRevision) || nativeModelRevision < 1 || typeof nativeProof !== "function") {
    throw new Error("The frozen direct Pi model scope is incomplete");
  }
  const nativeEndpoint = new URL("messages", base);
  const selected = session.model;
  if (!selected || selected.api !== "openai-completions") throw new Error("The Runner requires its approved platform model");
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
    try {
      assertSessionOpen(session);
      assertActive();
      if (options.signal?.aborted || model.provider !== selected.provider
          || model.id !== selected.id || model.api !== selected.api
          || new URL(model.baseUrl).href.replace(/\/$/, "") !== base.href.replace(/\/$/, "")) {
        throw new Error("The requested model is outside the frozen platform binding");
      }
      const preparedContext = await prepareContext(context, options);
      const journal = await confirm(options.signal);
      assertSessionOpen(session);
      assertActive();
      if (options.signal?.aborted) throw new Error("The request was aborted");
      const token = await credential();
      const sdkModel = { ...selected, api: "pi-messages", baseUrl: runtimeUrl };
      const action = session.isCompacting ? "summary" : "turn";
      const proof = nativeProof(journal);
      const logicalCallId = createHash("sha256").update(JSON.stringify([
        "bbx-native-pi-v1", binding.taskId, proof.binding.platform_session_id,
        session.sessionId, journal.leaf_id ?? journal.hash, journal.seq, action,
      ])).digest("hex");
      const directCallId = stableCallId([logicalCallId]);
      const attemptId = stableCallId([directCallId, "provider-attempt-1"]);
      let used = false;
      const source = streamPiMessages(sdkModel, preparedContext, {
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
      void (async () => {
        try {
          let terminal = false;
          for await (const event of source) {
            assertSessionOpen(session);
            assertActive();
            const logical = event.partial ?? event.message ?? event.error;
            if (logical) {
              // The session journal keeps its frozen logical model.
              Object.assign(logical, { api: selected.api, provider: selected.provider, model: selected.id });
              if (event.type === "error") logical.errorMessage = "The trusted Pi model result was not confirmed";
            }
            if (event.type === "error") freezeSession(session);
            if (event.type === "done" || event.type === "error") terminal = true;
            stream.push(event);
          }
          const message = await source.result();
          if (!terminal) throw new Error("The trusted Pi model stream has no terminal event");
          stream.end(message);
        } catch {
          frozenSessions.add(session);
          pushFailure(stream, selected, options.signal);
        }
      })();
    } catch {
      frozenSessions.add(session);
      pushFailure(stream, selected, options.signal);
    }
    return stream;
  };
}
