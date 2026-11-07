import assert from "node:assert/strict";
import test from "node:test";
import { access, readFile, readdir, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { decodeCommand, digest, jsonText } from "../src/protocol.mjs";
import { HttpPlatform, MountedCredentials } from "../src/platform.mjs";
import { TaskRunner } from "../src/runner.mjs";
import { deferred, fixture, waitFor } from "./support.mjs";

async function project(h, records) {
  const next = `${h.config.credential_file}.next`;
  await writeFile(next, JSON.stringify(records));
  await rename(next, h.config.credential_file);
}

test("a new binding waits for its projected credentials under a renewable lease", { timeout: 15000 }, async (t) => {
  const h = await fixture(t, ["existing", "late"]);
  h.state.enableRenewals = true;
  const records = { ...h.credentials };
  delete records["model-late"]; delete records["mcp-late"];
  await project(h, records);
  const credentials = new MountedCredentials(h.config.credential_file, "task");
  const runner = new TaskRunner(h.config, new HttpPlatform(h.config, credentials), credentials);
  const running = runner.run();
  t.after(async () => { await runner.stop(); await running; });
  h.command({ kind: "prepare", spec: h.spec("existing") });
  const payload = { kind: "prepare", spec: h.spec("late") };
  const late = h.command(payload);
  const waiting = await waitFor(() => runner.preparingSlots.get("late"), "late binding acquired");
  const initialExpiry = waiting.lease.lease_expires_at;
  await waitFor(() => runner.sessions.has("existing"), "existing session prepared");
  h.command({ kind: "input", binding_id: "existing", input_id: "existing-input", content: "Continue.", mode: "follow_up" });
  await waitFor(() => h.state.modelCalls.some((call) => call.bindingId === "existing"), "existing session continues");
  records["model-late"] = h.credentials["model-late"];
  await project(h, records);
  await waitFor(() => Date.now() / 1000 > initialExpiry + 0.1, "wait crosses the original lease", 6500);
  assert.ok(waiting.lease.lease_expires_at > initialExpiry);
  assert.equal(waiting.sdkSessionId, undefined);
  await assert.rejects(access(join(h.config.journal_root, "late")), { code: "ENOENT" });
  assert.equal(h.state.connections.filter((item) => item.bindingId === "late").length, 0);

  await project(h, h.credentials);
  await waitFor(() => h.state.receipts.some((r) => r.request_id === late.request_id && r.status === "prepared"), "projected binding prepared");
  h.command(payload, late.request_id);
  await waitFor(() => h.state.receipts.filter((r) => r.request_id === late.request_id && r.status === "prepared").length === 2, "same prepare receipt replayed");
  assert.equal(h.state.leases.filter((lease) => lease.binding.binding_id === "late").length, 1);
  assert.equal(h.state.connections.filter((item) => item.bindingId === "late").length, 1);
  h.assertClean();
});

test("prepare rejects conflicting or expired mounts and stops in-flight work before SDK creation", { timeout: 15000 }, async (t) => {
  const ids = ["closing", "conflict", "expired", "aborting", "shutdown"];
  const h = await fixture(t, ids);
  h.state.enableRenewals = true;
  const records = { ...h.credentials, "mcp-conflict": { ...h.credentials["mcp-conflict"], binding_id: "different" } };
  for (const id of ["closing", "expired"]) delete records[`mcp-${id}`];
  delete records["model-conflict"];
  await project(h, records);
  const credentials = new MountedCredentials(h.config.credential_file, "task");
  const runner = new TaskRunner(h.config, new HttpPlatform(h.config, credentials), credentials);
  const running = runner.run();
  t.after(async () => { await runner.stop(); await running; });
  const closing = h.command({ kind: "prepare", spec: h.spec("closing") });
  const conflict = h.command({ kind: "prepare", spec: h.spec("conflict") });
  const expired = h.command({ kind: "prepare", spec: h.spec("expired") });
  const rejected = (command) => h.state.receipts.some((r) => r.request_id === command.request_id && r.status === "rejected");
  await waitFor(() => runner.preparingSlots.has("closing") && runner.preparingSlots.has("expired"), "bindings wait on projection");
  h.state.enableRenewals = false;
  await waitFor(() => rejected(conflict), "conflict is rejected even while another reference is missing", 1500);
  const close = h.command({ kind: "close", binding_id: "closing" });
  const closed = await waitFor(() => h.state.receipts.find((r) => r.request_id === close.request_id && r.status === "closed"), "close cancels projection wait", 1500);
  assert.equal(closed.journal_confirmed, false);
  await waitFor(() => rejected(closing) && rejected(expired), "closed and expired prepares are rejected", 6500);

  const abortGate = deferred(), shutdownGate = deferred();
  h.state.acquireGates = new Map([["aborting", abortGate], ["shutdown", shutdownGate]]);
  const aborting = h.command({ kind: "prepare", spec: h.spec("aborting") });
  await waitFor(() => h.state.leases.some((lease) => lease.binding.binding_id === "aborting"), "acquire is in flight");
  h.command({ kind: "abort", binding_id: "aborting" });
  await waitFor(() => runner.abortedBindings.has("aborting"), "abort marks the binding");
  abortGate.resolve();
  await waitFor(() => rejected(aborting), "late acquire cannot create an aborted slot");
  h.command({ kind: "prepare", spec: h.spec("shutdown") });
  await waitFor(() => h.state.leases.some((lease) => lease.binding.binding_id === "shutdown"), "shutdown acquire is in flight");
  await runner.stop();
  await running;
  shutdownGate.resolve();
  assert.equal(h.state.connections.length, 0);
  assert.equal(runner.preparingSlots.size, 0);
  for (const id of ids) await assert.rejects(access(join(h.config.journal_root, id)), { code: "ENOENT" });
  h.assertClean();
});

test("trusted MCP aliases reject missing declarations, ambiguous catalogs and foreign name lookalikes", async (t) => {
  const ids = ["missing", "keys", "duplicate", "allowed", "ambiguous", "foreign", "raw"];
  const h = await fixture(t, ids);
  const credentials = new MountedCredentials(h.config.credential_file, "task");
  const runner = new TaskRunner(h.config, new HttpPlatform(h.config, credentials), credentials);
  t.after(() => runner.stop());
  const remote = { name: "business_remote_echo", inputSchema: { type: "object", properties: {} } };
  h.state.remoteTools = new Map([
    ["ambiguous", [remote, remote]], ["foreign", [{ ...remote, name: "foreign_remote_echo" }]],
    ["raw", [{ ...remote, name: "remote_echo" }]],
  ]);
  for (const id of ids) {
    const spec = h.spec(id);
    spec.mcp_tool_aliases = { remote_echo: "business_remote_echo" };
    if (id === "missing") delete spec.mcp_tool_aliases;
    if (id === "keys") spec.mcp_tool_aliases = { undeclared: "business_remote_echo" };
    if (id === "duplicate") {
      spec.allowed_tools.push("other");
      spec.mcp_tool_aliases.other = "business_remote_echo";
    }
    if (id === "allowed") spec.allowed_tools.push("remote_echo");
    const command = h.command({ kind: "prepare", spec });
    if (id === "missing") assert.throws(() => decodeCommand(command), /Invalid RunnerPayload/);
    else await assert.rejects(runner.handle(decodeCommand(command)), /aliases|unavailable, ambiguous/);
  }
  assert.equal(runner.sessions.size, 0);
  assert.equal(h.state.modelCalls.length, 0);
  assert.equal(h.state.toolCalls.length, 0);
  assert.equal(h.state.connections.length, 3, "invalid maps fail before contacting MCP");
  h.assertClean();
});

test("prepare ACK loss reopens the same native journal; input is applied only after confirmation and poll can abort", { timeout: 30000 }, async (t) => {
  const h = await fixture(t);
  h.state.dropPrepare = true;
  const prepare = h.command({ kind: "prepare", spec: h.spec("one") }, "prepare-one");
  const first = h.start();
  const original = await waitFor(() => h.state.receipts.find((receipt) => receipt.request_id === prepare.request_id), `first prepare (${first.output})`);
  assert.equal(original.status, "prepared", JSON.stringify(original));
  assert.deepEqual(original.supported_business_contracts, ["challenge-board-v1"]);
  assert.notEqual(original.sdk_session_id, "platform-one");
  await h.stop(first, "SIGKILL");
  const second = h.start();
  const restored = await waitFor(() => h.state.receipts.filter((receipt) => receipt.request_id === prepare.request_id).at(1), "restored prepare");
  assert.equal(restored.status, "prepared", JSON.stringify(restored));
  assert.equal(restored.sdk_session_id, original.sdk_session_id);
  assert.equal((await readdir(join(h.config.journal_root, "one"))).filter((name) => name.endsWith(".jsonl")).length, 1);
  h.state.holdApplied = true;
  const input = h.command({ kind: "input", binding_id: "one", input_id: "input-one", content: "\u0001".repeat(20000), mode: "follow_up" });
  await h.state.appliedEntered.promise;
  await waitFor(() => h.state.receipts.some((receipt) => receipt.request_id === input.request_id && receipt.status === "accepted"), "input accepted");
  assert.equal(h.state.receipts.some((receipt) => receipt.request_id === input.request_id && receipt.status === "applied"), false);
  assert.equal(h.state.modelCalls.length, 0);
  const abort = h.command({ kind: "abort", binding_id: "one" });
  const stopped = await waitFor(() => h.state.receipts.find((receipt) => receipt.request_id === abort.request_id), "abort while journal ACK is pending", 6000);
  assert.equal(stopped.status, "aborted", JSON.stringify(stopped));
  assert.equal(stopped.sdk_idle, true);
  assert.equal(stopped.journal_confirmed, false);
  const stale = h.command({ kind: "input", binding_id: "one", input_id: "stale-input", content: "Stale epoch input." }, "stale-command", { ...h.config.fence, epoch: 2 });
  const staleReceipt = await waitFor(() => h.state.receipts.find((receipt) => receipt.request_id === stale.request_id), "stale fence refusal");
  assert.equal(staleReceipt.error_code, "stale_fence");
  h.command(JSON.parse(abort.payload_json), abort.request_id, { ...h.config.fence, epoch: 2 });
  await waitFor(() => h.state.receipts.some((receipt) => receipt.request_id === abort.request_id && receipt.error_code === "stale_fence"), "cached operation still checks the fence");
  const abortReceipts = h.state.receipts.filter((receipt) => receipt.request_id === abort.request_id).length;
  h.command(JSON.parse(abort.payload_json), abort.request_id);
  const repeatedAbort = await waitFor(() => h.state.receipts.filter((receipt) => receipt.request_id === abort.request_id).at(abortReceipts), "current fence retains the original operation");
  assert.equal(repeatedAbort.status, "aborted");
  const close = h.command({ kind: "close", binding_id: "one" });
  const closed = await waitFor(() => h.state.receipts.find((receipt) => receipt.request_id === close.request_id), "close with an uncertain journal confirmation", 6000);
  assert.equal(closed.status, "closed");
  assert.equal(closed.sdk_idle, true);
  assert.equal(closed.journal_confirmed, false);
  assert.equal(closed.pending_confirmation.request_id, h.state.confirmations.find((confirmation) => confirmation.applied_inputs.length).request_id);
  assert.deepEqual(closed.cursor, stopped.cursor);
  h.state.appliedGate.resolve();
  assert.equal(h.state.modelCalls.length, 0);
  await h.stop(second);
  const nativeInputLine = (await readFile(join(h.config.journal_root, "one/session.jsonl"), "utf8")).split("\n")
    .find((line) => line.includes('"customType":"bbx.input"'));
  assert.ok(Buffer.byteLength(nativeInputLine) > 120000 && Buffer.byteLength(nativeInputLine) < 131072);
  h.assertClean();
});

test("two native sessions concurrently calling the same MCP tool and arguments keep independent identities", { timeout: 30000 }, async (t) => {
  let runner;
  let running;
  let exitCode = null;
  t.after(async () => { await runner?.stop(); await running; });
  const h = await fixture(t, ["one", "two", "preparing"]);
  h.state.useTools = true;
  h.state.enableRenewals = true;
  h.state.hotPrompt = true;
  h.state.manualToolBarrier = true;
  h.state.dropToolReplyFor = "two";
  const remote = { name: "business_remote_echo", inputSchema: { type: "object", properties: { value: { type: "string" } }, required: ["value"] } };
  h.state.remoteTools = new Map(["one", "two"].map((id) => [id, [remote,
    { ...remote, name: "foreign_remote_echo" }, { ...remote, name: "business_undeclared" }]]));
  h.state.wireNames = new Map(["one", "two"].map((id) => [id, "business_remote_echo"]));
  await h.configure({ mcp_metadata_timeout_ms: 500 });
  h.state.toolBarrier = deferred();
  const firstSpec = h.spec("one");
  firstSpec.mcp_tool_aliases = { remote_echo: "business_remote_echo" };
  firstSpec.model = { ...firstSpec.model, input: ["text", "image"], reasoning: true, thinking_level: "high",
    thinking_level_map: { off: "none", high: "high" } };
  const secondSpec = h.spec("two");
  secondSpec.mcp_tool_aliases = { remote_echo: "business_remote_echo" };
  secondSpec.model = { ...secondSpec.model, reasoning: true, thinking_level: "high",
    thinking_level_map: { off: "none", high: "high" } };
  h.command({ kind: "prepare", spec: firstSpec }, "prepare-one");
  h.command({ kind: "prepare", spec: secondSpec }, "prepare-two");
  const credentials = new MountedCredentials(h.config.credential_file, "task");
  runner = new TaskRunner(h.config, new HttpPlatform(h.config, credentials), credentials);
  running = runner.run().then(() => { exitCode = 0; }, () => { exitCode = 1; });
  await waitFor(() => h.state.receipts.filter((receipt) => receipt.status === "prepared").length === 2, "two prepared sessions");
  assert.equal(runner.preparing.size, 0, "resolved preparation must not retain SDK sessions");
  const activeOne = runner.sessions.get("one");
  const initialLease = activeOne.lease;
  assert.equal(activeOne.renew({ ...initialLease, sdk_session_id: null, lease_revision: initialLease.lease_revision + 1,
    lease_expires_at: initialLease.lease_expires_at + 10 }), false, "a preparation-era null SDK renewal is ignored");
  assert.equal(activeOne.lease, initialLease, "an ignored old renewal cannot extend the open session permit");
  const originalExpiry = Math.max(...[...h.state.leaseStates.values()].map((lease) => lease.lease_expires_at));
  h.command({ kind: "input", binding_id: "one", input_id: "same-input-one", content: "Use the tool.", mode: "follow_up" });
  h.command({ kind: "input", binding_id: "two", input_id: "same-input-two", content: "Use the tool.", mode: "follow_up" });
  await waitFor(() => h.state.toolCalls.length === 2, "two concurrent MCP calls");
  await waitFor(() => Date.now() / 1000 > originalExpiry + 0.2, "operation crossing its initial short permit", 7000);
  assert.ok(h.state.toolCalls.every((call) => Date.now() - call.receivedAt > 500), "the admitted wait outlives metadata connection timeout");
  assert.equal(new Set(h.state.renewalHistory.map((lease) => lease.binding_id)).size, 2);
  h.state.toolBarrier.resolve();
  await waitFor(() => h.state.modelReceipts.filter((receipt) => receipt.model_outcome === "completed").length === 4, "both native tool loops complete");
  const [left, right] = h.state.toolCalls;
  assert.equal(left.invocation.tool_call_id, right.invocation.tool_call_id);
  assert.deepEqual(left.args, right.args);
  assert.notEqual(left.invocation.invocation_id, right.invocation.invocation_id);
  assert.notEqual(left.invocation.sdk_session_id, right.invocation.sdk_session_id);
  assert.notEqual(left.sessionId, right.sessionId);
  assert.notEqual(left.bindingId, right.bindingId);
  assert.equal(left.wireName, "business_remote_echo");
  assert.equal(left.invocation.tool_name, "remote_echo");
  assert.equal(left.invocation.invocation_id, digest(jsonText(["task", left.bindingId,
    left.invocation.sdk_session_id, left.invocation.assistant_entry_id, left.invocation.tool_call_id, "remote_echo"])));
  for (const name of ["business_remote_echo", "foreign_remote_echo", "business_undeclared"]) {
    await assert.rejects(activeOne.callTool(name, left.invocation.tool_call_id, left.args), /not declared/);
  }
  assert.equal(h.state.toolCalls.length, 2);
  const firstCalls = h.state.modelCalls.filter((call) => call.bindingId === "one");
  assert.deepEqual(firstCalls.map((call) => call.promptRevision), [1, 2]);
  assert.ok(JSON.stringify(firstCalls[0].body.messages).includes("OLD ROLE one"));
  assert.ok(JSON.stringify(firstCalls[1].body.messages).includes("NEW ROLE one"));
  assert.equal(JSON.stringify(firstCalls[1].body.messages).includes("OLD ROLE one"), false);
  assert.ok(JSON.stringify(firstCalls[1].body.messages).includes("one remote result"));
  assert.equal(JSON.stringify(firstCalls[1].body.messages).includes("Untrusted transport copy"), false);
  assert.ok(firstCalls.every((call) => call.body.reasoning_effort === "high"));
  const secondCalls = h.state.modelCalls.filter((call) => call.bindingId === "two");
  assert.ok(secondCalls.every((call) => call.body.reasoning_effort === "high" && call.body.thinking === undefined));
  assert.ok(secondCalls.every((call) => call.body.max_completion_tokens === secondSpec.model.max_output_tokens && call.body.max_tokens === undefined));
  assert.ok(firstCalls.every((call) => call.body.max_completion_tokens === firstSpec.model.max_output_tokens && call.body.max_tokens === undefined));
  assert.equal(h.state.droppedToolReply, true);
  assert.deepEqual(firstCalls.map((call) => call.body.tools.map((tool) => tool.function.name)), [["remote_echo"], ["remote_echo"]]);
  assert.deepEqual([...h.state.attempts.values()].filter((attempt) => attempt.binding.binding_id === "one")[0].model_snapshot.input, ["text", "image"]);
  for (const id of ["one", "two"]) {
    const journal = await readFile(join(h.config.journal_root, id, "session.jsonl"), "utf8");
    for (const credential of Object.values(h.credentials)) assert.equal(journal.includes(credential.token), false);
    const inputEntries = journal.split("\n").filter(Boolean).map(JSON.parse).filter((entry) => entry.customType === "bbx.input");
    assert.equal(inputEntries.length, 1);
  }
  const prepare = h.state.commands[0];
  const payload = JSON.parse(prepare.payload_json);
  const invalid = jsonText({ ...payload, token: "must-not-be-persisted" });
  assert.throws(() => decodeCommand({ ...prepare, payload_json: invalid, payload_digest: digest(invalid) }), /Invalid RunnerPayload/);
  const conflicting = { ...payload, spec: { ...payload.spec, instructions: "Conflicting prepare." } };
  h.command(conflicting, prepare.request_id);
  await waitFor(() => h.state.receipts.some((receipt) => receipt.request_id === prepare.request_id && receipt.error_code === "request_conflict"), "request conflict");
  assert.equal(h.state.modelCalls.length, 4);
  assert.equal(h.state.commands.every((command) => command.payload_digest === digest(command.payload_json)), true);
  const abortOne = h.command({ kind: "abort", binding_id: "one" });
  await waitFor(() => h.state.receipts.some((receipt) => receipt.request_id === abortOne.request_id && receipt.status === "aborted"), "one session aborted");
  const oneGrant = h.state.leaseStates.get("one");
  h.state.lateRenewals.push({ ...oneGrant, lease_revision: oneGrant.lease_revision + 1,
    lease_expires_at: Math.floor(Date.now() / 1000) + 10, sdk_session_id: h.state.journal.get("one").sdk_session_id,
    cursor: h.state.journal.get("one").cursor });
  const one = runner.sessions.get("one");
  const oneMcp = one.mcp;
  const oneFile = one.journal.file;
  one.unresolvedAttempts.add("unresolved-attempt"); one.unresolvedInvocations.add("unresolved-invocation");
  const closeOne = h.command({ kind: "close", binding_id: "one" });
  const closedOne = await waitFor(() => h.state.receipts.find((receipt) => receipt.request_id === closeOne.request_id), "one local session released");
  assert.equal(closedOne.status, "closed");
  assert.deepEqual(closedOne.unresolved_attempts, ["unresolved-attempt"]);
  assert.deepEqual(closedOne.unresolved_invocations, ["unresolved-invocation"]);
  assert.equal(runner.sessions.has("one"), false);
  assert.equal(runner.preparing.has("one"), false);
  for (const field of ["session", "mcp", "transport", "journal", "spec", "promptText"]) assert.equal(one[field], undefined);
  assert.equal(oneMcp.transport, undefined);
  assert.equal(oneFile.fd, -1);
  const repeatClose = h.command({ kind: "close", binding_id: "one" });
  const repeatedClose = await waitFor(() => h.state.receipts.find((receipt) => receipt.request_id === repeatClose.request_id), "idempotent local close");
  assert.equal(repeatedClose.status, "closed");
  assert.deepEqual(repeatedClose.cursor, closedOne.cursor);
  const acquireCount = h.state.leases.length;
  h.command(JSON.parse(prepare.payload_json), prepare.request_id);
  await waitFor(() => h.state.receipts.some((receipt) => receipt.request_id === prepare.request_id && receipt.error_code === "binding_unavailable"), "late prepare cannot resurrect a closed binding");
  assert.equal(h.state.leases.length, acquireCount);
  h.state.lateRenewals.push({ ...oneGrant, lease_revision: oneGrant.lease_revision + 2,
    lease_expires_at: Math.floor(Date.now() / 1000) + 10, sdk_session_id: closedOne.sdk_session_id, cursor: closedOne.cursor });
  h.command({ kind: "input", binding_id: "two", input_id: "healthy-two-next", content: "Continue the healthy session.", mode: "follow_up" });
  await waitFor(() => h.state.modelReceipts.filter((receipt) => receipt.model_outcome === "completed").length === 5, "healthy session continues despite another session's close and late renewal");
  assert.equal(h.state.modelCalls.filter((call) => call.bindingId === "one").length, 2);
  assert.equal(exitCode, null);
  h.state.holdToolsList = "preparing";
  h.state.toolsListEntered = deferred(); h.state.toolsListGate = deferred();
  const preparing = h.command({ kind: "prepare", spec: h.spec("preparing") });
  await h.state.toolsListEntered.promise;
  const preparingSlot = runner.preparingSlots.get("preparing");
  const preparingMcp = preparingSlot.mcp;
  assert.equal(typeof preparingSlot.sdkSessionId, "string");
  assert.equal(preparingSlot.session, undefined);
  const closePreparing = h.command({ kind: "close", binding_id: "preparing" });
  await waitFor(() => runner.closedBindings.has("preparing"), "close marks the in-flight preparation first");
  await waitFor(() => h.state.receipts.some((receipt) => receipt.request_id === closePreparing.request_id && receipt.status === "closed"), "close finishes an in-flight preparation");
  h.state.toolsListGate.resolve();
  await waitFor(() => h.state.receipts.some((receipt) => receipt.request_id === preparing.request_id && receipt.status === "rejected"), "cancelled prepare is rejected");
  assert.equal(runner.sessions.has("preparing"), false);
  assert.equal(runner.preparing.size, 0);
  assert.equal(preparingSlot.session, undefined);
  assert.equal(preparingSlot.mcp, undefined);
  assert.equal(preparingMcp.transport, undefined);
  const preparingGrant = h.state.leaseStates.get("preparing");
  assert.equal(preparingGrant.sdk_session_id, null);
  h.state.lateRenewals.push({ ...preparingGrant, lease_revision: preparingGrant.lease_revision + 1,
    lease_expires_at: Math.floor(Date.now() / 1000) + 10 });
  await waitFor(() => h.state.lateRenewals.length === 0, "closed prepare ignores its legitimate null SDK renewal");
  assert.equal(exitCode, null);
  h.state.enableRenewals = false;
  const expiry = Math.max(...[...h.state.leaseStates.values()].map((lease) => lease.lease_expires_at));
  await waitFor(() => Date.now() / 1000 > expiry + 0.2, "local permit expiry", 7000);
  const grant = h.state.leaseStates.get("two");
  h.state.lateRenewals.push({ ...grant, lease_revision: grant.lease_revision + 1,
    lease_expires_at: Math.floor(Date.now() / 1000) + 10, sdk_session_id: h.state.journal.get("two").sdk_session_id,
    cursor: h.state.journal.get("two").cursor });
  const expiredInput = h.command({ kind: "input", binding_id: "two", input_id: "expired-input", content: "Cannot revive this session.", mode: "follow_up" });
  await waitFor(() => h.state.receipts.some((receipt) => receipt.request_id === expiredInput.request_id && receipt.status === "rejected"), "expired permit refuses self-revival");
  assert.equal(exitCode, null, "a benign late renewal must not terminate the task runner");
  assert.equal(h.state.modelCalls.length, 5);
  const two = runner.sessions.get("two");
  const twoMcp = two.mcp;
  const twoFile = two.journal.file;
  const closeMcp = twoMcp.close.bind(twoMcp);
  twoMcp.close = async () => { await closeMcp(); throw new Error("Injected MCP close failure"); };
  const failedClose = h.command({ kind: "close", binding_id: "two" });
  const failedReceipt = await waitFor(() => h.state.receipts.find((receipt) => receipt.request_id === failedClose.request_id), "cleanup failure remains visible");
  assert.equal(failedReceipt.status, "rejected");
  assert.equal(failedReceipt.error_code, "boundary_frozen");
  assert.equal(runner.sessions.size, 0);
  assert.equal(two.session, undefined);
  assert.equal(twoMcp.transport, undefined);
  assert.equal(twoFile.fd, -1, "an MCP close error cannot skip releasing the journal descriptor");
  h.state.lateRenewals.push({ ...grant, fence: { ...grant.fence, epoch: grant.fence.epoch + 1 },
    sdk_session_id: h.state.journal.get("two").sdk_session_id, cursor: h.state.journal.get("two").cursor });
  await waitFor(() => exitCode !== null, "a true renewal fence violation is rejected");
  assert.equal(exitCode, 1);
  assert.equal(h.state.modelCalls.length, 5);
  await running;
  const invalidMount = join(h.directory, "invalid-credentials.json");
  await writeFile(invalidMount, JSON.stringify({ ...h.credentials, service: { audience: "service", task_id: "task", token: "forbidden-service-credential" } }));
  await assert.rejects(new MountedCredentials(invalidMount, "task").resolve("control", "runner-control"), /only this task's scoped identities/);
  for (const expiresAt of [undefined, "not-a-number"]) {
    await writeFile(invalidMount, JSON.stringify({ control: { ...h.credentials.control, expires_at: expiresAt } }));
    await assert.rejects(new MountedCredentials(invalidMount, "task").resolve("control", "runner-control"), /scoped credential is unavailable/);
  }
  h.assertClean();
});
