// Reuse only P3a's synthetic MCP/model producers; control is the real Blackboard HTTP API.
import { readFile, writeFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import { fixture } from "../../agent-runtime/task-runner/test/support.mjs";

const cleanups = [];
const h = await fixture({ after: (cleanup) => cleanups.push(cleanup) }, []);
const syntheticBusiness = h.config.mcp_targets.business;
let actor;
async function configure(data) {
  Object.assign(h.config.fence, data.fence);
  for (const key of Object.keys(h.credentials)) delete h.credentials[key];
  h.credentials.control = { audience: "runner-control", task_id: data.fence.task_id,
    expires_at: data.credential_expires_at, token: data.token };
  h.state.leaseStates.clear();
  h.state.prompts.clear();
  h.state.readJournal = async (bid) => {
    const response = await fetch(`${data.control_url}/bindings/${bid}/journal`, {
      headers: { authorization: `Bearer ${data.token}` },
    });
    if (!response.ok) throw new Error(`Original journal read failed: ${response.status}`);
    return response.json();
  };
  for (const spec of data.specs) {
    const bid = spec.binding.binding_id;
    h.credentials[spec.model_credential_ref] = { audience: "model", task_id: data.fence.task_id,
      binding_id: bid, expires_at: Date.now() / 1000 + 240, token: `private-model-${bid}` };
    for (const [target, reference] of Object.entries(spec.mcp_target_credential_refs)) {
      h.credentials[reference] = { audience: `${target}-mcp`, task_id: data.fence.task_id,
        binding_id: bid, expires_at: Date.now() / 1000 + 240, token: h.token(bid, `${target}-mcp`) };
    }
    if (data.gateway_credentials?.[bid]) {
      h.credentials[spec.model_credential_ref] = data.gateway_credentials[bid].model;
      for (const [target, reference] of Object.entries(spec.mcp_target_credential_refs))
        h.credentials[reference] = data.gateway_credentials[bid][target];
    }
    h.state.leaseStates.set(bid, { binding: spec.binding, lease_expires_at: Date.now() / 1000 + 60 });
    h.state.prompts.set(bid, { prompt_revision: spec.prompt_revision, instructions: spec.instructions });
  }
  await writeFile(h.config.credential_file, JSON.stringify(h.credentials));
  const business = new URL(`/api/tasks/${data.fence.task_id}/business-mcp-v2/`, data.control_url).href;
  await writeFile(`${h.directory}/fixture-fetch.mjs`, `const originalFetch = globalThis.fetch;
globalThis.fetch = (input, init) => originalFetch(new URL(input).href === ${JSON.stringify(business)}
  ? ${JSON.stringify(syntheticBusiness)} : new URL(input).href === "http://127.0.0.1:8766/mcp/"
  ? ${JSON.stringify(h.base + "/mcp/")} : input, init);\n`);
  await h.configure({ control_url: data.control_url,
    mcp_targets: { ...h.config.mcp_targets, business },
    ...(data.model_proxy_url ? { model_proxy_url: data.model_proxy_url } : {}),
    ...(data.mcp_gateway_url ? { mcp_gateway_url: data.mcp_gateway_url } : {}) });
}
try {
  await configure(JSON.parse(await readFile(process.argv[2], "utf8")));
  actor = h.start();
  process.stdout.write(JSON.stringify({ started: true, journal_root: h.config.journal_root }) + "\n");
  for await (const line of createInterface({ input: process.stdin })) {
    const command = JSON.parse(line);
    if (command.action === "kill") await h.stop(actor, "SIGKILL");
    else if (command.action === "start") { await configure(command.data); actor = h.start(); }
    else if (command.action === "stop") await h.stop(actor);
    else if (command.action === "status") { /* Observe only this fixture's fake producers. */ }
    else if (command.action === "exit") break;
    else throw new Error("Unknown fixture control");
    process.stdout.write(JSON.stringify({ done: command.action, exit_code: actor.child.exitCode,
      signal: actor.child.signalCode, output: actor.output, model_receipts: h.state.modelReceipts.length }) + "\n");
  }
} finally {
  for (const cleanup of cleanups.reverse()) await cleanup();
  h.assertClean();
}
