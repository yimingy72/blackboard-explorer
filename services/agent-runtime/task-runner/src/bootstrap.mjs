import { readFile, writeFile } from "node:fs/promises";
import { setTimeout } from "node:timers/promises";
import { validate } from "./protocol.mjs";

const stop = new AbortController();
const cancel = () => stop.abort();
process.once("SIGTERM", cancel);
process.once("SIGINT", cancel);
try {
  const podUid = process.env.BBX_POD_UID;
  if (!podUid) throw new Error("A trusted Pod UID is required");
  const deadline = Date.now() + 180000;
  let text;
  while (text === undefined) {
    stop.signal.throwIfAborted();
    try { text = await readFile("/var/run/bbx/runner-bootstrap.json", "utf8"); }
    catch (error) {
      if (error.code !== "ENOENT" || Date.now() >= deadline) throw error;
      await setTimeout(200, undefined, { signal: stop.signal });
    }
  }
  const config = validate("RunnerBootstrap", JSON.parse(text));
  if (config.fence.pod_uid !== podUid
      || config.credential_file !== "/var/run/bbx/credentials.json"
      || config.journal_root !== "/journal") throw new Error("The Pod bootstrap identity or mount changed");
  // The projected Secret may rotate; main must read the exact UID-checked bootstrap snapshot.
  await writeFile("/run/bbx-start/bootstrap.json", text, { flag: "wx", mode: 0o400 });
  stop.signal.throwIfAborted();
  process.removeListener("SIGTERM", cancel);
  process.removeListener("SIGINT", cancel);
  process.argv[2] = "/run/bbx-start/bootstrap.json";
  await import("./main.mjs");
} catch {
  process.stderr.write("Task Runner bootstrap refused at its Pod identity boundary\n");
  process.exitCode = 1;
}
