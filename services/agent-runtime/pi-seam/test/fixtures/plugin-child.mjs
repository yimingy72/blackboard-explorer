import { createJiti } from "jiti";
import { InMemoryCredentialStore } from "@earendil-works/pi-ai";
import * as sdk from "@earendil-works/pi-coding-agent";
import { join } from "node:path";
import { root } from "../support.mjs";

export async function pluginChild(config, mode, { signal, onFactory } = {}) {
  const jiti = createJiti(import.meta.url, { moduleCache: false, fsCache: false });
  const base = join(root, ".fixture/pi-subagents/src");
  const { createDefaultChildSessionFactory } = await jiti.import(join(base, "runs/shared/child-session.ts"));
  const required = await jiti.import(join(base, "shared/required-child-extensions.ts"));
  const handle = required.registerRequiredChildExtensions({
    sessionId: `parent-${config.binding.agentId}`, requireForAllRunners: true,
    extensions: [{ id: "bbx.host", path: join(root, "test/fixtures/host-extension.mjs"), sessionHost: true }],
  });
  const snapshot = required.resolveRequiredChildExtensions(`parent-${config.binding.agentId}`);
  handle.dispose();
  const common = {
    cwd: config.directory, model: "bbx-gateway/fake", tools: ["bound_tool"], requiredExtensions: JSON.parse(JSON.stringify(snapshot)),
    inheritProjectContext: false, inheritGlobalContext: false, inheritSkills: false,
    sessionEnabled: true, sessionDir: join(config.directory, "journal"), waitToolEnabled: false,
    extensions: [], allowNestedSubagents: false,
  };
  let launch;
  if (mode === "background") {
    const { buildRunnerChildLaunch } = await jiti.import(join(base, "runs/background/runner-child-launch.ts"));
    launch = buildRunnerChildLaunch({ ...common, agent: "helper", task: "Fake child task" }, {
      cwd: common.cwd, id: "background-run", flatIndex: 0,
    }, { sessionEnabled: true, sessionDir: common.sessionDir, watchdogStatus() {} });
  } else {
    const { buildInProcessChildLaunch } = await jiti.import(join(base, "runs/shared/child-launch.ts"));
    launch = buildInProcessChildLaunch({ ...common, host: "parent", childAgentName: "helper", childIndex: 0, runId: "foreground-run" });
  }
  const factory = createDefaultChildSessionFactory({ loadPiCodingAgent: async () => ({
    ...sdk,
    ModelRuntime: { create: () => sdk.ModelRuntime.create({
      credentials: new InMemoryCredentialStore(), modelsPath: null, allowModelNetwork: false, refreshOnCreate: false,
    }) },
  }) });
  launch.session.signal = signal;
  onFactory?.(factory);
  try { return { child: await factory.create(launch.session), factory, snapshot }; }
  catch (error) { await factory.dispose(); throw error; }
}
