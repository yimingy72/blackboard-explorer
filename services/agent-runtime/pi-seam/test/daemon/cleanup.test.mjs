import assert from "node:assert/strict";
import test from "node:test";
import { mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { harness } from "../support.mjs";
import { confirmStoppedRuns } from "./cleanup.mjs";

test("failed stop and a stale terminal proof fail teardown and preserve the run directory", async (t) => {
  const callbacks = [];
  const h = await harness({ after: (callback) => callbacks.push(callback) });
  t.after(async () => {
    for (const cleanup of callbacks.slice(1)) await cleanup();
    await rm(h.directory, { recursive: true, force: true });
  });
  const run = { asyncId: "current-run", runnerProcessInstanceId: "current-runner", asyncDir: join(h.directory, "owned-run") };
  await mkdir(run.asyncDir);
  const proofPath = join(run.asyncDir, "process-terminal.json");
  const stale = { state: "observed", runId: "old-run", runnerProcessInstanceId: "old-runner",
    instances: [{ kind: "runner", processInstanceId: "old-runner", closeObservedAt: 1 }] };
  await writeFile(proofPath, JSON.stringify(stale));
  let stops = 0;
  h.state.beforeCleanup.push(() => confirmStoppedRuns([run], async (target) => {
    assert.equal(target, run);
    stops++;
    throw new Error("The public stop failed");
  }, 20));
  await assert.rejects(callbacks[0](), /No matching terminal proof.*retained/);
  assert.equal(stops, 1);
  assert.equal((await stat(run.asyncDir)).isDirectory(), true);
  assert.deepEqual(JSON.parse(await readFile(proofPath, "utf8")), stale);
});
