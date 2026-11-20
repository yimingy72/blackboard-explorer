import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Resources } from "../src/resources.mjs";

test("resource reads retain container units and unavailable fields", async () => {
  const dir = await mkdtemp(join(tmpdir(), "bbx-resource-"));
  const resources = new Resources({ workDir: dir, cgroupPath: dir, execution: () => ({ workers: 20 }) });
  try {
    await Promise.all(Object.entries({
      "cpu.max": "400000 100000", "cpu.stat": "usage_usec 100000\nnr_throttled 2\nthrottled_usec 500",
      "memory.max": "6442450944", "memory.current": "1024", "memory.peak": "4096",
      "memory.stat": "anon 768\nfile 256",
    }).map(([name, value]) => writeFile(join(dir, name), value)));
    const result = await resources.snapshot();
    assert.equal(result.cpu.quota_cpus, 4);
    assert.equal(result.cpu.counters.nr_throttled, 2);
    assert.equal(result.memory.limit_bytes, 6 * 1024 ** 3);
    assert.equal(result.memory.current_bytes, 1024);
    assert.equal(result.execution.workers, 20);
    assert.ok(result.disk.available_bytes > 0);
    await rm(join(dir, "cpu.max"));
    await rm(join(dir, "memory.max"));
    const missing = await resources.snapshot();
    assert.equal(missing.cpu.source, "unavailable");
    assert.equal(missing.cpu.quota_cpus, null);
    assert.equal(missing.memory.limit_bytes, null);
  } finally {
    resources.close();
    await rm(dir, { recursive: true, force: true });
  }
});
