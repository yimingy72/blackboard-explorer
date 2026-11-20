import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { mkdir, readdir, stat } from "node:fs/promises";
import { join } from "node:path";
import { Resources } from "../src/resources.mjs";

export const MODEL = { provider: "offline-ctf-benchmark", id: "scripted", api: "openai-completions",
  baseUrl: "http://127.0.0.1:1", apiKey: "synthetic-fixture-only", reasoning: false,
  contextWindow: 1000000, maxTokens: 4096 };
export const RETRY = { enabled: false, provider: { maxRetries: 0 } };

export function response(model, content, stopReason = "stop") {
  const stream = createAssistantMessageEventStream();
  const message = { role: "assistant", content, api: model.api, provider: model.provider, model: model.id,
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    timestamp: Date.now(), stopReason };
  stream.push({ type: "start", partial: { ...message, content: [] } });
  stream.push({ type: "done", reason: stopReason, message });
  stream.end(message);
  return stream;
}

export async function directoryBytes(root) {
  let bytes = 0;
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) bytes += await directoryBytes(path);
    else if (entry.isFile()) bytes += (await stat(path)).size;
  }
  return bytes;
}

export async function measurement(root, execution) {
  await mkdir(root, { recursive: true });
  const resources = new Resources({ workDir: root, execution });
  const before = await resources.snapshot();
  const beforeDisk = await directoryBytes(root);
  let sampling = false;
  let pending;
  let peakRss = before.memory.node.rss;
  let peakContainer = before.memory.current_bytes;
  const timer = setInterval(() => {
    if (sampling) return;
    sampling = true;
    pending = resources.snapshot().then((value) => {
      peakRss = Math.max(peakRss, value.memory.node.rss);
      if (value.memory.current_bytes !== null) peakContainer = Math.max(peakContainer ?? 0, value.memory.current_bytes);
    }).finally(() => { sampling = false; });
  }, 100);
  timer.unref();
  return {
    resources,
    before,
    async finish() {
      clearInterval(timer);
      await pending;
      const after = await resources.snapshot();
      resources.close();
      return { before, after,
        sampled_peak_node_rss_bytes: Math.max(peakRss, after.memory.node.rss),
        sampled_peak_container_bytes: after.memory.current_bytes === null ? null
          : Math.max(peakContainer ?? 0, after.memory.current_bytes),
        disk_growth_bytes: await directoryBytes(root) - beforeDisk,
        cpu_usage_usec: before.cpu.counters.usage_usec === undefined ? null
          : after.cpu.counters.usage_usec - before.cpu.counters.usage_usec,
        throttled_usec: before.cpu.counters.throttled_usec === undefined ? null
          : after.cpu.counters.throttled_usec - before.cpu.counters.throttled_usec };
    },
  };
}

export function execution(pool) {
  const sessions = pool?.status() ?? [];
  return { workers: sessions.length,
    models: sessions.reduce((sum, item) => sum + item.model_inflight, 0),
    tools: sessions.reduce((sum, item) => sum + item.tool_inflight, 0),
    processes: sessions.reduce((sum, item) => sum + item.active_processes, 0) };
}

export function positiveInteger(value, label) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 1) throw new Error(`Invalid ${label}`);
  return number;
}
