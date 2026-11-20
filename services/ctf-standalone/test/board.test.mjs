import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { Board, ASSISTANCE_REMINDER_MS } from "../src/board.mjs";

function setup(t, count = 20) {
  let time = 1000;
  const directory = mkdtempSync(join(tmpdir(), "bbx-ctf-board-"));
  const path = join(directory, "board.sqlite");
  const board = new Board({ path, initialWorkers: count, clock: () => time });
  t.after(() => { board.close(); rmSync(directory, { recursive: true, force: true }); });
  for (const worker of board.workers()) board.execute("platform", "worker_ready", { worker_id: worker.id });
  const lead = (action, args = {}) => board.execute("lead", action, { request_id: randomUUID(), ...args });
  const create = (args = {}) => lead("create_challenge", { unique_code: randomUUID(), title: "Fixture", ...args });
  const call = (workerId, action, args = {}, context = board.context(workerId)) => board.execute(workerId, action,
    { request_id: randomUUID(), expected_revision: board.challenge(context.challenge_id ?? args.challenge_id).revision, ...args }, context);
  return { board, path, lead, create, call, advance: (delta) => { time += delta; } };
}
function fact(call, worker, challenge, extra = {}) {
  return call(worker, "post_fact", { challenge_id: challenge, statement: "The fixture has an input",
    kind: "observation", evidence: [{ type: "text", summary: "Read fixture description" }], ...extra }).fact;
}
function intent(call, worker, challenge, basis, extra = {}) {
  return call(worker, "post_intent", { challenge_id: challenge, statement: "Inspect an input",
    expected: "A reproducible result", method: "Read the fixture", based_on: [basis], ...extra }).intent;
}
function review(call, worker, challenge, flag = "flag{fixture}") {
  return call(worker, "submit_review", { challenge_id: challenge, flag, summary: "Candidate from fixture" });
}
function accepted(board, lead, call, worker, challenge) {
  const result = review(call, worker, challenge);
  const started = lead("begin_submission", { challenge_id: challenge, candidate_id: result.candidate.id,
    review_revision: result.challenge.review_revision, expected_revision: board.challenge(challenge).revision });
  board.execute("platform", "submission_result", { submission_id: started.submission.id, status: "accepted", receipt: { correct: true, awarded: 0 } });
}
function fails(fn, code) { assert.throws(fn, (error) => error.code === code); }

test("twenty Workers start as records and preparation reports real readiness", (t) => {
  const board = new Board(); t.after(() => board.close());
  assert.equal(board.workers().length, 20);
  assert.ok(board.workers().every((item) => item.status === "starting" && !item.has_worked));
  const request = { request_id: "grow", target_total: 22 };
  const first = board.execute("lead", "grow_workers", request);
  assert.deepEqual(first.created, ["worker-21", "worker-22"]);
  assert.deepEqual(first.succeeded, []);
  board.execute("platform", "worker_ready", { worker_id: "worker-21" });
  board.execute("platform", "worker_failed", { worker_id: "worker-22", error: "Preparation failed" });
  const again = board.execute("lead", "grow_workers", request);
  assert.deepEqual(again.succeeded, ["worker-21"]);
  assert.deepEqual(again.failed, [{ worker_id: "worker-22", error: "Preparation failed" }]);
  const retry = board.execute("lead", "grow_workers", { request_id: "retry", target_total: 22 });
  assert.deepEqual(retry.created, ["worker-23"]);
  assert.equal(board.workers().length, 23);
  fails(() => board.execute("lead", "grow_workers", { ...request, target_total: 23 }), "request_conflict");
});

test("30 tasks dispatch twenty unique primary assignments before any aid", (t) => {
  const { board, create } = setup(t);
  const challenges = Array.from({ length: 30 }, (_, index) => create({ title: String(index), difficulty: "hard" }));
  const dispatched = board.execute("platform", "dispatch");
  assert.equal(dispatched.assignments.length, 20);
  assert.equal(new Set(dispatched.assignments.map((item) => item.challenge.id)).size, 20);
  assert.equal(dispatched.available.new_challenges.length, 10);
  assert.ok(dispatched.assignments.every((item) => item.challenge.members[0].role === "primary"));
  assert.equal(board.snapshot("lead").challenges.length, challenges.length);
  const cursor = board.getMeta("event_cursor");
  assert.deepEqual(board.execute("platform", "dispatch").assignments, []);
  assert.equal(board.getMeta("event_cursor"), cursor);
});

test("hard challenges get a primary and assistant; same team knowledge syncs", (t) => {
  const { board, create, call } = setup(t, 4);
  const challenge = create({ multi_stage: true });
  assert.equal(challenge.difficulty, "hard");
  const assigned = board.execute("platform", "dispatch").assignments;
  assert.equal(assigned.length, 2);
  const [primary, assistant] = assigned.map((item) => item.worker_id);
  const eventCursor = board.getMeta("event_cursor");
  const observation = fact(call, assistant, challenge.id);
  assert.equal(observation.author, assistant);
  const event = board.events(eventCursor).at(-1);
  assert.deepEqual(new Set(event.worker_ids), new Set([primary, assistant]));
  assert.equal(board.challenge(challenge.id).owner_id, primary);
  fails(() => board.snapshot("worker-3", { challengeId: challenge.id }), "forbidden");
});

test("subsequent tasks are chosen in an active idle response with frozen identity", (t) => {
  const { board, create, call, lead } = setup(t, 1);
  const a = create();
  board.execute("platform", "dispatch");
  accepted(board, lead, call, "worker-1", a.id);
  assert.deepEqual(board.execute("platform", "release_finished").released, ["worker-1"]);
  const b = create();
  const next = board.execute("platform", "dispatch");
  assert.equal(next.assignments.length, 0);
  assert.equal(next.notifications[0].worker_id, "worker-1");
  const idle = board.context("worker-1");
  board.execute("platform", "activity", { worker_id: "worker-1", context: idle, running: true });
  const args = { request_id: "claim-b", challenge_id: b.id, expected_revision: b.revision };
  const selected = board.execute("worker-1", "claim", args, idle);
  assert.equal(selected.challenge.id, b.id);
  assert.equal(board.execute("worker-1", "claim", args, idle).context.assignment_id, selected.context.assignment_id);
  fails(() => call("worker-1", "post_fact", { challenge_id: b.id, statement: "Stale", evidence: [{type:"text",summary:"stale"}] }, idle), "stale_context");
  board.execute("platform", "activity", { worker_id: "worker-1", context: idle, running: false });
  assert.equal(fact(call, "worker-1", b.id).assignment_ref.assignment_id, selected.context.assignment_id);
});

test("new work outranks assistance and competing claims fail without assigning alternatives", (t) => {
  const { board, create, call, lead } = setup(t, 3);
  const old = create({ difficulty: "hard" });
  board.execute("platform", "dispatch");
  accepted(board, lead, call, "worker-1", old.id);
  board.execute("platform", "release_finished");
  const hard = create({ difficulty: "hard" });
  board.execute("platform", "dispatch");
  assert.equal(board.challenge(hard.id).owner_id, "worker-3");
  const fresh = create();
  fails(() => call("worker-1", "claim", { challenge_id: hard.id }), "work_changed");
  const revision = fresh.revision;
  call("worker-1", "claim", { challenge_id: fresh.id, expected_revision: revision });
  fails(() => call("worker-2", "claim", { challenge_id: fresh.id, expected_revision: revision }), "revision_conflict");
  assert.equal(board.context("worker-2").assignment_id, null);
});

test("Intent facts, ownership, three holdings and parallel direction dependencies", (t) => {
  const { board, create, call } = setup(t, 12);
  const challenge = create(); board.execute("platform", "dispatch");
  const basis = fact(call, "worker-1", challenge.id);
  const first = intent(call, "worker-1", challenge.id, basis.id, { claim: true });
  intent(call, "worker-1", challenge.id, basis.id, { claim: true });
  intent(call, "worker-1", challenge.id, basis.id, { claim: true });
  fails(() => intent(call, "worker-1", challenge.id, basis.id, { claim: true }), "intent_limit");
  assert.equal(board.challenge(challenge.id).intents.length, 3);
  intent(call, "worker-1", challenge.id, basis.id, { parallel: true, parallel_key: "branch-a", depends_on: [first.id] });
  assert.equal(board.challenge(challenge.id).assistance_target, 0);
  fact(call, "worker-1", challenge.id, { resolves: first.id, result: "confirmed" });
  assert.equal(board.challenge(challenge.id).assistance_target, 1);
  board.execute("platform", "dispatch");
  fails(() => fact(call, "worker-2", challenge.id, { resolves: board.challenge(challenge.id).intents[1].id, result: "confirmed" }), "intent_not_held");
  fails(() => intent(call, "worker-1", challenge.id, basis.id, { parallel: true, parallel_key: "branch-a" }), "duplicate_direction");
  for (let index = 0; index < 15; index++) intent(call, "worker-1", challenge.id, basis.id, { parallel: true, parallel_key: `other-${index}` });
  board.execute("platform", "dispatch");
  assert.equal(board.challenge(challenge.id).members.length, 10);
  assert.equal(board.challenge(challenge.id).assistance_target, 9);
  const held = board.challenge(challenge.id).intents.find((item) => item.status === "claimed");
  call("worker-1", "release_intent", { challenge_id: challenge.id, intent_id: held.id, note: "Use another direction" });
  assert.equal(board.challenge(challenge.id).intents.find((item) => item.id === held.id).attempts, 1);
});

test("references are scoped and immutable file evidence keeps the registered version", (t) => {
  const { board, create, call } = setup(t, 2);
  const a = create(); const b = create(); board.execute("platform", "dispatch");
  const first = fact(call, "worker-1", a.id);
  fails(() => fact(call, "worker-2", b.id, { kind: "inference", derived_from: [first.id] }), "invalid_reference");
  fails(() => fact(call, "worker-1", a.id, { evidence: [{ type: "file", path: "/tmp/x", sha256: "a".repeat(64), summary: "Forged" }] }), "invalid_evidence");
  const artifact = board.execute("platform", "register_artifact", { challenge_id: a.id,
    worker_id: "worker-1", context: board.context("worker-1"), path: "/workspace/output", uri: "artifacts/one",
    sha256: "a".repeat(64), size: 4, call_id: "tool-call-1" });
  const saved = fact(call, "worker-1", a.id, { evidence: [{ artifact_id: artifact.id, summary: "Captured output" }] });
  board.execute("platform", "register_artifact", { challenge_id: a.id, worker_id: "worker-1",
    context: board.context("worker-1"), path: "/workspace/output", uri: "artifacts/two", sha256: "b".repeat(64), size: 5 });
  assert.equal(saved.evidence[0].sha256, "a".repeat(64));
  assert.equal(saved.provenance, "tool_backed");
  fact(call, "worker-1", a.id, { disputes: [first.id] });
  const old = board.challenge(a.id).facts.find((item) => item.id === first.id);
  assert.equal(old.statement, first.statement);
  assert.equal(old.status, "disputed");
});

test("parallel review candidates survive; rejection atomically records feedback", (t) => {
  const { board, create, call, lead } = setup(t, 3);
  const challenge = create({ difficulty: "hard" }); board.execute("platform", "dispatch");
  const revision = board.challenge(challenge.id).revision;
  const a = call("worker-1", "submit_review", { challenge_id: challenge.id, expected_revision: revision, flag: "flag{one}", summary: "one" });
  call("worker-2", "submit_review", { challenge_id: challenge.id, expected_revision: revision, flag: "flag{two}", summary: "two" });
  assert.equal(board.challenge(challenge.id).candidates.length, 2);
  assert.equal(board.challenge(challenge.id).review_revision, 1);
  assert.deepEqual(board.available().assistance, []);
  assert.deepEqual(new Set(board.events().at(-1).worker_ids), new Set(["lead", "worker-1", "worker-2"]));
  const started = lead("begin_submission", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision,
    candidate_id: a.candidate.id, review_revision: 1 });
  board.execute("platform", "submission_result", { submission_id: started.submission.id, status: "rejected",
    reason: "Official response: incorrect flag", receipt: { correct: false } });
  const result = board.challenge(challenge.id);
  assert.equal(result.phase, "solving");
  assert.equal(result.facts.find((item) => item.id === result.feedback_fact_id).statement, "Official response: incorrect flag");
  assert.equal(result.members.length, 2);
});

test("unknown submission survives retries and deferral cannot discard it", (t) => {
  const { board, create, call, lead } = setup(t, 1);
  const challenge = create(); board.execute("platform", "dispatch");
  const submitted = review(call, "worker-1", challenge.id);
  const request = { request_id: "submit", challenge_id: challenge.id, candidate_id: submitted.candidate.id,
    expected_revision: board.challenge(challenge.id).revision, review_revision: 1 };
  const started = board.execute("lead", "begin_submission", request);
  assert.equal(started.should_submit, true);
  board.execute("platform", "submission_result", { submission_id: started.submission.id, status: "unknown", receipt: {} });
  assert.equal(board.execute("lead", "begin_submission", request).should_submit, false);
  fails(() => lead("defer", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision, reason: "Later" }), "submission_unresolved");
  board.execute("platform", "submission_result", { submission_id: started.submission.id, status: "accepted", receipt: { correct: true, awarded: 0 } });
  assert.equal(board.challenge(challenge.id).phase, "completed");
  fails(() => board.execute("platform", "submission_result", { submission_id: started.submission.id, status: "rejected", reason: "Late" }), "receipt_conflict");
});

test("partial acceptance preserves the team and resumes solving until the final flag", (t) => {
  const { board, create, call, lead, advance } = setup(t, 2);
  const challenge = create({ difficulty: "hard" });
  board.execute("platform", "dispatch");
  const contexts = board.workers().map((worker) => board.context(worker.id));
  advance(2 * 60 * 1000);
  const first = review(call, "worker-1", challenge.id, "flag{first}");
  const started = lead("begin_submission", { challenge_id: challenge.id, candidate_id: first.candidate.id,
    expected_revision: board.challenge(challenge.id).revision, review_revision: first.challenge.review_revision });
  advance(4 * 60 * 1000);
  const partial = board.execute("platform", "submission_result", { submission_id: started.submission.id,
    status: "accepted", challenge_completed: false, receipt: { correct: true, flag_count: 1, total_flag_count: 2 } });
  assert.equal(partial.submission.challenge_completed, false);
  assert.equal(partial.challenge.phase, "solving");
  assert.equal(partial.challenge.environment.status, "active");
  assert.equal(partial.challenge.ready, true);
  assert.equal(partial.challenge.members.length, 2);
  assert.equal(partial.challenge.solve_elapsed_ms, 2 * 60 * 1000);
  assert.deepEqual(board.workers().map((worker) => board.context(worker.id)), contexts);
  assert.deepEqual(board.execute("platform", "release_finished").released, []);
  const progress = partial.challenge.facts.find((item) => item.id === partial.challenge.feedback_fact_id);
  assert.match(progress.statement, /accepted.*remaining flags/);
  assert.match(progress.evidence[0].summary, new RegExp(started.submission.id));
  assert.deepEqual(new Set(board.events().at(-1).worker_ids), new Set(["lead", "worker-1", "worker-2"]));

  advance(3 * 60 * 1000);
  const final = review(call, "worker-2", challenge.id, "flag{final}");
  const ending = lead("begin_submission", { challenge_id: challenge.id, candidate_id: final.candidate.id,
    expected_revision: board.challenge(challenge.id).revision, review_revision: final.challenge.review_revision });
  const completed = board.execute("platform", "submission_result", { submission_id: ending.submission.id,
    status: "accepted", challenge_completed: true, receipt: { correct: true, flag_count: 2, total_flag_count: 2 } });
  assert.equal(completed.challenge.phase, "completed");
  assert.equal(completed.challenge.environment.status, "closed");
  assert.equal(completed.challenge.solve_elapsed_ms, 5 * 60 * 1000);
  assert.equal(completed.challenge.solve_started_at, null);
  assert.equal(completed.challenge.submissions.length, 2);
  assert.deepEqual(board.execute("platform", "release_finished").released, ["worker-1", "worker-2"]);
});

test("accepted receipt retries only upgrade completion when explicitly confirmed", (t) => {
  const { board, create, call, lead, advance } = setup(t, 1);
  const challenge = create();
  board.execute("platform", "dispatch");
  const reviewed = review(call, "worker-1", challenge.id);
  const started = lead("begin_submission", { challenge_id: challenge.id, candidate_id: reviewed.candidate.id,
    expected_revision: board.challenge(challenge.id).revision, review_revision: reviewed.challenge.review_revision });
  const receipt = { submission_id: started.submission.id, status: "accepted", challenge_completed: false,
    receipt: { correct: true, flag_count: 1, total_flag_count: 2 } };
  const partial = board.execute("platform", "submission_result", receipt);
  const cursor = board.getMeta("event_cursor");
  advance(1000);
  assert.deepEqual(board.execute("platform", "submission_result", receipt), partial);
  assert.deepEqual(board.execute("platform", "submission_result", {
    submission_id: started.submission.id, status: "accepted",
  }), partial);
  assert.equal(board.getMeta("event_cursor"), cursor);

  const completion = { ...receipt, challenge_completed: true,
    receipt: { correct: true, flag_count: 2, total_flag_count: 2 } };
  const completed = board.execute("platform", "submission_result", completion);
  assert.equal(completed.challenge.phase, "completed");
  assert.equal(completed.challenge.solve_elapsed_ms, 1000);
  assert.equal(completed.challenge.solve_started_at, null);
  assert.equal(completed.submission.challenge_completed, true);
  assert.equal(completed.challenge.facts.length, partial.challenge.facts.length);
  const finalCursor = board.getMeta("event_cursor");
  assert.deepEqual(board.execute("platform", "submission_result", completion), completed);
  assert.deepEqual(board.execute("platform", "submission_result", receipt), completed);
  assert.equal(board.getMeta("event_cursor"), finalCursor);
});

test("whole-challenge confirmation may finish a later review in the same environment", (t) => {
  const { board, create, call, lead } = setup(t, 1);
  const challenge = create();
  board.execute("platform", "dispatch");
  const first = review(call, "worker-1", challenge.id, "flag{first}");
  const started = lead("begin_submission", { challenge_id: challenge.id, candidate_id: first.candidate.id,
    expected_revision: board.challenge(challenge.id).revision, review_revision: first.challenge.review_revision });
  board.execute("platform", "submission_result", {
    submission_id: started.submission.id, status: "accepted", challenge_completed: false,
  });
  const later = review(call, "worker-1", challenge.id, "flag{later}");
  assert.equal(later.challenge.review_revision, first.challenge.review_revision + 1);
  const completed = board.execute("platform", "submission_result", {
    submission_id: started.submission.id, status: "accepted", challenge_completed: true,
  });
  assert.equal(completed.challenge.phase, "completed");
});

test("completion receipts from an old environment cannot finish a new attempt", (t) => {
  const { board, create, call, lead } = setup(t, 1);
  const challenge = create();
  board.execute("platform", "dispatch");
  const first = review(call, "worker-1", challenge.id);
  const started = lead("begin_submission", { challenge_id: challenge.id, candidate_id: first.candidate.id,
    expected_revision: board.challenge(challenge.id).revision, review_revision: first.challenge.review_revision });
  board.execute("platform", "submission_result", {
    submission_id: started.submission.id, status: "accepted", challenge_completed: false,
  });
  lead("defer", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision,
    reason: "Resume with a fresh challenge environment" });
  board.execute("platform", "release_finished");
  board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "closed" });
  lead("resume", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision });
  board.execute("platform", "set_environment", {
    challenge_id: challenge.id, status: "active", ready: true, new_generation: true,
  });
  call("worker-1", "claim", { challenge_id: challenge.id });
  const later = review(call, "worker-1", challenge.id, "flag{new-environment}");
  const result = board.execute("platform", "submission_result", {
    submission_id: started.submission.id, status: "accepted", challenge_completed: true,
  });
  assert.equal(result.challenge.phase, "reviewing");
  assert.equal(result.challenge.environment.status, "active");
  assert.equal(result.challenge.generation, later.challenge.generation);
  assert.equal(result.challenge.facts.length, later.challenge.facts.length);
  assert.deepEqual(result.challenge.members, later.challenge.members);
});

test("submission completion accepts boolean input and rejects malformed values atomically", (t) => {
  const { board, create, call, lead } = setup(t, 1);
  const challenge = create();
  board.execute("platform", "dispatch");
  const reviewed = review(call, "worker-1", challenge.id);
  const started = lead("begin_submission", { challenge_id: challenge.id, candidate_id: reviewed.candidate.id,
    expected_revision: board.challenge(challenge.id).revision, review_revision: reviewed.challenge.review_revision });
  const cursor = board.getMeta("event_cursor");
  for (const value of [null, "false", 0, [], {}]) {
    fails(() => board.execute("platform", "submission_result", {
      submission_id: started.submission.id, status: "accepted", challenge_completed: value,
    }), "invalid_argument");
  }
  assert.equal(board.getMeta("event_cursor"), cursor);
  assert.equal(board.challenge(challenge.id).submissions[0].status, "pending");
  assert.equal(board.challenge(challenge.id).phase, "reviewing");
});

test("each finished execution releases separately; active recovery is preserved", (t) => {
  const { board, create, call, lead, path } = setup(t, 2);
  const challenge = create({ difficulty: "hard" }); board.execute("platform", "dispatch");
  const a = board.context("worker-1"); const b = board.context("worker-2");
  board.execute("platform", "activity", { worker_id: "worker-1", context: a, running: true });
  board.execute("platform", "activity", { worker_id: "worker-2", context: b, running: true });
  accepted(board, lead, call, "worker-1", challenge.id);
  assert.deepEqual(board.execute("platform", "release_finished").released, []);
  board.execute("platform", "activity", { worker_id: "worker-1", context: a, running: false });
  assert.deepEqual(board.execute("platform", "release_finished").released, ["worker-1"]);
  const recovered = new Board({ path });
  assert.equal(recovered.workers().find((item) => item.id === "worker-2").active, true);
  assert.deepEqual(recovered.execute("platform", "release_finished").released, []);
  recovered.close();
  board.execute("platform", "activity", { worker_id: "worker-2", context: b, running: false });
  assert.deepEqual(board.execute("platform", "release_finished").released, ["worker-2"]);
});

test("deferral retains progress, resumes explicitly and invalidates old assignment/environment", (t) => {
  const { board, create, call, lead } = setup(t, 1);
  const challenge = create(); board.execute("platform", "dispatch");
  const old = board.context("worker-1");
  const basis = fact(call, "worker-1", challenge.id);
  const direction = intent(call, "worker-1", challenge.id, basis.id, { claim: true });
  lead("defer", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision, reason: "Save progress" });
  assert.equal(board.available().new_challenges.length, 0);
  board.execute("platform", "release_finished");
  const kept = board.challenge(challenge.id);
  assert.equal(kept.intents.find((item) => item.id === direction.id).status, "open");
  board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "closed" });
  lead("resume", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision });
  assert.equal(board.available().new_challenges.length, 0);
  board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "pending" });
  assert.equal(board.challenge(challenge.id).generation, 1);
  board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "active", ready: true,
    connection: { host: "new-host" }, new_generation: true });
  call("worker-1", "claim", { challenge_id: challenge.id });
  assert.equal(board.context("worker-1").environment_generation, 2);
  fails(() => call("worker-1", "post_fact", { challenge_id: challenge.id, statement: "Late", evidence: [{ type: "text", summary: "old" }] }, old), "stale_context");
  assert.ok(board.challenge(challenge.id).facts.some((item) => item.id === basis.id));
});

test("thirty minute wall clock excludes review and deferral and fires only once", (t) => {
  const { board, create, call, lead, advance } = setup(t, 1);
  const challenge = create(); board.execute("platform", "dispatch");
  advance(10 * 60 * 1000);
  review(call, "worker-1", challenge.id);
  assert.equal(board.nextReminderAt(), null);
  advance(ASSISTANCE_REMINDER_MS);
  assert.deepEqual(board.execute("platform", "tick").reminded, []);
  lead("reject_review", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision, review_revision: 1, reason: "Need evidence" });
  advance(20 * 60 * 1000);
  assert.deepEqual(board.execute("platform", "tick").reminded, [challenge.id]);
  assert.equal(board.challenge(challenge.id).solve_elapsed_ms, ASSISTANCE_REMINDER_MS);
  assert.equal(board.challenge(challenge.id).assistance_target, 1);
  assert.equal(board.nextReminderAt(), null);
  assert.deepEqual(board.execute("platform", "tick").reminded, []);
});

test("existing assistance gets a reminder without automatic new seats", (t) => {
  const { board, create, advance } = setup(t, 4);
  const challenge = create({ difficulty: "hard" }); board.execute("platform", "dispatch");
  advance(ASSISTANCE_REMINDER_MS);
  board.execute("platform", "tick");
  assert.equal(board.challenge(challenge.id).assistance_target, 1);
  assert.equal(board.execute("platform", "dispatch").assignments.length, 0);
});

test("change listeners see committed state; rollback emits no partial facts", (t) => {
  const { board, create, call, path } = setup(t, 1);
  const reader = new Board({ path }); t.after(() => reader.close());
  const seen = [];
  board.on("change", (event) => { if (event.challenge_id) seen.push(reader.challenge(event.challenge_id).revision); });
  const challenge = create(); board.execute("platform", "dispatch");
  assert.equal(seen.at(-1), board.challenge(challenge.id).revision);
  const cursor = board.getMeta("event_cursor");
  fails(() => fact(call, "worker-1", challenge.id, { kind: "inference", derived_from: [] }), "invalid_fact");
  assert.deepEqual(board.events(cursor), []);
  board.setMeta("coordinator_cursor", cursor);
  assert.equal(reader.getMeta("coordinator_cursor"), cursor);
});

test("one held direction can trigger independent assistance before three holdings", (t) => {
  const { board, create, call } = setup(t, 2);
  const challenge = create(); board.execute("platform", "dispatch");
  const basis = fact(call, "worker-1", challenge.id);
  intent(call, "worker-1", challenge.id, basis.id, { claim: true });
  intent(call, "worker-1", challenge.id, basis.id, { parallel: true, parallel_key: "independent" });
  assert.equal(board.challenge(challenge.id).assistance_target, 1);
  assert.equal(board.execute("platform", "dispatch").assignments[0].worker_id, "worker-2");
});

test("normal snapshots omit event history and explicit cursors return scoped deltas", (t) => {
  const { board, create, call, lead } = setup(t, 2);
  const first = create();
  lead("post_fact", { challenge_id: first.id, expected_revision: first.revision,
    statement: "Actual challenge description", evidence: [{ type: "text", summary: first.title }] });
  const second = create(); board.execute("platform", "dispatch");
  const cursor = board.getMeta("event_cursor");
  fact(call, "worker-1", first.id);
  fact(call, "worker-2", second.id);
  assert.equal(board.snapshot("worker-1").events, undefined);
  const delta = board.snapshot("worker-1", { challengeId: first.id, sinceRevision: cursor });
  assert.equal(delta.events.length, 1);
  assert.equal(delta.events[0].actor_id, "worker-1");
  assert.equal(delta.challenge.facts, undefined);
  assert.equal(delta.event_cursor, board.getMeta("event_cursor"));
});

test("late candidate writes from an earlier review cannot enter a new round", (t) => {
  const { board, create, call, lead } = setup(t, 2);
  const challenge = create({ difficulty: "hard" }); board.execute("platform", "dispatch");
  const oldRevision = board.challenge(challenge.id).revision;
  review(call, "worker-1", challenge.id, "flag{one}");
  lead("reject_review", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision,
    review_revision: 1, reason: "Wrong result" });
  review(call, "worker-1", challenge.id, "flag{new}");
  fails(() => call("worker-2", "submit_review", { challenge_id: challenge.id, expected_revision: oldRevision,
    flag: "flag{late}", summary: "Old computation" }), "stale_review");
  assert.equal(board.challenge(challenge.id).candidates.length, 2);
});

test("definitive technical submission refusal permits retry without wrong-answer feedback", (t) => {
  const { board, create, call, lead } = setup(t, 1);
  const challenge = create(); board.execute("platform", "dispatch");
  const candidate = review(call, "worker-1", challenge.id).candidate;
  const submit = () => lead("begin_submission", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision,
    review_revision: 1, candidate_id: candidate.id });
  const attempt = submit();
  const receipt = { submission_id: attempt.submission.id, status: "not_sent", reason: "challenge_not_started", receipt: { status: 409 } };
  board.execute("platform", "submission_result", receipt);
  board.execute("platform", "submission_result", receipt);
  assert.equal(board.challenge(challenge.id).phase, "reviewing");
  assert.equal(board.challenge(challenge.id).facts.length, 0);
  const retry = submit();
  assert.equal(retry.should_submit, true);
  assert.notEqual(retry.submission.id, attempt.submission.id);
});

test("automatic assistance closes when members take the independent work", (t) => {
  const { board, create, call } = setup(t, 2);
  const challenge = create(); board.execute("platform", "dispatch");
  const basis = fact(call, "worker-1", challenge.id);
  intent(call, "worker-1", challenge.id, basis.id, { claim: true });
  const direction = intent(call, "worker-1", challenge.id, basis.id, { parallel: true, parallel_key: "independent" });
  assert.equal(board.challenge(challenge.id).assistance_target, 1);
  call("worker-1", "claim_intent", { challenge_id: challenge.id, intent_id: direction.id, expected_version: direction.version });
  assert.equal(board.challenge(challenge.id).assistance_target, 0);
  assert.deepEqual(board.execute("platform", "dispatch").assignments, []);
  call("worker-1", "request_assistance", { challenge_id: challenge.id, reason: "A second perspective is required" });
  fact(call, "worker-1", challenge.id);
  assert.equal(board.challenge(challenge.id).assistance_target, 1);
});

test("invalid argument shapes fail with domain codes before writing", (t) => {
  const { board, create, call } = setup(t, 1);
  fails(() => create({ ready: "false" }), "invalid_argument");
  const challenge = create(); board.execute("platform", "dispatch");
  fails(() => fact(call, "worker-1", challenge.id, { evidence: [null] }), "invalid_evidence");
  fails(() => board.execute("platform", "register_artifact", { challenge_id: challenge.id, worker_id: "worker-1" }), "stale_context");
  fails(() => board.execute("worker-1", "post_fact", null), "invalid_argument");
});

test("resume waits for environment confirmation even when the old environment stayed open", (t) => {
  const { board, create, lead } = setup(t, 1);
  const challenge = create(); board.execute("platform", "dispatch");
  lead("defer", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision, reason: "Temporary pause" });
  board.execute("platform", "release_finished");
  assert.equal(board.challenge(challenge.id).environment.status, "active");
  lead("resume", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision });
  assert.equal(board.challenge(challenge.id).ready, false);
  assert.equal(board.challenge(challenge.id).environment.status, "active");
  assert.equal(board.available().new_challenges.length, 0);
  board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "active", ready: true });
  assert.equal(board.available().new_challenges.length, 1);
  assert.equal(board.challenge(challenge.id).generation, 1);
});

test("assignment events persist the exact worker and assignment for delivery retries", (t) => {
  const { board, create, path } = setup(t, 1);
  const challenge = create();
  board.execute("platform", "dispatch");
  const context = board.context("worker-1");
  const event = board.events().find((item) => item.type === "worker.assigned");
  assert.equal(event.challenge_id, challenge.id);
  assert.equal(event.assigned_worker_id, "worker-1");
  assert.equal(event.assignment_id, context.assignment_id);
  assert.ok(event.worker_ids.includes("worker-1"));
  const reader = new Board({ path, initialWorkers: 1 });
  try {
    assert.deepEqual(reader.events(event.id - 1)[0], event);
  } finally {
    reader.close();
  }
});

test("new environments preserve prior directions while requiring current-generation attempts", (t) => {
  const { board, create, call, lead } = setup(t, 1);
  const challenge = create();
  board.execute("platform", "dispatch");
  const basis = fact(call, "worker-1", challenge.id);
  const previous = intent(call, "worker-1", challenge.id, basis.id, {
    parallel: true, parallel_key: "parse-input",
  });
  const dependency = intent(call, "worker-1", challenge.id, basis.id, { claim: true });
  fact(call, "worker-1", challenge.id, { resolves: dependency.id, result: "confirmed" });
  const oldCandidate = review(call, "worker-1", challenge.id, "flag{previous-environment}");
  lead("defer", {
    challenge_id: challenge.id,
    expected_revision: board.challenge(challenge.id).revision,
    reason: "Save progress before recreating the environment",
  });
  board.execute("platform", "release_finished");
  board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "closed" });
  lead("resume", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision });
  board.execute("platform", "set_environment", {
    challenge_id: challenge.id, status: "active", ready: true, new_generation: true,
  });
  call("worker-1", "claim", { challenge_id: challenge.id });
  const current = board.challenge(challenge.id);
  assert.equal(current.assistance_target, 0);
  assert.ok(current.facts.some((item) => item.id === basis.id));
  fails(() => call("worker-1", "claim_intent", {
    challenge_id: challenge.id, intent_id: previous.id, expected_version: previous.version,
  }), "stale_intent");
  const replacement = intent(call, "worker-1", challenge.id, basis.id, {
    parallel: true, parallel_key: "parse-input", retry_of: previous.id, claim: true,
  });
  assert.equal(replacement.environment_generation, current.generation);
  assert.equal(replacement.retry_of, previous.id);
  const dependent = intent(call, "worker-1", challenge.id, basis.id, { depends_on: [dependency.id] });
  fails(() => call("worker-1", "claim_intent", {
    challenge_id: challenge.id, intent_id: dependent.id, expected_version: dependent.version,
  }), "intent_unavailable");
  const newCandidate = review(call, "worker-1", challenge.id, "flag{current-environment}");
  fails(() => lead("begin_submission", {
    challenge_id: challenge.id,
    candidate_id: oldCandidate.candidate.id,
    review_revision: newCandidate.challenge.review_revision,
    expected_revision: board.challenge(challenge.id).revision,
  }), "stale_candidate");
  assert.equal(board.challenge(challenge.id).candidates.length, 2);
});

test("resuming the same environment allows a released direction to continue", (t) => {
  const { board, create, call, lead } = setup(t, 1);
  const challenge = create();
  board.execute("platform", "dispatch");
  const basis = fact(call, "worker-1", challenge.id);
  const direction = intent(call, "worker-1", challenge.id, basis.id, { claim: true });
  lead("defer", {
    challenge_id: challenge.id,
    expected_revision: board.challenge(challenge.id).revision,
    reason: "Resume after checking environment availability",
  });
  board.execute("platform", "release_finished");
  lead("resume", { challenge_id: challenge.id, expected_revision: board.challenge(challenge.id).revision });
  board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "active", ready: true });
  call("worker-1", "claim", { challenge_id: challenge.id });
  const saved = board.challenge(challenge.id).intents.find((item) => item.id === direction.id);
  const result = call("worker-1", "claim_intent", {
    challenge_id: challenge.id, intent_id: direction.id, expected_version: saved.version,
  });
  assert.equal(result.intent.environment_generation, direction.environment_generation);
  assert.equal(result.intent.id, direction.id);
});
