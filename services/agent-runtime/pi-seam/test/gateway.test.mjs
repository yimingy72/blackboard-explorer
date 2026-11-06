import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { Type } from "typebox";
import { confirmJournal, durableTool, FileReceiptStore, helperModes, installGatewayBoundary, toolInvocation } from "../src/gateway-session.mjs";
import { deferred, harness, sse } from "./support.mjs";

test("native SDK turns and manual/automatic compaction share awaited journal and one gateway admission", async (t) => {
  const h = await harness(t, (_request, response) => sse(response, { usage: 2800 }));
  const session = await h.session({ model: { contextWindow: 4096 } });
  session.settingsManager.applyOverrides({ compaction: { enabled: false, reserveTokens: 512, keepRecentTokens: 32 } });
  const callbackGate = deferred();
  const callbackEntered = deferred();
  session.subscribe(async (event) => {
    if (event.type === "message_start") { callbackEntered.resolve(); await callbackGate.promise; }
  });
  await session.prompt("Explain this fake attachment. ".repeat(100));
  await callbackEntered.promise;
  assert.equal(h.state.requests.length, 1, "ordinary subscribe is not an awaited barrier");
  callbackGate.resolve();
  await session.prompt("Continue the fake analysis. ".repeat(100));
  const compacted = await session.compact();
  assert.ok(compacted.summary);
  const reasons = [];
  session.subscribe((event) => { if (event.type === "compaction_start") reasons.push(event.reason); });
  session.settingsManager.applyOverrides({ compaction: { enabled: true, reserveTokens: 1600, keepRecentTokens: 32 } });
  await session.prompt("Trigger a fresh threshold check. ".repeat(100));
  await session.prompt("Finish after automatic compaction.");
  assert.ok(reasons.includes("threshold"), `auto compaction reasons: ${reasons}`);
  assert.ok(h.state.admissions.filter((request) => request.purpose === "summary").length >= 2);
  assert.equal(h.state.admissions.length, h.state.requests.length);
  assert.equal(h.state.reservations.size, h.state.requests.length);
  assert.equal(h.state.settlements.length, h.state.requests.length);
  assert.equal(session.settingsManager.getCacheWarmingMode(), "off");
  assert.equal(session.autoRetryEnabled, false);
  assert.equal(session.settingsManager.getProviderRetrySettings().maxRetries, 0);
  assert.deepEqual(session.getActiveToolNames(), []);
  assert.ok(h.state.settlements.every((receipt) => receipt.outcome === "completed"));
  assert.ok(h.state.admissions.every((request) => h.state.confirmations.some((cursor) => cursor.hash === request.journal.hash)));
  h.assertClean();
});

test("confirmation blocks sends; failure and cancellation return protocol error streams", async (t) => {
  const h = await harness(t);
  const entered = deferred();
  const release = deferred();
  const session = await h.session({ acknowledge: async () => { entered.resolve(); await release.promise; throw new Error("fake persistence failure"); } });
  const running = session.prompt("Do not send before journal acknowledgement.");
  await entered.promise;
  assert.equal(h.state.requests.length, 0);
  assert.equal(h.state.admissions.length, 0);
  release.resolve();
  await running;
  await session.prompt("A second prompt must remain frozen after the ACK failure.");
  assert.equal(h.state.requests.length, 0);
  assert.equal(session.messages.at(-1).stopReason, "error");
  const controller = new AbortController();
  controller.abort();
  const stream = await session.agent.streamFunction(session.model, [], { signal: controller.signal });
  assert.equal((await stream.result()).stopReason, "aborted");
  h.assertClean();
});

test("429, 503 and a disconnected stream each send one distinct attempt and retain unknown usage", async (t) => {
  const cases = [429, 503, "disconnect"];
  const h = await harness(t, ({ index }, response) => {
    const mode = cases[index - 1];
    if (typeof mode === "number") {
      response.writeHead(mode, { "content-type": "application/json", "retry-after": "0" });
      response.end(JSON.stringify({ error: { message: `fake status ${mode}`, type: "server_error" } }));
    } else {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(`data: ${JSON.stringify({ id: "broken", choices: [{ index: 0, delta: { content: "partial" }, finish_reason: null }] })}\n\n`);
      setImmediate(() => response.destroy());
    }
  });
  for (const mode of cases) {
    const session = await h.session();
    session.settingsManager.applyOverrides({ compaction: { enabled: false } });
    await session.prompt(`Exercise fake failure ${mode}.`);
    assert.equal(session.messages.at(-1).stopReason, "error");
  }
  assert.equal(h.state.requests.length, 3);
  assert.equal(h.state.admissions.length, 3);
  assert.equal(new Set(h.state.admissions.map((request) => request.attemptId)).size, 3);
  assert.equal(new Set(h.state.admissions.map((request) => request.callId)).size, 3);
  assert.deepEqual(h.state.settlements.map((receipt) => receipt.httpStatus), [429, 503, 200]);
  for (const receipt of h.state.settlements) {
    assert.equal(receipt.outcome, "unknown");
    assert.equal(receipt.usage, null);
    assert.equal((await h.store.read(receipt.attemptId)).state, "unknown");
  }
  h.assertClean();
});

test("native tools confirm before dispatch and persist results before the next model; crashes do not replay", async (t) => {
  const h = await harness(t, ({ index }, response) => sse(response, index % 2 === 1
    ? { tool: { id: "tool-fixed", name: "record", args: { value: "a" } } } : {}));
  const store = new FileReceiptStore(join(h.directory, "tools"));
  let executions = 0;
  let session;
  const tool = durableTool({
    name: "record", label: "Record", description: "A fake side effect", parameters: Type.Object({ value: Type.String() }),
    async execute() {
      assert.ok(h.state.confirmations.length >= 2);
      executions++;
      return { content: [{ type: "text", text: "saved" }], details: {} };
    },
  }, {
    store, confirm: () => confirmJournal(session, h.boundary.acknowledge),
    getSession: () => session,
    resolveInvocation: (id) => toolInvocation(session, id, { taskId: "task", toolName: "record" }),
  });
  session = await h.session({ customTools: [tool] });
  await session.prompt("Use the fake record tool once.");
  assert.equal(executions, 1);
  const firstInvocation = toolInvocation(session, "tool-fixed", { taskId: "task", toolName: "record" });
  assert.equal((await store.read(firstInvocation)).state, "completed");
  assert.ok(JSON.parse(h.state.admissions[1].bodyJson).messages.some((message) => message.role === "tool" && message.content.includes("saved")));
  await tool.execute("tool-fixed", { value: "a" });
  assert.equal(executions, 1);
  await session.prompt("A new turn may reuse the provider's short tool-call ID.");
  const secondInvocation = toolInvocation(session, "tool-fixed", { taskId: "task", toolName: "record" });
  assert.notEqual(firstInvocation, secondInvocation);
  assert.equal(executions, 2);
  const recovered = durableTool(tool, { store, confirm: async () => {}, resolveInvocation: () => firstInvocation, getSession: () => session });
  await recovered.execute("tool-fixed", { value: "a" });
  assert.equal(executions, 2, "only the original trusted invocation is replay-safe");
  let receiptSession = session;
  const crashing = durableTool({ ...tool, execute: async () => { executions++; return { content: [], details: {} }; } }, {
    confirm: async () => {},
    getSession: () => receiptSession,
    resolveInvocation: (id) => `task:trusted-recovery:${id}`,
    store: {
      read: (id) => store.read(id), claim: (id, value) => store.claim(id, value),
      async save(id, value) { await store.save(id, value); throw new Error("result saved but ACK lost"); },
    },
  });
  await assert.rejects(crashing.execute("saved-before-crash", {}), /ACK lost/);
  await assert.rejects(crashing.execute("new-id-after-failure", {}), /boundary is frozen/);
  receiptSession = await h.session();
  await crashing.execute("saved-before-crash", {});
  assert.equal(executions, 3);
  await store.claim("task:trusted-recovery:unknown-after-send", { state: "sent", inputHash: "unresolved" });
  await assert.rejects(crashing.execute("unknown-after-send", {}), /cannot be replayed/);
  assert.equal(executions, 3);
  const missingSession = await h.session();
  const missing = durableTool(tool, {
    store: { claim: async () => false, read: async () => undefined }, confirm: async () => {}, resolveInvocation: () => "missing-receipt",
    getSession: () => missingSession,
  });
  await assert.rejects(missing.execute("tool-fixed", {}), /could not be reconciled/);
  assert.equal(executions, 3);
  const journal = await readFile(session.sessionFile, "utf8");
  assert.ok(journal.includes('"role":"toolResult"'));
  h.assertClean();
});

test("unknown tool persistence freezes model and new tool IDs, while a known business refusal can continue", async (t) => {
  const h = await harness(t, ({ index }, response) => {
    if (index === 1) sse(response, { tools: [
      { id: "effect-first", name: "effect", args: {} }, { id: "effect-second", name: "effect", args: {} },
    ] });
    else if (index === 2) sse(response, { tool: { id: "known-refusal", name: "effect", args: {} } });
    else sse(response);
  });
  let session;
  let effects = 0;
  const disk = new FileReceiptStore(join(h.directory, "failing-tool"));
  const tool = durableTool({
    name: "effect", label: "Effect", description: "A fake side effect", parameters: Type.Object({}),
    async execute() { effects++; return { content: [{ type: "text", text: "external effect completed" }], details: {} }; },
  }, {
    getSession: () => session, confirm: () => confirmJournal(session, h.boundary.acknowledge),
    resolveInvocation: (id) => toolInvocation(session, id, { taskId: "task", toolName: "effect" }),
    store: { claim: (id, value) => disk.claim(id, value), read: (id) => disk.read(id), save: async () => { throw new Error("result storage unavailable"); } },
  });
  session = await h.session({ customTools: [tool] });
  session.agent.toolExecution = "sequential";
  await session.prompt("Run the first fake effect and then the second.");
  assert.equal(effects, 1, "the next tool in the batch cannot run after an unknown effect");
  assert.equal(h.state.requests.length, 1, "Pi cannot continue its model loop after unknown persistence");
  await assert.rejects(tool.execute("entirely-new-provider-id", {}), /boundary is frozen/);
  await session.prompt("A new turn cannot escape the same frozen session binding.");
  assert.equal(effects, 1);
  assert.equal(h.state.requests.length, 1);

  let knownSession;
  const known = durableTool({
    name: "effect", label: "Effect", description: "A known business refusal", parameters: Type.Object({}),
    async execute() { return { content: [{ type: "text", text: "Known refusal: no side effect was dispatched" }], details: { refused: true } }; },
  }, {
    getSession: () => knownSession, store: new FileReceiptStore(join(h.directory, "known-tool")),
    confirm: () => confirmJournal(knownSession, h.boundary.acknowledge),
    resolveInvocation: (id) => toolInvocation(knownSession, id, { taskId: "task", toolName: "effect" }),
  });
  knownSession = await h.session({ customTools: [known] });
  await knownSession.prompt("Accept a known business refusal and continue normally.");
  assert.equal(knownSession.messages.at(-1).stopReason, "stop");
  assert.equal(h.state.requests.length, 3);
  h.assertClean();
});

test("SDK done without provider usage remains a non-accounting SDK observation", async (t) => {
  const h = await harness(t, (_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.write(`data: ${JSON.stringify({ id: "no-usage", choices: [{ index: 0, delta: { content: "done" }, finish_reason: "stop" }] })}\n\n`);
    response.end("data: [DONE]\n\n");
  });
  const session = await h.session();
  await session.prompt("Complete without a usage receipt.");
  assert.equal(session.messages.at(-1).stopReason, "stop");
  assert.equal(h.state.settlements[0].outcome, "completed");
  assert.equal(h.state.settlements[0].modelOutcome, "completed");
  assert.equal(h.state.settlements[0].usage.totalTokens, 0);
  h.assertClean();
});

test("an unexpected source throw settles a sent attempt unknown once and freezes the boundary", async (t) => {
  const h = await harness(t);
  const session = await h.session();
  session.agent.streamFunction = async (_model, _context, options) => {
    await options.fetch(`${h.gatewayUrl}/chat/completions`, { method: "POST", body: JSON.stringify({ model: "fake" }) });
    return {
      async *[Symbol.asyncIterator]() { throw new Error("injected non-protocol stream failure"); },
      result: async () => { throw new Error("injected result rejection"); },
    };
  };
  installGatewayBoundary(session, h.boundary);
  await session.prompt("Inject a broken StreamFn implementation.");
  await session.prompt("The failed source boundary must remain frozen.");
  assert.equal(h.state.requests.length, 1);
  assert.equal(h.state.settlements.length, 1);
  assert.equal(h.state.settlements[0].outcome, "unknown");
  assert.equal(session.messages.at(-1).stopReason, "error");
  h.assertClean();
});

test("unpatched helper modes stay closed", () => {
  assert.deepEqual(helperModes, { foreground: false, background: false });
});
