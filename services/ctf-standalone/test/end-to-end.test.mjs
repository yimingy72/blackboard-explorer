import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { mkdir, mkdtemp, rm, readFile, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { Board } from "../src/board.mjs";
import { ContestRun } from "../src/coordinator.mjs";
import { acquireRunLock } from "../src/run-lock.mjs";
import { startFakeArena } from "./helpers/fake-arena.mjs";

function messageText(message) {
  if (typeof message?.content === "string") return message.content;
  return (message?.content ?? []).map((part) => part.text ?? "").join("\n");
}

function lastToolResult(messages) {
  const last = messages.at(-1);
  if (last?.role !== "tool") return null;
  let name;
  for (const message of messages.toReversed()) {
    const call = message.tool_calls?.find((item) => item.id === last.tool_call_id);
    if (call) { name = call.function.name; break; }
  }
  let value;
  try { value = JSON.parse(messageText(last)); }
  catch { value = messageText(last); }
  return { name, value };
}

function tool(name, args = {}) {
  return { name, args };
}

async function startModelFixture(decide, requestLimit = 300) {
  const requests = [];
  const errors = [];
  const server = createServer(async (request, response) => {
    try {
      assert.equal(request.url, "/v1/chat/completions");
      let body = "";
      for await (const chunk of request) body += chunk;
      const payload = JSON.parse(body);
      const instructions = payload.messages.map(messageText).join("\n");
      const agentId = instructions.includes("你是 Lead lead") ? "lead"
        : instructions.match(/你是同级 Worker (worker-\d+)/)?.[1];
      assert.ok(agentId, "Expected a named Lead or Worker system prompt");
      const decision = decide({ agentId, result: lastToolResult(payload.messages), messages: payload.messages });
      requests.push({ role: agentId === "lead" ? "lead" : "worker", agentId,
        tool: decision?.name ?? null, historicalFirstTask: JSON.stringify(payload.messages).includes("flag{first}") });
      assert.ok(requests.length < requestLimit, "Unexpected notification or model-call loop");
      const delta = decision ? { role: "assistant", tool_calls: [{ index: 0, id: randomUUID(), type: "function",
        function: { name: decision.name, arguments: JSON.stringify(decision.args) } }] } : { role: "assistant", content: "等待平台下一条更新。" };
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.end(`data: ${JSON.stringify({ id: randomUUID(), object: "chat.completion.chunk", model: "fixture",
        choices: [{ index: 0, delta, finish_reason: decision ? "tool_calls" : "stop" }],
        usage: { prompt_tokens: 20, completion_tokens: 5, total_tokens: 25 } })}\n\ndata: [DONE]\n\n`);
    } catch (error) {
      errors.push(error);
      response.writeHead(500, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { message: error.message } }));
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return {
    requests, errors,
    model: { provider: "offline-e2e", id: "fixture", api: "openai-completions",
      baseUrl: `http://127.0.0.1:${server.address().port}/v1`, apiKey: "local-fixture-model-key",
      contextWindow: 100000, maxTokens: 1024, reasoning: false },
    async close() {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

function leadDecision(result, codes = ["first", "second"], parallel = false) {
  if (result?.name === "finish_attempt") return null;
  if (result?.name !== "board_read") return tool("board_read");
  const challenges = result.value.challenges;
  if (!Array.isArray(challenges)) throw new Error("Expected full Lead board snapshot");
  if (parallel) {
    for (const code of codes) {
      if (!challenges.some((challenge) => challenge.unique_code === code)) {
        return tool("board_act", { action: "create_challenge", args: { unique_code: code, title: code, difficulty: "easy" } });
      }
    }
    for (const challenge of challenges) {
      if (challenge.phase === "pending" && !challenge.ready) {
        return tool("arena_start", { challenge_id: challenge.id });
      }
    }
  }
  for (const code of codes) {
    const challenge = challenges.find((item) => item.unique_code === code);
    if (!challenge) return tool("board_act", { action: "create_challenge", args: { unique_code: code, title: code, difficulty: "easy" } });
    if (challenge.phase === "completed") continue;
    if (!challenge.ready && challenge.phase === "pending") return tool("arena_start", { challenge_id: challenge.id });
    if (challenge.phase === "reviewing") {
      const candidate = challenge.candidates.at(-1);
      return tool("submit_flag", { challenge_id: challenge.id, candidate_id: candidate.id,
        review_revision: challenge.review_revision, expected_revision: challenge.revision });
    }
    if (!parallel) return null;
  }
  if (challenges.length === codes.length && challenges.every((challenge) => challenge.phase === "completed")) {
    return tool("finish_attempt", { reason: "All local synthetic challenges are confirmed complete" });
  }
  return null;
}

function workerDecision(result, commandRuns) {
  if (result?.name === "bash") {
    assert.match(String(result.value), /CTF_E2E_LOCAL_TOOL/);
  }
  if (result?.name !== "board_read") return tool("board_read");
  const board = result.value;
  if (!board.context?.assignment_id) {
    const choice = board.available.new_challenges[0] ?? board.available.assistance[0];
    return choice ? tool("board_act", { action: "claim", args: { challenge_id: choice.id, expected_revision: choice.revision } }) : null;
  }
  const challenge = board.challenges.find((item) => item.id === board.context.challenge_id);
  if (challenge.phase !== "solving") return null;
  if (!commandRuns.has(challenge.id)) {
    commandRuns.add(challenge.id);
    return tool("bash", { command: "printf CTF_E2E_LOCAL_TOOL" });
  }
  if (!challenge.facts.some((fact) => fact.statement === "Local fixture tool completed")) {
    return tool("board_act", { action: "post_fact", args: { challenge_id: challenge.id, expected_revision: challenge.revision,
      statement: "Local fixture tool completed", evidence: [{ type: "text", summary: "The command printed CTF_E2E_LOCAL_TOOL" }] } });
  }
  const rejected = challenge.submissions.some((submission) => submission.status === "rejected");
  const flag = challenge.unique_code === "first" && !rejected ? "flag{wrong-fixture}" : `flag{${challenge.unique_code}}`;
  return tool("board_act", { action: "submit_review", args: { challenge_id: challenge.id, expected_revision: challenge.revision,
    flag, summary: "Candidate from local fixture", fact_refs: challenge.facts.map((fact) => fact.id) } });
}

test("real Pi HTTP tools solve two local tasks, recover a rejected flag, reuse a session and explicitly finish", { timeout: 30000 }, async (t) => {
  const directory = fileURLToPath(new URL("../../../.data/ctf-standalone-tests/", import.meta.url));
  await mkdir(directory, { recursive: true });
  const workDir = await mkdtemp(join(directory, "end-to-end-"));
  const arena = await startFakeArena({ challenges: [
    { unique_code: "first", flag: "flag{first}" },
    { unique_code: "second", flag: "flag{second}" },
  ] });
  const commandRuns = new Set();
  const provider = await startModelFixture(({ agentId, result }) => agentId === "lead"
    ? leadDecision(result) : workerDecision(result, commandRuns));
  const { requests, errors } = provider;
  const config = {
    workDir, model: provider.model,
    arena: { baseUrl: arena.baseUrl, token: arena.token, identity: "local-fixture-attempt", expiresAt: null },
  };
  // One Worker isolates cross-task reuse; the separate test exercises twenty real sessions.
  const board = new Board({ path: join(workDir, "blackboard.sqlite"), initialWorkers: 1 });
  const run = await ContestRun.create(config, { board });
  let timer;
  t.after(async () => {
    clearTimeout(timer);
    await run.stop("test_complete");
    await provider.close();
    await arena.close();
    await rm(workDir, { recursive: true, force: true });
  });
  await run.start();
  const firstSessionId = run.pool.status().find((entry) => entry.id === "worker-1").sessionId;
  await Promise.race([run.finished, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`Local contest stalled. ${JSON.stringify({ errors: errors.map((error) => error.message), requests: requests.slice(-12), board: board.snapshot("lead") })}`)), 20000);
  })]);
  clearTimeout(timer);
  assert.deepEqual(errors, []);
  const completed = board.snapshot("lead").challenges;
  assert.equal(completed.length, 2);
  assert.ok(completed.every((challenge) => challenge.phase === "completed"));
  assert.equal(completed[0].submissions.filter((submission) => submission.status === "rejected").length, 1);
  assert.equal(arena.stats.starts, 2);
  assert.equal(arena.stats.submits, 3);
  assert.equal(arena.stats.finishes, 1);
  assert.equal(board.getMeta("cumulative_score"), 200);
  assert.equal(run.pool.status().find((entry) => entry.id === "worker-1").sessionId, firstSessionId);
  assert.equal(commandRuns.size, 2);
  assert.ok(requests.some((entry) => entry.role === "worker" && entry.historicalFirstTask));
  const session = await readFile(join(workDir, "sessions/worker-1/session.jsonl"), "utf8");
  assert.match(session, /CTF_E2E_LOCAL_TOOL/);
  assert.match(session, /flag\{first\}/);
  assert.match(session, /flag\{second\}/);
});

test("default twenty real Pi Workers execute tools and finish twenty local challenges", { timeout: 60000 }, async (t) => {
  const directory = fileURLToPath(new URL("../../../.data/ctf-standalone-tests/", import.meta.url));
  await mkdir(directory, { recursive: true });
  const workDir = await mkdtemp(join(directory, "twenty-workers-"));
  const codes = Array.from({ length: 20 }, (_, index) => `parallel-${index + 1}`);
  const arena = await startFakeArena({ challenges: codes.map((code) => ({ unique_code: code, flag: `flag{${code}}` })) });
  const executed = new Map();
  const flags = new Map();
  const provider = await startModelFixture(({ agentId, result }) => {
    if (agentId === "lead") return leadDecision(result, codes, true);
    if (result?.name === "bash") {
      const flag = String(result.value).match(/flag\{parallel-\d+\}/)?.[0];
      assert.ok(flag, "Worker must read the flag produced by its local command");
      flags.set(agentId, flag);
    }
    if (result?.name !== "board_read") return tool("board_read");
    const board = result.value;
    if (!board.context?.assignment_id) return null;
    const challenge = board.challenges.find((item) => item.id === board.context.challenge_id);
    if (challenge.phase !== "solving") return null;
    if (!executed.has(agentId)) {
      executed.set(agentId, challenge.id);
      return tool("bash", { command: `printf '%s' 'flag{${challenge.unique_code}}' > candidate.txt && cat candidate.txt` });
    }
    if (!challenge.facts.length) {
      return tool("board_act", { action: "post_fact", args: { challenge_id: challenge.id, expected_revision: challenge.revision,
        statement: `Local command produced ${flags.get(agentId)}`, evidence: [{ type: "text", summary: "Read from candidate.txt by the real bash tool" }] } });
    }
    return tool("board_act", { action: "submit_review", args: { challenge_id: challenge.id, expected_revision: challenge.revision,
      flag: flags.get(agentId), summary: "Candidate read from the local command", fact_refs: challenge.facts.map((fact) => fact.id) } });
  }, 1500);
  const config = { workDir, model: provider.model,
    arena: { baseUrl: arena.baseUrl, token: arena.token, identity: "twenty-worker-fixture", expiresAt: null } };
  const run = await ContestRun.create(config);
  let timer;
  t.after(async () => {
    clearTimeout(timer);
    await run.stop("test_complete");
    await provider.close();
    await arena.close();
    await rm(workDir, { recursive: true, force: true });
  });
  await run.start();
  assert.equal(run.board.workers().length, 20);
  await Promise.race([run.finished, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`Twenty-Worker flow stalled. ${JSON.stringify({ errors: provider.errors.map((error) => error.message),
      requests: provider.requests.slice(-12), tasks: run.board.snapshot("lead").challenges.map((challenge) => ({ code: challenge.unique_code, phase: challenge.phase })) })}`)), 45000);
  })]);
  clearTimeout(timer);
  await run.logWrites;
  assert.deepEqual(provider.errors, []);
  assert.equal(executed.size, 20);
  assert.equal(new Set(executed.values()).size, 20);
  assert.equal(flags.size, 20);
  assert.equal(run.pool.status().filter((session) => session.id.startsWith("worker-")).length, 20);
  assert.ok(run.board.snapshot("lead").challenges.every((challenge) => challenge.phase === "completed"));
  assert.equal(arena.stats.starts, 20);
  assert.equal(arena.stats.submits, 20);
  assert.equal(arena.stats.finishes, 1);
  assert.equal(run.board.getMeta("cumulative_score"), 2000);
  assert.equal(run.board.events(run.board.getMeta("coordinator_cursor", 0)).length, 0);
  const logs = (await readFile(join(workDir, "events.jsonl"), "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(logs.filter((event) => ["activity_rejected", "coordinator_error", "notification_failed"].includes(event.type)), []);
  assert.ok(provider.requests.length < 600, `Expected bounded event-driven calls, received ${provider.requests.length}`);
  const firstWorker = run.pool.status().find((session) => session.id === "worker-1");
  assert.ok(firstWorker.sessionId);
});

test("CLI explicit configuration solves a local challenge, exits cleanly and releases its run lock", { timeout: 30000 }, async (t) => {
  const directory = fileURLToPath(new URL("../../../.data/ctf-standalone-tests/", import.meta.url));
  await mkdir(directory, { recursive: true });
  const workDir = await mkdtemp(join(directory, "cli-end-to-end-"));
  const arena = await startFakeArena({ challenges: [{ unique_code: "cli", flag: "flag{cli}" }] });
  const commandRuns = new Set();
  const provider = await startModelFixture(({ agentId, result }) => agentId === "lead"
    ? leadDecision(result, ["cli"]) : workerDecision(result, commandRuns));
  const { apiKey, ...model } = provider.model;
  const configPath = join(workDir, "config.json");
  await writeFile(configPath, JSON.stringify({ workDir, model: { ...model, apiKeyEnv: "CTF_MODEL_API_KEY" } }), { mode: 0o600 });
  const entrypoint = fileURLToPath(new URL("../src/main.mjs", import.meta.url));
  const child = spawn(process.execPath, [entrypoint, "--config", configPath], {
    cwd: workDir,
    env: {
      PATH: process.env.PATH,
      HOME: workDir,
      CTF_MODEL_API_KEY: apiKey,
      ARENA_SOLVER_TOKEN: arena.token,
      CYBER_ARENA_HELP_MARKDOWN: "# Cyber Arena — Challenges API\nRendered for a single attempt.\n"
        + `- Attempt: cli-fixture\nARENA_SOLVER_BASE_URL=${arena.baseUrl}\n`,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const closed = once(child, "close");
  let timeout;
  t.after(async () => {
    clearTimeout(timeout);
    let forceStop;
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      forceStop = setTimeout(() => child.kill("SIGKILL"), 5000);
    }
    await closed.catch(() => {});
    clearTimeout(forceStop);
    await provider.close();
    await arena.close();
    await rm(workDir, { recursive: true, force: true });
  });
  const [code, signal] = await Promise.race([closed, new Promise((_, reject) => {
    timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`CLI fixture stalled. ${JSON.stringify({ stderr: stderr.slice(-4000),
        errors: provider.errors.map((error) => error.message), requests: provider.requests.slice(-8) })}`));
    }, 20000);
  })]);
  clearTimeout(timeout);
  assert.equal(signal, null);
  assert.equal(code, 0, stderr.slice(-4000));
  assert.deepEqual(provider.errors, []);
  const output = stdout.trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(output.at(-1).status, "stopped");
  assert.equal(output.at(-1).receipt.finished, true);
  assert.equal(arena.stats.starts, 1);
  assert.equal(arena.stats.submits, 1);
  assert.equal(arena.stats.finishes, 1);
  assert.equal(commandRuns.size, 1);
  const board = new Board({ path: join(workDir, "blackboard.sqlite") });
  try {
    assert.equal(board.workers().length, 20);
    const challenges = board.snapshot("lead").challenges;
    assert.equal(challenges.length, 1);
    assert.equal(challenges[0].phase, "completed");
    assert.equal(challenges[0].submissions[0].status, "accepted");
    assert.equal(board.getMeta("finish_attempt").status, "confirmed");
    assert.equal(board.getMeta("finished").completed, true);
  } finally { board.close(); }
  const release = await acquireRunLock(workDir);
  await release();
});
