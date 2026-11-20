import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { createHash } from "node:crypto";
import { SessionPool } from "../src/sessions.mjs";
import { MODEL, RETRY, response, measurement, execution, positiveInteger } from "./fixture.mjs";

export async function capacity({ count, rounds, root }) {
  let pool;
  const measured = await measurement(root, () => execution(pool));
  const started = performance.now();
  let modelRequests = 0;
  pool = await SessionPool.create({ root, modelConfig: MODEL, retry: RETRY,
    getContext: (id) => ({ worker_id: id, assignment_id: `synthetic-${id}`, challenge_id: "capacity", generation: 1 }),
    streamFunction: (model, _context, _options, { agentId }) => {
      modelRequests += 1;
      const block = createHash("sha256").update(`${agentId}-${modelRequests}`).digest("hex").repeat(16);
      return response(model, [{ type: "text", text: block }]);
    } });
  try {
    const ids = Array.from({ length: count }, (_, i) => `worker-${i + 1}`);
    await Promise.all(ids.map((id) => pool.ensure(id)));
    const createMs = performance.now() - started;
    const cold = await measured.resources.snapshot();
    if (modelRequests !== 0) throw new Error("Preparing idle sessions invoked a model");
    const historyStarted = performance.now();
    for (let round = 0; round < rounds; round++) {
      await Promise.all(ids.map((id) => pool.prompt(id, `Synthetic memory sample ${id}, round ${round + 1}.`)));
    }
    const historyMs = performance.now() - historyStarted;
    const final = pool.status();
    if (final.some((session) => !session.idle || session.error)) throw new Error("A synthetic session did not settle");
    return { phase: "session-capacity", node: process.version, count, rounds,
      create_ms: createMs, history_ms: historyMs, cold,
      model_calls: modelRequests, real_model_calls: 0, tools_executed: 0,
      synthetic_rounds_per_session: rounds, measurement: await measured.finish() };
  } finally { await pool.close(); measured.resources.close(); }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { values } = parseArgs({ options: { count: { type: "string", default: "20" },
    rounds: { type: "string", default: "16" }, root: { type: "string" } } });
  const result = await capacity({ count: positiveInteger(values.count, "count"),
    rounds: positiveInteger(values.rounds, "rounds"), root: resolve(values.root ?? ".data/ctf-session-capacity") });
  console.log(JSON.stringify(result));
}
