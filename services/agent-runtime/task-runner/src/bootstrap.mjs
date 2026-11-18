import { readFile, writeFile } from "node:fs/promises";
import { readBootstrapMount } from "./bootstrap-mount.mjs";
import { validate } from "./protocol.mjs";

const stop = new AbortController();
const cancel = () => stop.abort();
process.once("SIGTERM", cancel);
process.once("SIGINT", cancel);
try {
  const podUid = process.env.BBX_POD_UID;
  if (!podUid) throw new Error("A trusted Pod UID is required");
  const text = await readBootstrapMount(
    () => readFile("/var/run/bbx/runner-bootstrap.json", "utf8"),
    stop.signal,
    process.env.BBX_PROTECTED_BOARD_START === "1",
  );
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
