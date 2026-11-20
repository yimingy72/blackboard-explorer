import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { acquireRunLock } from "../src/run-lock.mjs";

test("only one main process owns a work directory and the lock can be reacquired", async () => {
  const directory = fileURLToPath(new URL("../../../.data/ctf-standalone-tests/", import.meta.url));
  await mkdir(directory, { recursive: true });
  const work = await mkdtemp(join(directory, "lock-"));
  try {
    const release = await acquireRunLock(work);
    try { await assert.rejects(acquireRunLock(work), /already owns/); }
    finally { await release(); }
    const releaseAgain = await acquireRunLock(work);
    await releaseAgain();
  } finally { await rm(work, { recursive: true, force: true }); }
});
