import assert from "node:assert/strict";
import test from "node:test";
import { fork } from "node:child_process";
import { once } from "node:events";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { registerRequiredChildExtensions } from "pi-subagents/required-child-extensions";
import { deferred, harness, root, sse } from "./support.mjs";
import { pluginChild } from "./fixtures/plugin-child.mjs";

async function configuration(h, agentId, extra = {}) {
  const directory = join(h.directory, agentId);
  await mkdir(directory, { recursive: true });
  const config = { directory, gatewayUrl: h.gatewayUrl, gatewayToken: `${agentId}-token`,
    binding: { taskId: "task", agentId }, ...extra };
  const path = join(directory, "host.json");
  await writeFile(path, JSON.stringify(config));
  return { config, path };
}

async function foreground(config, path, options) {
  const oldDir = process.env.PI_CODING_AGENT_DIR;
  const oldConfig = process.env.BBX_PI_SEAM_FIXTURE_CONFIG;
  process.env.PI_CODING_AGENT_DIR = join(config.directory, "agent");
  process.env.BBX_PI_SEAM_FIXTURE_CONFIG = path;
  try { return await pluginChild(config, "foreground", options); }
  finally {
    if (oldDir === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR = oldDir;
    if (oldConfig === undefined) delete process.env.BBX_PI_SEAM_FIXTURE_CONFIG; else process.env.BBX_PI_SEAM_FIXTURE_CONFIG = oldConfig;
  }
}

function background(t, config, path) {
  const processChild = fork(join(root, "test/fixtures/background-runner.mjs"), [], {
    env: { PATH: process.env.PATH, PI_CODING_AGENT_DIR: join(config.directory, "agent"),
      BBX_PI_SEAM_FIXTURE_CONFIG: path, PI_SUBAGENT_CHILD: "1" },
    stdio: ["ignore", "pipe", "pipe", "ipc"],
  });
  let stderr = "";
  processChild.stderr.on("data", (chunk) => { stderr += chunk; });
  processChild.stdout.resume();
  const ready = deferred();
  const pending = new Map();
  let sequence = 0;
  processChild.on("message", (message) => {
    if (message.event === "ready" || message.event === "startup-error") ready.resolve(message);
    else pending.get(message.id)?.resolve(message);
  });
  const exited = once(processChild, "exit");
  t.after(async () => { if (processChild.exitCode === null && processChild.signalCode === null) { processChild.kill(); await exited; } });
  return {
    ready: ready.promise, exited, stderr: () => stderr,
    async invoke(method, text) {
      const id = ++sequence;
      const response = deferred();
      pending.set(id, response);
      processChild.send({ id, method, text });
      const result = await response.promise;
      pending.delete(id);
      if (result.error) throw new Error(result.error);
      return result.result;
    },
  };
}

test("the unmodified public plugin API does not advertise the proposed raw-session setup", () => {
  assert.throws(() => registerRequiredChildExtensions({
    sessionId: "official-does-not-support-host", requireForAllRunners: true,
    extensions: [{ id: "bbx.host", path: join(root, "test/fixtures/host-extension.mjs"), sessionHost: true }],
  }), /requires only id and path/);
});

test("patched foreground factory awaits host installation, inherits only bound tools, and refuses persistence/startup failures", { timeout: 60000 }, async (t) => {
  const h = await harness(t, ({ index }, response) => sse(response, index === 1
    ? { tool: { id: "child-tool", name: "bound_tool", args: {} } } : {}));
  const { config, path } = await configuration(h, "foreground");
  const { child, factory, snapshot } = await foreground(config, path);
  t.after(() => child.dispose());
  assert.equal(snapshot[0].sessionHost, true);
  assert.equal(snapshot[0].requireForAllRunners, true);
  assert.deepEqual(h.state.hosts[0].tools, ["bound_tool"]);
  assert.equal(h.state.starts[0].installed, true, "host setup must precede session_start");
  assert.equal(h.state.hosts[0].pid, process.pid);
  await child.prompt("Read the fake remote material and return a result.");
  assert.equal(child.messages.at(-1).stopReason, "stop");
  assert.equal(h.state.requests.length, 2);
  assert.equal(h.state.tools[0].binding.agentId, "foreground");
  assert.ok(h.state.admissions.every((request) => request.binding.agentId === "foreground"));
  h.state.closedAgents.add("foreground");
  await factory.dispose();
  assert.equal(child.shutDown, true, "attached foreground children belong to factory shutdown");

  const denied = await configuration(h, "foreground-denied", { confirmFail: true });
  const failed = await foreground(denied.config, denied.path);
  await failed.child.prompt("A failed durable acknowledgement must not reach the model.");
  await failed.child.prompt("The failed child must remain frozen.");
  assert.equal(failed.child.messages.at(-1).stopReason, "error");
  assert.equal(h.state.requests.length, 2);
  await failed.factory.dispose();

  const setup = await configuration(h, "foreground-setup-failed", { setupFail: true });
  const starts = h.state.starts.length;
  await assert.rejects(foreground(setup.config, setup.path), /host setup failed/);
  assert.equal(h.state.starts.length, starts, "a failed required host cannot reach session_start");
  assert.equal(h.state.requests.length, 2);
  h.assertClean();
});

test("patched background launch survives parent disposal, then supervisor revocation and abort settle its real child process", { timeout: 60000 }, async (t) => {
  const held = deferred();
  const h = await harness(t, ({ index }, response) => {
    if (index === 1) sse(response);
    else {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(`data: ${JSON.stringify({ id: "held", choices: [{ index: 0, delta: { content: "waiting" }, finish_reason: null }] })}\n\n`);
      held.resolve();
    }
  });
  const { config, path } = await configuration(h, "background");
  const runner = background(t, config, path);
  const ready = await runner.ready;
  assert.equal(ready.event, "ready", `${ready.error ?? ""}\n${runner.stderr()}`);
  assert.notEqual(ready.pid, process.pid);
  assert.equal(ready.snapshot[0].sessionHost, true, "the serialized background snapshot carries the required host");
  assert.equal(h.state.hosts[0].pid, ready.pid);
  assert.equal(h.state.starts[0].installed, true);
  assert.deepEqual(h.state.hosts[0].tools, ["bound_tool"]);
  assert.equal((await runner.invoke("prompt", "Complete a fake background child turn.")).stopReason, "stop");
  await runner.invoke("detach-parent-dispose");
  const prompting = runner.invoke("prompt", "Continue after parent session disposal.");
  await held.promise;
  assert.equal(h.state.requests.length, 2, "detached work must not be mistaken for terminated work");
  h.state.closedAgents.add("background");
  await runner.invoke("abort");
  assert.equal((await prompting).stopReason, "aborted");
  assert.equal(h.state.settlements.at(-1).outcome, "unknown");
  assert.equal((await runner.invoke("prompt", "A late completion wake cannot regain model admission.")).stopReason, "error");
  assert.equal(h.state.requests.length, 2);
  await runner.invoke("dispose");
  assert.deepEqual(await runner.exited, [0, null]);
  h.assertClean();
});

test("patched background persistence failure is fail closed in its independent process", { timeout: 60000 }, async (t) => {
  const h = await harness(t);
  const { config, path } = await configuration(h, "background-denied", { confirmFail: true });
  const runner = background(t, config, path);
  const ready = await runner.ready;
  assert.equal(ready.event, "ready", `${ready.error ?? ""}\n${runner.stderr()}`);
  assert.equal((await runner.invoke("prompt", "Persist before attempting a background model call.")).stopReason, "error");
  assert.equal((await runner.invoke("prompt", "Keep the background binding frozen.")).stopReason, "error");
  assert.equal(h.state.requests.length, 0);
  assert.equal(h.state.admissions.length, 0);
  await runner.invoke("dispose");
  assert.deepEqual(await runner.exited, [0, null]);
  h.assertClean();
});

test("patched factory cannot publish a child cancelled or disposed while its required installer is pending", { timeout: 60000 }, async (t) => {
  const h = await harness(t);
  const key = Symbol.for("bbx.pi-seam.fixture-installers");
  const installers = globalThis[key] ??= new Map();
  for (const mode of ["dispose", "abort"]) {
    const { config, path } = await configuration(h, `cancel-install-${mode}`);
    const held = { entered: deferred(), release: deferred() };
    installers.set(config.binding.agentId, held);
    const controller = new AbortController();
    let factory;
    const creating = foreground(config, path, { signal: controller.signal, onFactory: (value) => { factory = value; } });
    const raw = await held.entered.promise;
    let disposals = 0;
    const dispose = raw.dispose.bind(raw);
    raw.dispose = () => { disposals++; dispose(); };
    if (mode === "dispose") await factory.dispose(); else controller.abort();
    held.release.resolve();
    await assert.rejects(creating, mode === "dispose" ? /factory was disposed/ : /launch was cancelled/);
    assert.equal(disposals, 1);
    assert.equal(globalThis[Symbol.for("bbx.pi-seam.fixture-hosts")]?.has(raw.sessionId), false);
    installers.delete(config.binding.agentId);
    await factory.dispose();
  }
  assert.equal(h.state.starts.length, 0, "a cancelled installer cannot enter session_start");
  assert.equal(h.state.requests.length, 0);
  h.assertClean();
});
