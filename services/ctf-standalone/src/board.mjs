import { DatabaseSync } from "node:sqlite";
import { EventEmitter } from "node:events";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

export const MAX_CHALLENGE_WORKERS = 10;
export const MAX_WORKER_INTENTS = 3;
export const ASSISTANCE_REMINDER_MS = 30 * 60 * 1000;

export class BoardError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.code = code;
    this.details = details;
  }
}

function requireValue(condition, code, message, details) {
  if (!condition) throw new BoardError(code, message, details);
}
function text(value, name) {
  requireValue(typeof value === "string" && value.trim().length > 0,
    "invalid_argument", `${name} must be nonblank`);
  return value;
}
function integer(value, minimum, maximum, name) {
  requireValue(Number.isSafeInteger(value) && value >= minimum && value <= maximum,
    "invalid_argument", `${name} must be an integer between ${minimum} and ${maximum}`);
  return value;
}
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  }
  return value;
}
function digest(value) {
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}

/** A single local writer owns business state and its durable change stream. */
export class Board extends EventEmitter {
  constructor({ path = ":memory:", initialWorkers = 20, clock = Date.now } = {}) {
    super();
    this.clock = clock;
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS records (
        kind TEXT NOT NULL, id TEXT NOT NULL, challenge_id TEXT,
        data TEXT NOT NULL, PRIMARY KEY(kind,id));
      CREATE INDEX IF NOT EXISTS records_scope ON records(kind,challenge_id);
      CREATE TABLE IF NOT EXISTS requests (
        actor TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL,
        PRIMARY KEY(actor,id));
      CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);`);
    if (!this.getMeta("initialized")) {
      integer(initialWorkers, 0, Number.MAX_SAFE_INTEGER, "initialWorkers");
      this.db.exec("BEGIN IMMEDIATE");
      try {
        for (let index = 0; index < initialWorkers; index++) this._newWorker();
        this.setMeta("initialized", true);
        this.db.exec("COMMIT");
      } catch (error) { this.db.exec("ROLLBACK"); throw error; }
    }
  }
  close() { this.db.close(); }
  getMeta(key, fallback = null) {
    const row = this.db.prepare("SELECT value FROM meta WHERE key=?").get(key);
    return row ? JSON.parse(row.value) : fallback;
  }
  setMeta(key, value) {
    this.db.prepare("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
      .run(key, JSON.stringify(value));
    return value;
  }
  _get(kind, id) {
    if (typeof id !== "string" || !id) return null;
    const row = this.db.prepare("SELECT data FROM records WHERE kind=? AND id=?").get(kind, id);
    return row ? JSON.parse(row.data) : null;
  }
  _all(kind, challengeId) {
    const rows = challengeId === undefined
      ? this.db.prepare("SELECT data FROM records WHERE kind=? ORDER BY rowid").all(kind)
      : this.db.prepare("SELECT data FROM records WHERE kind=? AND challenge_id=? ORDER BY rowid").all(kind, challengeId);
    return rows.map((row) => JSON.parse(row.data));
  }
  _save(kind, value) {
    this.db.prepare(`INSERT INTO records(kind,id,challenge_id,data) VALUES(?,?,?,?)
      ON CONFLICT(kind,id) DO UPDATE SET challenge_id=excluded.challenge_id,data=excluded.data`)
      .run(kind, value.id, value.challenge_id ?? null, JSON.stringify(value));
    return value;
  }
  _newWorker() {
    const sequence = this.getMeta("worker_sequence", 0) + 1;
    this.setMeta("worker_sequence", sequence);
    return this._save("worker", { id: `worker-${sequence}`, status: "starting", has_worked: false,
      generation: 0, idle_generation: 0, assignment_id: null, active: false,
      execution_context: null, error: null });
  }
  workers() { return this._all("worker"); }
  _worker(id) {
    const worker = this._get("worker", id);
    requireValue(worker, "worker_not_found", "Worker does not exist");
    return worker;
  }
  context(id) {
    const worker = this._worker(id);
    const assignment = worker.assignment_id ? this._get("assignment", worker.assignment_id) : null;
    return { worker_id: id, assignment_id: worker.assignment_id,
      challenge_id: assignment?.challenge_id ?? null, generation: worker.generation,
      environment_generation: assignment?.environment_generation ?? null };
  }
  _context(actor, context, requireAssignment = true) {
    const actual = this.context(actor);
    requireValue(context && Object.keys(actual).every((key) => context[key] === actual[key]),
      "stale_context", "The model response belongs to an older assignment", { context: actual });
    if (requireAssignment) requireValue(actual.assignment_id, "assignment_required", "Claim work first");
    return actual;
  }
  _challenge(id) {
    const challenge = this._get("challenge", id);
    requireValue(challenge, "challenge_not_found", "Challenge does not exist");
    return challenge;
  }
  _members(id) {
    return this._all("assignment", id).filter((item) => item.released_at === null)
      .map((item) => ({ ...item, worker_id: item.worker_id, assignment_id: item.id,
        active: this._worker(item.worker_id).active }));
  }
  challenge(id) {
    const challenge = this._challenge(id);
    return { ...challenge, members: this._members(id), facts: this._all("fact", id),
      intents: this._all("intent", id), candidates: this._all("candidate", id),
      submissions: this._all("submission", id), artifacts: this._all("artifact", id),
      needs_assistance: this._slots(challenge) > 0 };
  }
  _slots(challenge) {
    if (challenge.phase !== "solving" || !challenge.owner_id || !challenge.ready) return 0;
    const members = this._members(challenge.id);
    const assistants = members.filter((item) => item.role === "assistant").length;
    return Math.max(0, Math.min(MAX_CHALLENGE_WORKERS - members.length,
      challenge.assistance_target - assistants));
  }
  available() {
    const fresh = [], assistance = [];
    for (const challenge of this._all("challenge")) {
      const item = { challenge_id: challenge.id, id: challenge.id, revision: challenge.revision,
        title: challenge.title, description: challenge.description, difficulty: challenge.difficulty,
        priority: challenge.priority, created_at: challenge.created_at };
      if (challenge.phase === "pending" && challenge.ready && !challenge.owner_id && this._members(challenge.id).length === 0) {
        fresh.push(item);
      } else if (this._slots(challenge)) assistance.push({ ...item, open_slots: this._slots(challenge) });
    }
    const order = (a, b) => a.priority - b.priority || a.created_at - b.created_at;
    return { new_challenges: fresh.sort(order), assistance: assistance.sort(order) };
  }
  _readable(actor, challenge) {
    if (["lead", "platform"].includes(actor)) return true;
    this._worker(actor);
    return this._members(challenge.id).some((member) => member.worker_id === actor)
      || challenge.phase === "pending" || this._slots(challenge) > 0;
  }
  snapshot(actor, { challengeId, sinceRevision } = {}) {
    if (!["lead", "platform"].includes(actor)) this._worker(actor);
    if (challengeId) {
      const challenge = this._challenge(challengeId);
      requireValue(this._readable(actor, challenge), "forbidden", "Challenge is not visible to this Worker");
      if (sinceRevision === undefined) return this.challenge(challengeId);
    }
    if (sinceRevision !== undefined) {
      integer(sinceRevision, 0, Number.MAX_SAFE_INTEGER, "sinceRevision");
      return { event_cursor: this.getMeta("event_cursor", 0),
        context: ["lead", "platform"].includes(actor) ? null : this.context(actor),
        challenge: challengeId ? this._challenge(challengeId) : null,
        events: this.events(sinceRevision).filter((event) => (!challengeId || event.challenge_id === challengeId)
          && (["lead", "platform"].includes(actor) || event.worker_ids.includes(actor))) };
    }
    const challenges = this._all("challenge").filter((item) => this._readable(actor, item));
    return { challenges: challenges.map((item) => this.challenge(item.id)), available: this.available(),
      workers: ["lead", "platform"].includes(actor) ? this.workers() : [this._worker(actor)],
      context: ["lead", "platform"].includes(actor) ? null : this.context(actor),
      event_cursor: this.getMeta("event_cursor", 0) };
  }
  events(after = 0) {
    return this.db.prepare("SELECT id,data FROM events WHERE id>? ORDER BY id").all(after)
      .map((row) => ({ id: row.id, ...JSON.parse(row.data) }));
  }
  _event(type, challenge = null, workerIds = [], details = {}) {
    const recipients = new Set([...workerIds, ...(challenge ? this._members(challenge.id).map((member) => member.worker_id) : [])]);
    const event = { type, actor_id: this.currentActor, challenge_id: challenge?.id ?? null, revision: challenge?.revision ?? null,
      worker_ids: [...recipients], at: this.clock(), ...details };
    const row = this.db.prepare("INSERT INTO events(data) VALUES(?)").run(JSON.stringify(event));
    event.id = Number(row.lastInsertRowid);
    this.setMeta("event_cursor", event.id);
    this.pendingEvents.push(event);
  }
  _touch(challenge, type, workerIds = [], details = {}) {
    challenge.revision++;
    this._save("challenge", challenge);
    this._event(type, challenge, workerIds, details);
    return challenge;
  }
  _revision(challenge, args, allowEarlier = false) {
    requireValue(Number.isSafeInteger(args.expected_revision)
      && (allowEarlier ? args.expected_revision <= challenge.revision : args.expected_revision === challenge.revision)
      && args.expected_revision >= 1, "revision_conflict", "Refresh the challenge before retrying",
    { revision: challenge.revision });
  }
  _authority(actor, args, context, { phases = ["solving", "reviewing"], revision = true } = {}) {
    const challenge = this._challenge(text(args.challenge_id, "challenge_id"));
    if (!["lead", "platform"].includes(actor)) {
      const captured = this._context(actor, context);
      requireValue(captured.challenge_id === challenge.id && captured.environment_generation === challenge.generation,
        "stale_context", "Work context does not match the current challenge environment");
    }
    requireValue(phases.includes(challenge.phase), "invalid_phase", `Action unavailable while ${challenge.phase}`);
    if (revision && actor !== "platform") this._revision(challenge, args);
    return challenge;
  }
  _lead(actor) { requireValue(actor === "lead", "forbidden", "This action belongs to Lead"); }
  _platform(actor) { requireValue(actor === "platform", "forbidden", "This action belongs to the coordinator"); }

  execute(actor, action, args = {}, context = null) {
    requireValue(args && typeof args === "object" && !Array.isArray(args), "invalid_argument", "Action arguments must be an object");
    requireValue(actor === "lead" || actor === "platform" || this._get("worker", actor), "forbidden", "Unknown actor");
    const requestId = args.request_id;
    if (actor !== "platform") text(requestId, "request_id");
    const hash = digest({ action, args, context });
    this.pendingEvents = [];
    this.currentActor = actor;
    this.db.exec("BEGIN IMMEDIATE");
    let result;
    try {
      const old = requestId && this.db.prepare("SELECT digest,result FROM requests WHERE actor=? AND id=?").get(actor, requestId);
      if (old) {
        requireValue(old.digest === hash, "request_conflict", "request_id has different parameters");
        result = JSON.parse(old.result);
        if (action === "grow_workers") result = this._growthReceipt(result);
        if (action === "begin_submission") result = { ...result, should_submit: false };
      } else {
        result = this._execute(actor, action, args, context);
        if (requestId) this.db.prepare("INSERT INTO requests(actor,id,digest,result) VALUES(?,?,?,?)")
          .run(actor, requestId, hash, JSON.stringify(result));
      }
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      this.pendingEvents = [];
      throw error;
    }
    const events = this.pendingEvents;
    this.pendingEvents = [];
    for (const event of events) this.emit("change", event);
    return result;
  }

  _execute(actor, action, args, context) {
    switch (action) {
      case "grow_workers": {
        this._lead(actor);
        integer(args.target_total, 0, Number.MAX_SAFE_INTEGER, "target_total");
        const created = [];
        const active = this.workers().filter((worker) => worker.status !== "failed").length;
        for (let index = active; index < args.target_total; index++) created.push(this._newWorker().id);
        if (created.length) this._event("workers.created", null, ["lead"], { created });
        return this._growthReceipt({ request_id: args.request_id, target_total: args.target_total, created });
      }
      case "worker_ready": case "worker_failed": {
        this._platform(actor);
        const worker = this._worker(args.worker_id);
        requireValue(worker.status === "starting" || action === "worker_ready" && worker.status === "idle",
          "invalid_worker_state", "Only a starting Worker can report preparation");
        worker.status = action === "worker_ready" ? "idle" : "failed";
        worker.error = action === "worker_failed" ? text(args.error, "error") : null;
        this._save("worker", worker);
        this._event(action === "worker_ready" ? "worker.ready" : "worker.failed", null, [worker.id, "lead"]);
        return worker;
      }
      case "create_challenge": return this._createChallenge(actor, args);
      case "update_challenge": return this._updateChallenge(actor, args);
      case "set_environment": return this._environment(actor, args);
      case "dispatch": return this._dispatch(actor);
      case "claim": return this._claimAction(actor, args, context);
      case "activity": return this._activity(actor, args);
      case "release_finished": return this._releaseFinished(actor, args);
      case "post_fact": return this._postFactAction(actor, args, context);
      case "post_intent": return this._postIntent(actor, args, context);
      case "claim_intent": return this._claimIntentAction(actor, args, context);
      case "release_intent": return this._releaseIntent(actor, args, context);
      case "register_artifact": return this._registerArtifact(actor, args);
      case "request_assistance": return this._requestAssistance(actor, args, context);
      case "submit_review": return this._submitReview(actor, args, context);
      case "begin_submission": return this._beginSubmission(actor, args);
      case "submission_result": return this._submissionResult(actor, args);
      case "reject_review": return this._rejectReview(actor, args);
      case "defer": return this._defer(actor, args);
      case "resume": return this._resume(actor, args);
      case "tick": return this._tick(actor);
      default: throw new BoardError("unknown_action", `Unknown board action ${action}`);
    }
  }
  _growthReceipt(receipt) {
    const members = receipt.created.map((id) => this._worker(id));
    return { ...receipt, succeeded: members.filter((item) => !["starting", "failed"].includes(item.status)).map((item) => item.id),
      starting: members.filter((item) => item.status === "starting").map((item) => item.id),
      failed: members.filter((item) => item.status === "failed").map((item) => ({ worker_id: item.id, error: item.error })),
      total: this.workers().filter((item) => item.status !== "failed").length };
  }
  _createChallenge(actor, args) {
    this._lead(actor);
    const uniqueCode = text(args.unique_code, "unique_code");
    requireValue(args.description === undefined || typeof args.description === "string", "invalid_argument", "description must be a string");
    requireValue(args.multi_stage === undefined || typeof args.multi_stage === "boolean", "invalid_argument", "multi_stage must be boolean");
    requireValue(args.ready === undefined || typeof args.ready === "boolean", "invalid_argument", "ready must be boolean");
    requireValue(!this._all("challenge").some((item) => item.unique_code === uniqueCode),
      "duplicate_challenge", "External challenge already exists");
    const difficulty = args.difficulty ?? (args.multi_stage ? "hard" : "medium");
    requireValue(["easy", "medium", "hard"].includes(difficulty), "invalid_argument", "Invalid difficulty");
    const challenge = { id: randomUUID(), unique_code: uniqueCode, title: text(args.title, "title"),
      description: args.description ?? "", difficulty, multi_stage: args.multi_stage ?? false,
      priority: integer(args.priority ?? 1, 0, 3, "priority"), phase: "pending", revision: 1,
      generation: 1, ready: args.ready ?? true, owner_id: null,
      assistance_target: difficulty === "hard" ? 1 : 0,
      assistance_requested: difficulty === "hard" ? 1 : 0, assistance_reminded: false,
      solve_elapsed_ms: 0, solve_started_at: null, review_revision: 0,
      environment: { status: args.ready === false ? "pending" : "active", online: false, connection: null },
      created_at: this.clock(), review_summary: null };
    this._save("challenge", challenge);
    this._event("challenge.created", challenge, ["lead"]);
    return this.challenge(challenge.id);
  }
  _updateChallenge(actor, args) {
    this._lead(actor);
    const challenge = this._authority(actor, args, null, { phases: ["pending", "solving", "reviewing", "deferred"] });
    requireValue(args.description === undefined || typeof args.description === "string", "invalid_argument", "description must be a string");
    requireValue(args.multi_stage === undefined || typeof args.multi_stage === "boolean", "invalid_argument", "multi_stage must be boolean");
    for (const name of ["title", "description", "multi_stage"]) if (args[name] !== undefined) challenge[name] = args[name];
    text(challenge.title, "title");
    if (args.difficulty !== undefined) {
      requireValue(["easy", "medium", "hard"].includes(args.difficulty), "invalid_argument", "Invalid difficulty");
      challenge.difficulty = args.difficulty;
    } else if (args.multi_stage) challenge.difficulty = "hard";
    if (args.priority !== undefined) challenge.priority = integer(args.priority, 0, 3, "priority");
    if (challenge.difficulty === "hard") {
      challenge.assistance_requested = Math.max(1, challenge.assistance_requested);
      challenge.assistance_target = Math.max(1, challenge.assistance_target);
    }
    this._touch(challenge, "challenge.updated");
    return this.challenge(challenge.id);
  }
  _environment(actor, args) {
    this._platform(actor);
    const challenge = this._challenge(args.challenge_id);
    requireValue(["pending", "active", "closing", "unknown", "closed"].includes(args.status), "invalid_argument", "Invalid environment status");
    if (args.new_generation) {
      requireValue(this._members(challenge.id).length === 0, "team_not_released", "Release prior work before reopening the environment");
      challenge.generation++;
      challenge.assistance_target = challenge.assistance_requested ?? challenge.assistance_target;
    }
    challenge.environment = { ...challenge.environment, status: args.status,
      ...(args.connection !== undefined ? { connection: args.connection } : {}),
      ...(args.online !== undefined ? { online: args.online } : {}) };
    if (args.ready !== undefined) challenge.ready = Boolean(args.ready);
    if (args.status === "closed") challenge.ready = false;
    this._touch(challenge, "environment.updated", ["lead"]);
    return this.challenge(challenge.id);
  }
  _claim(worker, challenge) {
    const choices = this.available();
    const preferred = choices.new_challenges.length ? choices.new_challenges : choices.assistance;
    requireValue(worker.status === "idle" && !worker.assignment_id,
      "worker_not_idle", "Worker is not available for another assignment");
    requireValue(preferred.some((item) => item.id === challenge.id), "work_changed", "Select available work, with new challenges first", choices);
    requireValue(this._members(challenge.id).length < MAX_CHALLENGE_WORKERS, "team_full", "Challenge team is full");
    const role = challenge.owner_id ? "assistant" : "primary";
    worker.generation++;
    const assignment = { id: randomUUID(), worker_id: worker.id, challenge_id: challenge.id, role,
      generation: worker.generation, environment_generation: challenge.generation,
      created_at: this.clock(), released_at: null };
    worker.assignment_id = assignment.id;
    worker.status = "running";
    worker.has_worked = true;
    this._save("assignment", assignment);
    this._save("worker", worker);
    if (role === "primary") {
      challenge.owner_id = worker.id;
      challenge.phase = "solving";
      challenge.solve_started_at = this.clock();
    }
    this._touch(challenge, "worker.assigned", [worker.id], {
      assigned_worker_id: worker.id,
      assignment_id: assignment.id,
    });
    return { worker_id: worker.id, context: this.context(worker.id), challenge: this.challenge(challenge.id) };
  }
  _claimAction(actor, args, context) {
    requireValue(!["lead", "platform"].includes(actor), "forbidden", "Only Workers claim work");
    this._context(actor, context, false);
    const worker = this._worker(actor);
    requireValue(worker.has_worked, "first_assignment_required", "Coordinator assigns a Worker's first task");
    const challenge = this._challenge(args.challenge_id);
    this._revision(challenge, args);
    return this._claim(worker, challenge);
  }
  _dispatch(actor) {
    this._platform(actor);
    const assignments = [];
    for (const worker of this.workers().filter((item) => item.status === "idle" && !item.has_worked && !item.active)) {
      const available = this.available();
      const item = available.new_challenges[0] ?? available.assistance[0];
      if (!item) break;
      assignments.push(this._claim(worker, this._challenge(item.id)));
    }
    const available = this.available();
    const notifications = available.new_challenges.length || available.assistance.length
      ? this.workers().filter((item) => item.status === "idle" && item.has_worked && !item.active)
        .map((item) => ({ worker_id: item.id, idle_generation: item.idle_generation, available, context: this.context(item.id) })) : [];
    return { assignments, notifications, available };
  }
  _activity(actor, args) {
    this._platform(actor);
    const worker = this._worker(args.worker_id);
    const captured = args.context ?? { worker_id: worker.id, assignment_id: args.assignment_id ?? null,
      challenge_id: args.assignment_id ? this._get("assignment", args.assignment_id)?.challenge_id : null,
      generation: args.generation, environment_generation: args.assignment_id ? this._get("assignment", args.assignment_id)?.environment_generation : null };
    if (args.running) {
      this._context(worker.id, captured, false);
      if (worker.active && digest(worker.execution_context) === digest(captured)) return worker;
      requireValue(!worker.active, "already_running", "Worker already has an active execution");
      worker.active = true;
      worker.execution_context = captured;
    } else {
      requireValue(!worker.active || digest(worker.execution_context) === digest(captured), "stale_execution", "Execution completion does not match the active run");
      worker.active = false;
      worker.execution_context = null;
    }
    this._save("worker", worker);
    this._event(args.running ? "worker.execution_started" : "worker.execution_ended", null, [worker.id], { context: captured });
    return worker;
  }
  _releaseFinished(actor, args) {
    this._platform(actor);
    const released = [];
    for (const worker of this.workers()) {
      if (args.worker_id && worker.id !== args.worker_id || !worker.assignment_id || worker.active) continue;
      if (args.assignment_id && args.assignment_id !== worker.assignment_id) continue;
      const assignment = this._get("assignment", worker.assignment_id);
      const challenge = this._challenge(assignment.challenge_id);
      if (!["completed", "deferred"].includes(challenge.phase)) continue;
      assignment.released_at = this.clock();
      this._save("assignment", assignment);
      for (const intent of this._all("intent", challenge.id)) {
        if (intent.status === "claimed" && intent.holder === worker.id) {
          intent.status = "open"; intent.holder = null; intent.claim_assignment_ref = null;
          intent.attempts++; intent.version++;
          intent.notes.push({ by: "platform", at: this.clock(), text: `Assignment released after ${challenge.phase}` });
          this._save("intent", intent);
        }
      }
      worker.assignment_id = null; worker.status = "idle"; worker.idle_generation++;
      this._save("worker", worker);
      if (challenge.owner_id === worker.id) challenge.owner_id = null;
      this._touch(challenge, "worker.released", [worker.id]);
      released.push(worker.id);
    }
    return { released };
  }
  _references(kind, ids, challengeId, name) {
    requireValue(Array.isArray(ids), "invalid_argument", `${name} must be an array`);
    for (const id of ids) requireValue(this._get(kind, id)?.challenge_id === challengeId,
      "invalid_reference", `${name} contains a reference outside this challenge`);
    return [...new Set(ids)];
  }
  _evidence(values, challenge, actor) {
    requireValue(Array.isArray(values) && values.length > 0, "evidence_required", "Fact needs evidence");
    return values.map((value) => {
      requireValue(value && typeof value === "object" && !Array.isArray(value), "invalid_evidence", "Evidence must be an object");
      const summary = text(value.summary, "evidence.summary");
      if (value.artifact_id) {
        const artifact = this._get("artifact", value.artifact_id);
        requireValue(artifact && artifact.challenge_id === challenge.id, "invalid_evidence", "Unknown artifact for this challenge");
        return { ...artifact, artifact_id: artifact.id, summary };
      }
      requireValue(["text", "http", "code_ref"].includes(value.type), "invalid_evidence", "File and command evidence must reference a registered artifact");
      requireValue(!value.uri && !value.sha256 && !value.call_id, "invalid_evidence", "Evidence identity comes from registered artifacts");
      return { type: value.type, summary, ...(value.path ? { path: text(value.path, "evidence.path") } : {}), author: actor };
    });
  }
  _fact(actor, challenge, args, context = null) {
    requireValue(["observation", "inference", "structure"].includes(args.kind ?? "observation"), "invalid_argument", "Invalid fact kind");
    const derived = this._references("fact", args.derived_from ?? [], challenge.id, "derived_from");
    const disputes = this._references("fact", args.disputes ?? [], challenge.id, "disputes");
    requireValue(args.kind !== "inference" || derived.length > 0, "invalid_fact", "Inference requires existing facts");
    requireValue(Boolean(args.resolves) === Boolean(args.result), "invalid_fact", "resolves and result must be supplied together");
    const evidence = this._evidence(args.evidence, challenge, actor);
    const fact = { id: randomUUID(), challenge_id: challenge.id, kind: args.kind ?? "observation",
      statement: text(args.statement, "statement"), evidence, derived_from: derived, disputes,
      resolves: args.resolves ?? null, result: args.result ?? null, author: actor,
      assignment_ref: context, environment_generation: challenge.generation, status: "proposed",
      provenance: evidence.some((item) => item.call_id) ? "tool_backed" : "self_reported", version: 1, created_at: this.clock() };
    if (args.resolves) {
      const intent = this._get("intent", args.resolves);
      requireValue(intent?.challenge_id === challenge.id && intent.status === "claimed" && intent.holder === actor,
        "intent_not_held", "Only its current holder can close an Intent");
      requireValue(["confirmed", "rejected", "inconclusive"].includes(args.result), "invalid_argument", "Invalid Intent result");
      intent.status = "closed"; intent.result = args.result; intent.closed_by = actor;
      intent.result_facts.push(fact.id); intent.version++;
      this._save("intent", intent);
    }
    this._save("fact", fact);
    for (const id of disputes) { const old = this._get("fact", id); old.status = "disputed"; this._save("fact", old); }
    return fact;
  }
  _postFactAction(actor, args, context) {
    const challenge = this._authority(actor, args, context,
      { phases: actor === "lead" ? ["pending", "solving", "reviewing", "deferred"] : ["solving", "reviewing"] });
    const fact = this._fact(actor, challenge, args, context);
    this._autoAssistance(challenge);
    this._touch(challenge, "fact.posted", [], { fact_id: fact.id });
    return { fact, revision: challenge.revision };
  }
  _postIntent(actor, args, context) {
    const challenge = this._authority(actor, args, context, { phases: ["solving"] });
    const basedOn = this._references("fact", args.based_on, challenge.id, "based_on");
    requireValue(basedOn.length > 0, "invalid_intent", "Intent requires an existing Fact");
    const dependencies = this._references("intent", args.depends_on ?? [], challenge.id, "depends_on");
    if (args.retry_of) this._references("intent", [args.retry_of], challenge.id, "retry_of");
    const parallelKey = args.parallel ? text(args.parallel_key, "parallel_key") : null;
    if (parallelKey) requireValue(!this._all("intent", challenge.id).some((item) => item.environment_generation === challenge.generation && item.parallel_key === parallelKey && item.status !== "closed"),
      "duplicate_direction", "An open or held Intent already represents this parallel direction");
    const intent = { id: randomUUID(), challenge_id: challenge.id, statement: text(args.statement, "statement"),
      based_on: basedOn, expected: text(args.expected, "expected"), method: text(args.method, "method"),
      relates_to: args.relates_to ?? [], retry_of: args.retry_of ?? null, depends_on: dependencies,
      parallel: Boolean(args.parallel), parallel_key: parallelKey, status: "open", holder: null,
      claimed_at: null, claim_assignment_ref: null, result: null, closed_by: null, result_facts: [],
      notes: [], attempts: 0, author: actor, assignment_ref: context, environment_generation: challenge.generation,
      version: 1, created_at: this.clock() };
    this._save("intent", intent);
    if (args.claim) this._claimIntent(actor, challenge, intent, context);
    this._autoAssistance(challenge);
    this._touch(challenge, "intent.posted", [], { intent_id: intent.id });
    return { intent: this._get("intent", intent.id), revision: challenge.revision };
  }
  _readyIntent(intent, generation = intent.environment_generation) {
    return intent.environment_generation === generation && intent.depends_on.every((id) => {
      const dependency = this._get("intent", id);
      return dependency?.environment_generation === generation && dependency.status === "closed" && dependency.result === "confirmed";
    });
  }
  _claimIntent(actor, challenge, intent, context) {
    requireValue(actor !== "lead" && actor !== "platform", "forbidden", "Workers hold Intent work");
    requireValue(intent.environment_generation === challenge.generation, "stale_intent", "Post a current-environment Intent with retry_of before resuming this direction");
    requireValue(intent.status === "open" && this._readyIntent(intent), "intent_unavailable", "Intent is held or dependencies are not confirmed");
    requireValue(this._all("intent", challenge.id).filter((item) => item.status === "claimed" && item.holder === actor).length < MAX_WORKER_INTENTS,
      "intent_limit", "Worker already holds three Intents");
    intent.status = "claimed"; intent.holder = actor; intent.claimed_at = this.clock();
    intent.claim_assignment_ref = context; intent.version++;
    this._save("intent", intent);
  }
  _claimIntentAction(actor, args, context) {
    const challenge = this._authority(actor, args, context, { phases: ["solving"] });
    const intent = this._get("intent", args.intent_id);
    requireValue(intent?.challenge_id === challenge.id, "invalid_reference", "Intent belongs to another challenge");
    requireValue(intent.version === args.expected_version, "intent_version_conflict", "Intent version changed");
    this._claimIntent(actor, challenge, intent, context);
    this._autoAssistance(challenge);
    this._touch(challenge, "intent.claimed", [], { intent_id: intent.id });
    return { intent, revision: challenge.revision };
  }
  _releaseIntent(actor, args, context) {
    const challenge = this._authority(actor, args, context, { phases: ["solving"] });
    const intent = this._get("intent", args.intent_id);
    requireValue(intent?.challenge_id === challenge.id && intent.status === "claimed" && intent.holder === actor,
      "intent_not_held", "Only its holder can release an Intent");
    intent.notes.push({ by: actor, at: this.clock(), text: text(args.note, "note") });
    intent.status = "open"; intent.holder = null; intent.claim_assignment_ref = null; intent.attempts++; intent.version++;
    this._save("intent", intent);
    this._autoAssistance(challenge);
    this._touch(challenge, "intent.released", [], { intent_id: intent.id });
    return { intent, revision: challenge.revision };
  }
  _autoAssistance(challenge) {
    if (challenge.phase !== "solving") return;
    const intents = this._all("intent", challenge.id);
    const ready = intents.filter((item) => item.status === "open" && item.parallel && this._readyIntent(item, challenge.generation));
    const members = this._members(challenge.id);
    const unoccupied = members.filter((member) => !member.active && !intents.some((item) => item.status === "claimed" && item.holder === member.worker_id)).length;
    const needed = Math.max(0, ready.length - unoccupied);
    challenge.assistance_target = Math.min(MAX_CHALLENGE_WORKERS - 1,
      Math.max(challenge.assistance_requested, members.filter((item) => item.role === "assistant").length + needed));
  }
  _registerArtifact(actor, args) {
    this._platform(actor);
    const challenge = this._challenge(args.challenge_id);
    if (args.worker_id !== "lead") {
      const assignment = this._get("assignment", args.context?.assignment_id);
      requireValue(assignment && assignment.challenge_id === challenge.id && assignment.worker_id === args.worker_id
        && assignment.generation === args.context.generation
        && assignment.environment_generation === args.context.environment_generation, "stale_context", "Artifact has no valid execution origin");
    }
    requireValue(/^[0-9a-f]{64}$/.test(args.sha256), "invalid_artifact", "Artifact needs its content SHA-256");
    requireValue(!this._all("artifact").some((item) => item.uri === args.uri && item.sha256 !== args.sha256),
      "invalid_artifact", "An immutable evidence URI cannot change content");
    const artifact = { id: randomUUID(), challenge_id: challenge.id, worker_id: args.worker_id,
      assignment_ref: args.context ?? null, environment_generation: args.context?.environment_generation ?? challenge.generation,
      type: args.type ?? "file", path: text(args.path, "path"), uri: text(args.uri, "uri"),
      sha256: args.sha256, size: integer(args.size, 0, Number.MAX_SAFE_INTEGER, "size"),
      call_id: args.call_id ?? null, captured_at: this.clock(), capture: args.capture ?? "snapshot" };
    this._save("artifact", artifact);
    this._event("artifact.registered", challenge, [], { artifact_id: artifact.id });
    return artifact;
  }
  _requestAssistance(actor, args, context) {
    const challenge = this._authority(actor, args, context, { phases: ["solving"] });
    const additional = integer(args.additional_workers ?? 1, 1, MAX_CHALLENGE_WORKERS - 1, "additional_workers");
    const reason = text(args.reason, "reason");
    challenge.assistance_target = Math.min(MAX_CHALLENGE_WORKERS - 1, challenge.assistance_target + additional);
    challenge.assistance_requested = challenge.assistance_target;
    this._touch(challenge, "assistance.requested", [], { reason });
    return this.challenge(challenge.id);
  }
  _clock(challenge, running) {
    const now = this.clock();
    if (challenge.solve_started_at !== null) challenge.solve_elapsed_ms += Math.max(0, now - challenge.solve_started_at);
    challenge.solve_started_at = running ? now : null;
  }
  _submitReview(actor, args, context) {
    const challenge = this._authority(actor, args, context, { revision: false });
    this._revision(challenge, args, challenge.phase === "reviewing");
    const factRefs = this._references("fact", args.fact_refs ?? [], challenge.id, "fact_refs");
    const flag = text(args.flag, "flag");
    const summary = text(args.summary, "summary");
    if (challenge.phase === "reviewing") requireValue(args.expected_revision >= challenge.review_base_revision,
      "stale_review", "Candidate update belongs to an earlier review round");
    if (challenge.phase !== "reviewing") {
      challenge.review_base_revision = challenge.revision;
      this._clock(challenge, false); challenge.phase = "reviewing"; challenge.review_revision++;
    }
    let candidate = this._all("candidate", challenge.id).find((item) => item.flag === flag
      && item.environment_generation === challenge.generation && item.review_revision === challenge.review_revision);
    if (!candidate) {
      candidate = { id: randomUUID(), challenge_id: challenge.id, flag, summary, fact_refs: factRefs,
        author: actor, assignment_ref: context, environment_generation: challenge.generation,
        review_revision: challenge.review_revision, created_at: this.clock() };
      this._save("candidate", candidate);
    }
    challenge.review_summary = summary;
    this._touch(challenge, "review.requested", ["lead"], { candidate_id: candidate.id });
    return { candidate, challenge: this.challenge(challenge.id) };
  }
  _beginSubmission(actor, args) {
    this._lead(actor);
    const challenge = this._authority(actor, args, null, { phases: ["reviewing"] });
    requireValue(args.review_revision === challenge.review_revision, "stale_review", "Review round changed");
    const candidate = this._get("candidate", args.candidate_id);
    requireValue(candidate?.challenge_id === challenge.id && candidate.environment_generation === challenge.generation
      && candidate.review_revision === challenge.review_revision, "stale_candidate", "Candidate belongs to older work");
    requireValue(!this._all("submission", challenge.id).some((item) => ["pending", "unknown"].includes(item.status)),
      "submission_unresolved", "Reconcile the existing submission before sending another");
    const submission = { id: randomUUID(), challenge_id: challenge.id, candidate_id: candidate.id,
      flag: candidate.flag, unique_code: challenge.unique_code, review_revision: challenge.review_revision,
      environment_generation: challenge.generation, status: "pending", receipt: null, created_at: this.clock() };
    this._save("submission", submission);
    this._touch(challenge, "submission.started", ["lead"], { submission_id: submission.id });
    return { submission, should_submit: true };
  }
  _submissionResult(actor, args) {
    this._platform(actor);
    const submission = this._get("submission", args.submission_id);
    requireValue(submission, "submission_not_found", "Unknown submission");
    requireValue(["accepted", "rejected", "unknown", "not_sent"].includes(args.status), "invalid_argument", "Invalid submission result");
    requireValue(args.challenge_completed === undefined || typeof args.challenge_completed === "boolean",
      "invalid_argument", "challenge_completed must be boolean");
    const completing = submission.status === "accepted" && args.challenge_completed === true
      && submission.challenge_completed !== true;
    if (["accepted", "rejected", "not_sent"].includes(submission.status)) {
      requireValue(submission.status === args.status, "receipt_conflict", "A terminal receipt cannot be overwritten");
      if (!completing) return { submission, challenge: this.challenge(submission.challenge_id) };
    }
    submission.status = args.status; submission.receipt = args.receipt ?? submission.receipt ?? {};
    if (args.status === "accepted") submission.challenge_completed = args.challenge_completed ?? true;
    submission.reason = args.reason ?? (args.status === "accepted" ? "Official flag accepted" : "Submission outcome remains unknown");
    submission.updated_at = this.clock();
    this._save("submission", submission);
    const challenge = this._challenge(submission.challenge_id);
    const sameEnvironment = submission.environment_generation === challenge.generation;
    const currentReview = sameEnvironment && submission.review_revision === challenge.review_revision && challenge.phase === "reviewing";
    if (args.status === "accepted" && (currentReview || completing && sameEnvironment)) {
      if (submission.challenge_completed) {
        this._clock(challenge, false);
        challenge.phase = "completed"; challenge.environment.status = "closed"; challenge.ready = false;
      } else {
        this._reviewFeedback(challenge, "Official flag accepted. The challenge has remaining flags; continue solving.", submission);
      }
    } else if (currentReview && args.status === "rejected") {
      this._reviewFeedback(challenge, text(args.reason, "reason"), submission);
    }
    this._touch(challenge, `submission.${args.status}`, ["lead"], { submission_id: submission.id });
    return { submission, challenge: this.challenge(challenge.id) };
  }
  _reviewFeedback(challenge, reason, submission = null) {
    const fact = this._fact("lead", challenge, { kind: "observation", statement: reason,
      evidence: [{ type: "text", summary: submission ? `Official submission ${submission.id}: ${reason}` : `Lead review: ${reason}` }] });
    challenge.feedback_fact_id = fact.id;
    challenge.phase = "solving";
    this._clock(challenge, true);
  }
  _rejectReview(actor, args) {
    this._lead(actor);
    const challenge = this._authority(actor, args, null, { phases: ["reviewing"] });
    requireValue(args.review_revision === challenge.review_revision, "stale_review", "Review round changed");
    requireValue(!this._all("submission", challenge.id).some((item) => ["pending", "unknown"].includes(item.status)),
      "submission_unresolved", "Reconcile pending official submission first");
    this._reviewFeedback(challenge, text(args.reason, "reason"));
    this._touch(challenge, "review.rejected", ["lead"]);
    return this.challenge(challenge.id);
  }
  _defer(actor, args) {
    this._lead(actor);
    const challenge = this._authority(actor, args, null, { phases: ["pending", "solving", "reviewing"] });
    requireValue(!this._all("submission", challenge.id).some((item) => ["pending", "unknown"].includes(item.status)),
      "submission_unresolved", "Reconcile pending official submission before deferral");
    const reason = text(args.reason, "reason");
    this._fact("lead", challenge, { statement: reason, evidence: [{ type: "text", summary: `Lead deferral: ${reason}` }] });
    this._clock(challenge, false); challenge.phase = "deferred"; challenge.defer_reason = reason;
    this._touch(challenge, "challenge.deferred", ["lead"]);
    return this.challenge(challenge.id);
  }
  _resume(actor, args) {
    this._lead(actor);
    const challenge = this._authority(actor, args, null, { phases: ["deferred"] });
    requireValue(this._members(challenge.id).length === 0, "team_not_released", "Finish previous execution before resuming");
    challenge.phase = "pending"; challenge.owner_id = null; challenge.ready = false;
    this._touch(challenge, "challenge.resumed", ["lead"]);
    return this.challenge(challenge.id);
  }
  nextReminderAt() {
    const deadlines = this._all("challenge").filter((item) => item.phase === "solving" && !item.assistance_reminded && item.solve_started_at !== null)
      .map((item) => item.solve_started_at + Math.max(0, ASSISTANCE_REMINDER_MS - item.solve_elapsed_ms));
    return deadlines.length ? Math.min(...deadlines) : null;
  }
  _tick(actor) {
    this._platform(actor);
    const reminded = [];
    for (const challenge of this._all("challenge")) {
      if (challenge.phase !== "solving" || challenge.assistance_reminded || challenge.solve_started_at === null) continue;
      if (challenge.solve_elapsed_ms + this.clock() - challenge.solve_started_at < ASSISTANCE_REMINDER_MS) continue;
      this._clock(challenge, true); challenge.assistance_reminded = true;
      if (!this._members(challenge.id).some((item) => item.role === "assistant")) {
        challenge.assistance_requested = Math.max(1, challenge.assistance_requested);
        challenge.assistance_target = Math.max(1, challenge.assistance_target);
      }
      this._touch(challenge, "assistance.reminder", [challenge.owner_id, "lead"]);
      reminded.push(challenge.id);
    }
    return { reminded };
  }
}
