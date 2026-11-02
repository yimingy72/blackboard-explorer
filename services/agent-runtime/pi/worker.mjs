import { createInterface } from "node:readline";
import { AsyncLocalStorage } from "node:async_hooks";
import { once } from "node:events";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runAgentLoop } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { createModels, createProvider } from "@earendil-works/pi-ai/models";
import { InMemoryTelemetryContext } from "@earendil-works/pi-telemetry";
import { createParser } from "eventsource-parser";

// Google ignores onResponse and rejects custom fetch. Observe headers in its
// native SDK path without consuming the body or mixing concurrent requests.
const responseObservers = new AsyncLocalStorage();
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (...args) => {
  const response = await nativeFetch(...args);
  responseObservers.getStore()?.(response);
  return response;
};

const callbacks = new Map();
const jobs = new Map();
let ordinal = 0;
const versions = {};
for (const packageName of ["pi-agent-core", "pi-ai", "pi-telemetry"]) {
  const entry = import.meta.resolve(`@earendil-works/${packageName}`);
  versions[packageName] = JSON.parse(await readFile(new URL("../package.json", entry), "utf8")).version;
}

async function send(frame) {
  if (!process.stdout.write(`${JSON.stringify(frame)}\n`)) await once(process.stdout, "drain");
}

async function callback(job, method, params) {
  if (job.failure) throw job.failure;
  const id = `callback-${++ordinal}`;
  const result = new Promise((resolve, reject) => callbacks.set(id, { resolve, reject, job }));
  await send({ type: "callback", id, run_id: job.id, method, params });
  return result;
}

const zeroUsage = () => ({
  input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
});

// The native loop owns validation, tool ordering, results and continuation.
// Python callbacks contain platform admission, persistence and business tools.
async function runAgent(job, params) {
  const model = params.model ?? {
    id: "scripted", name: "scripted", provider: "bbx", api: "openai-completions",
    baseUrl: "http://localhost", input: ["text", "image"], reasoning: true,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 1048576, maxTokens: 65536,
  };
  const tools = params.tools.map((definition) => ({
    ...definition, label: definition.name, replay: "never",
    async execute(callId, args) {
      executed.add(callId);
      try {
        return await callback(job, "tool", { call_id: callId, name: definition.name, arguments: args });
      } catch (error) {
        job.failure ??= error;
        job.controller.abort();
        throw error;
      }
    },
  }));
  const executed = new Set();
  const toolArguments = new Map();
  let steps = 0;
  const streamFn = async () => {
    const stream = createAssistantMessageEventStream();
    // The production callback uses pi-ai below. Scripted tests substitute only
    // that model boundary, while this same native Pi loop still runs the tools.
    void (async () => {
      try {
        if (++steps > params.max_iterations) throw new Error("Agent iteration limit reached");
        const message = await callback(job, "model", {});
        stream.push({ type: "start", partial: { ...message, content: [] } });
        stream.push({ type: "done", reason: message.stopReason, message });
        stream.end(message);
      } catch (error) {
        job.failure ??= error;
        const message = {
          role: "assistant", content: [], api: model.api, provider: model.provider,
          model: model.id, usage: zeroUsage(), stopReason: "error", timestamp: Date.now(),
          errorMessage: "Platform model boundary stopped the run",
        };
        stream.push({ type: "error", reason: "error", error: message });
        stream.end(message);
      }
    })();
    return stream;
  };
  const messages = await runAgentLoop(
    [], { messages: [], tools },
    {
      model, convertToLlm: (messages) => messages,
      toolExecution: params.parallel ? "parallel" : "sequential",
      beforeToolCall: async () => {
        if (job.failure || job.controller.signal.aborted) return { block: true, terminate: true };
      },
      finishTurn: async (turn) => {
        if (job.failure) return { action: "end" };
        if (params.follow_up && turn.message.stopReason === "stop"
            && await callback(job, "should_continue", {})) return { action: "continue" };
      },
    },
    async (event) => {
      if (job.failure) return;
      if (event.type === "tool_execution_start") toolArguments.set(event.toolCallId, event.args);
      if (event.type === "tool_execution_end" && event.isError && !executed.has(event.toolCallId)
          && !job.controller.signal.aborted && tools.some((tool) => tool.name === event.toolName)) {
        // Pi validates schemas. The platform adds bounded field guidance and
        // audits the refusal, without sending argument values back as errors.
        const result = await callback(job, "invalid_tool", {
          call_id: event.toolCallId, name: event.toolName,
          arguments: toolArguments.get(event.toolCallId),
        });
        Object.assign(event.result, result);
      }
      if (event.type === "message_end" && event.message.role === "toolResult") {
        await callback(job, "tool_message", { message: event.message });
      }
    },
    job.controller.signal,
    streamFn,
  );
  if (job.failure) throw job.failure;
  return { native_engine: "pi-agent-core", steps };
}

const apiModules = {
  "openai-completions": "openai-completions",
  "openai-responses": "openai-responses",
  "azure-openai-responses": "azure-openai-responses",
  "anthropic-messages": "anthropic-messages",
  "google-generative-ai": "google-generative-ai",
  "google-vertex": "google-vertex",
  "bedrock-converse-stream": "bedrock-converse-stream",
  "mistral-conversations": "mistral-conversations",
};

async function streamModel(job, params) {
  const { model } = params;
  const module = apiModules[model.api];
  if (!module) throw new Error("Unsupported Pi model API");
  const api = await import(`@earendil-works/pi-ai/api/${module}`);
  const scopedCredentials = ["bedrock-converse-stream", "google-vertex"].includes(model.api);
  const models = createModels({ identity: { attribution: { name: "blackboard-explorer" } } });
  models.setProvider(createProvider({
    id: model.provider, baseUrl: model.baseUrl, models: [model], api,
    auth: { apiKey: { resolve: async () => ({
      auth: scopedCredentials ? {} : { apiKey: params.api_key || "not-required" }, source: "platform",
    }) } },
  }));
  const telemetry = new InMemoryTelemetryContext();
  let temporary;
  if (params.adapter === "gemini_vertex") {
    temporary = await mkdtemp(join(tmpdir(), "bbx-pi-google-"));
    const path = join(temporary, "service-account.json");
    await writeFile(path, params.credentials.service_account_json, { mode: 0o600 });
    Object.assign(params.options.env, {
      GOOGLE_APPLICATION_CREDENTIALS: path,
      GOOGLE_CLOUD_PROJECT: params.provider_options.project,
      GOOGLE_CLOUD_LOCATION: params.provider_options.location,
    });
  }
  try {
  const result = await telemetry.startSpan({ name: "bbx.pi.model", attributes: { "gen_ai.request.model": model.id } }, async (span) => {
    let status;
    let requestId;
    let inputObserved = false;
    let outputObserved = false;
    const terminal = { prepared: false };
    const observeHeaders = (response) => {
      status = response.status;
      const headers = response.headers;
      const header = (name) => typeof headers?.get === "function" ? headers.get(name) : headers?.[name];
      requestId = header("x-request-id") ?? header("request-id");
      terminal.retry_after = header("retry-after");
    };
    const observeResponse = async (response) => {
      observeHeaders(response);
      if (!response.ok) {
        try {
          const body = await response.clone().json();
          const error = body.error ?? body;
          terminal.code = error.code;
          terminal.request_id = error.request_id ?? body.request_id;
        } catch { /* An opaque response has no additional safe diagnostics. */ }
      }
      if (response.body && response.headers.get("content-type")?.includes("text/event-stream")) {
        const decoder = new TextDecoder();
        let parser = createParser({
          maxBufferSize: 65536,
          onEvent(event) {
            try {
              const body = JSON.parse(event.data);
              const error = body.error ?? (body.type === "error" ? body : null);
              if (error) {
                terminal.event_type = "error";
                terminal.code = error.code;
                terminal.request_id = error.request_id ?? body.request_id;
              }
            } catch { /* Only structured error identifiers are observed. */ }
          },
          onError(error) {
            if (error.type === "max-buffer-size-exceeded") parser = null;
          },
        });
        // Observe SDK-preempted SSE errors without interpreting model content or
        // changing a single byte. The parser has a fixed bound and can drop out.
        return new Response(response.body.pipeThrough(new TransformStream({
          transform(chunk, controller) {
            try { parser?.feed(decoder.decode(chunk, { stream: true })); }
            catch { parser = null; }
            controller.enqueue(chunk);
          },
        })), { status: response.status, headers: response.headers });
      }
      return response;
    };
    const transport = async (url, init) => {
      const headers = new Headers(init?.headers);
      if (params.adapter === "azure_openai_chat" || params.adapter === "anthropic_foundry") {
        headers.set("api-key", params.api_key);
        headers.delete("authorization");
        headers.delete("x-api-key");
      }
      if (params.adapter === "azure_openai_chat") {
        url = new URL(url);
        url.searchParams.set("api-version", params.provider_options.api_version);
      }
      if (params.adapter === "anthropic_vertex") {
        const body = JSON.parse(init.body);
        delete body.model;
        body.anthropic_version = "vertex-2023-10-16";
        if (headers.has("anthropic-beta")) {
          body.anthropic_beta = headers.get("anthropic-beta").split(",").map((value) => value.trim());
          headers.delete("anthropic-beta");
        }
        url = `https://${params.provider_options.location}-aiplatform.googleapis.com/v1/projects/${encodeURIComponent(params.provider_options.project)}/locations/${encodeURIComponent(params.provider_options.location)}/publishers/anthropic/models/${encodeURIComponent(model.id)}:streamRawPredict`;
        init = { ...init, body: JSON.stringify(body) };
        headers.set("authorization", `Bearer ${params.credentials.access_token}`);
        headers.delete("x-api-key");
      }
      if (params.custom_transport) {
        const reply = await callback(job, "http", {
          url: String(url), method: init?.method ?? "POST",
          headers: Object.fromEntries(headers), body: init?.body ?? null,
        });
        return observeResponse(new Response(Buffer.from(reply.body, "base64"), {
          status: reply.status, headers: reply.headers,
        }));
      }
      return observeResponse(await fetch(url, { ...init, headers }));
    };
    const stream = responseObservers.run(
      ["google-generative-ai", "google-vertex"].includes(model.api) ? observeHeaders : undefined,
      () => models.streamSimple(model, params.context, {
        ...params.options, apiKey: scopedCredentials ? undefined : params.api_key || "not-required",
        telemetry: span, signal: job.controller.signal,
        onPayload: async () => {
          terminal.prepared = true;
          await callback(job, "provider_ready", {});
        },
        ...(["bedrock-converse-stream", "google-generative-ai", "google-vertex"].includes(model.api)
          ? {} : { fetch: transport }),
        onResponse: observeHeaders,
        onProviderStreamEvent(event) {
          if (typeof event?.id === "string") terminal.response_id = event.id;
          const usage = event?.usage ?? event?.response?.usage ?? event?.message?.usage
            ?? event?.usageMetadata ?? event?.metadata?.usage;
          if (usage) {
            inputObserved ||= [usage.prompt_tokens, usage.input_tokens, usage.promptTokenCount, usage.inputTokens]
              .some((value) => Number.isSafeInteger(value) && value >= 0);
            outputObserved ||= [usage.completion_tokens, usage.output_tokens, usage.candidatesTokenCount, usage.outputTokens]
              .some((value) => Number.isSafeInteger(value) && value >= 0);
          }
          if (["response.incomplete", "response.failed", "error"].includes(event?.type)) {
            terminal.event_type = event.type;
            terminal.code = event.code ?? event.response?.error?.code;
            terminal.incomplete_reason = event.response?.incomplete_details?.reason;
            terminal.response_id = event.response?.id ?? terminal.response_id;
            const usage = event.response?.usage;
            if (usage && event.response.status === event.type.slice("response.".length)) {
              terminal.usage_details = {
                input_token_count: usage.input_tokens, output_token_count: usage.output_tokens,
                total_token_count: usage.total_tokens,
                cache_read_input_token_count: usage.input_tokens_details?.cached_tokens ?? 0,
                reasoning_output_token_count: usage.output_tokens_details?.reasoning_tokens ?? 0,
              };
            }
          }
        },
      }),
    );
    for await (const event of stream) {
      // Usage and the terminal message arrive once in the final response. Do
      // not repeatedly serialize the growing partial transcript for each token.
      if (event.type === "text_delta" || event.type === "thinking_delta") {
        await send({ type: "event", id: job.id, event: { type: event.type, delta: event.delta } });
      }
    }
    const message = await stream.result();
    message.bbxUsageObserved = inputObserved && outputObserved;
    terminal.truncated_stream = message.stopReason === "error"
      && /stream ended (without|before)/i.test(message.errorMessage ?? "");
    terminal.transport_failure = /^(?:.* API error(?: \([^)]*\))?: )?(?:Connection error\.?|fetch failed)$/.test(message.errorMessage ?? "");
    terminal.transport_timeout = /^(?:.* API error(?: \([^)]*\))?: )?Request timed out\.?$/.test(message.errorMessage ?? "");
    return { message, status, request_id: requestId, terminal };
  });
  const traces = await telemetry.findTraces();
  result.telemetry = traces.traces.flatMap((trace) => trace.spans)
    .filter((span) => span.name === "pi.ai.request").slice(0, 16)
    .map((span) => ({
      name: span.name, started_ms: span.startedMs, ended_ms: span.endedMs,
      attributes: span.attributes, error: span.status.status === "error",
    }));
  return result;
  } finally {
    if (temporary) await rm(temporary, { recursive: true, force: true });
  }
}

async function call(frame) {
  const job = { id: frame.id, controller: new AbortController(), failure: null };
  jobs.set(frame.id, job);
  try {
    let result;
    if (frame.method === "agent") result = await runAgent(job, frame.params);
    else if (frame.method === "model") result = await streamModel(job, frame.params);
    else if (frame.method === "health") result = { engine: "pi", versions, node: process.versions.node };
    else throw new Error("Unknown Pi bridge operation");
    if (job.failure) throw job.failure;
    await send({ type: "result", id: frame.id, result });
  } catch (error) {
    await send({ type: "result", id: frame.id, error: {
      code: error?.code ?? "pi_runtime_error", token: error?.token,
      name: error?.name ?? "Error",
    } });
  } finally {
    jobs.delete(frame.id);
  }
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  try {
    const frame = JSON.parse(line);
    if (frame.type === "reply") {
      const pending = callbacks.get(frame.id);
      if (!pending) return;
      callbacks.delete(frame.id);
      if (frame.error) {
        const error = Object.assign(new Error("Platform callback failed"), frame.error);
        pending.job.failure ??= error;
        pending.job.controller.abort();
        for (const [siblingId, sibling] of callbacks) {
          if (sibling.job !== pending.job) continue;
          void send({ type: "cancel_callback", id: siblingId });
        }
        pending.reject(error);
      } else pending.resolve(frame.result);
    } else if (frame.type === "cancel") {
      jobs.get(frame.id)?.controller.abort();
      for (const [id, pending] of callbacks) {
        if (pending.job.id !== frame.id) continue;
        callbacks.delete(id);
        pending.reject(Object.assign(new Error("Cancelled"), { code: "cancelled" }));
      }
    } else if (frame.type === "call") void call(frame);
  } catch {
    // No raw frame or exception text: both can contain model credentials.
    process.stderr.write("Invalid Pi bridge frame\n");
    process.exitCode = 1;
    input.close();
  }
});
input.on("close", () => {
  for (const job of jobs.values()) job.controller.abort();
  for (const pending of callbacks.values()) pending.reject(new Error("Bridge closed"));
  callbacks.clear();
});
