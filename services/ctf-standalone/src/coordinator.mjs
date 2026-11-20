import { mkdir, readFile, rename, open, unlink } from "node:fs/promises";
import { createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";
import { join, basename } from "node:path";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { Type } from "typebox";
import { Board } from "./board.mjs";
import { ArenaClient, challengeCompleted } from "./arena.mjs";
import { SessionPool } from "./sessions.mjs";
import { Resources } from "./resources.mjs";
import { snapshotFile } from "./artifacts.mjs";

const text = Type.String({ minLength: 1 });
const challengeParameter = Type.Object({ challenge_id: text });
const LEAD_ACTIONS = ["create_challenge", "update_challenge", "post_fact", "request_assistance", "reject_review"];
const WORKER_ACTIONS = ["claim", "post_fact", "post_intent", "claim_intent", "release_intent", "request_assistance", "submit_review"];

function failure(error) {
  return { error: error.code ?? "operation_failed", message: error.message, outcome_unknown: error.outcomeUnknown ?? false };
}

function environmentState(receipt) {
  const state = receipt?.container_status ?? receipt?.container?.status;
  if (receipt?.closed === true || ["stopped", "closed"].includes(state)) return "closed";
  if (["running", "ready", "active"].includes(state)) return "active";
  if (state === "none" && receipt?.container === false) return "active";
  if (["pending", "starting"].includes(state)) return "pending";
  return "unknown";
}

function assignmentContext(context) {
  if (!context) return null;
  return Object.fromEntries(["worker_id", "assignment_id", "challenge_id", "generation", "environment_generation"].map(key => [key, context[key]]));
}

export class ContestRun {
  static async create(config, { arena, board, poolFactory = options => SessionPool.create(options), streamFunction } = {}) {
    await mkdir(config.workDir, { recursive: true });
    const run = new ContestRun(config);
    run.board = board ?? new Board({ path: join(config.workDir, "blackboard.sqlite"), initialWorkers: 20 });
    const priorIdentity = run.board.getMeta("attempt_identity");
    if (priorIdentity && priorIdentity !== config.arena.identity) {
      run.board.close();
      throw new Error("This work directory belongs to a different attempt; choose a fresh workDir");
    }
    run.board.setMeta("attempt_identity", config.arena.identity);
    run.arena = arena ?? new ArenaClient(config.arena);
    run.arena.cumulativeScore = run.board.getMeta("cumulative_score");
    run.logFile = await open(join(config.workDir, "events.jsonl"), "a", 0o600);
    let toolSummary;
    try { toolSummary = await readFile("/opt/ctf-tools/worker-tools.md", "utf8"); }
    catch (error) {
      if (error.code !== "ENOENT") throw error;
      toolSummary = "Development host. Discover only the tool required by the current task; the container manifest is absent.";
    }
    run.pool = await poolFactory({
      root: config.workDir,
      modelConfig: config.model,
      toolSecrets: [config.arena.token],
      getContext: id => run.context(id),
      toolsForRole: (role, id) => run.tools(role, id),
      onEvent: event => run.sessionEvent(event),
      toolSummary,
      streamFunction,
    });
    run.resources = new Resources({
      workDir: config.workDir,
      execution: () => ({ workers: run.board.workers(), sessions: run.pool.status() }),
    });
    run.board.on("change", () => run.schedule());
    return run;
  }

  constructor(config) {
    this.config = config;
    this.closed = false;
    this.started = false;
    this.pumping = false;
    this.scheduled = false;
    this.control = Promise.resolve();
    this.logWrites = Promise.resolve();
    this.preparing = new Map();
    this.notices = new Map();
    this.retries = new Map();
    this.retryTimers = new Map();
    this.settledAgents = new Set();
    this.deliveries = new Map();
    this.stopController = new AbortController();
    this.finished = new Promise(resolve => { this.resolveFinished = resolve; });
  }

  context(id) {
    if (id === "lead") return { role: "lead" };
    const context = this.board.context(id);
    const challenge = context?.challenge_id ? this.board.challenge(context.challenge_id) : null;
    return { ...context, phase: challenge?.phase ?? null };
  }

  log(event) {
    const clean = JSON.stringify({ time: new Date().toISOString(), ...event })
      .split(this.config.arena.token).join("[redacted]")
      .split(this.config.model.apiKey).join("[redacted]");
    this.logWrites = this.logWrites.then(() => this.logFile.appendFile(clean + "\n"));
    this.logWrites.catch(error => { this.logError = error; });
  }

  start() {
    this.starting ??= this.initialize();
    return this.starting;
  }

  async initialize() {
    const finishAttempt = this.board.getMeta("finish_attempt");
    if (["pending", "unknown"].includes(finishAttempt?.status)) {
      await this.reconcileFinish(finishAttempt);
      return this;
    }
    const finished = this.board.getMeta("finished");
    if (finished) { this.resolveFinished(finished); return this; }
    if (this.config.arena.expiresAt && this.config.arena.expiresAt <= Date.now()) {
      this.resolveFinished({ reason: "attempt_deadline" });
      return this;
    }
    await this.pool.ensure("lead", "lead");
    await this.prepareWorkers();
    await this.retryRead(() => this.refreshCatalog());
    await this.retryRead(() => this.reconcileEnvironments());
    await this.retryRead(() => this.reconcilePendingSubmissions());
    if (this.ending) return this;
    try { await this.flushNotifications(); }
    catch (error) { this.log({ type: "notification_recovery_deferred", ...failure(error) }); }
    this.started = true;
    this.queueNotification("lead", "比赛开始或进程恢复。先读取当前题目目录及本地黑板，安排题目、设置难度并启动环境。"
      + "初始 20 个 Worker，按实际资源决定后续增员。优先提高得分和有效解题吞吐。"
      + JSON.stringify({ mode: this.config.arena.mode, rules: this.config.arena.helpMarkdown,
        catalog: this.catalog, board: this.board.snapshot("lead"), expires_at: this.config.arena.expiresAt }),
    { eventId: `startup:${randomUUID()}` });
    for (const worker of this.board.workers()) {
      if (!worker.assignment_id) continue;
      const context = this.board.context(worker.id);
      const challenge = this.board.challenge(context.challenge_id);
      const status = this.pool.status().find(item => item.id === worker.id);
      if (worker.active && status?.idle) {
        this.board.execute("platform", "activity", { worker_id: worker.id, context: worker.execution_context, running: false });
      }
      if (challenge.phase === "solving") {
        this.queueNotification(worker.id, "进程恢复。保留原题和会话，先核实未决命令与最新黑板，再继续解题。" + JSON.stringify(this.board.snapshot(worker.id)),
          { eventId: `resume:${randomUUID()}`, scope: challenge.id, version: challenge.revision });
      }
    }
    try { await this.flushNotifications(); }
    catch (error) { this.log({ type: "startup_notification_deferred", ...failure(error) }); }
    if (this.config.arena.expiresAt) {
      const remaining = this.config.arena.expiresAt - Date.now();
      this.deadlineTimer = setTimeout(() => { void this.stop("attempt_deadline"); }, Math.max(0, remaining));
    }
    this.schedule();
    return this;
  }

  async prepareWorkers() {
    const workers = this.board.workers().filter(worker => worker.status !== "failed");
    await Promise.all(workers.map(worker => {
      if (this.preparing.has(worker.id)) return this.preparing.get(worker.id);
      const preparation = this.pool.ensure(worker.id, "worker").then(() => {
        if (worker.status === "starting") this.board.execute("platform", "worker_ready", { worker_id: worker.id });
      }).catch(error => {
        if (worker.status === "starting") this.board.execute("platform", "worker_failed", { worker_id: worker.id, error: error.message });
        this.log({ type: "worker_preparation_failed", worker_id: worker.id, ...failure(error) });
        if (worker.status !== "starting") throw error;
      }).finally(() => this.preparing.delete(worker.id));
      this.preparing.set(worker.id, preparation);
      return preparation;
    }));
  }

  async retryRead(operation) {
    for (let attempt = 0; ; attempt++) {
      this.stopController.signal.throwIfAborted();
      try { return await operation(); }
      catch (error) {
        if (!error.transient || this.closed) throw error;
        if (this.config.arena.expiresAt && Date.now() >= this.config.arena.expiresAt) throw error;
        this.log({ type: "arena_read_retry", attempt: attempt + 1, ...failure(error) });
        await delay(Math.min(30000, 1000 * 2 ** Math.min(attempt, 5)), undefined, { signal: this.stopController.signal });
      }
    }
  }

  async refreshCatalog() {
    this.catalog = await this.arena.listChallenges({ signal: this.stopController.signal });
    this.board.setMeta("catalog", this.catalog);
    return this.catalog;
  }

  async reconcilePendingSubmissions() {
    const reconciled = [];
    for (const challenge of this.challenges()) {
      for (const submission of challenge.submissions ?? []) {
        if (!["pending", "unknown"].includes(submission.status)) continue;
        const result = await this.arena.reconcileSubmission(challenge.unique_code, { signal: this.stopController.signal });
        if (result.outcome === "accepted") {
          this.applySubmission(submission.id, result);
          if (this.config.arena.mode === "connectivity_test") this.endLocal("connectivity_verdict", result, true);
        }
        reconciled.push({ submission_id: submission.id, ...result });
      }
    }
    return reconciled;
  }

  challenges() { return this.board.snapshot("lead").challenges; }

  sessionEvent(event) {
    this.log(event);
    const id = event.agentId;
    if (event.type === "idle" && !event.aborted) this.retries.delete(id);
    if (id !== "lead" && (event.type === "model_start" || event.settled)) {
      try {
        const worker = this.board.workers().find(item => item.id === id);
        const context = assignmentContext(event.context ?? this.board.context(id));
        if (worker.active && (event.settled || JSON.stringify(worker.execution_context) !== JSON.stringify(context))) {
          this.board.execute("platform", "activity", { worker_id: id, context: worker.execution_context, running: false });
        }
        if (event.type === "model_start") this.board.execute("platform", "activity", { worker_id: id, context, running: true });
      } catch (error) { this.log({ type: "activity_rejected", worker_id: id, ...failure(error) }); }
    }
    if (event.type === "error") {
      this.retrySession(id, typeof event.error === "string" ? event.error : event.error?.message ?? event.message ?? "Session failed");
    }
    if (event.type === "idle" || event.settled) {
      this.notices.delete(id);
      if (!event.aborted && event.type === "idle") this.settledAgents.add(id);
      this.schedule();
    }
  }

  retrySession(id, message) {
    if (this.closed || this.ending || this.retryTimers.has(id)) return;
    const attempt = (this.retries.get(id) ?? 0) + 1;
    this.retries.set(id, attempt);
    this.log({ type: "session_retry", agent_id: id, attempt, message });
    const timer = setTimeout(() => {
      this.retryTimers.delete(id);
      if (this.closed || this.ending) return;
      if (id !== "lead") {
        const context = this.board.context(id);
        if (!context.assignment_id || this.board.challenge(context.challenge_id).phase !== "solving") { this.schedule(); return; }
      }
      void this.notify(id, "上次模型回合失败。读取当前状态，核实已有结果并继续。", { eventId: `retry:${id}:${randomUUID()}` }).catch(() => {});
    }, Math.min(30000, 1000 * 2 ** Math.min(attempt - 1, 5)));
    this.retryTimers.set(id, timer);
  }

  queueNotification(id, message, options = {}) {
    if (this.closed || this.ending) return;
    const context = id === "lead" ? null : this.board.context(id);
    const envelope = { id, text: message, eventId: randomUUID(), version: 0, scope: "global",
      ...(context ? { assignmentId: context.assignment_id, challengeId: context.challenge_id } : {}), ...options };
    const pending = this.board.getMeta("pending_notifications", {});
    if (!pending[envelope.eventId]) {
      pending[envelope.eventId] = envelope;
      this.board.setMeta("pending_notifications", pending);
    }
    return pending[envelope.eventId];
  }

  async deliverNotification(envelope) {
    if (!envelope || this.closed || this.ending) return;
    if (this.deliveries.has(envelope.eventId)) return this.deliveries.get(envelope.eventId);
    const delivery = (async () => {
      const { id, text: message, ...options } = envelope;
      const stale = id !== "lead" && options.assignmentId !== this.board.context(id)?.assignment_id;
      const result = stale ? { delivered: false, stale: true } : await this.pool.notify(id, message, options);
      const pending = this.board.getMeta("pending_notifications", {});
      delete pending[envelope.eventId];
      this.board.setMeta("pending_notifications", pending);
      return result;
    })();
    this.deliveries.set(envelope.eventId, delivery);
    try { return await delivery; }
    catch (error) {
      this.log({ type: "notification_failed", agent_id: envelope.id, event_id: envelope.eventId, ...failure(error) });
      this.scheduleRetry();
      throw error;
    } finally { this.deliveries.delete(envelope.eventId); }
  }

  async notify(id, message, options = {}) {
    return this.deliverNotification(this.queueNotification(id, message, options));
  }

  async flushNotifications() {
    for (const envelope of Object.values(this.board.getMeta("pending_notifications", {}))) {
      await this.deliverNotification(envelope);
    }
  }

  scheduleRetry() {
    if (this.closed || this.ending || this.pumpRetry) return;
    this.pumpRetry = setTimeout(() => {
      this.pumpRetry = null;
      this.schedule();
    }, 1000);
  }

  schedule() {
    if (!this.started || this.closed || this.ending || this.scheduled || this.pumpRetry) return;
    this.scheduled = true;
    setImmediate(() => {
      this.scheduled = false;
      void this.pump().catch(error => {
        this.log({ type: "coordinator_error", ...failure(error) });
        this.scheduleRetry();
      });
    });
  }

  async pump() {
    if (this.pumping || this.closed || this.ending) return;
    this.pumping = true;
    let succeeded = false;
    this.pumpDone = new Promise(resolve => { this.resolvePump = resolve; });
    try {
      const events = this.board.events(this.board.getMeta("coordinator_cursor", 0));
      const changed = new Map();
      for (const event of events) {
        if (!event.challenge_id) continue;
        if (event.type === "worker.assigned") {
          const workerId = event.assigned_worker_id;
          const context = this.board.context(workerId);
          if (context?.assignment_id === event.assignment_id && this.board.challenge(event.challenge_id).phase === "solving") {
            this.queueNotification(workerId, "工作归属已确定，读取当前题目并开始执行。" + JSON.stringify({ context,
              board: this.board.snapshot(workerId), directory: await this.challengeDirectory(event.challenge_id) }),
            { eventId: `assignment:${event.assignment_id}`, scope: event.challenge_id,
              version: this.board.challenge(event.challenge_id).revision });
          }
          continue;
        }
        const group = changed.get(event.challenge_id) ?? [];
        group.push(event);
        changed.set(event.challenge_id, group);
      }
      for (const [challengeId, changes] of changed) {
        const event = changes.at(-1);
        const challenge = this.board.challenge(challengeId);
        const message = JSON.stringify({ challenge_id: challengeId, phase: challenge.phase, revision: challenge.revision,
          events: changes, instruction: challenge.phase === "reviewing" ? "已有候选待核验，结束本轮并等待 Lead 反馈。"
            : challenge.phase === "completed" ? "本题已通过，请结束本轮，等待后续工作通知。" : "题目已更新，按需读取新增事实和意图。" });
        if (challenge.phase === "reviewing" && changes.some(item => ["review.requested", "submission.unknown"].includes(item.type))) {
          this.queueNotification("lead", "请核验并提交候选。" + JSON.stringify(challenge),
            { eventId: `review:${event.id}`, version: challenge.revision, scope: challengeId });
        } else if (changes.some(item => ["submission.accepted", "assistance.reminder"].includes(item.type))) {
          this.queueNotification("lead", message, { eventId: `progress:${event.id}`, version: challenge.revision, scope: challengeId });
        }
        for (const member of challenge.members) {
          if (challenge.phase === "deferred" || challenge.phase === "completed" && !member.active) continue;
          if (challenge.phase === "solving" && changes.every(item => item.actor_id === member.worker_id)) continue;
          this.queueNotification(member.worker_id, message,
            { eventId: `board:${event.id}:${member.worker_id}`, version: challenge.revision, scope: challengeId });
        }
      }
      if (events.length) this.board.setMeta("coordinator_cursor", events.at(-1).id);
      await this.flushNotifications();
      this.board.execute("platform", "release_finished", {});
      const dispatch = this.board.execute("platform", "dispatch", {});
      for (const notice of dispatch.notifications) {
        const key = JSON.stringify({ context: notice.context, available: notice.available });
        if (this.notices.get(notice.worker_id) === key || this.retryTimers.has(notice.worker_id)) continue;
        this.notices.set(notice.worker_id, key);
        await this.notify(notice.worker_id, "有可领取工作，请选择并认领。先选新题，其次援助，竞争失败继续选择。"
          + JSON.stringify(notice.available), { eventId: `available:${notice.worker_id}:${randomUUID()}` });
      }
      for (const id of this.settledAgents) {
        this.settledAgents.delete(id);
        if (id === "lead" || this.retryTimers.has(id)) continue;
        const context = this.board.context(id);
        const status = this.pool.status().find(item => item.id === id);
        if (context.assignment_id && status?.idle && this.board.challenge(context.challenge_id).phase === "solving") {
          await this.notify(id, "本题仍在解题中。结合现有事实、在途结果和意图继续推进，需要时申请援助或向 Lead 提出暂缓建议。",
            { eventId: `continue:${id}:${randomUUID()}`, scope: context.challenge_id, version: this.board.challenge(context.challenge_id).revision });
        }
      }
      const unfinished = this.challenges().filter(challenge => challenge.phase !== "completed");
      if (unfinished.length && unfinished.every(challenge => challenge.phase === "deferred")
        && this.pool.status().find(item => item.id === "lead")?.idle) {
        const key = JSON.stringify(unfinished.map(challenge => [challenge.id, challenge.revision]));
        if (this.board.getMeta("deferred_notice") !== key) {
          this.queueNotification("lead", "当前未完成题目均已暂缓。结合资源和剩余时间恢复题目，或明确决定结束比赛。" + JSON.stringify(unfinished),
            { eventId: `deferred:${randomUUID()}` });
          this.board.setMeta("deferred_notice", key);
          await this.flushNotifications();
        }
      }
      clearTimeout(this.reminderTimer);
      const next = this.board.nextReminderAt();
      if (next !== null && next !== undefined) {
        this.reminderTimer = setTimeout(() => {
          this.board.execute("platform", "tick", {});
          this.schedule();
        }, Math.max(0, next - Date.now()));
      }
      succeeded = true;
    } finally {
      this.pumping = false;
      this.resolvePump();
      if (succeeded && !this.closed && !this.ending && this.board.events(this.board.getMeta("coordinator_cursor", 0)).length) this.schedule();
    }
  }

  async challengeDirectory(id) {
    const directory = join(this.config.workDir, "shared", "challenges", id);
    await mkdir(join(directory, "inputs"), { recursive: true });
    return directory;
  }

  controlOperation(operation) {
    const result = this.control.then(operation);
    this.control = result.catch(() => {});
    return result;
  }

  async startChallenge(id) {
    return this.controlOperation(async () => {
      let challenge = this.board.challenge(id);
      if (challenge.phase !== "pending" && !["solving", "reviewing"].includes(challenge.phase)) throw new Error("Start a pending or resumed challenge");
      if (challenge.environment.status === "active" && challenge.ready) return { challenge, receipt: challenge.environment.connection };
      if (this.config.arena.mode === "connectivity_test") {
        const receipt = this.catalog.find(item => item.unique_code === challenge.unique_code);
        this.board.execute("platform", "set_environment", { challenge_id: id, ready: true, status: "active", online: false, connection: receipt });
        return { challenge: this.board.challenge(id), receipt };
      }
      if (this.challenges().some(item => ["closing", "unknown"].includes(item.environment.status))) {
        await this.reconcileEnvironments();
        challenge = this.board.challenge(id);
      }
      const catalog = this.catalog.find(item => item.unique_code === challenge.unique_code);
      const online = catalog?.container !== false;
      const occupied = this.challenges().filter(item => item.id !== id && item.environment.online && item.environment.status !== "closed");
      if (online && occupied.length >= 3) {
        throw Object.assign(new Error("All three online environment slots are occupied; close or reconcile one first"), { code: "environment_limit" });
      }
      if (challenge.environment.status === "closed") {
        this.board.setMeta(`environment_generation:${id}`, challenge.generation + 1);
      }
      this.board.setMeta(`environment_start:${id}`, { time: Date.now() });
      this.board.execute("platform", "set_environment", { challenge_id: id, ready: false, status: "pending", online });
      let receipt;
      try { receipt = await this.arena.start(challenge.unique_code, { signal: this.stopController.signal }); }
      catch (error) {
        this.board.execute("platform", "set_environment", { challenge_id: id, ready: false,
          status: error.outcomeUnknown ? "unknown" : "closed", online, connection: failure(error) });
        throw error;
      }
      let state = environmentState(receipt);
      if (state === "unknown") {
        const current = (await this.refreshCatalog()).find(item => item.unique_code === challenge.unique_code);
        state = environmentState(current);
        if (state === "active") receipt = current;
      }
      this.setEnvironment({
        challenge_id: id, ready: state === "active", status: state,
        online: receipt?.container === false ? false : online, connection: receipt,
      });
      return { challenge: this.board.challenge(id), receipt };
    });
  }

  setEnvironment(args) {
    const challenge = this.board.challenge(args.challenge_id);
    const target = this.board.getMeta(`environment_generation:${challenge.id}`, challenge.generation);
    return this.board.execute("platform", "set_environment", { ...args,
      new_generation: args.status === "active" && target > challenge.generation });
  }

  async reconcileEnvironments() {
    const catalog = await this.refreshCatalog();
    const results = [];
    for (const challenge of this.challenges()) {
      if (!["pending", "unknown", "closing"].includes(challenge.environment.status)) continue;
      if (challenge.environment.status === "pending" && !this.board.getMeta(`environment_start:${challenge.id}`)) continue;
      const actual = catalog.find(item => item.unique_code === challenge.unique_code);
      const status = environmentState(actual);
      if (status === "unknown") { results.push({ challenge_id: challenge.id, status }); continue; }
      this.setEnvironment({
        challenge_id: challenge.id, status, ready: status === "active" && challenge.phase !== "deferred",
        online: actual?.container !== false, connection: actual,
      });
      results.push({ challenge_id: challenge.id, status });
    }
    return results;
  }

  async files(id, path) {
    const challenge = this.board.challenge(id);
    if (this.config.arena.mode === "connectivity_test") {
      return { files: [], description: this.catalog.find(item => item.unique_code === challenge.unique_code)?.description ?? challenge.description };
    }
    const listed = await this.arena.listFiles(challenge.unique_code, { signal: this.stopController.signal });
    if (!path) return listed;
    const response = await this.arena.readFile(challenge.unique_code, path, { signal: this.stopController.signal });
    if (!response.body) throw new Error("Handout response has no body");
    const directory = join(await this.challengeDirectory(id), "inputs");
    const destination = join(directory, `${randomUUID()}-${basename(path)}`);
    const temporary = destination + ".partial";
    try {
      await pipeline(Readable.fromWeb(response.body), createWriteStream(temporary, { flags: "wx", mode: 0o600 }));
      await rename(temporary, destination);
    } catch (error) { await unlink(temporary).catch(() => {}); throw error; }
    return { path: destination, source_path: path };
  }

  applySubmission(id, result) {
    if (result.cumulativeScore !== null && result.cumulativeScore !== undefined) this.board.setMeta("cumulative_score", result.cumulativeScore);
    const receipt = result.receipt ?? result.challenge ?? {};
    const challengeCompletedByReceipt = challengeCompleted({ ...receipt, flag_count: receipt.total_flag_count ?? receipt.flag_count });
    const submission = this.challenges().flatMap(challenge => challenge.submissions ?? []).find(item => item.id === id);
    const catalog = this.catalog?.find(item => item.unique_code === submission?.unique_code);
    const completed = challengeCompletedByReceipt || challengeCompleted(result.challenge ?? {})
      || challengeCompleted(catalog ?? {}) || (result.outcome === "accepted" && catalog?.flag_count === 1);
    return this.board.execute("platform", "submission_result", {
      submission_id: id, status: result.outcome,
      challenge_completed: completed,
      reason: result.outcome === "rejected" ? result.receipt?.message ?? "赛事接口返回 correct=false。此候选未通过。" : result.reason,
      receipt: result.receipt ?? result.challenge ?? result.error ?? {},
    });
  }

  async submit(args, callId) {
    const challenge = this.board.challenge(args.challenge_id);
    const beginning = this.board.execute("lead", "begin_submission", { ...args, request_id: callId });
    if (!beginning.should_submit) return beginning;
    const candidate = challenge.candidates.find(item => item.id === args.candidate_id);
    let result;
    try { result = await this.arena.submit(challenge.unique_code, candidate.flag, { signal: this.stopController.signal }); }
    catch (error) {
      this.applySubmission(beginning.submission.id, { outcome: error.outcomeUnknown ? "unknown" : "not_sent", error: failure(error), reason: error.message });
      throw error;
    }
    if (result.outcome === "accepted" && this.config.arena.mode !== "connectivity_test") {
      try {
        await this.refreshCatalog();
        result.challenge = this.catalog.find(item => item.unique_code === challenge.unique_code);
      } catch (error) { this.log({ type: "progress_refresh_failed", ...failure(error) }); }
    }
    this.applySubmission(beginning.submission.id, result);
    if (this.config.arena.mode === "connectivity_test" && ["accepted", "rejected"].includes(result.outcome)) {
      this.endLocal("connectivity_verdict", result, result.outcome === "accepted");
      return result;
    }
    return result;
  }

  async defer(args, callId) {
    const result = this.board.execute("lead", "defer", { ...args, request_id: callId });
    const challenge = this.board.challenge(args.challenge_id);
    await Promise.all(challenge.members.map(async member => {
      await this.pool.abort(member.worker_id);
      await this.pool.waitIdle(member.worker_id);
      const worker = this.board.workers().find(item => item.id === member.worker_id);
      this.board.execute("platform", "activity", { worker_id: member.worker_id, context: worker.execution_context ?? this.board.context(member.worker_id), running: false });
    }));
    this.board.execute("platform", "release_finished", {});
    if (args.close_environment !== false && this.config.arena.mode !== "connectivity_test") {
      await this.controlOperation(async () => {
        this.board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "closing", ready: false });
        let receipt;
        try { receipt = await this.arena.close(challenge.unique_code, { signal: this.stopController.signal }); }
        catch (error) {
          this.board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "unknown", ready: false, connection: failure(error) });
          throw error;
        }
        if (environmentState(receipt) !== "closed") {
          this.board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "unknown", ready: false, connection: receipt });
          await this.reconcileEnvironments();
          return;
        }
        this.board.execute("platform", "set_environment", { challenge_id: challenge.id, status: "closed", ready: false, connection: receipt });
      });
    }
    return result;
  }

  endLocal(reason, receipt, completed = this.catalog?.length > 0 && this.catalog.every(challengeCompleted)) {
    const result = { reason, receipt, time: Date.now(), completed };
    this.board.setMeta("finished", result);
    this.ending = true;
    this.started = false;
    clearTimeout(this.reminderTimer);
    clearTimeout(this.pumpRetry);
    for (const timer of this.retryTimers.values()) clearTimeout(timer);
    this.resolveFinished(result);
  }

  async reconcileFinish(previous) {
    const record = { ...previous, status: "unknown", reconciled_at: Date.now() };
    try { record.catalog = await this.arena.listChallenges({ signal: this.stopController.signal }); }
    catch (error) { record.reconciliation_error = failure(error); }
    this.board.setMeta("finish_attempt", record);
    this.endLocal(record.reason, record);
    return record;
  }

  async finish(reason) {
    return this.controlOperation(async () => {
      const attempt = this.board.getMeta("finish_attempt");
      if (["pending", "unknown"].includes(attempt?.status)) return this.reconcileFinish(attempt);
      const previous = this.board.getMeta("finished");
      if (previous) return previous.receipt;
      if (!reason?.trim()) throw new Error("An explicit finish reason is required");
      if (this.config.arena.mode === "connectivity_test") throw new Error("Connectivity checks end automatically on the submission verdict");
      const pending = this.challenges().flatMap(challenge => challenge.submissions ?? []).filter(item => ["unknown", "pending"].includes(item.status));
      if (pending.length) throw new Error("Reconcile pending flag submissions before finishing the attempt");
      await this.refreshCatalog();
      const intent = { reason, status: "pending", time: Date.now() };
      this.board.setMeta("finish_attempt", intent);
      let receipt;
      try { receipt = await this.arena.finish({ signal: this.stopController.signal }); }
      catch (error) {
        const record = { ...intent, status: error.outcomeUnknown ? "unknown" : "not_sent", error: failure(error) };
        this.board.setMeta("finish_attempt", record);
        if (error.outcomeUnknown) this.endLocal(reason, record);
        throw error;
      }
      this.board.setMeta("finish_attempt", { ...intent, status: "confirmed", receipt });
      this.endLocal(reason, receipt);
      return receipt;
    });
  }

  tools(role, id) {
    const tool = (name, description, parameters, execute) => ({ name, description, parameters,
      execute: async (args, execution) => {
        try { return await execute(args, execution); }
        catch (error) {
          if (error.code === "task_not_found") this.endLocal("invalid_attempt_token", failure(error));
          throw error;
        }
      } });
    const shared = [
      tool("board_read", "Read available tasks or your current task, facts, intents, candidates, and current revisions. since_cursor retrieves incremental events using the previous event_cursor. Lead sees the whole board.",
        Type.Object({ challenge_id: Type.Optional(text), since_cursor: Type.Optional(Type.Integer({ minimum: 0 })) }),
        args => this.board.snapshot(id, { challengeId: args.challenge_id, sinceRevision: args.since_cursor })),
      tool("board_act", "Update the blackboard. Task writes require challenge_id and expected_revision. request_id is automatic. create_challenge: unique_code/title/description/difficulty(easy|medium|hard)/multi_stage/priority(0..3). post_fact: statement, kind(observation|inference|structure), evidence[{type:text,summary}|{artifact_id,summary}], derived_from; close your held Intent by resolves:intent_id and result:confirmed|rejected|inconclusive. post_intent: statement,based_on:[fact_id],expected,method, optional claim/parallel/parallel_key/depends_on/retry_of. claim_intent: intent_id,expected_version. release_intent: intent_id,note. request_assistance: reason,additional_workers. submit_review: flag,summary,fact_refs. reject_review: review_revision,reason. Read new revisions from each result.",
        Type.Object({ action: Type.Union((role === "lead" ? LEAD_ACTIONS : WORKER_ACTIONS).map(action => Type.Literal(action))), args: Type.Record(Type.String(), Type.Unknown()) }),
        (args, execution) => {
          if (args.action === "create_challenge" && !this.catalog?.some(item => item.unique_code === args.args.unique_code)) throw new Error("Create tasks using an actual code from arena_list");
          const input = { ...args.args, request_id: execution.callId };
          if (args.action === "create_challenge") {
            input.ready = false;
            input.description ??= this.catalog.find(item => item.unique_code === input.unique_code).description;
          }
          return this.board.execute(id, args.action, input, execution.context);
        }),
      tool("snapshot_file", "Capture a file as immutable evidence, then reference its artifact_id in a Fact. This records an observed file snapshot.",
        Type.Object({ path: text, challenge_id: text }), async (args, execution) => {
          const assertOrigin = () => {
            this.board.snapshot(id, { challengeId: args.challenge_id });
            if (id !== "lead" && (execution.context?.challenge_id !== args.challenge_id
              || JSON.stringify(assignmentContext(execution.context)) !== JSON.stringify(this.board.context(id)))) {
              throw new Error("File evidence requires the current task assignment");
            }
          };
          assertOrigin();
          const artifact = await snapshotFile({ root: await this.challengeDirectory(args.challenge_id), path: args.path, signal: execution.signal });
          assertOrigin();
          return this.board.execute("platform", "register_artifact", { ...artifact, challenge_id: args.challenge_id, worker_id: id, context: execution.context });
        }),
    ];
    if (role !== "lead") return shared;
    return [...shared,
      tool("arena_list", "Fetch the actual competition challenge catalog and progress.", Type.Object({}), () => this.refreshCatalog()),
      tool("arena_reconcile", "Read official progress to resolve pending environment outcomes and flag receipts.", Type.Object({}), async () => ({
        environments: await this.reconcileEnvironments(), submissions: await this.reconcilePendingSubmissions(),
      })),
      tool("arena_start", "Start a created task, including offline tasks. Pending environments require a later retry. At most 3 online environments.", challengeParameter, args => this.startChallenge(args.challenge_id)),
      tool("arena_files", "List official statement and attachment paths, or download one exact listed path to shared inputs.", Type.Object({ challenge_id: text, path: Type.Optional(text) }), args => this.files(args.challenge_id, args.path)),
      tool("submit_flag", "Authorize a candidate and submit its flag. Accepted, rejected, and unknown results are persisted. Unknown submissions require reconciliation.",
        Type.Object({ challenge_id: text, candidate_id: text, review_revision: Type.Integer(), expected_revision: Type.Integer() }), (args, execution) => this.submit(args, execution.callId)),
      tool("reconcile_submissions", "Read official progress to reconcile unknown submissions without submitting again.", Type.Object({}), () => this.reconcilePendingSubmissions()),
      tool("defer_challenge", "Save a reason and defer a task. End its executions before releasing members, optionally close the environment.",
        Type.Object({ challenge_id: text, reason: text, expected_revision: Type.Integer(), close_environment: Type.Optional(Type.Boolean()) }), (args, execution) => this.defer(args, execution.callId)),
      tool("resume_challenge", "Restore a deferred task and start its environment using a new generation when needed.",
        Type.Object({ challenge_id: text, expected_revision: Type.Integer() }), async (args, execution) => {
          this.board.execute("lead", "resume", { ...args, request_id: execution.callId });
          return this.startChallenge(args.challenge_id);
        }),
      tool("resources", "Read current container CPU, memory, disk, event loop, and actual worker execution status.", Type.Object({}), () => this.resources.snapshot()),
      tool("grow_workers", "Increase the peer Worker team to target_total. Inspect resource usage and useful parallel work before choosing the target.",
        Type.Object({ target_total: Type.Integer({ minimum: 1 }), reason: text }), async (args, execution) => {
          this.board.execute("lead", "grow_workers", { ...args, request_id: execution.callId });
          await this.prepareWorkers();
          return this.board.execute("lead", "grow_workers", { ...args, request_id: execution.callId });
        }),
      tool("finish_attempt", "Explicitly finish the competition after confirmed completion or a deliberate decision to stop. This ends the attempt.",
        Type.Object({ reason: text }), args => this.finish(args.reason)),
    ];
  }

  stop(reason = "shutdown") {
    this.stopping ??= (async () => {
      this.closed = true;
      this.stopController.abort();
      clearTimeout(this.reminderTimer);
      clearTimeout(this.deadlineTimer);
      clearTimeout(this.pumpRetry);
      for (const timer of this.retryTimers.values()) clearTimeout(timer);
      await this.pool.close();
      await this.starting?.catch(() => {});
      await this.control;
      await this.pumpDone;
      await Promise.allSettled([...this.deliveries.values()]);
      this.resources.close();
      this.log({ type: "stopped", reason });
      await this.logWrites;
      await this.logFile.close();
      this.board.close();
      this.resolveFinished({ reason });
    })();
    return this.stopping;
  }
}
