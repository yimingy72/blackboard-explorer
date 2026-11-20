import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, readFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { ContestRun } from "../src/coordinator.mjs";
import { ScriptedPool } from "./helpers/scripted-pool.mjs";
import { startFakeArena } from "./helpers/fake-arena.mjs";

const root = fileURLToPath(new URL("../../../.data/ctf-standalone-tests/", import.meta.url));

async function setup(t, challenges = [{ unique_code: "one", flag: "flag{one}" }], { beforeStart, mode = "attempt" } = {}) {
  await mkdir(root, { recursive: true });
  const workDir = await mkdtemp(join(root, "coordinator-"));
  const arena = await startFakeArena({ challenges, mode });
  const config = { workDir, model: { apiKey: "test-model-key" },
    arena: { baseUrl: arena.baseUrl, token: arena.token, mode, identity: "test-attempt", expiresAt: null } };
  let pool;
  const poolFactory = async (options) => { pool = new ScriptedPool(options); return pool; };
  const run = await ContestRun.create(config, { poolFactory });
  t.after(async () => {
    await run.stop("test_complete");
    await arena.close();
    await rm(workDir, { recursive: true, force: true });
  });
  await beforeStart?.(arena, run);
  await run.start();
  const lead = (action, args) => run.board.execute("lead", action, { request_id: randomUUID(), ...args });
  const create = (code, extra = {}) => lead("create_challenge", { unique_code: code, title: code, ready: false, ...extra });
  async function pump() {
    for (let attempt = 0; attempt < 10; attempt++) {
      if (!run.pumping) await run.pump();
      await delay(1);
      if (!run.pumping && !run.scheduled) return;
    }
  }
  async function review(challengeId, flag) {
    const challenge = run.board.challenge(challengeId);
    const worker = challenge.owner_id;
    return run.board.execute(worker, "submit_review", { request_id: randomUUID(), challenge_id: challengeId,
      expected_revision: challenge.revision, flag, summary: "Local synthetic candidate" }, run.board.context(worker));
  }
  async function submit(challengeId, flag, callId = randomUUID()) {
    const candidate = await review(challengeId, flag);
    const current = run.board.challenge(challengeId);
    return run.submit({ challenge_id: challengeId, candidate_id: candidate.candidate.id,
      review_revision: current.review_revision, expected_revision: current.revision }, callId);
  }
  return { workDir, arena, config, run, pool, poolFactory, lead, create, pump, review, submit };
}

async function waitFor(predicate, label) {
  const deadline = Date.now() + 3000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, `Timed out waiting for ${label}`);
    await delay(10);
  }
}

test("complete local contest performs start, candidate review, receipt, release and explicit finish", async (t) => {
  const { run, pool, arena, create, pump, submit } = await setup(t);
  assert.equal(run.board.workers().length, 20);
  assert.equal(pool.status().length, 21);
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await pump();
  assert.equal(run.board.challenge(challenge.id).phase, "solving");
  assert.equal((await submit(challenge.id, "flag{one}")).outcome, "accepted");
  await pump();
  assert.equal(run.board.challenge(challenge.id).phase, "completed");
  assert.equal(run.board.challenge(challenge.id).members.length, 0);
  assert.equal(run.board.getMeta("cumulative_score"), 100);
  assert.equal(arena.stats.finishes, 0);
  await run.finish("All synthetic challenges completed");
  assert.equal(arena.stats.finishes, 1);
});

test("pending start is never dispatched and starts can later become ready", async (t) => {
  const { run, arena, create, pump } = await setup(t, [{ unique_code: "one", flag: "flag{one}", container: true }]);
  const challenge = create("one");
  arena.setFault("start", { kind: "pending" });
  await run.startChallenge(challenge.id);
  await pump();
  assert.equal(run.board.challenge(challenge.id).members.length, 0);
  assert.equal(run.board.challenge(challenge.id).ready, false);
  await run.startChallenge(challenge.id);
  await pump();
  assert.equal(run.board.challenge(challenge.id).members.length, 1);
});

test("unrecognized start response cannot make a task dispatchable", async (t) => {
  const { run, create, pump } = await setup(t, [{ unique_code: "one", flag: "flag{one}", container: true }]);
  const challenge = create("one");
  run.arena.start = async () => ({ container_status: "failed", container: true });
  await run.startChallenge(challenge.id).catch(() => {});
  await pump();
  assert.equal(run.board.challenge(challenge.id).ready, false);
  assert.equal(run.board.challenge(challenge.id).members.length, 0);
});

test("review and failure facts notify Lead and the same challenge team", async (t) => {
  const { run, pool, create, pump, review } = await setup(t);
  const challenge = create("one", { difficulty: "hard" });
  await run.startChallenge(challenge.id);
  await pump();
  const initialTeam = run.board.challenge(challenge.id).members.map((member) => member.worker_id);
  assert.equal(initialTeam.length, 2);
  pool.notifications.length = 0;
  const candidate = await review(challenge.id, "wrong");
  await pump();
  assert.ok(pool.notifications.some((notice) => notice.id === "lead" && notice.text.includes("核验")));
  assert.ok(pool.notifications.some((notice) => notice.id === initialTeam[1] && notice.text.includes("reviewing")));
  const current = run.board.challenge(challenge.id);
  const result = await run.submit({ challenge_id: challenge.id, candidate_id: candidate.candidate.id,
    review_revision: current.review_revision, expected_revision: current.revision }, randomUUID());
  await pump();
  assert.equal(result.outcome, "rejected");
  const retried = run.board.challenge(challenge.id);
  assert.equal(retried.phase, "solving");
  assert.ok(retried.facts.some((fact) => fact.statement.includes("incorrect")));
  assert.deepEqual(retried.members.map((member) => member.worker_id), initialTeam);
});

test("unknown accepted receipt stays pending until progress reconciliation and never reposts", async (t) => {
  const { run, arena, create, pump, submit } = await setup(t);
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await pump();
  arena.setFault("submit", { kind: "accepted_disconnect" });
  assert.equal((await submit(challenge.id, "flag{one}")).outcome, "unknown");
  assert.equal(run.board.challenge(challenge.id).phase, "reviewing");
  await assert.rejects(run.finish("Try finishing"), /pending flag submissions/);
  assert.equal(arena.stats.finishes, 0);
  await run.reconcilePendingSubmissions();
  assert.equal(run.board.challenge(challenge.id).phase, "completed");
  assert.equal(arena.stats.submits, 1);
});

test("resuming a workdir reconciles a persisted unknown submission without a new POST", async (t) => {
  const { run, config, arena, create, pump, submit } = await setup(t);
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await pump();
  arena.setFault("submit", { kind: "accepted_disconnect" });
  await submit(challenge.id, "flag{one}");
  await run.stop("simulated_restart");
  const resumed = await ContestRun.create(config, { poolFactory: (options) => ScriptedPool.create(options) });
  t.after(() => resumed.stop("test_complete"));
  await resumed.start();
  assert.equal(resumed.board.challenge(challenge.id).phase, "completed");
  assert.equal(arena.stats.submits, 1);
  assert.equal(arena.stats.finishes, 0);
});

test("three active environments cap online starts while offline work proceeds", async (t) => {
  const definitions = ["a", "b", "c", "d"].map((unique_code) => ({ unique_code, flag: `flag{${unique_code}}`, container: true }));
  definitions.push({ unique_code: "offline", flag: "flag{offline}", container: false });
  const { run, arena, create, pump } = await setup(t, definitions);
  const challenges = definitions.map((item) => create(item.unique_code));
  await Promise.all(challenges.slice(0, 3).map((challenge) => run.startChallenge(challenge.id)));
  await assert.rejects(run.startChallenge(challenges[3].id), (error) => error.code === "environment_limit");
  await run.startChallenge(challenges[4].id);
  await pump();
  assert.equal(run.board.challenge(challenges[3].id).ready, false);
  assert.equal(run.board.challenge(challenges[4].id).phase, "solving");
  assert.equal(arena.stats.activeMax, 3);
});

test("ordinary coordinator stop keeps the remote attempt open and logs exclude credentials", async (t) => {
  const { run, arena, workDir } = await setup(t);
  run.log({ type: "redaction-fixture", message: `${arena.token} test-model-key` });
  await run.stop("local_failure");
  assert.equal(arena.stats.finishes, 0);
  const logs = await readFile(join(workDir, "events.jsonl"), "utf8");
  assert.equal(logs.includes(arena.token), false);
  assert.equal(logs.includes("test-model-key"), false);
});

test("unknown close stays recorded until list reconciliation and resuming uses a new environment generation", async (t) => {
  const { run, arena, pool, create, pump } = await setup(t, [{ unique_code: "one", flag: "flag{one}", container: true }]);
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await pump();
  const previous = run.board.challenge(challenge.id);
  arena.setFault("close", { kind: "closed_disconnect" });
  await assert.rejects(run.defer({ challenge_id: challenge.id, expected_revision: previous.revision, reason: "Try a different challenge" }, randomUUID()));
  const deferred = run.board.challenge(challenge.id);
  assert.equal(deferred.phase, "deferred");
  assert.equal(deferred.environment.status, "unknown");
  assert.equal(deferred.ready, false);
  assert.equal(deferred.members.length, 0);
  assert.equal(arena.stats.finishes, 0);
  await pool.tool("lead", "arena_reconcile", {});
  const reconciled = run.board.challenge(challenge.id);
  assert.equal(reconciled.environment.status, "closed");
  await pool.tool("lead", "resume_challenge", { challenge_id: challenge.id, expected_revision: reconciled.revision });
  await pump();
  const resumed = run.board.challenge(challenge.id);
  assert.equal(resumed.phase, "solving");
  assert.ok(resumed.generation > previous.generation);
  assert.equal(resumed.environment.status, "active");
});

test("accepted submission releases each worker only after its execution settles", async (t) => {
  const { run, pool, create, pump, submit } = await setup(t);
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await pump();
  const worker = run.board.challenge(challenge.id).owner_id;
  await pool.begin(worker);
  await submit(challenge.id, "flag{one}");
  await pump();
  assert.equal(run.board.challenge(challenge.id).members.length, 1);
  await pool.idle(worker);
  await pump();
  assert.equal(run.board.challenge(challenge.id).members.length, 0);
});

test("finished workdir restart does not start another remote interaction", async (t) => {
  const { run, arena, config, create, pump, submit } = await setup(t);
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await pump();
  await submit(challenge.id, "flag{one}");
  await run.finish("Synthetic run is complete");
  await run.stop("shutdown");
  const callsBefore = arena.calls.length;
  let restartedPool;
  const restarted = await ContestRun.create(config, { poolFactory: async (options) => {
    restartedPool = new ScriptedPool(options);
    return restartedPool;
  } });
  t.after(() => restarted.stop("test_complete"));
  await restarted.start();
  assert.equal(arena.calls.length, callsBefore);
  assert.equal(restartedPool.notifications.length, 0);
  assert.equal(arena.stats.finishes, 1);
});

test("settled model error releases a completed assignment without ending the attempt", async (t) => {
  const { run, pool, arena, create, pump, submit } = await setup(t);
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await pump();
  const worker = run.board.challenge(challenge.id).owner_id;
  await pool.begin(worker);
  await submit(challenge.id, "flag{one}");
  await pool.error(worker, "Synthetic provider unavailable");
  await pump();
  assert.equal(run.board.challenge(challenge.id).members.length, 0);
  assert.equal(arena.stats.finishes, 0);
});

test("startup transient catalog failure recovers without finishing or reporting an empty contest", async (t) => {
  const { run, arena, pool } = await setup(t, undefined, { beforeStart: (fixture) => {
    fixture.setFault("list", { status: 503, code: "resource_unavailable" });
  } });
  assert.equal(run.catalog.length, 1);
  assert.ok(arena.calls.filter((call) => call.operation === "list").length >= 2);
  assert.equal(arena.stats.finishes, 0);
  assert.ok(pool.notifications.some((notice) => notice.id === "lead"));
});

test("explicit not-started rejection stays distinct from a wrong flag and permits a corrected submission", async (t) => {
  const { run, arena, create, pump, review } = await setup(t);
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await pump();
  const candidate = await review(challenge.id, "flag{one}");
  arena.setFault("submit", { status: 409, code: "challenge_not_started", message: "Start the challenge first" });
  const args = () => ({ challenge_id: challenge.id, candidate_id: candidate.candidate.id,
    review_revision: run.board.challenge(challenge.id).review_revision, expected_revision: run.board.challenge(challenge.id).revision });
  await assert.rejects(run.submit(args(), randomUUID()), (error) => error.code === "challenge_not_started");
  const failed = run.board.challenge(challenge.id);
  assert.equal(failed.phase, "reviewing");
  assert.equal(failed.submissions[0].status, "not_sent");
  assert.equal(failed.facts.some((fact) => fact.statement.includes("incorrect")), false);
  await run.startChallenge(challenge.id);
  assert.equal((await run.submit(args(), randomUUID())).outcome, "accepted");
  assert.equal(arena.stats.submits, 2);
});

test("first assignment notification persists its exact envelope and retries after delivery failure", async (t) => {
  const { run, pool, create } = await setup(t);
  const notify = pool.notify.bind(pool);
  let failed;
  let persistedAtFailure;
  pool.notify = async (id, text, options) => {
    if (!failed && options.eventId.startsWith("assignment:")) {
      failed = { id, text, ...options };
      persistedAtFailure = JSON.stringify(run.board.getMeta("pending_notifications"));
      throw new Error("Synthetic first delivery failure");
    }
    return notify(id, text, options);
  };
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await waitFor(() => failed !== undefined, "first assignment delivery attempt");
  assert.ok(persistedAtFailure.includes(failed.eventId));
  await waitFor(() => pool.notifications.some((notice) => notice.eventId === failed.eventId), "durable assignment retry");
  assert.deepEqual(pool.notifications.find((notice) => notice.eventId === failed.eventId), failed);
  assert.equal(JSON.stringify(run.board.getMeta("pending_notifications")).includes(failed.eventId), false);
  assert.equal(run.board.challenge(challenge.id).members.length, 1);
  assert.equal(run.board.events().filter((event) => event.type === "worker.assigned").length, 1);
});

test("restart replays the saved assignment envelope after its initial delivery failed", async (t) => {
  const { run, pool, config, create } = await setup(t);
  const notify = pool.notify.bind(pool);
  let failed;
  pool.notify = async (id, text, options) => {
    if (options.eventId.startsWith("assignment:")) {
      failed = { id, text, ...options };
      throw new Error("Synthetic unavailable delivery transport");
    }
    return notify(id, text, options);
  };
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await waitFor(() => failed !== undefined, "persisted failed assignment notification");
  assert.ok(JSON.stringify(run.board.getMeta("pending_notifications")).includes(failed.eventId));
  await run.stop("simulated_restart");
  let restartedPool;
  const restarted = await ContestRun.create(config, { poolFactory: async (options) => {
    restartedPool = new ScriptedPool(options);
    return restartedPool;
  } });
  t.after(() => restarted.stop("test_complete"));
  await restarted.start();
  await waitFor(() => restartedPool.notifications.some((notice) => notice.eventId === failed.eventId), "saved assignment replay");
  assert.deepEqual(restartedPool.notifications.find((notice) => notice.eventId === failed.eventId), failed);
  assert.equal(JSON.stringify(restarted.board.getMeta("pending_notifications")).includes(failed.eventId), false);
  assert.equal(restarted.board.challenge(challenge.id).members.length, 1);
  assert.equal(restarted.board.events().filter((event) => event.type === "worker.assigned").length, 1);
});

test("finish success followed by disconnect stays unknown and restart only reconciles with GET", { timeout: 5000 }, async (t) => {
  const { run, arena, config } = await setup(t);
  arena.setFault("finish", { kind: "finished_disconnect" });
  await run.finish("Synthetic explicit stop").catch((error) => assert.equal(error.outcomeUnknown, true));
  assert.equal(run.board.getMeta("finish_attempt").status, "unknown");
  assert.equal(arena.stats.finishes, 1);
  await run.finished;
  await run.stop("simulated_restart");
  const callsBefore = arena.calls.length;
  let restartedPool;
  const restarted = await ContestRun.create(config, { poolFactory: async (options) => {
    restartedPool = new ScriptedPool(options);
    return restartedPool;
  } });
  t.after(() => restarted.stop("test_complete"));
  await restarted.start();
  await restarted.finished;
  const reconciliation = arena.calls.slice(callsBefore);
  assert.ok(reconciliation.length >= 1);
  assert.ok(reconciliation.every((call) => call.method === "GET"));
  assert.equal(arena.stats.finishes, 1);
  assert.equal(restartedPool.notifications.length, 0);
  assert.ok(["unknown", "confirmed"].includes(restarted.board.getMeta("finish_attempt").status));
});

test("definite finish rejection persists not_sent and permits a later explicit retry", async (t) => {
  const { run, arena } = await setup(t);
  arena.setFault("finish", { status: 400, code: "invalid_request", message: "Synthetic technical rejection" });
  await assert.rejects(run.finish("Synthetic stop"), (error) => error.code === "invalid_request");
  assert.equal(run.board.getMeta("finish_attempt").status, "not_sent");
  assert.ok(!run.ending);
  assert.equal(arena.stats.finishes, 1);
  await run.finish("Explicit retry after rejection");
  assert.equal(run.board.getMeta("finish_attempt").status, "confirmed");
  assert.equal(arena.stats.finishes, 2);
});

test("connectivity probe uses description, skips environment calls and ends on the verdict", { timeout: 5000 }, async (t) => {
  for (const flag of ["flag{probe}", "incorrect"]) {
    const { run, arena, create, pump, submit } = await setup(t,
      [{ unique_code: "opaque-code", flag: "flag{probe}", description: "Return flag{probe} from this synthetic statement" }],
      { mode: "connectivity_test" });
    const challenge = create("opaque-code", { description: run.catalog[0].description });
    await run.startChallenge(challenge.id);
    await pump();
    assert.equal(run.board.challenge(challenge.id).phase, "solving");
    assert.match(run.board.challenge(challenge.id).description, /Return flag\{probe\}/);
    assert.equal(arena.stats.starts, 0);
    const result = await submit(challenge.id, flag);
    assert.equal(result.outcome, flag === "incorrect" ? "rejected" : "accepted");
    await run.finished;
    assert.equal(run.board.getMeta("finished").completed, flag !== "incorrect");
    assert.equal(arena.stats.submits, 1);
    assert.equal(arena.stats.closes, 0);
    assert.equal(arena.stats.finishes, 0);
  }
});

test("reopening through repeated pending responses advances the environment generation exactly once", async (t) => {
  const { run, pool, arena, create, pump } = await setup(t,
    [{ unique_code: "one", flag: "flag{one}", container: true }]);
  const challenge = create("one");
  await run.startChallenge(challenge.id);
  await pump();
  const original = run.board.challenge(challenge.id);
  await run.defer({ challenge_id: challenge.id, expected_revision: original.revision,
    reason: "Synthetic environment restart" }, randomUUID());
  const deferred = run.board.challenge(challenge.id);
  arena.setFault("start", { kind: "pending", times: 2 });
  await pool.tool("lead", "resume_challenge", { challenge_id: challenge.id, expected_revision: deferred.revision });
  await pump();
  assert.equal(run.board.challenge(challenge.id).environment.status, "pending");
  assert.equal(run.board.challenge(challenge.id).generation, original.generation);
  assert.equal(run.board.challenge(challenge.id).members.length, 0);
  await run.startChallenge(challenge.id);
  assert.equal(run.board.challenge(challenge.id).generation, original.generation);
  await run.startChallenge(challenge.id);
  await pump();
  const reopened = run.board.challenge(challenge.id);
  assert.equal(reopened.environment.status, "active");
  assert.equal(reopened.generation, original.generation + 1);
  assert.equal(reopened.members.length, 1);
  await run.startChallenge(challenge.id);
  await run.reconcileEnvironments();
  assert.equal(run.board.challenge(challenge.id).generation, reopened.generation);
});

test("a queued old-assignment notice is discarded after that Worker claims another task", async (t) => {
  const { run, pool, create, pump } = await setup(t,
    [{ unique_code: "one", flag: "flag{one}" }, { unique_code: "two", flag: "flag{two}" }]);
  const first = create("one");
  await run.startChallenge(first.id);
  await pump();
  const original = run.board.challenge(first.id);
  const worker = original.owner_id;
  const oldContext = run.board.context(worker);
  const notify = pool.notify.bind(pool);
  let attempts = 0;
  pool.notify = async (id, text, options) => {
    if (options.eventId === "old-assignment-notice") {
      attempts += 1;
      throw new Error("Keep the old notice pending until reassignment");
    }
    return notify(id, text, options);
  };
  await assert.rejects(run.notify(worker, "Synthetic old task update", {
    eventId: "old-assignment-notice", scope: first.id, version: original.revision,
  }), /old notice/);
  await run.defer({ challenge_id: first.id, expected_revision: original.revision,
    close_environment: false, reason: "Switch to the second local task" }, randomUUID());
  const second = create("two");
  await run.startChallenge(second.id);
  const current = run.board.challenge(second.id);
  run.board.execute(worker, "claim", { challenge_id: second.id, expected_revision: current.revision,
    request_id: randomUUID() }, run.board.context(worker));
  assert.notEqual(run.board.context(worker).assignment_id, oldContext.assignment_id);
  await run.flushNotifications();
  assert.equal(attempts, 1);
  assert.equal(pool.notifications.some((notice) => notice.eventId === "old-assignment-notice"), false);
  assert.equal(JSON.stringify(run.board.getMeta("pending_notifications")).includes("old-assignment-notice"), false);
  assert.equal(run.board.context(worker).challenge_id, second.id);
});

test("an idle Lead receives one recovery notice when every unfinished task is deferred", async (t) => {
  const { run, pool, arena, create, pump } = await setup(t,
    [{ unique_code: "one", flag: "flag{one}" }, { unique_code: "two", flag: "flag{two}" }]);
  const first = create("one");
  const second = create("two");
  await run.startChallenge(first.id);
  await run.startChallenge(second.id);
  await pump();
  await pool.begin("lead");
  for (const challenge of [first, second]) {
    const current = run.board.challenge(challenge.id);
    await run.defer({ challenge_id: challenge.id, expected_revision: current.revision,
      reason: "Review the remaining strategy" }, randomUUID());
  }
  await pump();
  const notices = () => pool.notifications.filter((notice) => notice.id === "lead" && notice.eventId.startsWith("deferred:"));
  assert.equal(notices().length, 0);
  await pool.idle("lead");
  await pump();
  assert.equal(notices().length, 1);
  assert.match(notices()[0].text, /恢复题目/);
  await pump();
  assert.equal(notices().length, 1);
  assert.equal(arena.stats.finishes, 0);
});

test("a partial accepted flag keeps the same team solving until all flags complete", async (t) => {
  const { run, pool, arena, create, pump, submit } = await setup(t,
    [{ unique_code: "stages", flags: ["flag{first}", "flag{second}"], container: true }]);
  const challenge = create("stages", { difficulty: "hard" });
  await run.startChallenge(challenge.id);
  await pump();
  const members = run.board.challenge(challenge.id).members;
  assert.equal(members.length, 2);
  const first = await submit(challenge.id, "flag{first}");
  await pump();
  assert.equal(first.outcome, "accepted");
  const partial = run.board.challenge(challenge.id);
  assert.equal(partial.phase, "solving");
  assert.equal(partial.environment.status, "active");
  assert.deepEqual(partial.members.map((member) => member.id), members.map((member) => member.id));
  assert.ok(partial.members.every((member) => run.board.context(member.worker_id).challenge_id === challenge.id));
  assert.equal(partial.submissions.filter((submission) => submission.status === "accepted").length, 1);
  assert.equal(arena.stats.finishes, 0);
  await pool.begin(partial.owner_id);
  const second = await submit(challenge.id, "flag{second}");
  await pump();
  assert.equal(second.outcome, "accepted");
  assert.equal(run.board.challenge(challenge.id).phase, "completed");
  assert.equal(run.board.challenge(challenge.id).members.length, 1);
  await pool.idle(partial.owner_id);
  await pump();
  assert.equal(run.board.challenge(challenge.id).members.length, 0);
  assert.equal(arena.stats.submits, 2);
  assert.equal(run.board.getMeta("cumulative_score"), 100);
});

test("invalid attempt token from a Lead arena tool stops local work without finishing remotely", { timeout: 5000 }, async (t) => {
  const { run, pool, arena } = await setup(t);
  arena.setFault("list", { status: 404, code: "task_not_found", message: "The attempt token is no longer valid" });
  await assert.rejects(pool.tool("lead", "arena_list", {}), (error) => error.code === "task_not_found");
  const result = await run.finished;
  assert.equal(result.reason, "invalid_attempt_token");
  assert.equal(run.ending, true);
  assert.equal(run.started, false);
  assert.equal(arena.stats.finishes, 0);
});

test("reconciliation leaves an unstarted offline challenge pending and unassigned", async (t) => {
  const { run, pool, arena, create, pump } = await setup(t);
  const challenge = create("one");
  await pool.tool("lead", "arena_reconcile", {});
  await pump();
  const current = run.board.challenge(challenge.id);
  assert.equal(current.phase, "pending");
  assert.equal(current.ready, false);
  assert.equal(current.members.length, 0);
  assert.equal(arena.stats.starts, 0);
  await run.startChallenge(challenge.id);
  await pump();
  assert.equal(run.board.challenge(challenge.id).phase, "solving");
  assert.equal(arena.stats.starts, 1);
});
