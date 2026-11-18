import { readFile } from "node:fs/promises";
import { registerRequiredChildExtensions } from "pi-subagents/required-child-extensions";
import { registerSubagentCapabilityCeiling } from "pi-subagents/capability-ceiling";

export default async function (pi) {
  const config = JSON.parse(await readFile(process.env.BBX_PI_SEAM_FIXTURE_CONFIG, "utf8"));
  let required;
  let ceiling;
  pi.on("session_start", (_event, context) => {
    const sessionId = context.sessionManager.getSessionId();
    required = registerRequiredChildExtensions({ sessionId, requireForAllRunners: true,
      extensions: [{ id: "bbx.host", path: config.hostExtension, sessionHost: true }] });
    ceiling = registerSubagentCapabilityCeiling({ sessionId, source: "bbx-test", ceiling: {
      allowedAgents: ["helper"], allowedTools: ["bound_tool"], denyExtensions: false,
    } });
  });
  pi.on("session_shutdown", () => { required?.dispose(); ceiling?.dispose(); });
}
