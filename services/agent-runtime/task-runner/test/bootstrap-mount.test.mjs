import assert from "node:assert/strict";
import test from "node:test";
import { readBootstrapMount } from "../src/bootstrap-mount.mjs";

function projectedAt(availableAt, protectedBoard, code = "ENOENT") {
  let elapsed = 0;
  const signal = new AbortController().signal;
  const result = readBootstrapMount(async () => {
    if (elapsed >= availableAt) return "original bootstrap";
    throw Object.assign(new Error("Projecting bootstrap"), { code });
  }, signal, protectedBoard, {
    now: () => elapsed,
    pause: async (duration) => { elapsed += duration; },
  });
  return { result, elapsed: () => elapsed };
}

test("a protected Blackboard Runner can read the Secret after the ordinary cutoff", async () => {
  assert.equal(await projectedAt(182_000, true).result, "original bootstrap");
  const ordinary = projectedAt(182_000, false);
  await assert.rejects(ordinary.result, { code: "ENOENT" });
  assert.equal(ordinary.elapsed(), 180_000);
});

test("a missing protected mount has a deadline and other mount errors are not retried", async () => {
  const absent = projectedAt(Infinity, true);
  await assert.rejects(absent.result, { code: "ENOENT" });
  assert.equal(absent.elapsed(), 360_000);
  const refused = projectedAt(Infinity, true, "EACCES");
  await assert.rejects(refused.result, { code: "EACCES" });
  assert.equal(refused.elapsed(), 0);
});
