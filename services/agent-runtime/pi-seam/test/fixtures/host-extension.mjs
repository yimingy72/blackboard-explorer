import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { Type } from "typebox";
import { confirmJournal, durableTool, FileReceiptStore, installGatewayBoundary, toolInvocation } from "../../src/gateway-session.mjs";

const hosts = () => globalThis[Symbol.for("bbx.pi-seam.fixture-hosts")] ??= new Map();
const configuration = () => readFile(process.env.BBX_PI_SEAM_FIXTURE_CONFIG, "utf8").then(JSON.parse);
const post = async (config, path, body, signal) => {
  const response = await fetch(`${new URL(config.gatewayUrl).origin}/control/${path}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), signal,
  });
  if (!response.ok) throw new Error(`Fake control refused ${path}`);
  return response.json();
};

export default async function (pi) {
  const config = await configuration();
  pi.registerProvider("bbx-gateway", {
    api: "openai-completions", baseUrl: config.gatewayUrl, apiKey: "gateway-placeholder",
    models: [{ id: "fake", name: "Fake gateway", reasoning: false, input: ["text"], contextWindow: 65536, maxTokens: 128,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
  });
  pi.registerTool({
    name: "bound_tool", label: "Bound tool", description: "Read only the child's fake remote material",
    parameters: Type.Object({}),
    async execute(callId, args, _signal, _update, context) {
      const session = hosts().get(context.sessionManager.getSessionId());
      if (!session) throw new Error("The required raw-session host was not installed");
      return durableTool({ execute: () => post(config, "tool", { binding: config.binding, callId }, _signal) }, {
        store: new FileReceiptStore(join(config.directory, "tool-receipts")),
        getSession: () => session,
        confirm: () => confirmJournal(session, (cursor) => post(config, "confirm", { cursor, binding: config.binding, fail: config.confirmFail })),
        resolveInvocation: (id) => toolInvocation(session, id, { taskId: config.binding.taskId, toolName: "bound_tool" }),
      }).execute(callId, args);
    },
  });
  pi.on("session_start", (_event, context) => post(config, "start", {
    agentId: config.binding.agentId, installed: hosts().has(context.sessionManager.getSessionId()), pid: process.pid,
  }));
  pi.on("session_shutdown", (_event, context) => { hosts().delete(context.sessionManager.getSessionId()); });
}

/** This export is the proposed upstream contract, used only by the patched fixture. */
export async function installChildSessionHost({ session, run }) {
  const config = await configuration();
  if (config.setupFail) throw new Error("Required child host setup failed");
  const held = globalThis[Symbol.for("bbx.pi-seam.fixture-installers")]?.get(config.binding.agentId);
  if (held) { held.entered.resolve(session); await held.release.promise; }
  installGatewayBoundary(session, {
    ...config,
    acknowledge: (cursor) => post(config, "confirm", { cursor, binding: config.binding, fail: config.confirmFail }),
    admit: (request) => post(config, "admit", request), settle: (receipt) => post(config, "settle", receipt),
  });
  hosts().set(session.sessionId, session);
  await post(config, "host", { agentId: config.binding.agentId, run, pid: process.pid,
    sessionId: session.sessionId, sessionFile: session.sessionFile, tools: session.getActiveToolNames() });
}
