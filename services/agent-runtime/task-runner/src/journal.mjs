import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open } from "node:fs/promises";
import { dirname } from "node:path";
import { CURRENT_SESSION_VERSION, SessionManager } from "@earendil-works/pi-coding-agent";
import { PROTOCOL, digest, jsonText, sameValue } from "./protocol.mjs";

const EMPTY = "0".repeat(64);
export const emptyCursor = () => ({ offset: 0, seq: 0, hash: EMPTY, leaf_id: null,
  anchor_offset: 0, anchor_size: 0, anchor_hash: digest("") });
const chain = (previous, line) => createHash("sha256").update(Buffer.from(previous, "hex")).update(line).digest("hex");

function cancellable(pending, signal) {
  if (!signal) return pending;
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const abort = () => { signal.removeEventListener("abort", abort); reject(signal.reason); };
    signal.addEventListener("abort", abort, { once: true });
    pending.then((value) => { signal.removeEventListener("abort", abort); resolve(value); },
      (error) => { signal.removeEventListener("abort", abort); reject(error); });
  });
}

export async function verifyConfirmedPrefix(path, cursor, inspect, copy) {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    let position = 0;
    let seq = 0;
    let hash = EMPTY;
    let pending = Buffer.alloc(0);
    let leafFound = cursor.leaf_id === null;
    while (position < cursor.offset) {
      const bytes = Buffer.alloc(Math.min(65536, cursor.offset - position));
      const { bytesRead } = await file.read(bytes, 0, bytes.length, position);
      if (!bytesRead) throw new Error("The confirmed journal prefix was truncated");
      position += bytesRead;
      const block = bytes.subarray(0, bytesRead);
      if (copy) await copy(block);
      pending = Buffer.concat([pending, block]);
      let end;
      while ((end = pending.indexOf(10)) !== -1) {
        const line = pending.subarray(0, end + 1);
        if (line.length > 2 * 1024 * 1024) throw new Error("A confirmed journal record exceeded its bound");
        const entry = JSON.parse(line.toString("utf8"));
        inspect?.(entry, seq + 1);
        if (entry.id === cursor.leaf_id) leafFound = true;
        hash = chain(hash, line);
        seq++;
        pending = pending.subarray(end + 1);
      }
      if (pending.length > 2 * 1024 * 1024) throw new Error("A confirmed journal record exceeded its bound");
    }
    if (pending.length || seq !== cursor.seq || hash !== cursor.hash || !leafFound) {
      throw new Error("The confirmed journal prefix hash, sequence or boundary changed");
    }
  } finally { await file.close(); }
}

async function verifyUnconfirmedTail(path, offset) {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const target = (await file.stat()).size;
    let position = offset;
    let pending = Buffer.alloc(0);
    const decoder = new TextDecoder("utf-8", { fatal: true });
    while (position < target) {
      const bytes = Buffer.alloc(Math.min(65536, target - position));
      const { bytesRead } = await file.read(bytes, 0, bytes.length, position);
      if (!bytesRead) throw new Error("The unconfirmed journal tail was truncated");
      position += bytesRead;
      pending = Buffer.concat([pending, bytes.subarray(0, bytesRead)]);
      let end;
      while ((end = pending.indexOf(10)) !== -1) {
        const line = pending.subarray(0, end + 1);
        if (line.length > 2 * 1024 * 1024) throw new Error("An unconfirmed journal record exceeded its bound");
        JSON.parse(decoder.decode(line));
        pending = pending.subarray(end + 1);
      }
      if (pending.length > 2 * 1024 * 1024) throw new Error("An unconfirmed journal record exceeded its bound");
    }
    if (pending.length) throw new Error("The unconfirmed journal tail has an incomplete record");
  } finally { await file.close(); }
}

export async function openNativeJournal(path, cwd, binding, specDigest, expected = {}) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  let existing;
  try { existing = await lstat(path); }
  catch (error) {
    if (error.code !== "ENOENT") throw error;
    if (expected.sdkSessionId) throw new Error("The granted native journal was lost from the PVC");
  }
  if (!existing) {
    try {
      const file = await open(path, "wx", 0o600);
      try {
        const header = SessionManager.inMemory(cwd).getHeader();
        await file.writeFile(JSON.stringify(header) + "\n");
        await file.sync();
      } finally { await file.close(); }
      const directory = await open(dirname(path), "r");
      try { await directory.sync(); } finally { await directory.close(); }
    } catch (error) { if (error.code !== "EEXIST") throw error; }
  }
  const stat = await lstat(path);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error("The native journal is not a protected regular file");
  if (stat.size < (expected.minimumSize ?? 0)) throw new Error("The native journal is shorter than its confirmed cursor");
  const headerFile = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const bytes = Buffer.alloc(Math.min(stat.size, 65536));
    const { bytesRead } = await headerFile.read(bytes, 0, bytes.length, 0);
    const end = bytes.subarray(0, bytesRead).indexOf(10);
    if (end < 0) throw new Error("The native session header is incomplete");
    const header = JSON.parse(bytes.subarray(0, end).toString("utf8"));
    if (header.type !== "session" || header.version !== CURRENT_SESSION_VERSION || typeof header.id !== "string"
        || (expected.sdkSessionId && header.id !== expected.sdkSessionId)) throw new Error("The native session header does not match its binding");
  } finally { await headerFile.close(); }
  if (expected.cursor) await verifyConfirmedPrefix(path, expected.cursor);
  await verifyUnconfirmedTail(path, expected.cursor?.offset ?? 0);
  const manager = SessionManager.open(path, undefined, cwd);
  const bindingEntry = manager.getEntries().find((entry) => entry.type === "custom" && entry.customType === "bbx.binding");
  if (bindingEntry) {
    if (!sameValue(bindingEntry.data.binding, binding) || bindingEntry.data.spec_digest !== specDigest) throw new Error("The persisted session binding conflicts with prepare");
  } else {
    if (manager.getEntries().length) throw new Error("An existing journal has no trusted binding");
    manager.appendCustomEntry("bbx.binding", { binding, spec_digest: specDigest });
  }
  return manager;
}

/** PVC is the hard durability boundary. Confirmation sends metadata, never journal content. */
export class IncrementalJournal {
  constructor(session, { fence, binding, lease, platform }) {
    this.session = session;
    this.fence = fence;
    this.binding = binding;
    this.platform = platform;
    this.cursor = lease.cursor ?? emptyCursor();
    if (this.cursor.anchor_size > 2 * 1024 * 1024) throw new Error("The journal anchor exceeds the entry bound");
    this.expectedSdkId = lease.sdk_session_id;
    this.inputs = new Map();
    this.boardAssistants = new Map();
    this.boardRunInputs = new Set();
    this.boardTerminals = new Map();
    this.commands = new Map();
    this.entrySeq = new Map();
    this.queue = Promise.resolve();
    this.bytesRead = 0;
  }
  async initialize() {
    if (this.expectedSdkId && this.expectedSdkId !== this.session.sessionId) throw new Error("The PVC journal does not match its SDK session binding");
    this.file = await open(this.session.sessionFile, constants.O_RDONLY | constants.O_NOFOLLOW);
    this.identity = await this.file.stat();
    await this.assertFile();
    if (this.cursor.anchor_size) {
      const anchor = Buffer.alloc(this.cursor.anchor_size);
      const { bytesRead } = await this.file.read(anchor, 0, anchor.length, this.cursor.anchor_offset);
      this.bytesRead += bytesRead;
      if (bytesRead !== anchor.length || digest(anchor) !== this.cursor.anchor_hash
          || this.cursor.anchor_offset + this.cursor.anchor_size !== this.cursor.offset) throw new Error("The confirmed journal anchor changed");
    }
    const entries = this.session.sessionManager.getEntries();
    for (const [index, entry] of entries.entries()) this.index(entry, index + 2);
  }
  index(entry, seq) {
    if (entry.id && entry.type !== "session") this.entrySeq.set(entry.id, seq);
    if (this.binding.business_contract === "exploration-board-v1"
        && entry.type === "message" && entry.message.role === "assistant") {
      this.boardAssistants.set(entry.id, { seq, reason: entry.message.stopReason });
    }
    if (entry.type === "custom_message" && entry.customType === "bbx.input") {
      const id = entry.details?.input_id;
      if (typeof id !== "string") throw new Error("A native input has no stable platform identity");
      const previous = this.inputs.get(id);
      if (previous && previous.entry_id !== entry.id) throw new Error("A platform input was appended more than once");
      this.inputs.set(id, { input_id: id, entry_id: entry.id, seq });
    }
    if (entry.type === "custom" && entry.customType === "bbx.control") {
      const previous = this.commands.get(entry.data.request_id);
      if (previous && previous.payload_digest !== entry.data.payload_digest) throw new Error("A persisted command identity conflicts");
      this.commands.set(entry.data.request_id, entry.data);
      if (entry.data.board_run_input === true) {
        if (this.binding.business_contract !== "exploration-board-v1" || entry.data.kind !== "input"
            || typeof entry.data.input_id !== "string") throw new Error("An original blackboard input is required");
        this.boardRunInputs.add(entry.data.input_id);
      }
    }
    if (entry.type === "custom" && entry.customType === "bbx.board-terminal") {
      const { input_id, ...proof } = entry.data;
      const input = this.inputs.get(input_id);
      const assistant = this.boardAssistants.get(proof.assistant_entry_id);
      const previous = this.boardTerminals.get(input_id);
      if (!this.boardRunInputs.has(input_id) || !input || input.seq >= seq
          || (proof.assistant_entry_id && (!assistant || assistant.seq <= input.seq || assistant.seq >= seq))
          || !["completed", "aborted", "failed"].includes(proof.outcome)
          || (proof.outcome === "completed" && assistant?.reason !== "stop")
          || (previous && previous.entry_id !== entry.id)) {
        throw new Error("A blackboard terminal does not match its original journal input");
      }
      this.boardTerminals.set(input_id, { entry_id: entry.id, seq, proof });
    }
  }
  async assertFile(minimum = this.cursor.offset) {
    const path = await lstat(this.session.sessionFile);
    const stat = await this.file.stat();
    if (path.isSymbolicLink() || path.ino !== this.identity.ino || path.dev !== this.identity.dev
        || stat.ino !== path.ino || stat.size < minimum) throw new Error("The PVC journal was replaced, truncated or lost");
    return stat;
  }
  confirm(signal) {
    if (signal?.aborted) return Promise.reject(signal.reason);
    const pending = this.queue.then(() => { signal?.throwIfAborted(); return this.commit(signal); });
    this.queue = pending.catch(() => {});
    return cancellable(pending, signal);
  }
  async commit(signal) {
    signal?.throwIfAborted();
    if (this.pendingConfirmation) {
      const pending = this.pendingConfirmation;
      await this.platform.confirmJournal(pending, signal);
      signal?.throwIfAborted();
      await this.assertFile(pending.cursor.offset);
      this.cursor = pending.cursor;
      this.pendingConfirmation = undefined;
    }
    const previous = this.cursor;
    const target = (await this.assertFile()).size;
    signal?.throwIfAborted();
    let cursor = { ...previous };
    let lastEntry = previous.leaf_id;
    let position = previous.offset;
    let pending = Buffer.alloc(0);
    while (position < target) {
      const bytes = Buffer.alloc(Math.min(65536, target - position));
      const { bytesRead } = await this.file.read(bytes, 0, bytes.length, position);
      signal?.throwIfAborted();
      if (!bytesRead) throw new Error("The journal increment could not be read");
      position += bytesRead;
      this.bytesRead += bytesRead;
      pending = Buffer.concat([pending, bytes.subarray(0, bytesRead)]);
      let end;
      while ((end = pending.indexOf(10)) !== -1) {
        const line = pending.subarray(0, end + 1);
        if (line.length > 2 * 1024 * 1024) throw new Error("A native journal entry exceeded its bound");
        const entry = JSON.parse(line.toString("utf8"));
        if (entry.id && entry.type !== "session") lastEntry = entry.id;
        cursor = { ...cursor, offset: cursor.offset + line.length, seq: cursor.seq + 1,
          hash: chain(cursor.hash, line), anchor_offset: cursor.offset, anchor_size: line.length, anchor_hash: digest(line) };
        this.index(entry, cursor.seq);
        pending = pending.subarray(end + 1);
      }
      if (pending.length > 2 * 1024 * 1024) throw new Error("A native journal entry exceeded its bound");
    }
    if (pending.length) throw new Error("The native journal has an incomplete record");
    const leaf = this.session.sessionManager.getLeafId();
    cursor.leaf_id = leaf && this.entrySeq.get(leaf) <= cursor.seq ? leaf : lastEntry;
    if (sameValue(cursor, previous)) return cursor;
    await this.file.sync();
    signal?.throwIfAborted();
    await this.assertFile(cursor.offset);
    const body = { protocol: PROTOCOL, fence: this.fence, binding: this.binding,
      sdk_session_id: this.session.sessionId, previous, cursor,
      applied_inputs: [...this.inputs.values()].filter((input) => input.seq > previous.seq && input.seq <= cursor.seq) };
    body.request_id = digest(jsonText([this.binding.binding_id, body.sdk_session_id, cursor.offset, cursor.hash, cursor.leaf_id]));
    this.pendingConfirmation = body;
    await this.platform.confirmJournal(body, signal);
    signal?.throwIfAborted();
    await this.assertFile(cursor.offset);
    this.cursor = cursor;
    this.pendingConfirmation = undefined;
    return cursor;
  }
  async close() { await this.queue; await this.file?.close(); }
}
