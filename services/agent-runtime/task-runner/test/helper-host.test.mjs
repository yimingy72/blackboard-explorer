import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { digest, validate } from "../src/protocol.mjs";
import { HttpPlatform, MountedCredentials } from "../src/platform.mjs";
import { RunnerSession } from "../src/session.mjs";
import { deferred, fixture, waitFor } from "./support.mjs";

const parentIds = ["parent-a", "parent-b"];
const childIds = ["child-a", "child-b"];
const remote = {
  name: "exec_command", description: "Synthetic Sandbox command", inputSchema: { type: "object",
    required: ["value"], properties: { value: { type: "string" } }, additionalProperties: false },
};
const delegate = { name: "delegate_helper", arguments: {
  goal: "Read the scoped test marker.", input_refs: [], expected_output: "One marker.",
} };

test("a Worker cannot start a foreground helper profile not frozen at Runner bootstrap", async (t) => {
  const h = await fixture(t);
  const actor = h.start();
  const prepare = h.command({ kind: "prepare", spec: { ...h.spec("one"), helper_foreground: true } });
  await waitFor(() => h.state.receipts.find((item) => item.request_id === prepare.request_id && item.status === "rejected"), "untrusted helper profile refused");
  assert.deepEqual(h.state.leases, []);
  await h.stop(actor);
});

test("a partially initialized Worker plugin still receives shutdown", async (t) => {
  const h = await fixture(t);
  const extension = join(h.directory, "startup-failure.mjs");
  const marker = join(h.directory, "shutdown-observed");
  await writeFile(extension, `import { writeFile } from "node:fs/promises";
    export default function (pi) {
      pi.on("session_start", () => { throw new Error("synthetic partial startup"); });
      pi.on("session_shutdown", () => writeFile(${JSON.stringify(marker)}, "closed"));
    }`);
  const credentials = new MountedCredentials(h.config.credential_file, "task");
  const platform = new HttpPlatform(h.config, credentials);
  const spec = { ...h.spec("one"), helper_foreground: true };
  const command = { ...h.command({ kind: "prepare", spec }), payload: { kind: "prepare", spec } };
  const lease = await platform.acquire(command);
  const helpers = { parentPaths: async () => [extension],
    tool: () => ({ name: "delegate_helper", label: "Fixture", description: "Fixture", parameters: { type: "object" } }),
    cancelParent: () => {}, stopParent: async () => {} };
  const slot = new RunnerSession(h.config, spec, command.payload_digest, lease, platform, credentials, undefined, helpers);
  try { await assert.rejects(slot.initialize(), /Trusted helper plugin startup failed/); }
  finally { await slot.close(); }
  assert.equal(await readFile(marker, "utf8"), "closed");
});

test("the actual parent model launches one plugin child; cancelling a second parent during setup is scoped",
  { timeout: 25000 }, async (t) => {
    const h = await fixture(t, [...parentIds, ...childIds]);
    await h.configure({ helper_foreground: true });
    const mount = Object.fromEntries(Object.entries(h.credentials).filter(([key]) => !key.includes("child")));
    await writeFile(h.config.credential_file, JSON.stringify(mount));
    const helper = new Map();
    const childAList = deferred();
    const releaseChildAList = deferred();
    const childBModel = deferred();
    const releaseChildB = deferred();
    t.after(() => { releaseChildAList.resolve(); releaseChildB.resolve(); });
    h.state.holdToolsList = "child-a";
    h.state.toolsListEntered = childAList;
    h.state.toolsListGate = releaseChildAList;
    h.state.logicalNames = new Map([...parentIds, ...childIds].map((id) => [id, "exec_command"]));
    h.state.wireNames = h.state.logicalNames;
    h.state.remoteTools = new Map([...parentIds, ...childIds].map((id) => [id, [remote]]));
    h.state.modelGate = async (id, index) => {
      if (id === "child-b" && index === 1) { childBModel.resolve(); await releaseChildB.promise; }
    };
    h.state.modelTool = (bindingId, index, body) => {
      const expected = parentIds.includes(bindingId) ? ["delegate_helper", "exec_command"] : ["exec_command"];
      assert.deepEqual(body.tools.map((item) => item.function.name).sort(), expected);
      if (index !== 1) return null;
      return parentIds.includes(bindingId) ? delegate : { name: "exec_command", arguments: { value: "marker" } };
    };
    function spec(id) {
      return { ...h.spec(id), allowed_tools: ["exec_command"], mcp_tool_aliases: { exec_command: "exec_command" } };
    }
    h.state.helperApi = async ({ url, bindingId, body, response, send }) => {
      const action = url.pathname.split("/").at(-1);
      const id = childIds[parentIds.indexOf(bindingId)];
      let run = helper.get(bindingId);
      if (action === "reserve") {
        validate("ToolInvocation", body);
        assert.equal(body.tool_name, "delegate_helper");
        assert.equal(body.binding.binding_id, bindingId);
        assert.deepEqual(body.journal, h.state.journal.get(bindingId).cursor);
        assert.equal(run, undefined);
        run = { state: "reserved", invocation_id: body.invocation_id, child_id: `run-${id}`,
          binding_id: id, deadline_ms: Date.now() + 20000 };
        helper.set(bindingId, run);
      } else if (action === "launch") {
        assert.equal(run.state, "reserved");
        assert.equal(body.invocation_id, run.invocation_id);
        run.state = "launch_claimed";
        const prepare = h.command({ kind: "prepare", spec: { ...spec(id),
          helper_child: { child_id: run.child_id, parent_binding_id: bindingId, deadline_ms: run.deadline_ms } } }, `prepare-${id}`);
        h.state.commands.pop(); // Its original prepare is available only through the launch grant.
        send(response, { ...run, launch_allowed: true, command: prepare, credentials: {
          [`model-${id}`]: h.credentials[`model-${id}`], [`mcp-${id}`]: h.credentials[`mcp-${id}`],
        } });
        return;
      } else if (action === "cancel") {
        if (run.state !== "saved") run.state = "draining";
      } else if (action === "terminal") {
        assert.equal(body.child_id, run.child_id);
        assert.equal(body.binding_id, id);
        assert.equal(body.disposed, true);
        assert.equal(body.sdk_idle, true);
        assert.deepEqual(body.cursor, h.state.journal.get(id).cursor);
        assert.ok(h.state.receipts.some((item) => item.helper_run_id === body.plugin_run_id
          && item.sdk_session_id === body.sdk_session_id && item.status === "prepared"));
        if (!run.terminal) {
          run.terminal = body;
          h.command({ kind: "close", binding_id: id });
          response.destroy(); // Persisted under the original proof; the ACK does not reach the host.
          return;
        }
        assert.deepEqual(body, run.terminal);
        run.terminalRetried = true;
      } else if (action === "result") {
        assert.ok(run.terminal);
        run.state = "saved";
        run.result_json = body.result_json;
        run.result_digest = digest(body.result_json);
      } else assert.equal(action, run.invocation_id);
      send(response, run);
    };
    const actor = h.start();
    try {
      for (const id of parentIds) h.command({ kind: "prepare", spec: { ...spec(id), helper_foreground: true } });
      await waitFor(() => parentIds.every((id) => h.state.receipts.some((r) => r.binding_id === id && r.status === "prepared")), "two parent preparations");
      for (const id of parentIds) h.command({ kind: "input", binding_id: id, input_id: `input-${id}`,
        content: "Delegate the scoped goal.", mode: "follow_up" });
      await Promise.race([
        Promise.all([childAList.promise, childBModel.promise]),
        waitFor(() => h.state.errors.length && Promise.reject(h.state.errors[0]), "child startup", 10000),
      ]);
      const closeA = h.command({ kind: "close", binding_id: "parent-a" });
      await waitFor(() => helper.get("parent-a")?.state === "draining", "parent A cancellation recorded");
      releaseChildAList.resolve();
      await waitFor(() => h.state.receipts.find((r) => r.request_id === closeA.request_id && r.status === "closed"), "parent A closed");
      releaseChildB.resolve();
      await waitFor(() => helper.get("parent-b")?.state === "saved" &&
        h.state.modelCalls.filter((item) => item.bindingId === "parent-b").length === 2, "unaffected parent B result");
      assert.equal(h.state.modelCalls.some((item) => item.bindingId === "child-a"), false);
      assert.equal(helper.get("parent-a").terminal, undefined);
      assert.equal(h.state.toolCalls.filter((item) => item.bindingId === "child-b").length, 1);
      assert.equal(helper.get("parent-b").terminalRetried, true);
      assert.notEqual(h.state.journal.get("child-b").sdk_session_id, h.state.journal.get("parent-b").sdk_session_id);
      const journal = await readFile(join(h.config.journal_root, "child-b", "session.jsonl"));
      assert.equal(journal.length, helper.get("parent-b").terminal.cursor.offset);
      const workerB = h.state.modelCalls.filter((item) => item.bindingId === "parent-b").at(-1);
      const childB = h.state.modelCalls.filter((item) => item.bindingId === "child-b").at(-1);
      assert.ok(JSON.stringify(childB.body.messages).includes("child-b remote result"));
      assert.ok(JSON.stringify(workerB.body.messages.filter((item) => item.role === "tool")).includes("Done."));
      assert.ok(!JSON.parse(helper.get("parent-b").result_json).isError);
      h.assertClean();
    } catch (error) {
      throw new Error(`${error.message}\n${actor.output}\n${h.state.errors.map((item) => item.message).join("\n")}\n${JSON.stringify({
        model: h.state.modelCalls.map((item) => item.bindingId), helper: [...helper.entries()].map(([id, entry]) => [id, entry.state]),
        leases: h.state.leases.map((item) => item.binding.binding_id), connections: h.state.connections,
        modelReceipts: h.state.modelReceipts.map((item) => [item.binding_id, item.model_outcome]),
      })}`, { cause: error });
    } finally {
      releaseChildAList.resolve(); releaseChildB.resolve();
      await h.stop(actor);
    }
  });
