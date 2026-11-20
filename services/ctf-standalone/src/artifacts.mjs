import { createReadStream, createWriteStream } from "node:fs";
import { mkdir, stat, link, unlink, chmod } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import { Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

export async function snapshotFile({ root, path, signal }) {
  const source = resolve(path);
  const before = await stat(source);
  if (!before.isFile()) throw new Error("Evidence path must be a regular file");
  const directory = join(root, "artifacts");
  await mkdir(directory, { recursive: true });
  const temporary = join(directory, `.${randomUUID()}.partial`);
  const hash = createHash("sha256");
  let bytes = 0;
  const digest = new Transform({
    transform(chunk, encoding, callback) {
      bytes += chunk.length;
      hash.update(chunk);
      callback(null, chunk);
    },
  });
  try {
    await pipeline(createReadStream(source), digest, createWriteStream(temporary, { flags: "wx", mode: 0o600 }), { signal });
    const after = await stat(source);
    if (before.ino !== after.ino || before.size !== after.size || before.mtimeMs !== after.mtimeMs) {
      throw new Error("Evidence file changed during snapshot; retry after the writer finishes");
    }
    const sha256 = hash.digest("hex");
    const destination = join(directory, sha256);
    await chmod(temporary, 0o444);
    try { await link(temporary, destination); }
    catch (error) { if (error.code !== "EEXIST") throw error; }
    return {
      path: source,
      uri: pathToFileURL(destination).href,
      sha256,
      size: bytes,
      captured_at: new Date().toISOString(),
      provenance: "file_snapshot_after_execution",
    };
  } finally {
    await unlink(temporary).catch(error => { if (error.code !== "ENOENT") throw error; });
  }
}
