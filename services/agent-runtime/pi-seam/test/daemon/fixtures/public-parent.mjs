import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { InMemoryCredentialStore } from "@earendil-works/pi-ai";
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import { installGatewayBoundary } from "../../../src/gateway-session.mjs";
import { root } from "../../support.mjs";

const config = JSON.parse(await readFile(process.env.BBX_PI_SEAM_FIXTURE_CONFIG, "utf8"));
const directory = config.directory;
const agentDir = process.env.PI_CODING_AGENT_DIR;
await mkdir(join(agentDir, "agents"), { recursive: true });
await mkdir(join(agentDir, "extensions/subagent"), { recursive: true });
await writeFile(join(agentDir, "agents/helper.md"), `---\nname: helper\ndescription: Restricted fake helper\nmodel: bbx-gateway/fake\ntools: bound_tool\nextensions: []\ninheritSkills: false\ninheritGlobalContext: false\ninheritProjectContext: false\n---\nUse the bound fake tool when asked, then report the result.\n`);
await writeFile(join(agentDir, "extensions/subagent/config.json"), JSON.stringify({
  toolActivation: "eager", defaultSubagentContext: "fresh", asyncWidget: false, fleetView: false,
  enableSlashCommands: false, scheduledRuns: { enabled: false }, autoResume: false, resultScanLogging: "off",
  completionBatch: { enabled: false }, programStatus: false, maxActiveAsyncRunsPerSession: 2,
}));
await writeFile(join(agentDir, "settings.json"), JSON.stringify({ subagents: { disableBuiltins: true, defaultExtensions: [] } }));
const settingsManager = SettingsManager.inMemory({
  cacheWarming: "off", compaction: { enabled: false }, retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
});
const modelRuntime = await ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null,
  allowModelNetwork: false, refreshOnCreate: false });
modelRuntime.registerProvider("bbx-gateway", { api: "openai-completions", baseUrl: config.gatewayUrl, apiKey: "gateway-placeholder",
  models: [{ id: "fake", name: "Fake gateway", reasoning: false, input: ["text"], contextWindow: 65536, maxTokens: 128,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }] });
await modelRuntime.refresh({ allowNetwork: false });
const loader = new DefaultResourceLoader({ cwd: directory, agentDir, settingsManager,
  noExtensions: true, noSkills: true, noContextFiles: true, noPromptTemplates: true, noThemes: true,
  additionalExtensionPaths: [join(root, ".fixture/pi-subagents-patched/index.js"), join(root, ".fixture/host/parent-policy.mjs")],
  systemPrompt: "A controlled parent for fake subagent lifecycle tests.", appendSystemPrompt: [],
});
await loader.reload();
if (loader.getExtensions().errors.length) throw new Error(JSON.stringify(loader.getExtensions().errors));
const { session } = await createAgentSession({ cwd: directory, agentDir, modelRuntime, settingsManager, resourceLoader: loader,
  model: modelRuntime.getModel("bbx-gateway", "fake"), sessionManager: SessionManager.create(directory, join(directory, "parent-journal")),
  noTools: "builtin", tools: ["subagent", "bg_wait", "subagent_supervisor"],
});
const post = async (path, body) => {
  const response = await fetch(`${new URL(config.gatewayUrl).origin}/control/${path}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`Fake parent ${path} was refused`);
  return response.json();
};
installGatewayBoundary(session, { gatewayUrl: config.gatewayUrl, gatewayToken: "public-parent-token",
  binding: { taskId: "task", agentId: "public-parent" }, acknowledge: (cursor) => post("confirm", { cursor }),
  admit: (request) => post("admit", request), settle: (receipt) => post("settle", receipt),
});
const errors = [];
await session.bindExtensions({ mode: "print", onError: (error) => errors.push(error) });
if (errors.length) throw new Error(JSON.stringify(errors));
const notices = [];
session.subscribe((event) => {
  if (event.type === "message_end" && event.message.customType === "subagent-notify") notices.push(event.message);
});
await session.prompt("Initialize the fake parent and then wait for child work.");
process.send({ event: "ready", pid: process.pid, tools: session.getActiveToolNames(), sessionFile: session.sessionFile });

process.on("message", async ({ id, method, args }) => {
  try {
    let result;
    if (method === "tool") {
      // subagent is model-only, so nested ctx.executeTool intentionally cannot select it.
      const tool = session.getToolDefinition("subagent");
      const value = await tool.execute(`supervisor-${id}`, args, undefined, undefined,
        session.extensionRunner.createToolContext(`supervisor-${id}`, undefined));
      result = { result: value, isError: value.isError === true };
    }
    else if (method === "snapshot") result = { notices, messages: session.messages.map((message) => ({ role: message.role,
      customType: message.customType, stopReason: message.stopReason, content: message.content, details: message.details })), errors };
    else if (method === "abort") { await session.abort(); result = {}; }
    else if (method === "shutdown") {
      await session.abort(); await session.extensionRunner.emit({ type: "session_shutdown", reason: "quit" }); session.dispose(); result = {};
    } else throw new Error("Unknown public parent fixture operation");
    process.send({ id, result });
    if (method === "shutdown") process.disconnect();
  } catch (error) { process.send({ id, error: error.message }); }
});
