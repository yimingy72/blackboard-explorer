import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createGatewaySession, FileReceiptStore } from "../src/gateway-session.mjs";

export const root = dirname(dirname(fileURLToPath(import.meta.url)));
export const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

export function sse(response, { text = "A reproducible fake answer. ".repeat(60), tool, tools, usage = 100 } = {}) {
  response.writeHead(200, { "content-type": "text/event-stream", connection: "close" });
  const calls = tools ?? (tool ? [tool] : []);
  const delta = calls.length ? { role: "assistant", tool_calls: calls.map((tool, index) => ({ index, id: tool.id, type: "function", function: { name: tool.name, arguments: JSON.stringify(tool.args) } })) }
    : { role: "assistant", content: text };
  const chunk = (choices, extra = {}) => ({ id: "fake", object: "chat.completion.chunk", created: 1, model: "fake", choices, ...extra });
  response.write(`data: ${JSON.stringify(chunk([{ index: 0, delta, finish_reason: null }]))}\n\n`);
  response.write(`data: ${JSON.stringify(chunk([{ index: 0, delta: {}, finish_reason: calls.length ? "tool_calls" : "stop" }], {
    usage: { prompt_tokens: usage, completion_tokens: 10, total_tokens: usage + 10 },
  }))}\n\n`);
  response.end("data: [DONE]\n\n");
}

/** One local gateway and a fake provider; admission is shared, never reserved twice. */
export async function harness(t, responder = (_request, response) => sse(response)) {
  await mkdir(join(root, ".fixture"), { recursive: true });
  const directory = await mkdtemp(join(root, ".fixture", "run-"));
  t.after(async () => {
    for (const cleanup of state.beforeCleanup) await cleanup();
    await rm(directory, { recursive: true, force: true });
  });
  const state = {
    requests: [], admissions: [], settlements: [], confirmations: [], reservations: new Map(), errors: [],
    hosts: [], starts: [], tools: [], closedAgents: new Set(), beforeCleanup: [],
  };
  const store = new FileReceiptStore(join(directory, "receipts"));
  const server = createServer(async (request, response) => {
    try {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks));
      if (request.url.startsWith("/control/")) {
        let result = {};
        if (request.url === "/control/confirm") {
          if (body.fail) { response.writeHead(503); response.end("confirmation failed"); return; }
          await boundary.acknowledge(body.cursor);
        } else if (request.url === "/control/admit") {
          if (state.closedAgents.has(body.binding.agentId)) { response.writeHead(403); response.end("stopped"); return; }
          result = await boundary.admit(body);
        } else if (request.url === "/control/settle") await boundary.settle(body);
        else if (request.url === "/control/host") state.hosts.push(body);
        else if (request.url === "/control/start") state.starts.push(body);
        else if (request.url === "/control/tool") {
          if (state.closedAgents.has(body.binding.agentId)) { response.writeHead(403); response.end("stopped"); return; }
          state.tools.push(body);
          if (state.toolResponder) { await state.toolResponder(body, response); return; }
          result = { content: [{ type: "text", text: "remote fixture material" }], details: {} };
        }
        else throw new Error("Unknown fake control operation");
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify(result));
        return;
      }
      const callId = request.headers["x-bbx-call-id"];
      const attemptId = request.headers["x-bbx-attempt-id"];
      const admission = state.reservations.get(attemptId);
      assert.ok(admission, "HTTP gateway must find the wrapper's existing admission");
      assert.equal(admission.callId, callId);
      assert.equal(request.headers["x-bbx-journal-hash"], admission.journal.hash);
      assert.equal(request.headers.authorization, `Bearer ${admission.binding.agentId}-token`);
      assert.equal(request.headers["x-bbx-agent-id"], admission.binding.agentId);
      assert.equal(request.headers["x-bbx-task-id"], admission.binding.taskId);
      state.requests.push({ headers: request.headers, body, purpose: admission.purpose });
      await responder({ headers: request.headers, body, admission, index: state.requests.length }, response);
    } catch (error) {
      state.errors.push(error);
      response.writeHead(500, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: { message: "fake gateway rejected an invalid identity" } }));
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => { server.closeAllConnections(); return new Promise((resolve) => server.close(resolve)); });
  const gatewayUrl = `http://127.0.0.1:${server.address().port}/v1`;
  const boundary = {
    gatewayUrl, gatewayToken: "main-token", binding: { taskId: "task", agentId: "main" },
    acknowledge: async (cursor) => { state.confirmations.push(cursor); },
    admit: async (request) => {
      assert.ok(!state.reservations.has(request.attemptId));
      state.admissions.push(request);
      state.reservations.set(request.attemptId, request);
      await store.save(request.attemptId, { state: "reserved", request });
      return { callId: request.callId, attemptId: request.attemptId };
    },
    settle: async (receipt) => {
      state.settlements.push(receipt);
      await store.save(receipt.attemptId, { state: receipt.outcome, receipt });
    },
  };
  const sessions = [];
  t.after(async () => { for (const session of sessions) { await session.abort(); session.dispose(); } });
  return {
    directory, state, store, boundary, gatewayUrl,
    async session(extra = {}) {
      const session = await createGatewaySession({ directory: join(directory, `session-${sessions.length}`), ...boundary, ...extra });
      sessions.push(session);
      return session;
    },
    assertClean() { assert.deepEqual(state.errors, []); },
  };
}
