import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import test from "node:test";
import { HttpPlatform, MountedCredentials } from "../src/platform.mjs";
import { validate } from "../src/protocol.mjs";
import { TaskRunner } from "../src/runner.mjs";
import { fixture, waitFor } from "./support.mjs";

test("official Pi Runner connects both frozen MCP targets, pages and routes the original permits", { timeout: 15000 }, async (t) => {
  const h = await fixture(t);
  h.state.directMcp = true;
  h.state.directTargetByTool = new Map([["business_echo", "business"], ["sandbox_echo", "sandbox"]]);
  const remote = { name: "same_echo", description: "Synthetic echo",
    inputSchema: { type: "object", properties: { value: { type: "string" } }, required: ["value"] } };
  h.state.directTools = new Map([["business", (cursor) => cursor ? [remote] : []], ["sandbox", () => [remote]]]);
  h.state.modelTool = (_binding, index) => index <= 2 ? {
    name: index === 1 ? "business_echo" : "sandbox_echo", arguments: { value: "synthetic" },
  } : null;
  h.state.dropToolReplyFor = "one";

  // The production sandbox destination is fixed Pod loopback. Route only that exact destination
  // into the owned synthetic HTTP fixture so no reserved host port or gateway is needed.
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (input, init) => new URL(input).href === "http://127.0.0.1:8766/mcp/"
    ? originalFetch(h.base + "/mcp/", init) : originalFetch(input, init);
  t.after(() => { globalThis.fetch = originalFetch; });

  const spec = h.spec("one");
  delete spec.mcp_credential_ref;
  delete spec.mcp_tool_aliases;
  spec.allowed_tools = ["business_echo", "sandbox_echo"];
  spec.mcp_target_credential_refs = { business: "business-ref", sandbox: "sandbox-ref" };
  spec.mcp_tool_routes = Object.fromEntries([...h.state.directTargetByTool].map(([name, target]) =>
    [name, { target, wire_name: "same_echo" }]));
  const credentials = { control: h.credentials.control, "model-one": h.credentials["model-one"] };
  for (const target of ["business", "sandbox"]) credentials[`${target}-ref`] = {
    audience: `${target}-mcp`, binding_id: "one", task_id: "task", expires_at: 4102444800,
    token: h.token("one", `${target}-mcp`),
  };
  await writeFile(h.config.credential_file, JSON.stringify(credentials));
  await h.configure({ mcp_gateway_url: undefined, mcp_targets: {
    business: h.base + "/api/tasks/task/business-mcp-v2/", sandbox: "http://127.0.0.1:8766/mcp/",
  } });
  const config = validate("RunnerBootstrap", JSON.parse(JSON.stringify(h.config)));
  const mount = new MountedCredentials(config.credential_file, "task");
  const runner = new TaskRunner(config, new HttpPlatform(config, mount), mount);
  const running = runner.run();
  t.after(async () => { await runner.stop(); await running; });

  const prepare = h.command({ kind: "prepare", spec });
  const prepared = await waitFor(() => h.state.receipts.find(r => r.request_id === prepare.request_id && r.status === "prepared"), "direct Runner prepared");
  assert.deepEqual(h.state.directLists.map(({target, cursor}) => [target, cursor]), [
    ["business", null], ["business", "approved-page"], ["sandbox", null],
  ]);
  assert.ok(h.state.directLists.every(({sdk}) => sdk === prepared.sdk_session_id));
  h.command({ kind: "input", binding_id: "one", input_id: "synthetic-input", content: "Use both synthetic echoes.", mode: "follow_up" });
  await waitFor(() => h.state.toolCalls.length === 2 && h.state.modelReceipts.length === 3, "two independently scoped tools");
  assert.deepEqual(h.state.toolCalls.map(({target, wireName, invocation}) => [target, wireName, invocation.tool_name]), [
    ["business", "same_echo", "business_echo"], ["sandbox", "same_echo", "sandbox_echo"],
  ]);
  assert.equal(h.state.connections.length, 2);
  assert.equal(h.state.droppedToolReply, true);
  assert.equal(h.state.invocations.size, 2, "lost result acknowledgement used the original receipt");
  h.assertClean();
});
