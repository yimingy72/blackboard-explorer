import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { spawn } from "node:child_process";
import { Readable } from "node:stream";
import { getCurrentTools } from "@earendil-works/pi-ai";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { PROTOCOL, digest, jsonText, validate } from "../src/protocol.mjs";

export const root = dirname(dirname(fileURLToPath(import.meta.url)));
export const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
export async function waitFor(read, label, timeoutMs = 10000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) { const value = await read(); if (value) return value; await new Promise((resolve) => setTimeout(resolve, 20)); }
  throw new Error(`Timed out waiting for ${label}`);
}
function sendPiMessage(response, tool) {
  response.writeHead(200, { "content-type": "text/event-stream" });
  const event = (value) => response.write(`data: ${JSON.stringify(value)}\n\n`);
  const definition = typeof tool === "object" ? tool : { name: "remote_echo", arguments: { value: "same" } };
  event({ type: "start" });
  if (tool) {
    event({ type: "toolcall_start", contentIndex: 0, id: "same-sdk-call", toolName: definition.name });
    event({ type: "toolcall_end", contentIndex: 0, toolCall: {
      type: "toolCall", id: "same-sdk-call", name: definition.name, arguments: definition.arguments,
    } });
  } else {
    event({ type: "text_start", contentIndex: 0 });
    event({ type: "text_end", contentIndex: 0, content: "Done." });
  }
  event({ type: "done", reason: tool ? "toolUse" : "stop", usage: {
    input: 30, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 35,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  } });
  response.end();
}

export async function fixture(t, ids = ["one"], { taskId = "task" } = {}) {
  await mkdir(join(root, ".test-runs"), { recursive: true });
  const directory = await mkdtemp(join(root, ".test-runs/run-"));
  const fence = { task_id: taskId, runner_id: "runner", pod_uid: "pod", epoch: 1 };
  const state = { commands: [], receipts: [], journal: new Map(), confirmations: [], leases: [], attempts: new Map(),
    modelCalls: [], modelReceipts: [], invocations: new Map(), toolResults: new Map(), toolCalls: [], connections: [], errors: [],
    prompts: new Map(), leaseStates: new Map(), renewalHistory: [], lateRenewals: [],
    holdApplied: false, appliedGate: deferred(), appliedEntered: deferred(), actors: [], dropPrepare: false, dropped: false };
  const token = (binding, audience) => `private-${audience}-${binding}`;
  const credentials = { control: { audience: "runner-control", task_id: taskId, expires_at: 4102444800, token: token("task", "control") } };
  for (const id of ids) for (const [ref, audience] of [["model", "model"], ["mcp", "business-mcp"], ["sandbox", "sandbox-mcp"]]) {
    credentials[`${ref}-${id}`] = { audience, binding_id: id, task_id: taskId, expires_at: 4102444800, token: token(id, audience) };
  }
  const send = (response, value, headers = {}) => { response.writeHead(200, { "content-type": "application/json", ...headers }); response.end(JSON.stringify(value)); };
  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url, "http://fixture");
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const raw = Buffer.concat(chunks);
      const body = chunks.length ? JSON.parse(raw) : undefined;
      const wireDigest = digest(raw);
      if (url.pathname.startsWith("/blackboard/")) {
        const bindingId = url.pathname.split("/")[3];
        const tool = body?.tool_name ?? body?.invocation?.tool_name;
        const target = url.pathname.includes("/helpers/") ? "business" :
          state.directTargetByTool?.get(tool) ?? (tool === "exec_command" ? "sandbox" : "business");
        assert.equal(request.headers.authorization, url.pathname.includes("/tools/") ||
          (url.pathname.includes("/helpers/") &&
            (/\/(reserve|launch)$/.test(url.pathname) || /\/helpers\/(status|cancel)$/.test(url.pathname)))
          ? `Bearer ${token(bindingId, `${target}-mcp`)}` : `Bearer ${token("task", "control")}`);
        if (url.pathname.includes("/helpers/") && state.helperApi) {
          await state.helperApi({ url, bindingId, body, response, send });
          return;
        }
        if (url.pathname === "/blackboard/commands") {
          await new Promise((resolve) => setTimeout(resolve, 25));
          const renewals = state.lateRenewals.splice(0);
          if (state.enableRenewals) for (const [bindingId, grant] of state.leaseStates) {
            if (grant.lease_expires_at - Date.now() / 1000 < 2) {
              const previous = state.journal.get(bindingId);
              const receipt = { ...grant, lease_revision: grant.lease_revision + 1,
                lease_expires_at: Math.floor(Date.now() / 1000) + 4,
                sdk_session_id: previous?.sdk_session_id ?? null, cursor: previous?.cursor ?? null };
              state.leaseStates.set(bindingId, receipt); state.renewalHistory.push(receipt); renewals.push(receipt);
            }
          }
          send(response, { protocol: PROTOCOL, lease_renewals: renewals,
            commands: state.commands.filter((command) => command.command_seq > Number(url.searchParams.get("after_seq"))) });
          return;
        }
        if (url.pathname.match(/\/commands\/[^/]+\/receipts$/)) {
          validate("CommandReceipt", body);
          state.receipts.push(body);
          if (state.dropPrepare && !state.dropped && body.status === "prepared") { state.dropped = true; response.destroy(); return; }
          if (state.dropBoardTerminal && body.status === "completed") { response.destroy(); return; }
          send(response, { protocol: PROTOCOL, confirmed: true, request_id: body.request_id, status: body.status, receipt_digest: wireDigest });
          return;
        }
        if (url.pathname.endsWith("/acquire")) {
          validate("SessionLeaseRequest", body);
          state.leases.push(body);
          if (state.acquireGates?.has(bindingId)) await state.acquireGates.get(bindingId).promise;
          const previous = state.journal.get(bindingId);
          const grant = { protocol: PROTOCOL, confirmed: true, request_id: body.request_id, fence,
            binding_id: bindingId, binding: body.binding, lease_expires_at: state.enableRenewals ? Math.floor(Date.now() / 1000) + 4 : 4102444800,
            lease_revision: (state.leaseStates.get(bindingId)?.lease_revision ?? 0) + 1,
            sdk_session_id: previous?.sdk_session_id ?? null, cursor: previous?.cursor ?? null };
          state.leaseStates.set(bindingId, grant);
          send(response, grant);
          return;
        }
        if (url.pathname.endsWith("/prompt")) {
          const prompt = state.prompts.get(bindingId);
          const board = state.boardContexts?.get(bindingId)
            ?? (state.leaseStates.get(bindingId)?.binding.business_contract === "exploration-board-v1"
              ? { version: 0, content: "" } : undefined);
          if (board) {
            state.boardVersions ??= [];
            state.boardVersions.push(Number(url.searchParams.get("known_board_version")));
            if (Number(url.searchParams.get("known_revision")) === prompt.prompt_revision
                && Number(url.searchParams.get("known_board_version")) === board.version) {
              response.writeHead(304); response.end(); return;
            }
            send(response, { protocol: PROTOCOL, fence, binding_id: bindingId, ...prompt,
              board_context: { ...board, source_digest: digest(board.content) } }); return;
          }
          if (Number(url.searchParams.get("known_revision")) === prompt.prompt_revision) { response.writeHead(304); response.end(); return; }
          send(response, { protocol: PROTOCOL, fence, binding_id: bindingId, ...prompt }); return;
        }
        if (url.pathname.endsWith("/journal/confirm")) {
          validate("JournalConfirmation", body);
          state.confirmations.push(body);
          if (state.holdApplied && body.applied_inputs.length) { state.appliedEntered.resolve(); await state.appliedGate.promise; }
          const previous = state.journal.get(bindingId);
          if (previous && previous.request_id !== body.request_id) assert.deepEqual(body.previous, previous.cursor);
          state.journal.set(bindingId, body);
          if (!response.destroyed) send(response, { protocol: PROTOCOL, confirmed: true, request_id: body.request_id,
            binding_id: bindingId, sdk_session_id: body.sdk_session_id, cursor: body.cursor });
          return;
        }
        if (url.pathname.endsWith("/tools/admit")) {
          validate("ToolInvocation", body);
          assert.deepEqual(body.fence, fence);
          assert.deepEqual(body.binding, state.leaseStates.get(bindingId).binding);
          assert.ok(state.leaseStates.get(bindingId).lease_expires_at > Date.now() / 1000);
          assert.equal(digest(body.arguments_json), body.arguments_digest);
          assert.equal(body.binding.binding_id, bindingId);
          assert.equal(body.sdk_session_id, state.journal.get(bindingId).sdk_session_id);
          state.invocations.set(body.invocation_id, body);
          send(response, { protocol: PROTOCOL, confirmed: true, invocation_id: body.invocation_id, disposition: "admitted",
            arguments_digest: body.arguments_digest, result_digest: null, result_json: null, authorized_wait_until_ms: Date.now() + 10000 });
          return;
        }
        if (url.pathname.endsWith("/result")) {
          validate("ToolResultConfirmation", body);
          if (state.unknownToolBindings?.has(bindingId)) { response.writeHead(503); response.end(); return; }
          const resultJson = state.toolResults.get(body.invocation.invocation_id);
          assert.equal(typeof resultJson, "string");
          send(response, { protocol: PROTOCOL, confirmed: true, invocation_id: body.invocation.invocation_id,
            result_json: resultJson, result_digest: digest(resultJson) });
          return;
        }
        if (url.pathname.endsWith("/unknown")) { send(response, { protocol: PROTOCOL, confirmed: true, invocation_id: body.invocation.invocation_id,
          disposition: "unknown", arguments_digest: body.invocation.arguments_digest, result_digest: null, result_json: null }); return; }
      }
      if (url.pathname === "/model/messages") {
        const proof = body.bbx.proof;
        const bindingId = proof.binding.binding_id;
        assert.equal(request.headers.authorization, `Bearer ${token(bindingId, "model")}`);
        assert.deepEqual(proof.fence, fence);
        assert.equal(proof.sdk_session_id, state.journal.get(bindingId).sdk_session_id);
        assert.deepEqual(proof.journal, state.journal.get(bindingId).cursor);
        const context = { ...body.context,
          messages: [{ role: "system", content: body.context.systemPrompt }, ...body.context.messages],
          tools: getCurrentTools(body.context.messages).map(tool => ({ function: tool })),
          reasoning_effort: body.options.reasoning, max_completion_tokens: body.options.maxTokens,
        };
        state.modelCalls.push({ bindingId, body: context, action: body.bbx.action,
          promptRevision: proof.prompt_revision, callId: body.bbx.call_id, logicalCallId: body.bbx.logical_call_id });
        if (state.directModelUrl) {
          const forwarded = await fetch(state.directModelUrl + "/messages", {
            method: "POST", body: raw,
            headers: { "content-type": "application/json", authorization: request.headers.authorization },
            redirect: "error",
          });
          response.writeHead(forwarded.status, { "content-type": forwarded.headers.get("content-type") ?? "text/plain" });
          if (!forwarded.body) response.end();
          else Readable.fromWeb(forwarded.body).pipe(response);
          return;
        }
        const index = state.modelCalls.filter((call) => call.bindingId === bindingId).length;
        if (state.modelGate) await state.modelGate(bindingId, index);
        const tool = state.modelTool ? state.modelTool(bindingId, index, context) : state.useTools && index === 1;
        state.modelReceipts.push({ binding_id: bindingId, model_outcome: "completed" });
        sendPiMessage(response, tool);
        return;
      }
      const target = url.pathname === "/mcp/" ? "sandbox" :
        url.pathname === `/api/tasks/${taskId}/business-mcp-v2/` ? "business" : undefined;
      if (target) {
        const bindingId = request.headers["x-bbx-binding-id"];
        assert.equal(request.headers.authorization, `Bearer ${token(bindingId, `${target}-mcp`)}`);
        if (request.method === "DELETE") { response.writeHead(204); response.end(); return; }
        if (body.id === undefined) { response.writeHead(202); response.end(); return; }
        let result;
        let sessionId = request.headers["mcp-session-id"];
        if (body.method === "initialize") {
          sessionId = `${bindingId}-${state.connections.length + 1}`;
          state.connections.push({ bindingId, sessionId, ...(target ? { target } : {}) });
          result = { protocolVersion: "2025-11-25", capabilities: { tools: {} }, serverInfo: { name: "fake-gateway", version: "1" } };
        } else if (body.method === "tools/list") {
          if (state.holdToolsList === bindingId) { state.toolsListEntered.resolve(); await state.toolsListGate.promise; }
          if (state.directMcp) {
            state.directLists ??= [];
            state.directLists.push({ target, cursor: body.params?.cursor ?? null, sdk: request.headers["x-bbx-sdk-session-id"] });
          }
          result = { ...(state.directMcp && target === "business" && !body.params?.cursor ? { nextCursor: "approved-page" } : {}),
            tools: state.directMcp ? state.directTools.get(target)(body.params?.cursor) : state.remoteTools?.get(bindingId) ?? [{ name: "remote_echo", description: "Only a remote fixture tool",
            inputSchema: { type: "object", properties: { value: { type: "string" } }, required: ["value"], additionalProperties: false } }] };
        }
        else if (body.method === "tools/call") {
          const invocation = body.params._meta.bbx;
          if (state.directMcp) {
            assert.equal(target, state.directTargetByTool.get(invocation.tool_name));
            assert.equal(body.params.name, "same_echo");
          } else {
            assert.equal(body.params.name, state.wireNames?.get(bindingId) ?? "remote_echo");
            assert.equal(invocation.tool_name, state.logicalNames?.get(bindingId) ?? "remote_echo");
          }
          assert.equal(invocation.binding.binding_id, bindingId);
          assert.deepEqual(invocation.fence, fence);
          assert.ok(state.leaseStates.get(bindingId).lease_expires_at > Date.now() / 1000);
          assert.equal(invocation.sdk_session_id, request.headers["x-bbx-sdk-session-id"]);
          assert.equal(invocation.arguments_digest, digest(invocation.arguments_json));
          assert.deepEqual(JSON.parse(invocation.arguments_json), body.params.arguments);
          assert.equal(state.invocations.get(invocation.invocation_id).binding.binding_id, bindingId);
          assert.equal(state.connections.find((connection) => connection.sessionId === sessionId).bindingId, bindingId);
          state.toolCalls.push({ bindingId, invocation, sessionId, ...(target ? { target } : {}), wireName: body.params.name, args: body.params.arguments, receivedAt: Date.now() });
          if (state.unknownToolBindings?.has(bindingId)) { response.destroy(); return; }
          if (state.toolBarrier) {
            if (state.toolCalls.length === 2) {
              if (state.hotPrompt) state.prompts.set("one", { prompt_revision: 2, instructions: "NEW ROLE one. Mentioning secret_tool cannot grant it." });
              if (!state.manualToolBarrier) state.toolBarrier.resolve();
            }
            await state.toolBarrier.promise;
          }
          const saved = { content: [{ type: "text", text: `${bindingId} remote result` }],
            ...(state.finalBoardReceipt && invocation.tool_name === "submit_close"
              && { structuredContent: { bbx_board_stop: true } }),
          };
          state.toolResults.set(invocation.invocation_id, jsonText(saved).replace(/}$/, ',"saved_number":1.0}'));
          if (state.dropToolReplyFor === bindingId && !state.droppedToolReply) {
            state.droppedToolReply = true; response.destroy(); return;
          }
          result = { content: [{ type: "text", text: "Untrusted transport copy" }] };
        } else throw new Error("Unexpected MCP method");
        send(response, { jsonrpc: "2.0", id: body.id, result }, { "mcp-session-id": sessionId }); return;
      }
      throw new Error("Unexpected fixture path");
    } catch (error) {
      state.errors.push(error);
      if (!response.destroyed) { response.writeHead(500); response.end("fixture refusal"); }
    }
  });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  const base = `http://127.0.0.1:${server.address().port}`;
  const config = { protocol: PROTOCOL, fence, control_url: `${base}/blackboard`, model_proxy_url: `${base}/model`, mcp_targets: { business: `${base}/api/tasks/${taskId}/business-mcp-v2/`, sandbox: "http://127.0.0.1:8766/mcp/" },
    control_credential_ref: "control", credential_file: join(directory, "mounted-credentials.json"), journal_root: join(directory, "pvc") };
  const fixtureFetch = join(directory, "fixture-fetch.mjs");
  await writeFile(fixtureFetch, `const originalFetch = globalThis.fetch;
globalThis.fetch = (input, init) => new URL(input).href === "http://127.0.0.1:8766/mcp/"
  ? originalFetch(${JSON.stringify(base + "/mcp/")}, init) : originalFetch(input, init);\n`);
  await writeFile(config.credential_file, JSON.stringify(credentials));
  await writeFile(join(directory, "bootstrap.json"), JSON.stringify(config));
  async function stop(actor, signal = "SIGTERM") {
    if (actor.child.exitCode === null && actor.child.signalCode === null) actor.child.kill(signal);
    let timeout;
    try {
      await Promise.race([actor.exited, new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error("The owned Runner did not exit; PVC retained")), 5000); })]);
    } finally { clearTimeout(timeout); }
  }
  t.after(async () => {
    state.appliedGate.resolve(); state.toolBarrier?.resolve(); state.toolsListGate?.resolve();
    for (const gate of state.acquireGates?.values() ?? []) gate.resolve();
    let failure;
    for (const actor of state.actors) {
      try { await stop(actor); }
      catch (error) {
        failure ??= error;
        if (actor.child.exitCode === null && actor.child.signalCode === null) await stop(actor, "SIGKILL").catch(() => {});
      }
    }
    server.closeAllConnections(); await new Promise((resolve) => server.close(resolve));
    if (failure) throw failure;
    await rm(directory, { recursive: true, force: true });
  });
  return { directory, config, state, credentials, token, base,
    command(payload, requestId = `request-${state.commands.length + 1}`, overrideFence = fence) {
      const payloadJson = jsonText(payload);
      const command = { protocol: PROTOCOL, request_id: requestId, command_seq: state.commands.length + 1,
        fence: overrideFence, payload_digest: digest(payloadJson), payload_json: payloadJson };
      state.commands.push(command); return command;
    },
    spec(id) {
      if (!state.prompts.has(id)) state.prompts.set(id, { prompt_revision: 1, instructions: `OLD ROLE ${id}` });
      return { binding: { business_contract: "challenge-board-v1", binding_id: id, platform_session_id: `platform-${id}`, agent_id: `agent-${id}`,
      generation: 1, journal_generation: `journal-${id}`, assignment_ref: null },
      model: { id: "fake", model_revision: 2, api: "openai-completions", input: ["text"], reasoning: false, thinking_level: "off",
        thinking_level_map: null, thinking_budgets: null, context_window: 65536, max_output_tokens: 64 },
      model_credential_ref: `model-${id}`, mcp_target_credential_refs: { business: `mcp-${id}` }, allowed_tools: ["remote_echo"],
      mcp_tool_routes: { remote_echo: { target: "business", wire_name: "remote_echo" } },
      instructions: `OLD ROLE ${id}`, profile_revision: 1, prompt_revision: 1 }; },
    start() {
      const child = spawn(process.execPath, ["--import", fixtureFetch, join(root, "src/main.mjs"), join(directory, "bootstrap.json")], {
        env: { PATH: process.env.PATH, PI_OFFLINE: "1" }, stdio: ["ignore", "pipe", "pipe"],
      });
      const actor = { child, output: "", exited: once(child, "exit") };
      child.stdout.on("data", (chunk) => { actor.output += chunk; }); child.stderr.on("data", (chunk) => { actor.output += chunk; });
      state.actors.push(actor); return actor;
    }, stop,
    async configure(patch) { Object.assign(config, patch); await writeFile(join(directory, "bootstrap.json"), JSON.stringify(config)); },
    assertClean() { assert.deepEqual(state.errors, []); },
  };
}
