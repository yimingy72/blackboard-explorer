import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { snapshotFile } from "../src/artifacts.mjs";

test("file snapshots retain old bytes when working files change", async () => {
  const root = await mkdtemp(join(tmpdir(), "bbx-artifacts-"));
  try {
    const path = join(root, "solve.py");
    await writeFile(path, "print(1)\n");
    const first = await snapshotFile({ root, path });
    const duplicate = await snapshotFile({ root, path });
    assert.equal(first.uri, duplicate.uri);
    await writeFile(path, "print(2)\n");
    const second = await snapshotFile({ root, path });
    assert.notEqual(first.sha256, second.sha256);
    assert.equal(await readFile(fileURLToPath(first.uri), "utf8"), "print(1)\n");
    assert.equal(second.provenance, "file_snapshot_after_execution");
    await assert.rejects(snapshotFile({ root, path: root }), /regular file/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
