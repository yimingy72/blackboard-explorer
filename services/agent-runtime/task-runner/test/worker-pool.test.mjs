import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test from "node:test";
import { deferred, fixture, waitFor } from "./support.mjs";

test("a real Pi session retains context and MCP while tools keep their originating work", { timeout: 20000 }, async (t) => {
  const h = await fixture(t);
  const empty = { revision: 2, assignment_ref: null, challenge_id: null };
  const first = { revision: 3, assignment_ref: randomUUID(), challenge_id: randomUUID() };
  const second = { revision: 5, assignment_ref: randomUUID(), challenge_id: randomUUID() };
  h.state.workerContexts = new Map([["one", empty]]);
  let next = first;
  h.state.modelTool = (_id, index) => index === 1 || index === 4 ? [
    { name: "remote_echo", arguments: { value: "claim" } },
    { name: "remote_echo", arguments: { value: "stale-same-response-execution" } },
  ] : index === 2 || index === 5 ? { name: "remote_echo", arguments: { value: "execute-current" } } : null;
  h.state.admitTool = (invocation) => {
    const source = h.state.modelCalls.find((item) => item.callId === invocation.source_model_call_id);
    assert.ok(source, "every tool identifies its actual originating model request");
    if (JSON.stringify(source.workerContext) !== JSON.stringify(h.state.workerContexts.get("one"))) {
      return { isError: true, content: [{ type: "text", text: "Earlier work context; not executed." }] };
    }
  };
  h.state.toolResult = (_id, _invocation, args) => {
    if (args.value === "claim") h.state.workerContexts.set("one", next);
    return { content: [{ type: "text", text: `Persisted ${args.value}; useful tool experience` }] };
  };
  h.state.dropToolReplyFor = "one";
  const spec = { ...h.spec("one"), worker_pool: true };
  h.start();
  h.command({ kind: "prepare", spec });
  await waitFor(() => h.state.receipts.find((item) => item.status === "prepared"), "Worker prepared");
  h.command({ kind: "input", binding_id: "one", input_id: "select-first", content: "Claim available work.", mode: "steer", worker_context: empty });
  await waitFor(() => h.state.confirmations.some((item) => item.worker_idle?.context.revision === 3), "first work naturally settled");
  assert.equal(h.state.modelCalls.length, 3);
  assert.deepEqual(h.state.toolCalls.map((item) => item.args.value), ["claim", "execute-current"]);
  assert.equal(h.state.droppedToolReply, true, "lost claim transport result is recovered without a second claim");
  assert.deepEqual(h.state.modelCalls.map((item) => item.workerContext), [empty, first, first]);
  const sdk = h.state.journal.get("one").sdk_session_id;
  const idle = { revision: 4, assignment_ref: null, challenge_id: null };
  h.state.workerContexts.set("one", idle);
  next = second;
  h.command({ kind: "input", binding_id: "one", input_id: "select-second", content: "Previous work passed. Claim another task.", mode: "steer", worker_context: idle });
  await waitFor(() => h.state.confirmations.some((item) => item.worker_idle?.context.revision === 5), "second work naturally settled");
  assert.equal(h.state.journal.get("one").sdk_session_id, sdk);
  assert.equal(h.state.connections.length, 1, "normal reassignment reuses the MCP connection");
  assert.equal(h.state.leases.length, 1, "normal reassignment does not prepare another session");
  assert.equal(h.state.toolCalls.length, 4);
  assert.match(JSON.stringify(h.state.modelCalls[3].body), /useful tool experience/);
  h.assertClean();
});

test("steer during a real Pi tool finishes through the public queue before idle is recorded", { timeout: 15000 }, async (t) => {
  const h = await fixture(t);
  const context = { revision: 1, assignment_ref: randomUUID(), challenge_id: randomUUID() };
  h.state.workerContexts = new Map([["one", context]]);
  h.state.toolBarrier = deferred();
  h.state.manualToolBarrier = true;
  h.state.modelTool = (_id, index) => index === 1 ? { name: "remote_echo", arguments: { value: "long-tool" } } : null;
  h.start();
  h.command({ kind: "prepare", spec: { ...h.spec("one"), worker_pool: true } });
  await waitFor(() => h.state.receipts.some((item) => item.status === "prepared"), "Worker prepared");
  h.command({ kind: "input", binding_id: "one", input_id: "work", content: "Inspect the sample.", mode: "steer", worker_context: context });
  await waitFor(() => h.state.toolCalls.length === 1, "long tool started");
  const waiting = h.command({ kind: "input", binding_id: "one", input_id: "review", content: "The task awaits Lead review; finish this round and wait.", mode: "steer", worker_context: context });
  await waitFor(() => h.state.receipts.some((item) => item.request_id === waiting.request_id && item.status === "accepted"), "steer queued while tool is active");
  assert.equal(h.state.confirmations.some((item) => item.worker_idle), false);
  h.state.toolBarrier.resolve();
  const idle = await waitFor(() => h.state.confirmations.find((item) => item.worker_idle), "queued steer consumed and execution naturally idle");
  const applied = h.state.confirmations.flatMap((item) => item.applied_inputs);
  assert.ok(applied.some((item) => item.input_id === "review"));
  assert.ok(applied.every((item) => item.seq < idle.worker_idle.seq));
  assert.equal(h.state.modelCalls.length, 2, "no additional model acknowledgement round");
  assert.match(JSON.stringify(h.state.modelCalls[1].body), /awaits Lead review/);
  h.assertClean();
});
