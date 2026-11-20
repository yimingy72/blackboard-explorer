import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadConfig } from "./config.mjs";
import { ContestRun } from "./coordinator.mjs";
import { acquireRunLock } from "./run-lock.mjs";

export async function main(argv = process.argv.slice(2), env = process.env) {
  if (argv.includes("--help")) {
    console.log("Usage: node src/main.mjs --config /path/to/config.json\nCTF_CONFIG_PATH may supply the same explicit configuration path.");
    return;
  }
  let configPath = env.CTF_CONFIG_PATH;
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === "--config" && argv[index + 1]) configPath = argv[++index];
    else throw new Error(`Unknown or incomplete argument: ${argv[index]}`);
  }
  const config = await loadConfig({ configPath, env });
  const release = await acquireRunLock(config.workDir);
  let run;
  const stopping = signal => { void run?.stop(signal).catch(error => console.error(error.message)); };
  const onTerm = () => stopping("SIGTERM");
  const onInt = () => stopping("SIGINT");
  process.once("SIGTERM", onTerm);
  process.once("SIGINT", onInt);
  try {
    run = await ContestRun.create(config);
    await run.start();
    const result = await run.finished;
    console.log(JSON.stringify({ status: "stopped", reason: result.reason, receipt: result.receipt ?? null }));
  } finally {
    await run?.stop("shutdown");
    process.off("SIGTERM", onTerm);
    process.off("SIGINT", onInt);
    await release();
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(JSON.stringify({ status: "error", message: error.message }));
    process.exitCode = 1;
  });
}
