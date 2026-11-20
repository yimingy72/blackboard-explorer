import { parseArgs } from "node:util";
import { fileURLToPath, pathToFileURL } from "node:url";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join, resolve, dirname } from "node:path";
import { spawn } from "node:child_process";
import { SessionPool } from "../src/sessions.mjs";
import { MODEL, RETRY, response, measurement, execution, positiveInteger } from "./fixture.mjs";

const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`;

export async function combined({ count, rounds, root }) {
  let pool;
  const measured = await measurement(root, () => execution(pool));
  let tools = 0;
  let failures = 0;
  let requests = 0;
  const turns = new Map();
  const script = "const fs=require('node:fs'),c=require('node:crypto');"
    + "const key=c.pbkdf2Sync('synthetic-ctf-fixture','fixed-salt',100000,32,'sha256');"
    + "const b=Buffer.concat(Array(128).fill(key));"
    + "fs.writeFileSync('synthetic.bin',b);"
    + "if(!fs.readFileSync('synthetic.bin').equals(b))throw Error('fixture mismatch');"
    + "console.log('CTF_BENCH_OK',c.createHash('sha256').update(b).digest('hex'))";
  const started = performance.now();
  pool = await SessionPool.create({ root, modelConfig: MODEL, retry: RETRY,
    getContext: (id) => ({ worker_id: id, assignment_id: `synthetic-${id}`, challenge_id: id, generation: 1 }),
    onEvent: (event) => {
      if (event.type === "tool_end") { tools += 1; if (event.isError) failures += 1; }
      if (event.type === "error") failures += 1;
    },
    streamFunction: async (model, context, _options, { agentId }) => {
      requests += 1;
      const turn = (turns.get(agentId) ?? 0) + 1;
      turns.set(agentId, turn);
      await new Promise((accept) => setTimeout(accept, 5));
      if (turn % 2 === 1) return response(model, [{ type: "toolCall", id: `${agentId}-${turn}`, name: "bash",
        arguments: { command: `${quote(process.execPath)} -e ${quote(script)}`, timeout: 60 } }], "toolUse");
      const last = context.messages.findLast((message) => message.role === "toolResult");
      if (last?.isError || !JSON.stringify(last).includes("CTF_BENCH_OK")) {
        throw new Error("The synthetic command did not produce its expected evidence");
      }
      return response(model, [{ type: "text", text: "Synthetic CPU/file evidence verified." }]);
    } });
  try {
    const ids = Array.from({ length: count }, (_, index) => `worker-${index + 1}`);
    const prepared = await Promise.all(ids.map((id) => pool.ensure(id)));
    const createMs = performance.now() - started;
    const workloadStarted = performance.now();
    await Promise.all(ids.map(async (id) => {
      for (let round = 0; round < rounds; round++) await pool.prompt(id, `Run synthetic fixture ${round + 1}.`);
    }));
    const duration = performance.now() - workloadStarted;
    const final = pool.status();
    if (final.some((session) => !session.idle || session.error)) failures += 1;
    for (const item of prepared) if ((await readFile(join(item.cwd, "synthetic.bin"))).length !== 4096) failures += 1;
    const completed = Math.max(0, tools - failures);
    return { phase: "combined", node: process.version, count, rounds,
      create_ms: createMs, workload_ms: duration, jobs: count * rounds,
      completed_jobs: completed, jobs_per_second: completed / (duration / 1000),
      model_calls: requests, real_model_calls: 0, tools_executed: tools, failures,
      failure_rate: failures / (count * rounds), fixture: "pbkdf2-sha256-100000-32B-and-4096B-file-roundtrip",
      measurement: await measured.finish() };
  } finally { await pool.close(); measured.resources.close(); }
}

async function child(file, args) {
  const process = spawn(globalThis.process.execPath, [file, ...args], { stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  process.stdout.on("data", (chunk) => { stdout += chunk; });
  process.stderr.on("data", (chunk) => { stderr += chunk; });
  const code = await new Promise((accept, reject) => {
    process.once("error", reject);
    process.once("exit", accept);
  });
  if (code !== 0) throw new Error(`Benchmark child failed (${code}) ${stderr}`);
  return JSON.parse(stdout);
}

async function main() {
  const { values } = parseArgs({ options: { counts: { type: "string", default: "1,8,16,20,32,64" },
    count: { type: "string" }, rounds: { type: "string", default: "2" },
    "history-rounds": { type: "string", default: "16" }, output: { type: "string" }, root: { type: "string" } } });
  const rounds = positiveInteger(values.rounds, "rounds");
  if (values.count) {
    console.log(JSON.stringify(await combined({ count: positiveInteger(values.count, "count"), rounds,
      root: resolve(values.root ?? ".data/ctf-combined-benchmark") })));
    return;
  }
  const counts = values.counts.split(",").map((value) => positiveInteger(value, "count"));
  const historyRounds = positiveInteger(values["history-rounds"], "history-rounds");
  const output = resolve(values.output ?? `.data/ctf-standalone-benchmark-${Date.now()}`);
  await mkdir(output, { recursive: true });
  const directory = dirname(fileURLToPath(import.meta.url));
  const results = [];
  for (const count of counts) {
    for (const [phase, file, phaseRounds] of [["session-capacity", "session-capacity.mjs", historyRounds], ["combined", "run.mjs", rounds]]) {
      const result = await child(join(directory, file), ["--count", String(count), "--rounds", String(phaseRounds),
        "--root", join(output, `${phase}-${count}`)]);
      results.push(result);
      await writeFile(join(output, `${phase}-${count}.json`), JSON.stringify(result, null, 2) + "\n");
      console.error(`${phase} ${count} completed`);
    }
  }
  const summary = { kind: "synthetic-session-and-tool-benchmark", real_model_calls: 0,
    interpretation: "Synthetic fixed work per Worker. This measures runtime overhead and contention, not CTF solving success.", results };
  await writeFile(join(output, "summary.json"), JSON.stringify(summary, null, 2) + "\n");
  console.log(JSON.stringify(summary));
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) await main();
