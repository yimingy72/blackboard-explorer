import { readFile } from "node:fs/promises";
import { join } from "node:path";

export async function terminalProof(run) {
  if (!run.asyncId || !run.runnerProcessInstanceId) return undefined;
  let proof;
  try { proof = JSON.parse(await readFile(join(run.asyncDir, "process-terminal.json"), "utf8")); }
  catch (error) { if (error.code === "ENOENT") return undefined; throw error; }
  return proof.state === "observed" && proof.runId === run.asyncId
    && proof.runnerProcessInstanceId === run.runnerProcessInstanceId
    && proof.instances?.some((instance) => instance.kind === "runner"
      && instance.processInstanceId === run.runnerProcessInstanceId && Number.isFinite(instance.closeObservedAt))
    ? proof : undefined;
}

async function bounded(operation, timeoutMs) {
  let timeout;
  try {
    return await Promise.race([operation, new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error("The public stop did not finish in time")), timeoutMs);
    })]);
  } finally { clearTimeout(timeout); }
}

/** Only the plugin's identity-checked stop may signal a Runner; an old PID is not authority. */
export async function confirmStoppedRuns(runs, stop, timeoutMs = 2000) {
  for (const run of runs) {
    if (await terminalProof(run)) continue;
    await bounded(Promise.resolve().then(() => stop(run)), timeoutMs).catch(() => {});
    const deadline = Date.now() + timeoutMs;
    do {
      if (await terminalProof(run)) break;
      if (Date.now() >= deadline) throw new Error(`No matching terminal proof for run ${run.asyncId}; retained ${run.asyncDir}`);
      await new Promise((resolve) => setTimeout(resolve, Math.min(50, timeoutMs)));
    } while (true);
  }
}
