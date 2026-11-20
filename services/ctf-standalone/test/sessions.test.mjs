import assert from "node:assert/strict";
import { test } from "node:test";
import { appendFile, mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { SessionPool } from "../src/sessions.mjs";

const config = { provider: "offline-ctf", id: "scripted", api: "openai-completions", baseUrl: "http://127.0.0.1:1",
  apiKey: "test-only-key", reasoning: false, contextWindow: 100000, maxTokens: 1024 };
const usage = { input: 2, output: 3, cacheRead: 0, cacheWrite: 0, totalTokens: 5,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };

function scripted(model, content, stopReason = "stop", errorMessage) {
  const stream = createAssistantMessageEventStream();
  const message = { role: "assistant", content, api: model.api, provider: model.provider,
    model: model.id, usage, stopReason, timestamp: Date.now(), ...(errorMessage ? { errorMessage } : {}) };
  stream.push({ type: "start", partial: { ...message, content: [] } });
  stream.push(stopReason === "error" ? { type: "error", error: message, reason: "error" }
    : { type: "done", message, reason: stopReason });
  stream.end(message);
  return stream;
}

async function setup(t, options = {}) {
  const scratch = fileURLToPath(new URL("../../../.data/ctf-standalone-core-20261121/tests/", import.meta.url));
  await mkdir(scratch, { recursive: true });
  const root = await mkdtemp(join(scratch, "session-"));
  const pool = await SessionPool.create({ root, modelConfig: config,
    getContext: () => ({ worker_id: "worker-1", assignment_id: "assignment-1", challenge_id: "one", generation: 1 }),
    retry: { enabled: false, provider: { maxRetries: 0 } }, ...options });
  t.after(async () => { await pool.close(); await rm(root, { recursive: true, force: true }); });
  return { pool, root };
}

test("persistent Pi sessions are cold until notified and keep history across assignments and restart", async (t) => {
  const requests = [];
  const streamFunction = (model, context, _options, meta) => {
    requests.push({ context: structuredClone(context), meta });
    return scripted(model, [{ type: "text", text: "fixture-result" }]);
  };
  const { pool, root } = await setup(t, { streamFunction });
  const prepared = await Promise.all(Array.from({ length: 20 }, (_, index) => pool.ensure(`worker-${index + 1}`)));
  assert.equal(requests.length, 0);
  assert.equal(pool.status().length, 20);
  const first = prepared[0];
  await pool.prompt("worker-1", "first challenge");
  await pool.prompt("worker-1", "second challenge");
  assert.equal(requests.length, 2);
  assert.match(JSON.stringify(requests[1].context), /first challenge/);
  assert.equal(pool.status().find((item) => item.id === "worker-1").sessionId, first.sessionId);
  await pool.close();
  const restored = await SessionPool.create({ root, modelConfig: config, streamFunction,
    retry: { enabled: false }, getContext: () => ({ role: "lead" }) });
  t.after(() => restored.close());
  assert.equal((await restored.ensure("worker-1")).sessionId, first.sessionId);
  await restored.prompt("worker-1", "after restart");
  assert.match(JSON.stringify(requests.at(-1).context), /second challenge/);
});

test("all tools in one model response keep the pre-claim assignment", async (t) => {
  let assigned = null;
  const identities = [];
  let calls = 0;
  const { pool, root } = await setup(t, {
    getContext: () => assigned,
    toolsForRole: () => [
      { name: "claim", description: "fixture claim", parameters: Type.Object({}), execute: () => {
        assigned = { worker_id: "worker-1", assignment_id: "new-assignment", challenge_id: "one", generation: 1 };
        return assigned;
      } },
      { name: "observe", description: "fixture observation", parameters: Type.Object({}), execute: (_args, meta) => {
        identities.push(meta.context);
        return { ok: true };
      } },
    ],
    streamFunction: (model, context) => {
      calls += 1;
      if (calls === 1) return scripted(model, [
        { type: "toolCall", id: "claim-1", name: "claim", arguments: {} },
        { type: "toolCall", id: "observe-old", name: "observe", arguments: {} },
        { type: "toolCall", id: "write-old", name: "write", arguments: { path: "should-not-exist", content: "bad" } },
      ], "toolUse");
      if (calls === 2) {
        assert.match(JSON.stringify(context), /earlier assignment/);
        return scripted(model, [{ type: "toolCall", id: "observe-new", name: "observe", arguments: {} }], "toolUse");
      }
      return scripted(model, [{ type: "text", text: "done" }]);
    },
  });
  await pool.ensure("worker-1");
  await pool.prompt("worker-1", "claim a challenge");
  assert.deepEqual(identities, [null, assigned]);
  await assert.rejects(readFile(join(root, "work/worker-1/should-not-exist")), { code: "ENOENT" });
});

test("environment changes reject stale local tools and the next model request uses the refreshed context", async (t) => {
  const assignment = { worker_id: "worker-1", assignment_id: "assignment-1", challenge_id: "one",
    generation: 1, environment_generation: 1 };
  const generations = [];
  let requests = 0;
  const { pool, root } = await setup(t, {
    getContext: () => assignment,
    streamFunction: (model, context, _options, meta) => {
      generations.push(meta.context.environment_generation);
      requests += 1;
      if (requests === 1) {
        assignment.environment_generation = 2;
        return scripted(model, [{ type: "toolCall", id: "write-stale", name: "write",
          arguments: { path: "stale.txt", content: "obsolete" } }], "toolUse");
      }
      if (requests === 2) {
        assert.match(JSON.stringify(context), /earlier assignment/);
        return scripted(model, [{ type: "toolCall", id: "write-current", name: "write",
          arguments: { path: "current.txt", content: "refreshed" } }], "toolUse");
      }
      return scripted(model, [{ type: "text", text: "done" }]);
    },
  });
  await pool.ensure("worker-1");
  await pool.prompt("worker-1", "inspect the current challenge environment");
  assert.deepEqual(generations, [1, 2, 2]);
  await assert.rejects(readFile(join(root, "work/worker-1/stale.txt")), { code: "ENOENT" });
  assert.equal(await readFile(join(root, "work/worker-1/current.txt"), "utf8"), "refreshed");
});

test("custom model and arena environment keys stay out of local commands", async (t) => {
  let requests = 0;
  const { pool } = await setup(t, {
    toolEnv: { CTF_TEST_CUSTOM_MODEL_KEY: config.apiKey, CTF_TEST_CUSTOM_ARENA_KEY: "synthetic-arena-secret" },
    toolSecrets: ["synthetic-arena-secret"],
    streamFunction: (model, context) => {
      if (++requests === 1) return scripted(model, [{ type: "toolCall", id: "credentials", name: "bash",
        arguments: { command: "printf '%s|%s' \"${CTF_TEST_CUSTOM_MODEL_KEY-unset}\" \"${CTF_TEST_CUSTOM_ARENA_KEY-unset}\"" } }], "toolUse");
      const result = context.messages.findLast((message) => message.role === "toolResult");
      assert.equal(result.isError, false);
      assert.match(result.content[0].text, /^unset\|unset\n/);
      return scripted(model, [{ type: "text", text: "credentials excluded" }]);
    },
  });
  await pool.ensure("worker-1");
  await pool.prompt("worker-1", "check the local command environment");
  assert.equal(requests, 2);
  assert.equal(pool.status()[0].error, null);
});

test("twenty assigned Workers execute local commands concurrently and settle independently", async (t) => {
  const turns = new Map();
  let active = 0;
  let peak = 0;
  let completed = 0;
  const { pool } = await setup(t, {
    getContext: (id) => ({ worker_id: id, assignment_id: `assignment-${id}`, challenge_id: id, generation: 1 }),
    onEvent: (event) => {
      if (event.type === "tool_start") peak = Math.max(peak, ++active);
      if (event.type === "tool_end") { active -= 1; completed += 1; assert.equal(event.isError, false); }
    },
    streamFunction: (model, _context, _options, { agentId }) => {
      const turn = (turns.get(agentId) ?? 0) + 1;
      turns.set(agentId, turn);
      if (turn === 1) return scripted(model, [{ type: "toolCall", id: `parallel-${agentId}`, name: "bash",
        arguments: { command: "sleep 0.05; printf fixture-complete" } }], "toolUse");
      return scripted(model, [{ type: "text", text: "done" }]);
    },
  });
  const ids = Array.from({ length: 20 }, (_, index) => `worker-${index + 1}`);
  await Promise.all(ids.map((id) => pool.ensure(id)));
  await Promise.all(ids.map((id) => pool.prompt(id, "run the independent fixture")));
  assert.equal(completed, 20);
  assert.equal(active, 0);
  assert.ok(peak > 1);
  assert.ok(pool.status().every((session) => session.idle && session.active_processes === 0 && !session.error));
});

test("steer delivers the newest event, deduplicates, and starts no polling calls", async (t) => {
  let release;
  const gate = new Promise((accept) => { release = accept; });
  let started;
  const entered = new Promise((accept) => { started = accept; });
  const requests = [];
  const { pool } = await setup(t, { streamFunction: async (model, context) => {
    requests.push(structuredClone(context));
    if (requests.length === 1) { started(); await gate; }
    return scripted(model, [{ type: "text", text: "settled" }]);
  } });
  await pool.ensure("worker-1");
  const run = pool.prompt("worker-1", "solve");
  await entered;
  await pool.notify("worker-1", "old-review-request", { eventId: "old", version: 1, scope: "one" });
  await pool.notify("worker-1", "new-accepted-result", { eventId: "new", version: 2, scope: "one" });
  assert.equal((await pool.notify("worker-1", "late", { eventId: "late", version: 1, scope: "one" })).stale, true);
  release();
  await run;
  await pool.waitIdle("worker-1");
  assert.ok(requests.length >= 2);
  assert.match(JSON.stringify(requests.at(-1)), /new-accepted-result/);
  assert.doesNotMatch(JSON.stringify(requests.at(-1)), /old-review-request/);
  const count = requests.length;
  assert.equal((await pool.notify("worker-1", "new-accepted-result", { eventId: "new", version: 2, scope: "one" })).duplicate, true);
  await new Promise((accept) => setTimeout(accept, 20));
  assert.equal(requests.length, count);
});

test("model errors settle with usage and can resume without ending the run", async (t) => {
  const events = [];
  let count = 0;
  const { pool } = await setup(t, { onEvent: (event) => events.push(event), streamFunction: (model) => {
    if (++count === 1) return scripted(model, [], "error", "fixture unavailable");
    return scripted(model, [{ type: "text", text: "recovered" }]);
  } });
  await pool.ensure("worker-1");
  await pool.prompt("worker-1", "start");
  assert.equal(pool.status()[0].idle, true);
  assert.match(pool.status()[0].error, /fixture unavailable/);
  assert.ok(events.some((event) => event.type === "error" && event.settled && event.model_inflight === 0));
  await pool.prompt("worker-1", "retry with current task");
  assert.equal(pool.status()[0].error, null);
  assert.ok(events.some((event) => event.type === "model_end" && event.usage?.totalTokens === 5));
});

test("interrupted tools remain unknown after restart and are never replayed", async (t) => {
  const { pool, root } = await setup(t, { streamFunction: (model) => scripted(model, [{ type: "text", text: "ready" }]) });
  const prepared = await pool.ensure("worker-1");
  await pool.prompt("worker-1", "first run");
  await pool.close();
  const args = { command: "touch must-not-run" };
  const manager = SessionManager.open(prepared.sessionFile);
  manager.appendMessage({ role: "assistant", content: [
    { type: "toolCall", id: "interrupted", name: "bash", arguments: args },
  ], api: config.api, provider: config.provider, model: config.id, usage, stopReason: "toolUse", timestamp: Date.now() });
  await appendFile(join(root, "sessions/worker-1/events.jsonl"), JSON.stringify({ type: "tool_start", call_id: "interrupted",
    tool: "bash", args_digest: createHash("sha256").update(JSON.stringify(args)).digest("hex"), context: { assignment_id: "assignment-1" } }) + "\n");
  let request = 0;
  const restored = await SessionPool.create({ root, modelConfig: config, retry: { enabled: false },
    getContext: () => ({ assignment_id: "assignment-1" }),
    streamFunction: (model, context) => {
      if (++request === 1) {
        assert.match(JSON.stringify(context), /outcome is unknown/);
        return scripted(model, [{ type: "toolCall", id: "interrupted", name: "bash", arguments: args }], "toolUse");
      }
      assert.match(JSON.stringify(context), /unknown result after interruption/);
      return scripted(model, [{ type: "text", text: "inspect evidence instead" }]);
    } });
  t.after(() => restored.close());
  const recovered = await restored.ensure("worker-1");
  assert.equal(recovered.unresolved.length, 1);
  await restored.prompt("worker-1", "recover");
  await assert.rejects(readFile(join(prepared.cwd, "must-not-run")), { code: "ENOENT" });
});

test("pool abort waits for the active tool and process group to settle", async (t) => {
  const events = [];
  let started;
  const entered = new Promise((accept) => { started = accept; });
  let requests = 0;
  const { pool } = await setup(t, {
    onEvent: (event) => { events.push(event); if (event.type === "tool_start") started(); },
    streamFunction: (model) => ++requests === 1 ? scripted(model, [
      { type: "toolCall", id: "long-command", name: "bash", arguments: { command: "sleep 60 & wait" } },
    ], "toolUse") : scripted(model, [{ type: "text", text: "finished" }]),
  });
  await pool.ensure("worker-1");
  const run = pool.prompt("worker-1", "long tool");
  await entered;
  const result = await pool.abort("worker-1");
  await run;
  assert.equal(result.active_processes, 0);
  assert.equal(result.tool_inflight, 0);
  assert.equal(result.model_inflight, 0);
  assert.equal(result.idle, true);
  assert.ok(events.some((event) => event.type === "idle" && event.aborted && event.settled));
});

test("real Pi provider transport reaches only the local fixture and executes local tools", async (t) => {
  const requests = [];
  const server = createServer(async (request, response) => {
    assert.equal(request.url, "/v1/chat/completions");
    let body = "";
    for await (const chunk of request) body += chunk;
    const payload = JSON.parse(body);
    requests.push(payload);
    const delta = requests.length === 1 ? { role: "assistant", tool_calls: [{ index: 0, id: "http-tool", type: "function",
      function: { name: "bash", arguments: JSON.stringify({ command: "printf CTF_TRANSPORT_OK" }) } }] }
      : { role: "assistant", content: "fixture verified" };
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end(`data: ${JSON.stringify({ id: "fixture", object: "chat.completion.chunk", model: "scripted", choices: [
      { index: 0, delta, finish_reason: requests.length === 1 ? "tool_calls" : "stop" },
    ], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } })}\n\ndata: [DONE]\n\n`);
  });
  await new Promise((accept) => server.listen(0, "127.0.0.1", accept));
  t.after(() => new Promise((accept) => server.close(accept)));
  const { pool } = await setup(t, { modelConfig: { ...config, baseUrl: `http://127.0.0.1:${server.address().port}/v1` } });
  await pool.ensure("worker-1");
  await pool.prompt("worker-1", "run the synthetic tool");
  assert.equal(requests.length, 2);
  assert.match(JSON.stringify(requests[1].messages), /CTF_TRANSPORT_OK/);
  assert.equal(pool.status()[0].error, null);
});

test("notifications survive restart before delivery and ignore older versions", async (t) => {
  let requests = 0;
  const { pool, root } = await setup(t, { streamFunction: (model) => {
    requests += 1;
    return scripted(model, [{ type: "text", text: "done" }]);
  } });
  await pool.ensure("worker-1");
  await pool.close();
  await appendFile(join(root, "sessions/worker-1/events.jsonl"), JSON.stringify({
    type: "notification", eventId: "saved", version: 7, scope: "challenge-one", text: "committed task update",
  }) + "\n");
  const restored = await SessionPool.create({ root, modelConfig: config, retry: { enabled: false },
    streamFunction: (model) => { requests += 1; return scripted(model, [{ type: "text", text: "received" }]); } });
  t.after(() => restored.close());
  await restored.ensure("worker-1");
  assert.equal(requests, 0);
  assert.equal(restored.pendingNotifications("worker-1").length, 1);
  assert.equal((await restored.notify("worker-1", "stale", { eventId: "old", version: 6, scope: "challenge-one" })).stale, true);
  const event = restored.pendingNotifications("worker-1")[0];
  await restored.notify("worker-1", event.text, event);
  await restored.waitIdle("worker-1");
  assert.equal(requests, 1);
  assert.equal(restored.pendingNotifications("worker-1").length, 0);
  await assert.rejects(restored.ensure("worker-1", "lead"), /role cannot change/);
});

test("queued notifications are filtered when their assignment is released or replaced", async (t) => {
  let assignment = null;
  let release;
  const gate = new Promise((accept) => { release = accept; });
  let entered;
  const started = new Promise((accept) => { entered = accept; });
  const requests = [];
  const { pool } = await setup(t, { getContext: () => assignment,
    streamFunction: async (model, context) => {
      requests.push(structuredClone(context));
      if (requests.length === 1) { entered(); await gate; }
      return scripted(model, [{ type: "text", text: "done" }]);
    } });
  await pool.ensure("worker-1");
  const run = pool.prompt("worker-1", "select work");
  await started;
  await pool.notify("worker-1", "obsolete-idle-list", { eventId: "idle", version: 1, scope: "work-list", assignmentId: null });
  assignment = { assignment_id: "assignment-a", challenge_id: "a", generation: 1 };
  await pool.notify("worker-1", "obsolete-task-a", { eventId: "a", version: 10, scope: "a", assignmentId: "assignment-a", challengeId: "a" });
  assignment = { assignment_id: "assignment-b", challenge_id: "b", generation: 1 };
  await pool.notify("worker-1", "current-task-b", { eventId: "b", version: 2, scope: "b", assignmentId: "assignment-b", challengeId: "b" });
  assert.equal((await pool.notify("worker-1", "late-a", { eventId: "late-a", version: 11, scope: "a", assignmentId: "assignment-a" })).stale, true);
  release();
  await run;
  await pool.waitIdle("worker-1");
  assert.match(JSON.stringify(requests.at(-1)), /current-task-b/);
  assert.doesNotMatch(JSON.stringify(requests.at(-1)), /obsolete-idle-list|obsolete-task-a|late-a/);
});
