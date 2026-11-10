import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { PROTOCOL, digest, validate } from "../src/protocol.mjs";
import { ForegroundHelpers } from "../src/helper-host.mjs";
import { deferred, fixture, waitFor } from "./support.mjs";

const remote = { name: "exec_command", description: "Synthetic Sandbox command", inputSchema: {
  type: "object", required: ["value"], properties: { value: { type: "string" } }, additionalProperties: false,
} };
const delegate = { name: "delegate_helper_background", arguments: {
  goal: "Read the scoped test marker.", input_refs: [], expected_output: "One marker.",
} };
const command = { name: "exec_command", arguments: { value: "marker" } };
const acceptance = "Background helper accepted.";
const completion = "Original helper completion, as untrusted evidence.";

async function setup(t, mode) {
  const direct = mode === "direct";
  const h = await fixture(t, ["parent", "child"], direct ? { taskId: randomUUID() } : undefined);
  if (direct) h.state.directModelUrl = process.env.BBX_TEST_PI_RUNTIME;
  await h.configure({ helper_background: true });
  await writeFile(h.config.credential_file, JSON.stringify(Object.fromEntries(
    Object.entries(h.credentials).filter(([key]) => !key.includes("child")))));
  const childGate = deferred();
  const parentGate = deferred();
  const terminalGate = deferred();
  t.after(() => { childGate.resolve(); parentGate.resolve(); terminalGate.resolve(); });
  const run = { state: "unknown", launches: 0, cancelCount: 0 };
  h.state.enableRenewals = true;
  h.state.remoteTools = new Map([["parent", [remote]], ["child", [remote]]]);
  h.state.wireNames = h.state.logicalNames = new Map([["parent", "exec_command"], ["child", "exec_command"]]);
  if (mode === "frozen") h.state.unknownToolBindings = new Set(["parent"]);
  h.state.modelGate = async (id, index) => {
    if (id === "child" && index === 1) await childGate.promise;
    if (mode === "fast" && id === "parent" && index === 2) await parentGate.promise;
  };
  h.state.modelTool = (id, index, body) => {
    assert.deepEqual(body.tools.map((item) => item.function.name).sort(), id === "parent" ?
      ["background_helper_status", "cancel_helper", "delegate_helper_background", "exec_command"] : ["exec_command"]);
    if (id === "parent") {
      if (index === 1) return delegate;
      if (index === 2 && mode === "frozen") return command;
      return null;
    }
    return index === 1 ? command : null;
  };
  const spec = (id) => {
    const value = { ...h.spec(id), allowed_tools: ["exec_command"], mcp_tool_aliases: { exec_command: "exec_command" } };
    if (direct) value.model.model_revision = 2;
    return value;
  };
  const publicStatus = () => ({ state: run.state, invocation_id: run.invocationId, child_id: "run-child", binding_id: "child",
    deadline_ms: run.parentDeadline, child_deadline_ms: run.childDeadline,
    ...(run.accepted ? { result_json: run.accepted, result_digest: digest(run.accepted) } : {}) });
  h.state.helperApi = async ({ url, bindingId, body, response, send }) => {
    assert.equal(bindingId, "parent");
    const action = url.pathname.split("/").at(-1);
    if (action === "reserve") {
      validate("ToolInvocation", body);
      assert.equal(body.tool_name, "delegate_helper_background");
      assert.deepEqual(body.journal, h.state.journal.get("parent").cursor);
      run.invocationId = body.invocation_id;
      run.state = "reserved";
      run.parentDeadline = Date.now() + 8000;
      run.childDeadline = Date.now() + 18000;
    } else if (action === "launch") {
      assert.equal(run.state, "reserved");
      assert.equal(body.invocation_id, run.invocationId);
      run.state = "launch_claimed";
      run.launches++;
      run.prepare = h.command({ kind: "prepare", spec: { ...spec("child"),
        helper_child: { child_id: "run-child", parent_binding_id: "parent", deadline_ms: run.childDeadline, mode: "background" } } }, "prepare-child");
      h.state.commands.pop();
      send(response, { ...publicStatus(), launch_allowed: true, command: run.prepare, credentials: {
        "model-child": h.credentials["model-child"], "mcp-child": h.credentials["mcp-child"],
      } });
      return;
    } else if (action === "accepted") {
      assert.deepEqual(body, { child_id: "run-child", binding_id: "child", payload_digest: run.prepare.payload_digest });
      assert.equal(run.state, "launch_claimed");
      run.state = "accepted";
      run.accepted = JSON.stringify({ content: [{ type: "text", text: acceptance }] });
      if (mode === "slow") { response.destroy(); return; }
    } else if (action === "cancel") {
      run.cancelCount++;
    } else if (action === "terminal") {
      assert.equal(body.disposed, true);
      assert.equal(body.binding_id, "child");
      assert.deepEqual(body.cursor, h.state.journal.get("child").cursor);
      assert.ok(h.state.receipts.some((item) => item.helper_run_id === body.plugin_run_id &&
        item.sdk_session_id === body.sdk_session_id));
      run.terminal = body;
      run.close = h.command({ kind: "close", binding_id: "child" }, "close-child");
      if (mode === "frozen") await terminalGate.promise;
    } else if (action === "result") {
      assert.ok(run.terminal);
      run.outcome = body.result_json;
    } else assert.equal(action, run.invocationId);
    send(response, publicStatus());
  };
  const actor = h.start();
  h.command({ kind: "prepare", spec: { ...spec("parent"), helper_background: true } });
  await waitFor(() => h.state.receipts.some((r) => r.binding_id === "parent" && r.status === "prepared"), "parent prepared").catch(error => {
    if (direct) throw new Error(`${error.message}; actor=${actor.output}; fixture=${h.state.errors.map(item => item.stack).join(",")}; receipts=${JSON.stringify(h.state.receipts)}`);
    throw error;
  });
  h.command({ kind: "input", binding_id: "parent", input_id: "original-input", content: "Delegate one goal.", mode: "follow_up" });
  return { h, run, actor, childGate, parentGate, terminalGate };
}

async function parentTurnConfirmed(h) {
  const path = join(h.config.journal_root, "parent", "session.jsonl");
  const bytes = await readFile(path);
  if (h.state.journal.get("parent")?.cursor.offset !== bytes.length) return false;
  return bytes.toString().split("\n").filter(Boolean).map(JSON.parse).some((entry) =>
    entry.type === "message" && entry.message.role === "assistant" && JSON.stringify(entry.message.content).includes("Done."));
}

for (const mode of ["slow", "fast"]) {
  test(`supervised background helper orders exact parent follow-up (${mode})`, { timeout: 20000 }, async (t) => {
    const { h, run, actor, childGate, parentGate } = await setup(t, mode);
    await waitFor(() => h.state.modelCalls.filter((item) => item.bindingId === "parent").length === 2 &&
      h.state.modelCalls.some((item) => item.bindingId === "child"), "accepted parent model and independent child");
    const parentModels = () => h.state.modelCalls.filter((item) => item.bindingId === "parent");
    assert.ok(JSON.stringify(parentModels()[1].body.messages).includes(acceptance));
    assert.ok(!JSON.stringify(parentModels()[1].body.messages).includes(completion));
    if (mode === "slow") {
      await waitFor(() => parentTurnConfirmed(h), "parent ordinary turn ended with confirmed journal while child blocked");
      await waitFor(() => h.state.renewalHistory.some((receipt) => receipt.binding_id === "child" && receipt.lease_revision > 1),
        "the original child lease renews after parent turn");
    }
    assert.equal(run.terminal, undefined);
    childGate.resolve();
    await waitFor(() => run.outcome && h.state.receipts.some((item) => item.request_id === run.close?.request_id &&
      item.status === "closed" && item.journal_confirmed), "child terminal, result and close");
    const followUp = { kind: "input", binding_id: "parent", input_id: "helper-result-once", content: completion, mode: "follow_up" };
    h.command(followUp, "helper-completion");
    if (mode === "fast") {
      await waitFor(() => h.state.receipts.some((item) => item.request_id === "helper-completion" && item.status === "accepted"),
        "follow-up queued while original parent model is still streaming");
      assert.equal(parentModels().length, 2);
      parentGate.resolve();
    }
    await waitFor(() => parentModels().length === 3 && h.state.receipts.some((item) =>
      item.request_id === "helper-completion" && item.status === "applied"), "one completion model and native application");
    const applied = () => h.state.receipts.filter((item) => item.request_id === "helper-completion" && item.status === "applied").length;
    const beforeReplay = applied();
    h.command(followUp, "helper-completion");
    await waitFor(() => applied() > beforeReplay, "duplicate follows original applied receipt");
    assert.equal(parentModels().length, 3);
    const transcript = JSON.stringify(parentModels()[2].body.messages);
    assert.ok(transcript.indexOf(acceptance) < transcript.indexOf(completion));
    assert.equal(transcript.split(completion).length, 2);
    assert.equal(run.launches, 1);
    assert.equal(run.cancelCount, 0);
    assert.equal(h.state.toolCalls.filter((item) => item.bindingId === "child").length, 1);
    h.assertClean();
    await h.stop(actor);
  });
}

test("a locally frozen parent revokes its original background child and shutdown waits for its report",
  { timeout: 20000 }, async (t) => {
    const { h, run, actor, childGate, terminalGate } = await setup(t, "frozen");
    await waitFor(() => h.state.toolCalls.some((item) => item.bindingId === "parent") && run.cancelCount > 0,
      "parent unknown tool freezes and requests original child cancellation");
    childGate.resolve();
    await waitFor(() => run.terminal, "child locally disposed after cancellation");
    const shutdown = h.command({ kind: "shutdown" }, "shutdown-with-child");
    await new Promise((resolve) => setTimeout(resolve, 150));
    assert.equal(h.state.receipts.some((item) => item.request_id === shutdown.request_id && item.status === "closed"), false);
    terminalGate.resolve();
    await waitFor(() => h.state.receipts.some((item) => item.request_id === shutdown.request_id && item.status === "closed"),
      "shutdown waits for the original child terminal report");
    assert.equal(run.launches, 1);
    assert.equal(h.state.toolCalls.some((item) => item.bindingId === "child"), false);
    h.assertClean();
    await h.stop(actor);
  });

test("model cancellation recovers a lost ACK by the same original invocation", async () => {
  const invocation = { invocation_id: digest("one original cancellation") };
  const result_json = JSON.stringify({ content: [{ type: "text", text: "Cancellation requested." }] });
  const receipt = { protocol: PROTOCOL, confirmed: true, invocation_id: invocation.invocation_id,
    result_json, result_digest: digest(result_json) };
  const parent = { spec: { helper_background: true }, toolNames: new Map(), unresolvedInvocations: new Set(),
    session: { getActiveToolNames: () => ["delegate_helper_background", "background_helper_status", "cancel_helper"] },
    assertActive: () => {},
    toolInvocation: async (name) => { assert.equal(name, "cancel_helper"); return invocation; } };
  let attempts = 0;
  const helpers = new ForegroundHelpers({}, { helper: async (slot, original, action) => {
    assert.equal(slot, parent);
    assert.equal(original, invocation);
    assert.equal(action, "model-cancel");
    if (++attempts === 1) throw new TypeError("The saved acknowledgement was lost");
    return receipt;
  } });
  const result = await helpers.execute(parent, "cancel-call", { child_run_id: "run-child" }, undefined, "cancel_helper");
  assert.equal(attempts, 2);
  assert.equal(result.content[0].text, "Cancellation requested.");
  assert.equal(parent.unresolvedInvocations.size, 0);
});

test("an unresolved original background close cannot disappear from Task shutdown proof", async () => {
  const helpers = new ForegroundHelpers({}, {});
  const child = { background: true, controller: new AbortController(), done: { promise: Promise.resolve() } };
  helpers.active.set("original-child", child);
  await assert.rejects(helpers.closeBinding("original-child"), /original helper exit is unresolved/);
  assert.equal(helpers.active.get("original-child"), child);
  assert.equal(await helpers.stopAll(), false);
});


test("direct P6a hosted helper calls the trusted Pi sender independently", {
  timeout: 20000, skip: !process.env.BBX_TEST_PI_RUNTIME,
}, async (t) => {
  const { h, run, actor } = await setup(t, "direct");
  await waitFor(() => run.outcome && h.state.receipts.some((item) =>
    item.request_id === run.close?.request_id && item.status === "closed" && item.journal_confirmed), "direct helper original result");
  assert.equal(run.launches, 1);
  assert.equal(h.state.toolCalls.filter((item) => item.bindingId === "child").length, 1);
  assert.equal(h.state.modelCalls.filter((item) => item.bindingId === "child").length, 2);
  assert.ok(h.state.modelCalls.some((item) => item.bindingId === "parent"));
  assert.equal(h.state.attempts.size, 0);
  h.assertClean();
  await h.stop(actor);
});
