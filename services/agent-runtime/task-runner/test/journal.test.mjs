import assert from "node:assert/strict";
import test from "node:test";
import { mkdir, mkdtemp, readFile, rm, stat, truncate, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { IncrementalJournal, openNativeJournal } from "../src/journal.mjs";
import { digest } from "../src/protocol.mjs";
import { deferred, root } from "./support.mjs";

test("PVC confirmation reads only appended bytes, restores from an anchor, and refuses truncation", async (t) => {
  await mkdir(join(root, ".test-runs"), { recursive: true });
  const directory = await mkdtemp(join(root, ".test-runs/journal-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const binding = { business_contract: "challenge-board-v1", binding_id: "binding", platform_session_id: "platform", agent_id: "agent", generation: 1,
    journal_generation: "generation", assignment_ref: null };
  const path = join(directory, "session.jsonl");
  const manager = await openNativeJournal(path, directory, binding, digest("spec"));
  const session = { sessionManager: manager, sessionId: manager.getSessionId(), sessionFile: path };
  const confirmations = [];
  const platform = { confirmJournal: async (body) => { confirmations.push(body); } };
  const journal = new IncrementalJournal(session, { fence: { task_id: "task", runner_id: "runner", pod_uid: "pod", epoch: 1 },
    binding, platform, lease: { cursor: null, sdk_session_id: null } });
  await journal.initialize();
  await journal.confirm();
  manager.appendCustomEntry("large", { marker: "unuploaded-content".repeat(20000) });
  const large = await journal.confirm();
  const readBefore = journal.bytesRead;
  manager.appendCustomEntry("small", { value: 1 });
  let small = await journal.confirm();
  assert.equal(journal.bytesRead - readBefore, small.offset - large.offset);
  assert.ok(journal.bytesRead - readBefore < 1000);
  assert.equal(JSON.stringify(confirmations).includes("unuploaded-content"), false, "confirmation carries metadata, not object-store uploads");
  manager.appendCustomEntry("covered-before-sync", { value: 2 });
  const coveredEnd = (await stat(path)).size;
  const coveredLeaf = manager.getLeafId();
  const sync = journal.file.sync.bind(journal.file);
  let synchronizedEnd;
  journal.file.sync = async () => {
    synchronizedEnd = (await stat(path)).size;
    await sync();
    manager.appendCustomEntry("appended-after-sync", { value: 3 });
  };
  const fixedRange = await journal.confirm();
  journal.file.sync = sync;
  assert.equal(fixedRange.offset, coveredEnd);
  assert.equal(fixedRange.offset, synchronizedEnd);
  assert.equal(fixedRange.leaf_id, coveredLeaf);
  assert.ok((await stat(path)).size > fixedRange.offset, "the post-sync append belongs to the next confirmation");
  small = await journal.confirm();
  const release = deferred();
  const entered = deferred();
  const confirm = platform.confirmJournal;
  platform.confirmJournal = async (body) => { entered.resolve(body); await release.promise; return confirm(body); };
  manager.appendCustomEntry("held-confirmation", { value: 4 });
  const running = journal.confirm();
  const held = await entered.promise;
  manager.appendCustomEntry("must-not-be-read-by-cancelled-queue-entry", { value: 5 });
  const controller = new AbortController();
  const queued = journal.confirm(controller.signal);
  let rejected = false;
  void queued.catch(() => { rejected = true; });
  controller.abort(new Error("queued confirmation cancelled"));
  try {
    await Promise.resolve();
    assert.equal(rejected, true, "queue waiting itself must respond to cancellation");
    await assert.rejects(queued, /queued confirmation cancelled/);
    assert.equal(journal.pendingConfirmation.request_id, held.request_id);
  } finally { release.resolve(); }
  await running;
  await journal.queue;
  assert.equal(journal.cursor.offset, held.cursor.offset, "the cancelled queued operation cannot execute later");
  platform.confirmJournal = confirm;
  small = await journal.confirm();
  await journal.close();
  const original = await readFile(path);
  const tampered = Buffer.from(original.toString("utf8").replace("unuploaded-content", "Unuploaded-content"));
  assert.equal(tampered.length, original.length);
  assert.deepEqual(tampered.subarray(small.anchor_offset), original.subarray(small.anchor_offset), "the tail anchor is unchanged");
  await writeFile(path, tampered);
  const expected = { sdkSessionId: session.sessionId, minimumSize: small.offset, cursor: small };
  await assert.rejects(openNativeJournal(path, directory, binding, digest("spec"), expected), /confirmed journal prefix hash/);
  for (const tail of ['{"type":"custom","id":"missing-newline","customType":"tail","data":{}}', '{"type":"custom","data":\n']) {
    const damaged = Buffer.concat([original, Buffer.from(tail)]);
    await writeFile(path, damaged);
    await assert.rejects(openNativeJournal(path, directory, binding, digest("spec"), expected));
    assert.deepEqual(await readFile(path), damaged, "SDK recovery cannot repair or skip an unconfirmed damaged tail");
  }
  await writeFile(path, original);
  const restoredManager = await openNativeJournal(path, directory, binding, digest("spec"), expected);
  assert.equal(restoredManager.getSessionId(), session.sessionId);
  const restored = new IncrementalJournal({ ...session, sessionManager: restoredManager }, {
    fence: { task_id: "task", runner_id: "runner", pod_uid: "pod", epoch: 1 }, binding, platform,
    lease: { cursor: small, sdk_session_id: session.sessionId },
  });
  await restored.initialize();
  assert.equal(restored.bytesRead, small.anchor_size);
  await restored.confirm();
  assert.equal(restored.bytesRead, small.anchor_size, "normal confirmation reuses the prefix already verified at recovery");
  await truncate(path, small.offset - 1);
  await assert.rejects(restored.confirm(), /truncated or lost/);
  await restored.close();
  await assert.rejects(openNativeJournal(path, directory, binding, digest("spec"), {
    sdkSessionId: session.sessionId, minimumSize: small.offset,
  }), /shorter than/);
  assert.equal((await stat(path)).size, small.offset - 1, "SDK recovery cannot rewrite a truncated confirmed journal");
  await assert.rejects(openNativeJournal(join(directory, "lost.jsonl"), directory, binding, digest("spec"), {
    sdkSessionId: session.sessionId, minimumSize: small.offset,
  }), /was lost/);
});
