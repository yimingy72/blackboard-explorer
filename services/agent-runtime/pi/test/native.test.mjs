import assert from "node:assert/strict";
import { test } from "node:test";
import { runAgentLoop } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream, normalizeContext, Type } from "@earendil-works/pi-ai";

const usage = { input: 10, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 12,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const model = { id: "offline", name: "Offline", api: "openai-completions", provider: "offline",
  baseUrl: "https://offline.invalid/v1", input: ["text", "image"], reasoning: false,
  contextWindow: 1000000, maxTokens: 4096, cost: usage.cost };
function scripted(content, stopReason = "stop") {
  const stream = createAssistantMessageEventStream();
  const message = { role: "assistant", content, api: model.api, provider: model.provider,
    model: model.id, usage, stopReason, timestamp: Date.now() };
  stream.push({ type: "start", partial: { ...message, content: [] } });
  stream.push({ type: "done", message, reason: stopReason });
  stream.end(message);
  return stream;
}

test("native Pi awaits the model checkpoint and executes each tool once", async () => {
  const order = [];
  let requests = 0;
  await runAgentLoop([], { messages: [], tools: [{
    name: "inspect", label: "inspect", description: "offline", parameters: Type.Object({ value: Type.Number() }),
    replay: "never", execute: async (_id, args) => {
      assert.equal(order.at(-1), "checkpoint");
      order.push(`effect:${args.value}`);
      return { content: [{ type: "text", text: "saved" }], details: {} };
    },
  }] }, { model, convertToLlm: (messages) => messages, toolExecution: "sequential" }, async (event) => {
    if (event.type === "message_end" && event.message.role === "assistant") {
      await Promise.resolve();
      order.push("checkpoint");
    }
  }, undefined, () => ++requests === 1
    ? scripted([{ type: "toolCall", id: "call-1", name: "inspect", arguments: { value: 3 } }], "toolUse")
    : scripted([{ type: "text", text: "done" }]));
  assert.equal(requests, 2);
  assert.deepEqual(order, ["checkpoint", "effect:3", "checkpoint"]);
});

test("native Pi schema validation gives a tool result without an effect", async () => {
  let effects = 0;
  let requests = 0;
  await runAgentLoop([], { messages: [], tools: [{
    name: "inspect", label: "inspect", description: "offline", parameters: Type.Object({ value: Type.Number() }),
    execute: async () => { effects++; return { content: [], details: {} }; },
  }] }, { model, convertToLlm: (messages) => messages }, () => {}, undefined, (_model, context) => {
    if (++requests === 1) return scripted([{ type: "toolCall", id: "invalid", name: "inspect", arguments: {} }], "toolUse");
    assert.equal(context.messages.at(-1).role, "toolResult");
    assert.equal(context.messages.at(-1).isError, true);
    return scripted([{ type: "text", text: "correct fields" }]);
  });
  assert.equal(effects, 0);
});

const image = { type: "image", data: "AA==", mimeType: "image/png" };
const context = () => normalizeContext({ messages: [{ role: "user", content: [{ type: "text", text: "look" }, image], timestamp: 0 }] });
const sse = (events) => new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""),
  { headers: { "content-type": "text/event-stream" } });

test("Pi OpenAI adapter sends native images and keeps cache usage", async () => {
  const api = await import("@earendil-works/pi-ai/api/openai-completions");
  const requests = [];
  const stream = api.streamSimple(model, context(), { apiKey: "test-only", maxRetries: 0,
    fetch: async (_url, init) => {
      requests.push(JSON.parse(init.body));
      return sse([{ id: "offline", choices: [{ index: 0, delta: { content: "ok" }, finish_reason: "stop" }],
        usage: { prompt_tokens: 10, completion_tokens: 2, prompt_tokens_details: { cached_tokens: 7 } } }]);
    },
  });
  const message = await stream.result();
  assert.equal(message.stopReason, "stop");
  assert.equal(message.usage.cacheRead, 7);
  assert.equal(message.usage.input, 3);
  assert.match(JSON.stringify(requests), /data:image\/png;base64,AA==/);
});

test("Pi Anthropic adapter sends native image blocks", async () => {
  const api = await import("@earendil-works/pi-ai/api/anthropic-messages");
  let payload;
  const stream = api.streamSimple({ ...model, api: "anthropic-messages", provider: "anthropic" }, context(), {
    apiKey: "test-only", maxRetries: 0, fetch: async (_url, init) => {
      payload = JSON.parse(init.body);
      const events = [
        { type: "message_start", message: { id: "msg_1", type: "message", role: "assistant", content: [], model: "offline", usage: { input_tokens: 10, output_tokens: 0 } } },
        { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
        { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "ok" } },
        { type: "content_block_stop", index: 0 },
        { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 2 } },
        { type: "message_stop" },
      ];
      return new Response(events.map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(""),
        { headers: { "content-type": "text/event-stream" } });
    },
  });
  const message = await stream.result();
  assert.equal(message.stopReason, "stop");
  assert.equal(payload.messages[0].content[1].source.data, "AA==");
});

test("Pi Google adapter uses its own SDK and native inline image", async (t) => {
  const api = await import("@earendil-works/pi-ai/api/google-generative-ai");
  let payload;
  t.mock.method(globalThis, "fetch", async (url, init) => {
    const request = new Request(url, init);
    payload = JSON.parse(await request.text());
    return sse([{ candidates: [{ content: { role: "model", parts: [{ text: "ok" }] }, finishReason: "STOP" }],
      usageMetadata: { promptTokenCount: 10, candidatesTokenCount: 2, totalTokenCount: 12 } }]);
  });
  const stream = api.streamSimple({ ...model, api: "google-generative-ai", provider: "google", id: "gemini-test" },
    context(), { apiKey: "test-only", maxRetries: 0 });
  assert.equal((await stream.result()).stopReason, "stop");
  assert.equal(payload.contents[0].parts[1].inlineData.data, "AA==");
});
