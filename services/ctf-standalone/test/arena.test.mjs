import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { ArenaClient, ArenaError, challengeCompleted } from "../src/arena.mjs";
import { startFakeArena } from "./helpers/fake-arena.mjs";

const TOKEN = "test-attempt-token";

async function fixture(t, handler, options = {}) {
  const calls = [];
  const server = createServer(async (request, response) => {
    const buffers = [];
    for await (const chunk of request) buffers.push(chunk);
    const text = Buffer.concat(buffers).toString();
    const call = {
      method: request.method, url: new URL(request.url, "http://fixture"),
      token: request.headers.benchmark_token,
      body: text ? JSON.parse(text) : undefined,
    };
    calls.push(call);
    const result = await handler(call, calls.length);
    if (result === null) return;
    if (result.stream) {
      await result.stream(response);
      return;
    }
    response.writeHead(result.status ?? 200, result.headers ?? { "Content-Type": "application/json" });
    response.end(result.raw ?? JSON.stringify(result.json));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  return { calls, client: new ArenaClient({ baseUrl, token: TOKEN, ...options }) };
}

test("lists documented challenge fields through both explicit envelopes", async (t) => {
  const challenge = {
    unique_code: "crypto-1", description: "fixture", points: 42, total_score: 42,
    flag_count: 1, correct_flag_count: 0, is_completed: false, container_status: "none",
  };
  const { client, calls } = await fixture(t, (_, count) => ({ json: count === 1 ? [challenge] : { challenges: [challenge] } }));
  assert.deepEqual(await client.listChallenges(), [challenge]);
  assert.deepEqual(await client.listChallenges(), [challenge]);
  assert.equal(calls[0].url.pathname, "/openapi/v1/challenges");
  assert.equal(calls[0].token, TOKEN);
});

test("unsupported and duplicate challenge lists fail explicitly", async (t) => {
  const { client } = await fixture(t, (_, count) => ({ json: count === 1 ? { data: [] } : [{ unique_code: "x" }, { unique_code: "x" }] }));
  await assert.rejects(client.listChallenges(), (error) => error.code === "invalid_response" && !error.transient);
  await assert.rejects(client.listChallenges(), /duplicate codes/);
});

test("JSON response bodies remain bounded by the request timeout", async (t) => {
  const { client } = await fixture(t, () => ({
    stream: async (response) => {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.flushHeaders();
      await delay(100);
      response.end(JSON.stringify([{ unique_code: "late" }]));
    },
  }), { timeoutMs: 40 });
  await assert.rejects(client.listChallenges(), (error) => error.code === "transport_error");
});

test("start, close, help and finish use the official paths and methods", async (t) => {
  const { client, calls } = await fixture(t, (call) => call.url.pathname.endsWith("/help")
    ? { raw: `Help containing ${TOKEN}`, headers: { "Content-Type": "text/markdown" } }
    : { json: { container_status: "pending" } });
  assert.equal((await client.start("offline / 1")).container_status, "pending");
  await client.close("offline / 1");
  assert.equal(await client.help(), "Help containing [redacted]");
  assert.equal(calls.length, 3);
  await client.finish();
  assert.deepEqual(calls.map((call) => call.method), ["POST", "POST", "GET", "POST"]);
  assert.equal(calls[0].url.searchParams.get("unique_code"), "offline / 1");
  assert.equal(calls[3].url.pathname, "/openapi/v1/challenges/finish");
});

test("only listed handout paths can be read and content streams from the official endpoint", async (t) => {
  const content = Buffer.from([0, 1, 254, 255]);
  const { client, calls } = await fixture(t, (call) => call.url.searchParams.has("path")
    ? { raw: content, headers: { "Content-Type": "application/octet-stream" } }
    : { json: { files: [{ path: "handouts/unusual spelling.bin" }] } });
  assert.throws(() => client.readFile("x", "guessed.txt"), (error) => error.code === "unlisted_handout");
  await client.listFiles("x");
  const response = await client.readFile("x", "handouts/unusual spelling.bin");
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), content);
  assert.equal(calls[1].url.pathname, "/openapi/v1/challenges/files");
  assert.equal(calls[1].url.searchParams.get("path"), "handouts/unusual spelling.bin");
  assert.throws(() => client.readFile("another", "handouts/unusual spelling.bin"), ArenaError);
});

test("handout headers use the request timeout while body uses the download timeout", async (t) => {
  const content = Buffer.from("slow handout body");
  const { client } = await fixture(t, (call) => call.url.searchParams.has("path")
    ? {
      stream: async (response) => {
        response.writeHead(200, { "Content-Type": "application/octet-stream" });
        response.flushHeaders();
        await delay(100);
        response.end(content);
      },
    }
    : { json: [{ path: "handouts/slow.bin" }] }, {
    timeoutMs: 40,
    downloadTimeoutMs: 500,
  });
  await client.listFiles("x");
  const response = await client.readFile("x", "handouts/slow.bin");
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), content);
});

test("a handout body that exceeds the download timeout aborts without hanging", async (t) => {
  const { client } = await fixture(t, (call) => call.url.searchParams.has("path")
    ? {
      stream: async (response) => {
        response.writeHead(200, { "Content-Type": "application/octet-stream" });
        response.flushHeaders();
        await delay(250);
        response.end("too late");
      },
    }
    : { json: [{ path: "handouts/stuck.bin" }] }, {
    timeoutMs: 40,
    downloadTimeoutMs: 50,
  });
  await client.listFiles("x");
  const response = await client.readFile("x", "handouts/stuck.bin");
  await assert.rejects(response.arrayBuffer(), (error) => error.code === "download_timeout");
});

test("caller cancellation still aborts an in-progress handout body", async (t) => {
  const { client } = await fixture(t, (call) => call.url.searchParams.has("path")
    ? {
      stream: async (response) => {
        response.writeHead(200, { "Content-Type": "application/octet-stream" });
        response.flushHeaders();
        await delay(200);
        response.end("cancelled");
      },
    }
    : { json: [{ path: "handouts/cancelled.bin" }] });
  await client.listFiles("x");
  const controller = new AbortController();
  const response = await client.readFile("x", "handouts/cancelled.bin", { signal: controller.signal });
  const body = response.arrayBuffer();
  setTimeout(() => controller.abort(), 20);
  await assert.rejects(body, (error) => error.code === "cancelled");
});

test("redirects never forward the attempt token to a handout destination", async (t) => {
  const { client, calls } = await fixture(t, (call) => call.url.searchParams.has("path")
    ? { status: 302, headers: { Location: "http://127.0.0.1:1/foreign" }, raw: "" }
    : { json: [{ path: "handouts/file.bin" }] });
  await client.listFiles("x");
  await assert.rejects(client.readFile("x", "handouts/file.bin"), (error) => error.status === 302);
  assert.equal(calls.length, 2);
});

test("correct=true with zero award is accepted and rejected/testing zero never erases a score", async (t) => {
  const receipts = [
    { correct: true, awarded: 100, cumulative_score: 100 },
    { correct: false, awarded: 0, cumulative_score: 0, message: "Wrong flag" },
    { correct: true, awarded: 0, cumulative_score: 0, testing: true },
    { correct: true, awarded: 50, cumulative_score: 150 },
    { correct: true, awarded: 25, cumulative_score: 125 },
  ];
  const { client, calls } = await fixture(t, (_, index) => ({ json: receipts[index - 1] }));
  assert.equal((await client.submit("x", "flag{a}")).cumulativeScore, 100);
  const rejected = await client.submit("x", "flag{bad}");
  assert.equal(rejected.outcome, "rejected");
  assert.equal(rejected.cumulativeScore, 100);
  const testing = await client.submit("y", "flag{b}");
  assert.equal(testing.outcome, "accepted");
  assert.equal(testing.cumulativeScore, 100);
  assert.equal((await client.submit("z", "flag{c}")).cumulativeScore, 150);
  assert.equal((await client.submit("older", "flag{d}")).cumulativeScore, 150);
  assert.deepEqual(calls[0].body, { unique_code: "x", flag: "flag{a}" });
});

test("unknown submission responses and 503 are never retried or counted as rejection", async (t) => {
  const { client, calls } = await fixture(t, (_, index) => index === 1
    ? { json: { awarded: 0 } }
    : { status: 503, json: { code: "resource_unavailable", message: "Unavailable" } });
  assert.equal((await client.submit("x", "flag{x}")).outcome, "unknown");
  assert.equal((await client.submit("x", "flag{x}")).outcome, "unknown");
  assert.equal(calls.length, 2);
});

test("submission timeout is unknown and makes exactly one request", async (t) => {
  const { client, calls } = await fixture(t, () => null, { timeoutMs: 40 });
  const outcome = await client.submit("x", "flag{x}");
  assert.equal(outcome.outcome, "unknown");
  assert.equal(outcome.error.code, "transport_error");
  assert.equal(calls.length, 1);
});

test("duplicate submission reconciles progress with one GET and no repeated POST", async (t) => {
  const { client, calls } = await fixture(t, (call) => call.method === "POST"
    ? { status: 409, json: { code: "duplicate", message: "Already accepted" } }
    : { json: [{ unique_code: "x", flag_count: 1, correct_flag_count: 1, is_completed: true }] });
  const result = await client.submit("x", "flag{x}");
  assert.equal(result.outcome, "accepted");
  assert.equal(result.duplicate, true);
  assert.equal(result.reconciled, true);
  assert.equal(result.cumulativeScore, null);
  assert.deepEqual(calls.map((call) => call.method), ["POST", "GET"]);
});

test("duplicate submission is accepted even while a multi-flag challenge remains incomplete", async (t) => {
  const { client } = await fixture(t, (call) => call.method === "POST"
    ? { status: 409, json: { error: { code: "duplicate", message: "Duplicate" } } }
    : { json: [{ unique_code: "x", is_completed: false, flag_count: 2, correct_flag_count: 1 }] });
  const result = await client.submit("x", "flag{x}");
  assert.equal(result.outcome, "accepted");
  assert.equal(result.duplicate, true);
});

test("unknown submission can be reconciled after a restart without submitting again", async (t) => {
  const { client, calls } = await fixture(t, () => ({ json: { challenges: [{ unique_code: "x", is_completed: true }] } }), { cumulativeScore: 120 });
  const result = await client.reconcileSubmission("x");
  assert.equal(result.outcome, "accepted");
  assert.equal(result.cumulativeScore, 120);
  assert.deepEqual(calls.map((call) => call.method), ["GET"]);
});

test("documented HTTP failures preserve codes and slot holders without side effects", async (t) => {
  const errors = [
    [409, "environment_limit", { challenges: [{ unique_code: "holder" }] }],
    [409, "challenge_not_started", null],
    [409, "invalid_state", null],
    [404, "task_not_found", null],
    [404, "challenge_not_found", null],
    [503, "resource_unavailable", null],
  ];
  const { client, calls } = await fixture(t, (_, index) => ({ status: errors[index - 1][0], json: {
    code: errors[index - 1][1], message: "Declared failure", detail: errors[index - 1][2],
  } }));
  for (const [status, code, detail] of errors) {
    await assert.rejects(client.start("x"), (error) => {
      assert.equal(error.status, status);
      assert.equal(error.code, code);
      assert.deepEqual(error.detail, detail);
      assert.equal(error.transient, status === 503);
      return true;
    });
  }
  assert.equal(calls.length, errors.length);
});

test("token is private and server echo is redacted from observable errors", async (t) => {
  const { client } = await fixture(t, () => ({ status: 404, json: {
    code: "task_not_found", message: `Invalid ${TOKEN}`, detail: { echoed: TOKEN },
  } }));
  assert.equal(JSON.stringify(client).includes(TOKEN), false);
  await assert.rejects(client.listChallenges(), (error) => {
    assert.equal(String(error).includes(TOKEN), false);
    assert.equal(JSON.stringify(error).includes(TOKEN), false);
    return true;
  });
});

test("completion and configuration validation use exact types", () => {
  assert.equal(challengeCompleted({ flag_count: 0, correct_flag_count: 0 }), false);
  assert.equal(challengeCompleted({ flag_count: 1, correct_flag_count: 1 }), true);
  assert.equal(challengeCompleted({ is_completed: "false" }), false);
  assert.throws(() => new ArenaClient({ baseUrl: "file:///tmp/socket", token: TOKEN }));
  assert.throws(() => new ArenaClient({ baseUrl: "http://user:pass@fixture", token: TOKEN }));
  assert.throws(() => new ArenaClient({ baseUrl: "http://fixture", token: "bad\r\nheader" }));
  assert.throws(() => new ArenaClient({ baseUrl: "http://fixture", token: TOKEN, downloadTimeoutMs: 0 }));
});

test("shared fake arena exercises slots, score, recovery, attachments and explicit finish", async (t) => {
  const arena = await startFakeArena({ challenges: [
    ...["a", "b", "c", "d"].map((unique_code) => ({ unique_code, flag: `flag{${unique_code}}`, container: true })),
    { unique_code: "offline", flag: "flag{offline}", handout: { path: "handouts/input.bin", content: Buffer.from([0, 255]) } },
  ] });
  t.after(() => arena.close());
  const client = new ArenaClient(arena);
  assert.equal((await client.listChallenges()).length, 5);
  await assert.rejects(client.submit("offline", "flag{offline}"), (error) => error.code === "challenge_not_started");
  await Promise.all(["a", "b", "c"].map((code) => client.start(code)));
  await assert.rejects(client.start("d"), (error) => error.code === "environment_limit" && error.detail.challenges.length === 3);
  await client.start("offline");
  assert.equal((await client.submit("offline", "wrong")).outcome, "rejected");
  await client.listFiles("offline");
  assert.deepEqual(Buffer.from(await (await client.readFile("offline", "handouts/input.bin")).arrayBuffer()), Buffer.from([0, 255]));
  arena.setFault("submit", { uniqueCode: "a", kind: "accepted_disconnect" });
  assert.equal((await client.submit("a", "flag{a}")).outcome, "unknown");
  assert.equal((await client.reconcileSubmission("a")).outcome, "accepted");
  assert.equal((await client.submit("a", "flag{a}")).duplicate, true);
  await client.start("d");
  await client.close("d");
  arena.setFault("start", { uniqueCode: "d", status: 503 });
  await assert.rejects(client.start("d"), (error) => error.transient);
  arena.setFault("start", { uniqueCode: "d", kind: "pending" });
  assert.equal((await client.start("d")).container_status, "pending");
  assert.equal((await client.start("d")).container_status, "running");
  assert.equal(arena.stats.activeMax, 3);
  assert.equal(arena.stats.finishes, 0);
  await client.finish();
  assert.equal(arena.stats.finishes, 1);
  await assert.rejects(client.listChallenges(), (error) => error.code === "invalid_state");
});

test("connectivity fixture accepts a direct submission and closes as soon as a verdict is recorded", async (t) => {
  for (const flag of ["flag{probe}", "wrong"]) {
    const arena = await startFakeArena({ mode: "connectivity_test", testing: true,
      challenges: [{ unique_code: "opaque-code", flag: "flag{probe}", description: "Return the synthetic flag from this statement" }] });
    t.after(() => arena.close());
    const client = new ArenaClient(arena);
    assert.match(await client.help(), /Rendered for a connectivity check/);
    assert.equal((await client.listChallenges())[0].unique_code, "opaque-code");
    const result = await client.submit("opaque-code", flag);
    assert.equal(result.outcome, flag === "wrong" ? "rejected" : "accepted");
    assert.equal(result.receipt.awarded, 0);
    await assert.rejects(client.listChallenges(), (error) => error.code === "invalid_state");
    assert.equal(arena.stats.starts, 0);
    assert.equal(arena.stats.closes, 0);
    assert.equal(arena.stats.finishes, 0);
  }
});

test("connectivity fixture rejects start and close and keeps ordinary offline start semantics separate", async (t) => {
  const arena = await startFakeArena({ mode: "connectivity_test", challenges: [{ unique_code: "probe", flag: "flag{probe}" }] });
  t.after(() => arena.close());
  const client = new ArenaClient(arena);
  for (const operation of ["start", "close"]) {
    await assert.rejects(client[operation]("probe"), (error) => error.code === "invalid_state" && /no environment/.test(error.message));
  }
  assert.equal((await client.submit("probe", "flag{probe}")).outcome, "accepted");
});

test("finish transport loss reports an unknown outcome after one POST", async (t) => {
  const arena = await startFakeArena();
  t.after(() => arena.close());
  const client = new ArenaClient(arena);
  arena.setFault("finish", { kind: "finished_disconnect" });
  await assert.rejects(client.finish(), (error) => error.outcomeUnknown && error.code === "transport_error");
  assert.equal(arena.stats.finishes, 1);
  await assert.rejects(client.listChallenges(), (error) => error.code === "invalid_state");
  assert.equal(arena.stats.finishes, 1);
});

test("multi-flag fixture retains partial progress and the environment until every flag is accepted", async (t) => {
  const arena = await startFakeArena({ challenges: [{ unique_code: "stages", flags: ["flag{first}", "flag{second}"], container: true }] });
  t.after(() => arena.close());
  const client = new ArenaClient(arena);
  await client.start("stages");
  const first = await client.submit("stages", "flag{first}");
  assert.equal(first.outcome, "accepted");
  assert.equal(first.receipt.correct_flag_count, 1);
  assert.equal(first.receipt.total_flag_count, 2);
  const partial = (await client.listChallenges())[0];
  assert.equal(partial.is_completed, false);
  assert.equal(partial.container_status, "running");
  assert.equal(challengeCompleted(partial), false);
  const second = await client.submit("stages", "flag{second}");
  assert.equal(second.receipt.correct_flag_count, 2);
  assert.equal(second.cumulativeScore, 100);
  const completed = (await client.listChallenges())[0];
  assert.equal(challengeCompleted(completed), true);
  assert.equal(completed.container_status, "stopped");
});

test("reconciliation recognizes an unknown partial flag without waiting for completion", async (t) => {
  const arena = await startFakeArena({
    challenges: [{ unique_code: "stages", flags: ["flag{first}", "flag{second}"], container: true }],
  });
  t.after(() => arena.close());
  const client = new ArenaClient(arena);
  await client.start("stages");
  arena.setFault("submit", { kind: "accepted_disconnect" });
  assert.equal((await client.submit("stages", "flag{first}")).outcome, "unknown");
  const reconciled = await client.reconcileSubmission("stages", { baselineCorrectFlagCount: 0 });
  assert.equal(reconciled.outcome, "accepted");
  assert.equal(reconciled.challenge.correct_flag_count, 1);
  assert.equal(reconciled.challenge.is_completed, false);
  assert.equal((await client.submit("stages", "flag{second}")).outcome, "accepted");
});
