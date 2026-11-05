import assert from "node:assert/strict";
import test from "node:test";
import { fork } from "node:child_process";
import { once } from "node:events";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { FileReceiptStore } from "../../src/gateway-session.mjs";
import { deferred, harness, root, sse } from "../support.mjs";
import { confirmStoppedRuns, terminalProof } from "./cleanup.mjs";

async function waitFor(read, description, timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await read();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${description}`);
}

async function parent(t, h) {
  const directory = join(h.directory, "public-host");
  await mkdir(directory);
  const config = { directory, gatewayUrl: h.gatewayUrl, gatewayToken: "daemon-child-token",
    binding: { taskId: "task", agentId: "daemon-child" }, hostExtension: join(root, "test/fixtures/host-extension.mjs") };
  const path = join(directory, "host.json");
  await writeFile(path, JSON.stringify(config));
  const actor = fork(join(root, "test/daemon/fixtures/public-parent.mjs"), [], {
    env: { PATH: process.env.PATH, PI_CODING_AGENT_DIR: join(directory, "agent"), PI_OFFLINE: "1",
      PI_SUBAGENTS_TEMP_ROOT: join(directory, "plugin-state"), BBX_PI_SEAM_FIXTURE_CONFIG: path,
      PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT: join(root, "node_modules/@earendil-works/pi-coding-agent"),
      NODE_OPTIONS: `--import=${pathToFileURL(join(root, "test/daemon/fixtures/network-guard.mjs")).href}` },
    stdio: ["ignore", "pipe", "pipe", "ipc"],
  });
  let diagnostics = "";
  actor.stderr.on("data", (chunk) => { diagnostics += chunk; });
  actor.stdout.on("data", (chunk) => { diagnostics += chunk; });
  const pending = new Map();
  const ready = deferred();
  let sequence = 0;
  actor.on("message", (message) => {
    if (message.event === "ready") ready.resolve(message);
    else pending.get(message.id)?.resolve(message);
  });
  const exited = once(actor, "exit");
  actor.on("exit", () => { ready.resolve({ event: "failed", diagnostics }); for (const item of pending.values()) item.resolve({ error: `Parent exited: ${diagnostics}` }); });
  const invoke = async (method, args) => {
    if (!actor.connected) throw new Error("The parent fixture is closed");
    const id = ++sequence;
    const reply = deferred();
    pending.set(id, reply);
    actor.send({ id, method, args });
    const message = await reply.promise;
    pending.delete(id);
    if (message.error) throw new Error(message.error);
    return message.result;
  };
  const runs = new Map();
  h.state.beforeCleanup.push(async () => {
    try {
      await confirmStoppedRuns(runs.values(), (run) => invoke("tool", { action: "stop", id: run.asyncId }));
    } finally {
      let timeout;
      await Promise.race([invoke("shutdown").catch(() => {}), new Promise((resolve) => {
        timeout = setTimeout(resolve, 2000);
      })]).finally(() => clearTimeout(timeout));
      if (actor.exitCode === null && actor.signalCode === null) actor.kill();
      await exited;
    }
  });
  const started = await ready.promise;
  assert.equal(started.event, "ready", diagnostics);
  assert.ok(started.tools.includes("subagent"));
  return {
    config, directory, pid: started.pid, diagnostics: () => diagnostics, invoke,
    async tool(args) {
      const outcome = await invoke("tool", args);
      if (outcome.result?.details?.asyncId) {
        const run = outcome.result.details;
        if (!runs.has(run.asyncId)) {
          const retained = { ...run };
          runs.set(run.asyncId, retained);
          const status = JSON.parse(await readFile(join(run.asyncDir, "status.json"), "utf8"));
          if ((status.runId ?? status.id) !== run.asyncId) throw new Error("Runner status identity did not match the launched run");
          retained.runnerProcessInstanceId = status.processTerminal?.runnerProcessInstanceId;
          if (!retained.runnerProcessInstanceId) throw new Error("The launched Runner has no retained process instance");
        }
      }
      return outcome;
    },
    result: async (run) => {
      try {
        const status = JSON.parse(await readFile(join(run.asyncDir, "status.json"), "utf8"));
        return ["complete", "failed", "paused", "stopped"].includes(status.state) ? status : undefined;
      }
      catch (error) { if (error.code === "ENOENT") return undefined; throw error; }
    },
    proof: async (run) => {
      return terminalProof(runs.get(run.asyncId));
    },
  };
}

test("public subagent tool uses the packaged real daemon and watcher for one normal completion", { timeout: 60000 }, async (t) => {
  let childRequests = 0;
  const h = await harness(t, ({ admission }, response) => {
    if (admission.binding.agentId === "daemon-child" && ++childRequests === 1) {
      sse(response, { tool: { id: "daemon-tool", name: "bound_tool", args: {} } });
    } else sse(response, { text: "The fake background work completed." });
  });
  const host = await parent(t, h);
  const launched = await host.tool({ agent: "helper", task: "Read the fake material and report.", async: true });
  assert.equal(launched.isError, false, JSON.stringify(launched));
  const run = launched.result.details;
  assert.ok(run.asyncId, JSON.stringify(launched));
  const result = await waitFor(() => host.result(run), `real daemon result: ${host.diagnostics()}`);
  const snapshot = await waitFor(async () => {
    const snapshot = await host.invoke("snapshot");
    return snapshot.notices.some((notice) => notice.details?.runs?.some((item) => (item.runId ?? item.id) === run.asyncId)) ? snapshot : undefined;
  }, "watcher completion notification");
  const proof = await waitFor(() => host.proof(run), "real runner process-terminal proof");
  assert.equal(result.state, "complete", JSON.stringify(result));
  assert.equal(childRequests, 2);
  assert.equal(h.state.tools.length, 1);
  assert.equal(h.state.hosts[0].pid, result.pid);
  assert.notEqual(h.state.hosts[0].pid, host.pid);
  assert.equal(h.state.hosts[0].run.runId, run.asyncId);
  assert.ok(proof.instances.some((instance) => instance.kind === "runner" && instance.exitCode === 0));
  assert.equal(snapshot.notices.filter((notice) => notice.details.runs.some((item) => (item.runId ?? item.id) === run.asyncId)).length, 1);
  await waitFor(() => h.state.requests.filter((request) => request.headers["x-bbx-agent-id"] === "public-parent").length === 2, "single completion wake through parent admission");
  h.assertClean();
});

test("parent revocation and public stop cancel the daemon after an unknown side effect without replay", { timeout: 60000 }, async (t) => {
  const effect = deferred();
  const h = await harness(t, ({ admission }, response) => {
    if (admission.binding.agentId === "daemon-child") sse(response, { tool: { id: "unknown-daemon-tool", name: "bound_tool", args: {} } });
    else sse(response, { text: "Parent initialized." });
  });
  h.state.toolResponder = async (_body, _response) => { effect.resolve(); };
  const host = await parent(t, h);
  const launched = await host.tool({ agent: "helper", task: "Exercise the fake remote side effect.", async: true });
  assert.equal(launched.isError, false, JSON.stringify(launched));
  const run = launched.result.details;
  await effect.promise;
  h.state.closedAgents.add("daemon-child");
  h.state.closedAgents.add("public-parent");
  await host.invoke("abort");
  const stopped = await host.tool({ action: "stop", id: run.asyncId });
  assert.equal(stopped.isError, false, JSON.stringify(stopped));
  await waitFor(() => host.result(run), "cancelled real daemon result");
  await waitFor(() => host.proof(run), "cancelled real runner terminal proof");
  await waitFor(async () => (await host.invoke("snapshot")).notices.some((notice) => notice.details?.runs?.some((item) => (item.runId ?? item.id) === run.asyncId)), "cancelled result watcher notification");
  const native = h.state.hosts[0];
  const entries = (await readFile(native.sessionFile, "utf8")).trim().split("\n").map(JSON.parse);
  const assistant = entries.find((entry) => Array.isArray(entry.message?.content)
    && entry.message.content.some((part) => part.type === "toolCall" && part.id === "unknown-daemon-tool"));
  const invocation = `task:${native.sessionId}:${assistant.id}:bound_tool:unknown-daemon-tool`;
  assert.equal((await new FileReceiptStore(join(host.directory, "tool-receipts")).read(invocation)).state, "sent");
  await assert.rejects(host.tool({ action: "resume", id: run.asyncId, message: "Try recovering the prior work." }), /was stopped and cannot be resumed/);
  assert.equal((await new FileReceiptStore(join(host.directory, "tool-receipts")).read(invocation)).state, "sent");
  assert.equal(h.state.tools.length, 1, "a lost side-effect receipt cannot dispatch a replacement");
  assert.equal(h.state.requests.filter((request) => request.headers["x-bbx-agent-id"] === "daemon-child").length, 1);
  assert.equal(h.state.requests.filter((request) => request.headers["x-bbx-agent-id"] === "public-parent").length, 1, "the stopped parent cannot be reawakened into a model send");
  h.assertClean();
});
