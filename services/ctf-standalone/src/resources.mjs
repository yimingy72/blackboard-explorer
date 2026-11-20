import { readFile, statfs } from "node:fs/promises";
import { join } from "node:path";
import { monitorEventLoopDelay } from "node:perf_hooks";

async function optionalFile(path) {
  try { return (await readFile(path, "utf8")).trim(); }
  catch (error) {
    if (["ENOENT", "EACCES", "ENOTDIR"].includes(error.code)) return null;
    throw error;
  }
}

function counters(text) {
  return Object.fromEntries((text ?? "").split("\n").filter(Boolean).map(line => {
    const [key, value] = line.split(/\s+/);
    return [key, Number(value)];
  }));
}

export class Resources {
  constructor({ workDir, cgroupPath = "/sys/fs/cgroup", execution = () => ({}) }) {
    this.workDir = workDir;
    this.cgroupPath = cgroupPath;
    this.execution = execution;
    this.previous = null;
    this.delay = monitorEventLoopDelay({ resolution: 20 });
    this.delay.enable();
  }

  async snapshot() {
    const files = ["cpu.max", "cpu.stat", "memory.max", "memory.current", "memory.stat", "memory.peak"];
    const [values, disk] = await Promise.all([
      Promise.all(files.map(name => optionalFile(join(this.cgroupPath, name)))),
      statfs(this.workDir),
    ]);
    const [cpuMax, cpuStat, memoryMax, memoryCurrent, memoryStat, memoryPeak] = values;
    const now = Date.now();
    const cpu = counters(cpuStat);
    const [quota, period] = (cpuMax ?? "").split(/\s+/);
    const quotaCpus = quota && quota !== "max" ? Number(quota) / Number(period) : null;
    const elapsed = this.previous ? now - this.previous.time : 0;
    const usedCpus = elapsed > 0 && cpu.usage_usec !== undefined && this.previous.usage !== undefined
      ? (cpu.usage_usec - this.previous.usage) / (elapsed * 1000) : null;
    this.previous = { time: now, usage: cpu.usage_usec };
    const memory = counters(memoryStat);
    return {
      sampled_at: new Date(now).toISOString(),
      cpu: {
        source: cpuMax === null ? "unavailable" : `${this.cgroupPath}/cpu.max`,
        quota_cpus: quotaCpus,
        usage_interval_ms: elapsed || null,
        used_cpus: usedCpus,
        counters: cpu,
      },
      memory: {
        source: memoryMax === null ? "unavailable" : this.cgroupPath,
        limit_bytes: memoryMax === null || memoryMax === "max" ? null : Number(memoryMax),
        current_bytes: memoryCurrent === null ? null : Number(memoryCurrent),
        peak_bytes: memoryPeak === null ? null : Number(memoryPeak),
        anonymous_bytes: memory.anon ?? null,
        file_cache_bytes: memory.file ?? null,
        node: process.memoryUsage(),
      },
      disk: { source: this.workDir, available_bytes: disk.bavail * disk.bsize, total_bytes: disk.blocks * disk.bsize },
      event_loop: {
        p99_ms: this.delay.percentile(99) / 1e6,
        max_ms: this.delay.max / 1e6,
      },
      execution: this.execution(),
    };
  }

  close() { this.delay.disable(); }
}
