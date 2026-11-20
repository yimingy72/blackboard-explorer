import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createLocalTools } from "../src/local-tools.mjs";

async function tools(t, options = {}) {
  const scratch = fileURLToPath(new URL("../../../.data/ctf-standalone-core-20261121/tests/", import.meta.url));
  await mkdir(scratch, { recursive: true });
  const cwd = await mkdtemp(join(scratch, "tools-"));
  const local = createLocalTools({ cwd, ...options });
  t.after(async () => { local.abort(); await rm(cwd, { recursive: true, force: true }); });
  return { cwd, local, tool: (name) => local.tools.find((item) => item.name === name) };
}

test("local tools preserve file edits and spool large command output", async (t) => {
  const { cwd, tool } = await tools(t);
  await tool("write").execute({ path: "a.txt", content: "one\ntwo\n" }, { callId: "write" });
  await tool("edit").execute({ path: "a.txt", edits: [{ oldText: "two", newText: "three" }] }, { callId: "edit" });
  const file = await tool("read").execute({ path: "a.txt" }, { callId: "read" });
  assert.match(file.content[0].text, /three/);
  const output = await tool("bash").execute({ command: `${JSON.stringify(process.execPath)} -e 'process.stdout.write("x".repeat(200000))'` }, { callId: "large" });
  assert.equal(output.details.bytes, 200000);
  assert.equal(output.details.truncated, true);
  assert.equal((await readFile(output.details.output_path)).length, 200000);
  assert.ok(output.content[0].text.length < 51000);
  assert.equal(process.cwd().includes(cwd), false);
});

test("timeout and cancellation reap shell descendants", async (t) => {
  const { local, tool } = await tools(t);
  const timeout = await tool("bash").execute({ command: "sleep 60 & wait", timeout: 0.05 }, { callId: "timeout" });
  assert.equal(timeout.details.timed_out, true);
  assert.equal(timeout.isError, true);
  assert.equal(local.activeProcesses(), 0);
  const controller = new AbortController();
  const work = tool("bash").execute({ command: "sleep 60 & wait" }, { callId: "abort", signal: controller.signal });
  setTimeout(() => controller.abort(), 50);
  const cancelled = await work;
  assert.equal(cancelled.details.aborted, true);
  assert.equal(local.activeProcesses(), 0);
});

test("a missing working directory fails promptly and preserves nonzero exit status", async (t) => {
  const { tool } = await tools(t);
  await assert.rejects(tool("bash").execute({ command: "pwd", cwd: "absent" }, { callId: "absent" }), /ENOENT/);
  const result = await tool("bash").execute({ command: "printf failure >&2; exit 7" }, { callId: "failure" });
  assert.equal(result.details.exit_code, 7);
  assert.equal(result.isError, true);
});

test("commands exclude credential aliases from inherited and overridden environments", async (t) => {
  const prior = process.env.CTF_TEST_CUSTOM_INHERITED;
  process.env.CTF_TEST_CUSTOM_INHERITED = " synthetic-custom-secret ";
  t.after(() => {
    if (prior === undefined) delete process.env.CTF_TEST_CUSTOM_INHERITED;
    else process.env.CTF_TEST_CUSTOM_INHERITED = prior;
  });
  const { tool } = await tools(t, {
    env: { CTF_TEST_CUSTOM_OVERRIDE: "synthetic-custom-secret", ARENA_SOLVER_TOKEN: "synthetic-token",
      CTF_TEST_PUBLIC_VALUE: "public-fixture" },
    secrets: ["synthetic-custom-secret"],
  });
  const result = await tool("bash").execute({ command: "printf '%s|%s|%s|%s' \"${CTF_TEST_CUSTOM_INHERITED-unset}\" \"${CTF_TEST_CUSTOM_OVERRIDE-unset}\" \"${ARENA_SOLVER_TOKEN-unset}\" \"$CTF_TEST_PUBLIC_VALUE\"" }, { callId: "environment" });
  assert.equal(result.isError, false);
  assert.match(result.content[0].text, /^unset\|unset\|unset\|public-fixture\n/);
});

test("Linux recovery stops persisted shell groups after their Node host is SIGKILLed", { skip: process.platform !== "linux" }, async (t) => {
  const { cwd, local } = await tools(t);
  const moduleUrl = new URL("../src/local-tools.mjs", import.meta.url).href;
  const source = `import { createLocalTools } from ${JSON.stringify(moduleUrl)};
    const local = createLocalTools({ cwd: ${JSON.stringify(cwd)} });
    await local.tools[0].execute({ command: 'echo $$ > shell.pid; sleep 60 & echo $! > descendant.pid; wait' }, { callId: 'crash' });`;
  const child = spawn(process.execPath, ["--input-type=module", "-e", source], { stdio: ["ignore", "ignore", "pipe"] });
  t.after(() => { if (child.exitCode === null) child.kill("SIGKILL"); });
  let pid;
  const deadline = Date.now() + 10000;
  while (!pid) {
    try { pid = Number(await readFile(join(cwd, "descendant.pid"), "utf8")); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    if (Date.now() > deadline) throw new Error("Synthetic shell did not start");
    if (!pid) await new Promise((accept) => setTimeout(accept, 10));
  }
  child.kill("SIGKILL");
  await new Promise((accept) => child.once("exit", accept));
  const recovered = await local.recover();
  assert.equal(recovered.length, 1);
  assert.equal(recovered[0].outcome, "stopped");
  let state;
  try {
    const raw = await readFile(`/proc/${pid}/stat`, "utf8");
    state = raw.slice(raw.lastIndexOf(")") + 2).split(/\s+/)[0];
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  assert.ok(state === undefined || state === "Z" || state === "X");
  assert.deepEqual(await local.recover(), []);
});
