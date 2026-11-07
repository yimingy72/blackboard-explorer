import assert from "node:assert/strict";
import { appendFile, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { CURRENT_SESSION_VERSION } from "@earendil-works/pi-coding-agent";
import { emptyCursor } from "../src/journal.mjs";
import { digest } from "../src/protocol.mjs";
import { prepareJournalDirectory, verifyRecoveryJournal } from "../src/recovery-journal.mjs";

test("recovery copies only exact confirmed journal bytes and rejects extra tails or unsafe paths", async () => {
  const root = await mkdtemp(join(tmpdir(), "bbx-recovery-journal-"));
  try {
    await prepareJournalDirectory(root, "epoch-1", true);
    await prepareJournalDirectory(root, "epoch-2");
    const original = join(root, "epoch-1", "old-binding");
    const work = join(root, "epoch-2");
    await mkdir(original);
    const binding = { binding_id: "old-binding" };
    const prepare = digest("exact prepare");
    const records = [
      { type: "session", version: CURRENT_SESSION_VERSION, id: "old-sdk" },
      { type: "custom", customType: "bbx.binding", id: "leaf", data: { binding, spec_digest: prepare } },
    ].map((item) => Buffer.from(JSON.stringify(item) + "\n"));
    let cursor = emptyCursor();
    for (const line of records) cursor = { offset: cursor.offset + line.length, seq: cursor.seq + 1,
      hash: digest(Buffer.concat([Buffer.from(cursor.hash, "hex"), line])), leaf_id: "leaf",
      anchor_offset: cursor.offset, anchor_size: line.length, anchor_hash: digest(line) };
    const path = join(original, "session.jsonl");
    const bytes = Buffer.concat(records);
    await writeFile(path, bytes);
    const entry = { binding_id: binding.binding_id, binding, prepare_digest: prepare,
      sdk_session_id: "old-sdk", cursor };
    const manifest = { resource_generation: 2, original_journal_subdir: "epoch-1", bindings: [entry] };
    const proof = await verifyRecoveryJournal(root, work, manifest);
    assert.deepEqual(proof, { directory: "epoch-2", bindings: [
      { binding_id: "old-binding", sdk_session_id: "old-sdk", cursor },
    ] });
    assert.deepEqual(await readFile(join(work, "recovery-evidence", "old-binding.jsonl")), bytes);
    assert.deepEqual(await verifyRecoveryJournal(root, work, manifest), proof);
    await appendFile(path, '{"type":"custom","id":"unknown"}\n');
    await assert.rejects(verifyRecoveryJournal(root, work, manifest), /confirmed end/);
    await writeFile(path, bytes);
    await assert.rejects(verifyRecoveryJournal(root, work, {
      ...manifest, bindings: [{ ...entry, sdk_session_id: "wrong-sdk" }],
    }), /header/);
    await symlink(original, join(root, "epoch-1", "never-opened"), "dir");
    await assert.rejects(verifyRecoveryJournal(root, work, { ...manifest, bindings: [{
      binding_id: "never-opened", binding: { binding_id: "never-opened" }, prepare_digest: prepare,
      sdk_session_id: null, cursor: null,
    }] }), /protected journal path/);
  } finally { await rm(root, { force: true, recursive: true }); }
});
