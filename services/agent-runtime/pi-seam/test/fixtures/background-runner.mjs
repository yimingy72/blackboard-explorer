import { readFile } from "node:fs/promises";
import { pluginChild } from "./plugin-child.mjs";

const config = JSON.parse(await readFile(process.env.BBX_PI_SEAM_FIXTURE_CONFIG, "utf8"));
let runtime;
try {
  runtime = await pluginChild(config, "background");
  process.send({ event: "ready", pid: process.pid, sessionId: runtime.child.sessionId, snapshot: runtime.snapshot });
} catch (error) {
  process.send({ event: "startup-error", error: error.message });
  process.disconnect();
}

process.on("message", async ({ id, method, text }) => {
  try {
    if (method === "prompt") await runtime.child.prompt(text);
    else if (method === "abort") await runtime.child.abort();
    else if (method === "dispose") { await runtime.child.dispose(); await runtime.factory.dispose(); }
    else if (method === "detach-parent-dispose") { runtime.child.detached = true; await runtime.factory.dispose(); }
    else throw new Error("Unknown fixture supervisor method");
    process.send({ id, result: { stopReason: runtime.child.messages.at(-1)?.stopReason, pid: process.pid } });
    if (method === "dispose") process.disconnect();
  } catch (error) { process.send({ id, error: error.message }); }
});
