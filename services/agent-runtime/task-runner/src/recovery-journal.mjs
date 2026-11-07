import { spawn } from "node:child_process";
import { constants } from "node:fs";
import { lstat, mkdir, mkdtemp, open, rename, rm } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { CURRENT_SESSION_VERSION } from "@earendil-works/pi-coding-agent";
import { emptyCursor } from "./journal.mjs";
import { digest, sameValue, validate } from "./protocol.mjs";

const limit = 128 * 1024 * 1024;
const recordLimit = 2 * 1024 * 1024;
const lockName = ".bbx-writer.lock";
const safe = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const epoch = /^epoch-[1-9][0-9]{0,8}$/;
const decoder = new TextDecoder("utf-8", { fatal: true });

export async function withWriterLock(path, expected, run) {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await file.stat({ bigint: true });
    const identity = `${info.dev}:${info.ino}`;
    if (!info.isFile() || info.nlink !== 1n || (expected && expected !== identity))
      throw new Error("Protected journal lock identity changed");
    // fd 3 shares the parent's open description; the lock stays on our fd after flock exits.
    await new Promise((resolve, reject) => {
      const child = spawn("flock", ["--exclusive", "--nonblock", "3"], {
        stdio: ["ignore", "ignore", "ignore", file.fd],
      });
      child.once("error", reject);
      child.once("exit", (code) => code === 0 ? resolve() : reject(new Error("Journal writer lock unavailable")));
    });
    const named = await lstat(path, { bigint: true });
    if (named.dev !== info.dev || named.ino !== info.ino)
      throw new Error("Protected journal lock was replaced");
    return await run(identity);
  } finally { await file.close(); }
}

async function stat(path, directory, missing = false) {
  try {
    const info = await lstat(path);
    if (directory ? !info.isDirectory() : !info.isFile() || info.nlink !== 1)
      throw new Error("Unexpected protected journal path");
    return info;
  } catch (error) {
    if (missing && error.code === "ENOENT") return null;
    throw error;
  }
}

async function syncDirectory(path) {
  const fd = await open(path, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { await fd.sync(); } finally { await fd.close(); }
}

export async function prepareJournalDirectory(root, directory, initial = false) {
  if (!epoch.test(directory) || initial !== (directory === "epoch-1"))
    throw new Error("Unexpected epoch working directory");
  await stat(root, true);
  const lock = join(root, lockName);
  if (initial) {
    try {
      const fd = await open(lock, "wx", 0o600);
      try { await fd.sync(); } finally { await fd.close(); }
    } catch (error) { if (error.code !== "EEXIST") throw error; }
  }
  await stat(lock, false);
  try { await mkdir(join(root, directory), { mode: 0o700 }); }
  catch (error) { if (error.code !== "EEXIST") throw error; }
  await stat(join(root, directory), true);
  await syncDirectory(root);
}

async function verifiedFile(path, proof, copy) {
  const before = await stat(path, false);
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const current = await file.stat();
    if (current.ino !== before.ino || current.dev !== before.dev ||
        current.size !== proof.cursor.offset || current.size > limit)
      throw new Error("Original journal is not at the confirmed end");
    let cursor = emptyCursor();
    let read = 0;
    let pending = Buffer.alloc(0);
    let binding = false;
    let leafFound = proof.cursor.leaf_id === null;
    while (read < current.size) {
      const chunk = Buffer.alloc(Math.min(65536, current.size - read));
      const { bytesRead } = await file.read(chunk, 0, chunk.length, read);
      if (!bytesRead) throw new Error("Original journal was shortened");
      read += bytesRead;
      const data = chunk.subarray(0, bytesRead);
      if (copy) {
        let written = 0;
        while (written < data.length) {
          const result = await copy.write(data, written, data.length - written);
          if (!result.bytesWritten) throw new Error("Recovery journal copy stalled");
          written += result.bytesWritten;
        }
      }
      pending = Buffer.concat([pending, data]);
      let end;
      while ((end = pending.indexOf(10)) !== -1) {
        const line = pending.subarray(0, end + 1);
        if (line.length > recordLimit) throw new Error("Original journal record exceeded its bound");
        const entry = JSON.parse(decoder.decode(line));
        if (cursor.seq === 0 && (entry.type !== "session" || entry.version !== CURRENT_SESSION_VERSION ||
            entry.id !== proof.sdk_session_id)) throw new Error("Original SDK journal header changed");
        if (entry.type === "custom" && entry.customType === "bbx.binding") {
          if (binding || !sameValue(entry.data?.binding, proof.binding) ||
              entry.data?.spec_digest !== proof.prepare_digest)
            throw new Error("Original journal binding changed");
          binding = true;
        }
        if (entry.id === proof.cursor.leaf_id && entry.type !== "session") leafFound = true;
        cursor = { offset: cursor.offset + line.length, seq: cursor.seq + 1,
          hash: digest(Buffer.concat([Buffer.from(cursor.hash, "hex"), line])),
          anchor_offset: cursor.offset, anchor_size: line.length, anchor_hash: digest(line),
          leaf_id: proof.cursor.leaf_id };
        pending = pending.subarray(end + 1);
      }
      if (pending.length > recordLimit) throw new Error("Original journal record exceeded its bound");
    }
    const final = await stat(path, false);
    if (pending.length || !binding || !leafFound || !sameValue(cursor, proof.cursor) ||
        final.dev !== current.dev || final.ino !== current.ino || final.size !== current.size ||
        final.mtimeMs !== current.mtimeMs || final.ctimeMs !== current.ctimeMs)
      throw new Error("Original journal confirmation could not be verified");
  } finally { await file.close(); }
}

export async function verifyRecoveryJournal(original, work, manifest) {
  const { resource_generation: generation, original_journal_subdir: prior, bindings } = manifest;
  if (!Number.isSafeInteger(generation) || generation < 2 || generation > 999999999 ||
      prior !== `epoch-${generation - 1}` || !Array.isArray(bindings) || bindings.length > 32 ||
      new Set(bindings.map((item) => item.binding_id)).size !== bindings.length)
    throw new Error("Invalid original journal manifest");
  await stat(original, true);
  await stat(join(original, lockName), false);
  await stat(work, true);
  const history = join(work, "recovery-evidence");
  try { await mkdir(history, { mode: 0o700 }); }
  catch (error) { if (error.code !== "EEXIST") throw error; }
  await stat(history, true);
  const originalEpoch = join(original, prior);
  await stat(originalEpoch, true);
  for (const proof of bindings) {
    const { binding_id: id, sdk_session_id: sdk, cursor } = proof;
    if (!safe.test(id) || proof.binding?.binding_id !== id ||
        !/^[a-f0-9]{64}$/.test(proof.prepare_digest))
      throw new Error("Invalid original journal binding identity");
    const bindingDirectory = join(originalEpoch, id);
    const exists = await stat(bindingDirectory, true, true);
    const source = join(bindingDirectory, "session.jsonl");
    if (sdk === null && cursor === null) {
      if (exists && await stat(source, false, true)) throw new Error("Unconfirmed journal exists");
      continue;
    }
    if (!exists || typeof sdk !== "string" || !sdk || !cursor)
      throw new Error("Original SDK session or cursor was not confirmed");
    validate("JournalCursor", cursor);
    const destination = join(history, `${id}.jsonl`);
    if (await stat(destination, false, true)) {
      await verifiedFile(source, proof);
      await verifiedFile(destination, proof);
      continue;
    }
    const staging = await mkdtemp(join(work, ".recovery-copy-"));
    try {
      const path = join(staging, "prefix.jsonl");
      const copy = await open(path, "wx", 0o400);
      try { await verifiedFile(source, proof, copy); await copy.sync(); }
      finally { await copy.close(); }
      await rename(path, destination);
      await syncDirectory(history);
    } finally { await rm(staging, { recursive: true, force: true }); }
  }
  await syncDirectory(work);
  return {
    directory: `epoch-${generation}`,
    bindings: bindings.map(({ binding_id, sdk_session_id, cursor }) => ({ binding_id, sdk_session_id, cursor })),
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const [mode, input] = process.argv.slice(2);
    if (mode === "initial") await prepareJournalDirectory("/journal-root", "epoch-1", true);
    else if (mode === "run") {
      await withWriterLock("/run/bbx-journal-lock", input, async (identity) => {
        process.stdout.write(JSON.stringify({ journal_lock_id: identity }) + "\n");
        await import("./bootstrap.mjs");
      });
    } else if (mode === "prepare") {
      await withWriterLock(join("/journal-root", lockName), process.argv[4], () =>
        prepareJournalDirectory("/journal-root", input));
    }
    else if (mode === "verify") {
      const manifest = JSON.parse(input);
      const work = await withWriterLock(join("/journal-original", lockName), manifest.journal_lock_id, () =>
        verifyRecoveryJournal("/journal-original", "/journal-work", manifest));
      process.stdout.write(JSON.stringify({ manifest_digest: digest(input), work_copy: work }) + "\n");
    } else throw new Error("Unexpected journal verification mode");
  } catch {
    process.stderr.write("Native journal recovery proof was refused\n");
    process.exitCode = 1;
  }
}
