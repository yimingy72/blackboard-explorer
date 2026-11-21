import { mkdir, readFile, rename, open, stat, unlink, writeFile } from "node:fs/promises";
import { createReadStream, createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";
import { join, basename, isAbsolute, relative, resolve } from "node:path";
import { createHash, randomUUID } from "node:crypto";
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
const WORKER_ACTIONS = ["claim", "post_fact", "post_intent", "claim_intent", "release_intent",
  "request_assistance", "request_deferral", "submit_review"];
const MAX_SESSION_ERROR_RETRIES = 3;
const PREPARED_INPUT_VERSION = 1;

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

function catalogRequiresEnvironment(item, fallback = true) {
  if (typeof item?.container === "boolean") return item.container;
  if ([0, "false"].includes(item?.container)) return false;
  if ([1, "true"].includes(item?.container)) return true;
  if (item?.container_status === "none" || item?.container?.status === "none") return false;
  return fallback;
}

function preparationMarkerKey(challenge) {
  return `offline_prepared:${challenge.id}:${challenge.generation}`;
}

function normalizedPreparedFiles(files) {
  if (!Array.isArray(files)) return null;
  const normalized = [];
  for (const file of files) {
    if (!file || typeof file !== "object"
      || typeof file.source_path !== "string" || !file.source_path
      || typeof file.path !== "string" || !file.path
      || !Number.isSafeInteger(file.size) || file.size < 0
      || typeof file.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(file.sha256)) return null;
    normalized.push({
      source_path: file.source_path,
      path: file.path,
      size: file.size,
      sha256: file.sha256,
    });
  }
  return normalized.sort((left, right) => left.source_path.localeCompare(right.source_path));
}

function samePreparedFiles(left, right) {
  const normalizedLeft = normalizedPreparedFiles(left);
  const normalizedRight = normalizedPreparedFiles(right);
  return normalizedLeft !== null && normalizedRight !== null
    && JSON.stringify(normalizedLeft) === JSON.stringify(normalizedRight);
}

function pathInside(root, path) {
  const nested = relative(root, resolve(path));
  return nested !== "" && !nested.startsWith("..") && !isAbsolute(nested);
}

function challengeSummary(challenge) {
  const assistantCount = challenge.members.filter(member => member.role === "assistant").length;
  const openSlots = ["assembling", "solving"].includes(challenge.phase) && challenge.ready && challenge.owner_id
    ? Math.max(0, Math.min(9 - challenge.members.length,
      (challenge.assistance_target ?? 0) - assistantCount))
    : 0;
  return {
    id: challenge.id,
    unique_code: challenge.unique_code,
    title: challenge.title,
    description: challenge.description.length > 1200
      ? `${challenge.description.slice(0, 1200)}…`
      : challenge.description,
    difficulty: challenge.difficulty,
    priority: challenge.priority,
    phase: challenge.phase,
    revision: challenge.revision,
    generation: challenge.generation,
    ready: challenge.ready,
    offline: challenge.requires_online_environment === false,
    points: challenge.points ?? challenge.total_score ?? null,
    flag_count: challenge.flag_count ?? null,
    correct_flag_count: challenge.correct_flag_count ?? null,
    owner_id: challenge.owner_id,
    needs_assistance: challenge.needs_assistance,
    member_count: challenge.members.length,
    assistant_count: assistantCount,
    assistance_target: challenge.assistance_target ?? 0,
    open_assistance_slots: openSlots,
    fact_count: challenge.facts.length,
    intent_count: challenge.intents.length,
    candidate_count: challenge.candidates.length,
    submission_count: challenge.submissions.length,
    environment: {
      status: challenge.environment?.status ?? null,
      online: challenge.environment?.online === true,
      last_error: ["closed", "unknown"].includes(challenge.environment?.status)
        && challenge.environment?.connection?.code
        ? {
            code: challenge.environment.connection.code,
            message: challenge.environment.connection.message,
            status: challenge.environment.connection.status ?? null,
            detail: challenge.environment.connection.detail ?? null,
          }
        : null,
    },
  };
}

function compactResources(snapshot) {
  const workers = snapshot?.execution?.workers ?? [];
  const idleWorkers = workers.filter(worker => worker.status === "idle" && !worker.active && !worker.assignment_id);
  return {
    cpu: snapshot?.cpu ?? null,
    memory: snapshot?.memory ?? null,
    disk: snapshot?.disk ?? null,
    event_loop: snapshot?.event_loop ?? null,
    workers: {
      total: workers.length,
      idle: idleWorkers.length,
    },
  };
}

function assistanceNeedSummary(challenge) {
  const summary = challengeSummary(challenge);
  return {
    id: summary.id,
    unique_code: summary.unique_code,
    title: summary.title,
    difficulty: summary.difficulty,
    priority: summary.priority,
    phase: summary.phase,
    revision: summary.revision,
    owner_id: summary.owner_id,
    member_count: summary.member_count,
    assistance_target: summary.assistance_target,
    open_assistance_slots: summary.open_assistance_slots,
    needs_assistance: summary.needs_assistance,
  };
}

function assistanceGapMarkerKey(challenge) {
  return `lead_assistance_gap:${challenge.id}:${challenge.generation}`;
}

function assistanceGapState(challenge) {
  const summary = challengeSummary(challenge);
  return {
    generation: challenge.generation,
    assistance_target: summary.assistance_target,
    open_assistance_slots: summary.open_assistance_slots,
    member_count: summary.member_count,
  };
}

function candidateSummary(candidate) {
  return {
    id: candidate.id,
    author: candidate.author,
    summary: candidate.summary,
    review_revision: candidate.review_revision,
    environment_generation: candidate.environment_generation,
    flag: candidate.flag,
  };
}

async function fileMetadata(path) {
  const info = await stat(path);
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return { path, size: info.size, sha256: hash.digest("hex") };
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
    this.continuations = new Map();
    this.stalledReports = new Set();
    this.retryExhausted = new Set();
    this.retryDelayBaseMs = 1000;
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
    this.queueNotification("lead", "比赛开始或进程恢复。先读取当前题目目录及本地黑板。"
      + "离线题优先是 Lead 的策略：根据 container=false、priority、附件准备收益和当前资源自行决定是否调用 arena_prepare_offline。"
      + "在线实例的实际配额由赛事 API 判定；平台不本地预判 3 个上限，收到 environment_limit 或 quota 错误时原样展示给你调整。"
      + "困难题先只分配一个主 Worker 并进入 assembling；收到平台的需要援助通知后，查看资源再 grow_workers。"
      + "新未领取题始终优先，只有没有新题时才把空闲 Worker 加入援助。"
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
      if (["assembling", "solving"].includes(challenge.phase)) {
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
    const byCode = new Map(this.catalog.map(item => [item.unique_code, item]));
    for (const challenge of this.challenges()) {
      const item = byCode.get(challenge.unique_code);
      if (!item) continue;
      this.board.execute("platform", "apply_catalog_metadata", {
        challenge_id: challenge.id,
        requires_online_environment: catalogRequiresEnvironment(item),
        ...(Number.isFinite(item.points) ? { points: item.points } : {}),
        ...(Number.isFinite(item.total_score) ? { total_score: item.total_score } : {}),
        ...(Number.isSafeInteger(item.flag_count) ? { flag_count: item.flag_count } : {}),
        ...(Number.isSafeInteger(item.correct_flag_count) ? { correct_flag_count: item.correct_flag_count } : {}),
      });
    }
    return this.catalog;
  }

  async reconcilePendingSubmissions() {
    const reconciled = [];
    for (const challenge of this.challenges()) {
      for (const submission of challenge.submissions ?? []) {
        if (!["pending", "unknown"].includes(submission.status)) continue;
        const legacyAcceptedFlags = new Set((challenge.submissions ?? [])
          .filter(item => item.status === "accepted").map(item => item.flag));
        const baselineCorrectFlagCount = Number.isSafeInteger(submission.baseline_correct_flag_count)
          ? submission.baseline_correct_flag_count
          : legacyAcceptedFlags.size;
        const result = await this.arena.reconcileSubmission(challenge.unique_code, {
          baselineCorrectFlagCount,
          signal: this.stopController.signal,
        });
        if (result.outcome === "accepted") {
          if (Number.isSafeInteger(result.challenge?.correct_flag_count)) {
            this.board.execute("platform", "apply_catalog_metadata", {
              challenge_id: challenge.id,
              correct_flag_count: result.challenge.correct_flag_count,
            });
          }
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
    if (event.type === "idle" && !event.aborted) {
      this.retries.delete(id);
      this.retryExhausted.delete(id);
    }
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
    if (attempt > MAX_SESSION_ERROR_RETRIES) {
      if (!this.retryExhausted.has(id)) {
        this.retryExhausted.add(id);
        const context = id === "lead" ? null : this.board.context(id);
        if (id !== "lead" && context?.assignment_id) {
          const challenge = this.board.challenge(context.challenge_id);
          this.queueNotification("lead", "Worker 的模型请求在有限重试后仍持续失败。读取该题黑板决定增加援助、调整方向或暂缓。"
            + JSON.stringify({
              worker_id: id,
              error: message,
              attempts: attempt,
              challenge: challengeSummary(challenge),
            }), { eventId: `retry-exhausted:${id}:${challenge.id}:${challenge.revision}`,
            scope: challenge.id, version: challenge.revision });
        }
      }
      this.schedule();
      return;
    }
    const timer = setTimeout(() => {
      this.retryTimers.delete(id);
      if (this.closed || this.ending) return;
      if (id !== "lead") {
        const context = this.board.context(id);
        const challenge = context.assignment_id ? this.board.challenge(context.challenge_id) : null;
        if (!challenge || !["assembling", "solving"].includes(challenge.phase)
          || challenge.environment?.status !== "active" || !challenge.ready) {
          this.schedule();
          return;
        }
      }
      void this.notify(id, "上次模型回合失败。读取当前状态，核实已有结果并继续。", { eventId: `retry:${id}:${randomUUID()}` }).catch(() => {});
    }, Math.min(30000, this.retryDelayBaseMs * 2 ** Math.min(attempt - 1, 5)));
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
      const notifiedAssignments = new Set();
      const hardAssistanceNeeds = new Set();
      for (const event of events) {
        if (!event.challenge_id) continue;
        if (event.type === "worker.assigned") {
          const challenge = this.board.challenge(event.challenge_id);
          if (!["assembling", "solving"].includes(challenge.phase)) continue;
          if (challenge.difficulty === "hard"
            && event.assigned_worker_id === challenge.owner_id
            && event.team_ready === false) {
            hardAssistanceNeeds.add(challenge.id);
          }
          const markerKey = assistanceGapMarkerKey(challenge);
          const currentGap = assistanceGapState(challenge);
          const previousGap = this.board.getMeta(markerKey);
          if (previousGap && currentGap.open_assistance_slots < previousGap.open_assistance_slots) {
            this.board.setMeta(markerKey, currentGap);
          }
          const workerIds = event.team_ready ? challenge.members.map(member => member.worker_id)
            : [event.assigned_worker_id];
          const directory = await this.challengeDirectory(event.challenge_id);
          for (const workerId of workerIds) {
            const context = this.board.context(workerId);
            if (!context?.assignment_id || context.challenge_id !== event.challenge_id) continue;
            if (notifiedAssignments.has(context.assignment_id)) continue;
            notifiedAssignments.add(context.assignment_id);
            this.queueNotification(workerId, "工作归属已确定。直接开始当前题，不要重复领取。"
              + JSON.stringify({
                context,
                challenge: challengeSummary(challenge),
                paths: {
                  worker_cwd: join(this.config.workDir, "work", workerId),
                  challenge_root: directory,
                  inputs: join(directory, "inputs"),
                },
              }),
            { eventId: `assignment:${context.assignment_id}`, scope: event.challenge_id,
              version: challenge.revision });
          }
          continue;
        }
        const group = changed.get(event.challenge_id) ?? [];
        group.push(event);
        changed.set(event.challenge_id, group);
      }
      const leadUpdates = [];
      for (const [challengeId, changes] of changed) {
        const event = changes.at(-1);
        const challenge = this.board.challenge(challengeId);
        const environmentReleased = challenge.environment.status === "closed"
          && (challenge.phase === "completed"
            || changes.some(item => item.type === "environment.updated"));
        const compactEvents = changes.map(item => ({
          type: item.type,
          event_id: item.id,
          revision: item.revision,
          at: item.at,
        }));
        const leadUpdate = { challenge: challengeSummary(challenge), events: compactEvents, sections: [] };
        if (challenge.phase === "reviewing"
          && changes.some(item => ["review.requested", "submission.unknown"].includes(item.type))) {
          leadUpdate.sections.push({
            type: "review_required",
            candidates: challenge.candidates.map(candidateSummary),
            instruction: "核验候选并处理尚无确定结果的提交。",
          });
        }
        if (changes.some(item => item.type === "deferral.requested")) {
          leadUpdate.sections.push({
            type: "deferral_requested",
            instruction: "读取阻塞 Fact 后决定继续、增加援助或暂缓。",
          });
        }
        const releaseKey = `online_slot_release_notice:${challenge.id}:${challenge.generation}`;
        if (environmentReleased && challenge.environment.online === true
          && !this.board.getMeta(releaseKey)
          && !this.board.getMeta(`external_environment_closed:${challenge.id}:${challenge.generation}`)) {
          this.board.setMeta(releaseKey, event.id);
          leadUpdate.sections.push({
            type: "online_environment_released",
            instruction: "立即检查未启动的在线题，用空出的名额启动下一道。",
          });
        }
        if (changes.some(item => ["submission.accepted", "assistance.reminder"].includes(item.type))) {
          leadUpdate.sections.push({
            type: "progress",
            instruction: "根据最新提交回执或援助提醒继续调度。",
          });
        }
        if (leadUpdate.sections.length) leadUpdates.push(leadUpdate);
        const instruction = challenge.phase === "reviewing"
          ? "已有候选待核验，结束本轮并等待 Lead 反馈。"
          : challenge.phase === "completed"
            ? "本题已通过，请结束本轮，等待后续工作通知。"
            : "题目已更新，按需读取新增事实和意图。";
        const message = JSON.stringify({
          challenge: challengeSummary(challenge),
          events: compactEvents,
          instruction,
        });
        for (const member of challenge.members) {
          if (challenge.phase === "deferred" || challenge.phase === "completed" && !member.active) continue;
          if (["assembling", "solving"].includes(challenge.phase)
            && changes.every(item => item.actor_id === member.worker_id)) continue;
          this.queueNotification(member.worker_id, message,
            { eventId: `board:${event.id}:${member.worker_id}`, version: challenge.revision, scope: challengeId });
        }
      }
      if (leadUpdates.length) {
        const event = events.at(-1);
        const releasedOnline = leadUpdates.some(update => update.sections
          .some(section => section.type === "online_environment_released"));
        this.queueNotification("lead", "黑板有新批次需要处理。只读取下面列出的题目和增量，不要重读无关环境。"
          + (releasedOnline ? "本批次包含在线环境名额已释放事件，立即补位或按策略调整。" : "")
          + JSON.stringify({ updates: leadUpdates }),
        { eventId: `lead-batch:${event.id}`, version: 0, scope: "global" });
      }
      if (events.length) this.board.setMeta("coordinator_cursor", events.at(-1).id);
      await this.flushNotifications();
      this.board.execute("platform", "release_finished", {});
      const dispatch = this.board.execute("platform", "dispatch", {});
      const idleWorkerCount = this.board.workers()
        .filter(worker => worker.status === "idle" && !worker.active && !worker.assignment_id).length;
      const assistanceGapEvents = new Set([
        "assistance.reminder",
        "assistance.requested",
        "challenge.updated",
        "challenge.resumed",
        "fact.posted",
        "intent.claimed",
        "intent.posted",
        "intent.released",
        "worker.released",
      ]);
      const gapCandidates = new Map();
      for (const item of dispatch.available.assistance) gapCandidates.set(item.id, this.board.challenge(item.id));
      for (const [challengeId, changes] of changed) {
        if (changes.some(change => assistanceGapEvents.has(change.type))) {
          gapCandidates.set(challengeId, this.board.challenge(challengeId));
        }
      }
      const assistanceByChallenge = new Map();
      const addAssistanceNeed = (challenge) => {
        const current = assistanceNeedSummary(challenge);
        assistanceByChallenge.set(current.id, current);
      };
      for (const challengeId of hardAssistanceNeeds) {
        const challenge = this.board.challenge(challengeId);
        const current = assistanceGapState(challenge);
        const previous = this.board.getMeta(assistanceGapMarkerKey(challenge));
        const increased = !previous || previous.generation !== current.generation
          || current.assistance_target > previous.assistance_target
          || current.open_assistance_slots > previous.open_assistance_slots;
        if (current.open_assistance_slots > 0 && increased) {
          addAssistanceNeed(challenge);
          this.board.setMeta(assistanceGapMarkerKey(challenge), current);
        }
      }
      for (const challenge of gapCandidates.values()) {
        const summary = challengeSummary(challenge);
        const markerKey = assistanceGapMarkerKey(challenge);
        const current = assistanceGapState(challenge);
        const previous = this.board.getMeta(markerKey);
        const increased = !previous || previous.generation !== current.generation
          || current.assistance_target > previous.assistance_target
          || current.open_assistance_slots > previous.open_assistance_slots;
        if (summary.open_assistance_slots > 0 && idleWorkerCount === 0 && increased) {
          addAssistanceNeed(challenge);
          this.board.setMeta(markerKey, current);
        } else if (previous && !increased) {
          this.board.setMeta(markerKey, current);
        }
      }
      if (assistanceByChallenge.size) {
        let resources;
        try {
          resources = compactResources(await this.resources.snapshot());
        } catch (error) {
          resources = { error: failure(error) };
        }
        const assistanceNeeds = [...assistanceByChallenge.values()];
        this.queueNotification("lead", "有题目进入需要援助状态。新题仍优先；只有没有新题时，空闲 Worker 才领取援助。"
          + "请结合未认领新题、实际空闲 Worker 和一次真实资源采样，决定是否调用 grow_workers。"
          + JSON.stringify({
            event: "challenges_need_assistance",
            challenge_count: assistanceNeeds.length,
            challenges: assistanceNeeds,
            unclaimed_new_challenges: dispatch.available.new_challenges.map(
              ({ description: _description, ...item }) => item),
            resources,
            idle_worker_count: idleWorkerCount,
            assistance_work_continues_broadcast: true,
          }), {
          eventId: `lead-needs-assistance:${randomUUID()}`,
          scope: "global",
          version: 0,
        });
      }
      const availableSummary = {
        new_challenges: dispatch.available.new_challenges.map(({ description: _description, ...item }) => item),
        assistance: dispatch.available.assistance.map(({ description: _description, ...item }) => item),
      };
      for (const notice of dispatch.notifications) {
        const key = JSON.stringify({ context: notice.context, available: availableSummary });
        if (this.notices.get(notice.worker_id) === key || this.retryTimers.has(notice.worker_id)) continue;
        this.notices.set(notice.worker_id, key);
        await this.notify(notice.worker_id, "有可领取工作，请选择并认领。只要还有新题就先选新题；没有新题时再领取援助，竞争失败继续选择。"
          + JSON.stringify({
            available: availableSummary,
            priority_order: "new challenges before assistance; within new challenges follow Lead-defined priority 0 highest; "
              + "offline metadata is shown for Worker selection",
          }),
        { eventId: `available:${notice.worker_id}:${randomUUID()}` });
      }
      for (const id of this.settledAgents) {
        this.settledAgents.delete(id);
        if (id === "lead" || this.retryTimers.has(id)) continue;
        const context = this.board.context(id);
        const status = this.pool.status().find(item => item.id === id);
        if (context.assignment_id && status?.idle
          && ["assembling", "solving"].includes(this.board.challenge(context.challenge_id).phase)) {
          const challenge = this.board.challenge(context.challenge_id);
          const key = `${id}:${context.challenge_id}:${challenge.revision}`;
          const continuations = this.continuations.get(key) ?? 0;
          if (continuations >= 2) {
            if (!this.stalledReports.has(key)) {
              this.stalledReports.add(key);
              this.queueNotification("lead", "Worker 在没有新增黑板版本时连续两轮未形成可核验结果。读取当前题和该 Worker 的 Fact/Intent，决定增加援助、调整方向或暂缓。"
                + JSON.stringify({ worker_id: id, challenge_id: challenge.id, revision: challenge.revision }),
              { eventId: `stalled:${key}`, scope: challenge.id, version: challenge.revision });
            }
            continue;
          }
          this.continuations.set(key, continuations + 1);
          await this.notify(id, "本题仍在解题中。结合现有事实、在途结果和意图继续推进，需要时申请援助或向 Lead 提出暂缓建议。",
            { eventId: `continue:${key}:${continuations + 1}`, scope: context.challenge_id, version: challenge.revision });
        }
      }
      await this.flushNotifications();
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

  async validatePreparedInputs(challenge) {
    const marker = this.board.getMeta(preparationMarkerKey(challenge));
    if (!marker) return { valid: false, reason: "missing_marker" };
    if (marker.version !== PREPARED_INPUT_VERSION
      || marker.challenge_id !== challenge.id
      || marker.unique_code !== challenge.unique_code
      || marker.generation !== challenge.generation
      || typeof marker.manifest_path !== "string"
      || !Number.isSafeInteger(marker.manifest_size)
      || typeof marker.manifest_sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(marker.manifest_sha256)
      || !normalizedPreparedFiles(marker.files)) {
      return { valid: false, reason: "invalid_marker" };
    }
    const directory = await this.challengeDirectory(challenge.id);
    const inputs = join(directory, "inputs");
    const expectedManifest = join(inputs, "manifest.json");
    if (resolve(marker.manifest_path) !== resolve(expectedManifest)) {
      return { valid: false, reason: "unexpected_manifest_path" };
    }
    try {
      const manifestInfo = await fileMetadata(expectedManifest);
      if (manifestInfo.size !== marker.manifest_size
        || manifestInfo.sha256 !== marker.manifest_sha256) {
        return { valid: false, reason: "manifest_changed" };
      }
      const manifest = JSON.parse((await readFile(expectedManifest)).toString("utf8"));
      if (manifest.version !== PREPARED_INPUT_VERSION
        || manifest.challenge_id !== challenge.id
        || manifest.unique_code !== challenge.unique_code
        || manifest.generation !== challenge.generation
        || !samePreparedFiles(marker.files, manifest.files)) {
        return { valid: false, reason: "manifest_mismatch" };
      }
      const files = normalizedPreparedFiles(marker.files);
      for (const file of files) {
        if (!pathInside(inputs, file.path)) {
          return { valid: false, reason: "file_outside_inputs", source_path: file.source_path };
        }
        const current = await fileMetadata(file.path);
        if (current.size !== file.size || current.sha256 !== file.sha256) {
          return { valid: false, reason: "file_changed", source_path: file.source_path };
        }
      }
      return { valid: true, marker, manifest, files };
    } catch (error) {
      return {
        valid: false,
        reason: error.code === "ENOENT" ? "missing_file" : "invalid_inputs",
        message: error.message,
      };
    }
  }

  async invalidatePreparedInputs(challenge, reason) {
    this.board.setMeta(preparationMarkerKey(challenge), {
      version: PREPARED_INPUT_VERSION,
      challenge_id: challenge.id,
      generation: challenge.generation,
      invalid_reason: reason,
      invalidated_at: Date.now(),
    });
    const current = this.board.challenge(challenge.id);
    if (current.ready && current.environment.status === "active") {
      this.setEnvironment({ challenge_id: current.id, status: "active", ready: false });
    }
  }

  async writePreparationManifest(challenge, files) {
    const directory = await this.challengeDirectory(challenge.id);
    const manifestPath = join(directory, "inputs", "manifest.json");
    const manifest = {
      version: PREPARED_INPUT_VERSION,
      challenge_id: challenge.id,
      unique_code: challenge.unique_code,
      generation: challenge.generation,
      files: normalizedPreparedFiles(files),
    };
    if (!manifest.files) throw new Error("Prepared input manifest is invalid");
    const content = Buffer.from(JSON.stringify(manifest, null, 2) + "\n");
    const temporary = `${manifestPath}.${randomUUID()}.partial`;
    try {
      await writeFile(temporary, content, { flag: "wx", mode: 0o600 });
      await rename(temporary, manifestPath);
    } catch (error) {
      await unlink(temporary).catch(() => {});
      throw error;
    }
    const info = await fileMetadata(manifestPath);
    return { manifest, manifestPath, manifestSize: info.size, manifestSha256: info.sha256 };
  }

  async recordPreparationFact(challenge, manifestPath, manifestSha256, files) {
    const requestId = `prepare-inputs:${challenge.id}:${challenge.generation}:${manifestSha256}`;
    const intentKey = `preparation_fact_intent:${challenge.id}:${challenge.generation}:${manifestSha256}`;
    let intent = this.board.getMeta(intentKey);
    if (!intent || intent.manifest_sha256 !== manifestSha256) {
      intent = {
        request_id: requestId,
        expected_revision: challenge.revision,
        manifest_sha256: manifestSha256,
      };
      this.board.setMeta(intentKey, intent);
    }
    const args = expectedRevision => ({
      request_id: requestId,
      challenge_id: challenge.id,
      expected_revision: expectedRevision,
      statement: `Inputs prepared for environment generation ${challenge.generation}: ${files.length} file(s).`,
      kind: "observation",
      evidence: [{
        type: "text",
        summary: `Input manifest ${manifestPath}; sha256=${manifestSha256}; files=${files.length}`,
      }],
    });
    try {
      return this.board.execute("lead", "post_fact", args(intent.expected_revision));
    } catch (error) {
      if (error.code !== "revision_conflict") throw error;
      intent = { ...intent, expected_revision: this.board.challenge(challenge.id).revision };
      this.board.setMeta(intentKey, intent);
      return this.board.execute("lead", "post_fact", args(intent.expected_revision));
    }
  }

  async prepareChallengeInputs(challenge, { markReady = true, refresh = false } = {}) {
    let current = this.board.challenge(challenge.id);
    if (current.environment.status !== "active") {
      throw Object.assign(new Error("Inputs can only be prepared in an active environment"), {
        code: "environment_not_active",
        details: { challenge_id: current.id, status: current.environment.status },
      });
    }
    const existing = refresh ? { valid: false } : await this.validatePreparedInputs(current);
    if (existing.valid) {
      if (markReady && !current.ready) {
        this.setEnvironment({ challenge_id: current.id, status: "active", ready: true });
        current = this.board.challenge(current.id);
      }
      return {
        challenge: current,
        marker: existing.marker,
        manifest_path: existing.marker.manifest_path,
        files: existing.files,
        cached: true,
      };
    }
    if (current.ready) {
      this.setEnvironment({ challenge_id: current.id, status: "active", ready: false });
      current = this.board.challenge(current.id);
    }
    const listed = this.config.arena.mode === "connectivity_test"
      ? []
      : await this.arena.listFiles(current.unique_code, { signal: this.stopController.signal });
    const files = [];
    for (const entry of [...listed].sort((left, right) => {
      const leftPath = typeof left === "string" ? left : left.path;
      const rightPath = typeof right === "string" ? right : right.path;
      return leftPath.localeCompare(rightPath);
    })) {
      const path = typeof entry === "string" ? entry : entry.path;
      files.push(await this.saveListedFile(current, path));
    }
    const written = await this.writePreparationManifest(current, files);
    const marker = {
      version: PREPARED_INPUT_VERSION,
      challenge_id: current.id,
      unique_code: current.unique_code,
      generation: current.generation,
      manifest_path: written.manifestPath,
      manifest_size: written.manifestSize,
      manifest_sha256: written.manifestSha256,
      files: normalizedPreparedFiles(files),
      prepared_at: Date.now(),
    };
    await this.recordPreparationFact(current, written.manifestPath, written.manifestSha256, marker.files);
    this.board.setMeta(preparationMarkerKey(current), marker);
    const validation = await this.validatePreparedInputs(current);
    if (!validation.valid) {
      await this.invalidatePreparedInputs(current, validation.reason);
      throw Object.assign(new Error("Prepared inputs failed validation"), {
        code: "invalid_prepared_inputs",
        details: validation,
      });
    }
    if (markReady) this.setEnvironment({ challenge_id: current.id, status: "active", ready: true });
    current = this.board.challenge(current.id);
    return {
      challenge: current,
      marker,
      manifest_path: marker.manifest_path,
      files,
      cached: files.every(file => file.cached),
    };
  }

  controlOperation(operation) {
    const result = this.control.then(operation);
    this.control = result.catch(() => {});
    return result;
  }

  async abortChallengeMembers(challengeId) {
    const challenge = this.board.challenge(challengeId);
    for (const member of challenge.members) {
      try {
        await this.pool.abort(member.worker_id);
        await this.pool.waitIdle(member.worker_id);
        const worker = this.board.workers().find(item => item.id === member.worker_id);
        const context = worker?.execution_context ?? this.board.context(member.worker_id);
        this.board.execute("platform", "activity", {
          worker_id: member.worker_id,
          context,
          running: false,
        });
      } catch (error) {
        this.log({ type: "abandoned_environment_abort_failed", worker_id: member.worker_id, ...failure(error) });
      }
    }
  }

  async handleExternallyClosedEnvironment(challenge, connection, online) {
    const markerKey = `external_environment_closed:${challenge.id}:${challenge.generation}`;
    const previous = this.board.getMeta(markerKey);
    if (previous?.status === "handled") return previous;
    const detectedAt = previous?.detected_at ?? Date.now();
    if (!previous) {
      this.board.setMeta(markerKey, {
        status: "handling",
        online,
        detected_at: detectedAt,
      });
    }
    this.setEnvironment({
      challenge_id: challenge.id,
      status: "closed",
      ready: false,
      online,
      connection,
    });
    if (online) this.board.setMeta(`online_slot_release_notice:${challenge.id}:${challenge.generation}`, "external-close");
    await this.abortChallengeMembers(challenge.id);
    let deferred = false;
    let deferError = null;
    if (["pending", "assembling", "solving", "reviewing"].includes(challenge.phase)) {
      try {
        const current = this.board.challenge(challenge.id);
        await this.defer({
          challenge_id: challenge.id,
          expected_revision: current.revision,
          reason: "The competition environment was closed outside the coordinator. Preserve the work and return the task to Lead.",
          close_environment: false,
        }, `reconcile-closed-environment:${challenge.id}:${challenge.generation}`);
        deferred = true;
      } catch (error) {
        deferError = failure(error);
      }
    }
    const finalChallenge = this.board.challenge(challenge.id);
    const record = {
      status: "handled",
      online,
      deferred,
      released: finalChallenge.members.length === 0,
      ...(deferError ? { error: deferError } : {}),
      detected_at: detectedAt,
    };
    this.board.setMeta(markerKey, record);
    this.queueNotification("lead", "赛事环境已在平台外关闭。相关 Worker 已停止；平台已保存状态并尝试按受支持流程释放。"
      + JSON.stringify({
        challenge: challengeSummary(finalChallenge),
        released: record.released,
        deferred,
        needs_manual_recovery: !record.released,
        error: deferError,
      }), {
      eventId: `external-close:${challenge.id}:${challenge.generation}`,
      scope: challenge.id,
      version: finalChallenge.revision,
    });
    return record;
  }

  queueEnvironmentStartRejection(challenge, error, online) {
    const apiError = {
      code: error.code,
      message: error.message,
      status: error.status ?? null,
      detail: error.detail ?? null,
    };
    const isQuota = error.status === 409 || /limit|quota/i.test(`${error.code} ${error.message}`);
    if (!isQuota) return;
    const current = this.board.challenge(challenge.id);
    const activeOnline = this.challenges()
      .filter(item => item.environment.online === true
        && ["pending", "active", "unknown", "closing"].includes(item.environment.status))
      .map(challengeSummary);
    this.queueNotification("lead", "赛事 API 拒绝了环境启动。协调器保留了原始错误和当前状态，没有本地占用名额或改写配额。"
      + "请根据 active_online_environments 与 api_error 决定关闭、对账或选择其他题目。"
      + JSON.stringify({
        event: "environment_start_rejected",
        challenge: challengeSummary(current),
        online,
        api_error: apiError,
        active_online_environments: activeOnline,
      }), {
      eventId: `environment-start-rejected:${challenge.id}:${apiError.code}:${apiError.status}`,
      scope: challenge.id,
      version: current.revision,
    });
  }

  async startChallenge(id, { dispatchReady = true } = {}) {
    return this.controlOperation(async () => {
      let challenge = this.board.challenge(id);
      if (challenge.phase !== "pending" && !["assembling", "solving", "reviewing"].includes(challenge.phase)) {
        throw new Error("Start a pending or resumed challenge");
      }
      if (this.challenges().some(item => ["closing", "unknown"].includes(item.environment.status))) {
        await this.reconcileEnvironments();
        challenge = this.board.challenge(id);
      }
      const catalog = this.catalog.find(item => item.unique_code === challenge.unique_code);
      const online = catalogRequiresEnvironment(catalog, challenge.requires_online_environment !== false);
      if (challenge.environment.status === "active") {
        if (challenge.ready !== dispatchReady) {
          this.setEnvironment({ challenge_id: id, status: "active", ready: dispatchReady });
          challenge = this.board.challenge(id);
        }
        return { challenge, receipt: challenge.environment.connection };
      }
      if (challenge.environment.status === "closed") {
        this.board.setMeta(`environment_generation:${id}`, challenge.generation + 1);
      }
      this.board.setMeta(`environment_start:${id}`, { time: Date.now() });
      this.board.execute("platform", "set_environment", { challenge_id: id, ready: false, status: "pending", online });
      let receipt;
      let state;
      try {
        if (this.config.arena.mode === "connectivity_test") {
          receipt = catalog;
          state = "active";
        } else {
          receipt = await this.arena.start(challenge.unique_code, { signal: this.stopController.signal });
          state = environmentState(receipt);
        }
      }
      catch (error) {
        const connection = {
          code: error.code ?? "operation_failed",
          message: error.message,
          status: error.status ?? null,
          detail: error.detail ?? null,
          outcome_unknown: error.outcomeUnknown ?? false,
        };
        this.board.execute("platform", "set_environment", { challenge_id: id, ready: false,
          status: error.outcomeUnknown ? "unknown" : "closed", online, connection });
        this.queueEnvironmentStartRejection(challenge, error, online);
        throw error;
      }
      if (state === "unknown") {
        const current = (await this.refreshCatalog()).find(item => item.unique_code === challenge.unique_code);
        state = environmentState(current);
        if (state === "active") receipt = current;
      }
      this.setEnvironment({
        challenge_id: id, ready: state === "active" && dispatchReady, status: state,
        online: catalogRequiresEnvironment(receipt, online), connection: receipt,
      });
      challenge = this.board.challenge(id);
      return { challenge, receipt };
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
      if (!["pending", "active", "unknown", "closing", "closed"].includes(challenge.environment.status)) continue;
      if (challenge.environment.status === "pending" && !this.board.getMeta(`environment_start:${challenge.id}`)) continue;
      const actual = catalog.find(item => item.unique_code === challenge.unique_code);
      const status = environmentState(actual);
      const online = catalogRequiresEnvironment(actual, challenge.requires_online_environment !== false);
      if (status === "unknown") {
        if (challenge.ready || challenge.environment.status !== "unknown") {
          this.setEnvironment({
            challenge_id: challenge.id,
            status: "unknown",
            ready: false,
            online,
            connection: actual,
          });
        }
        results.push({ challenge_id: challenge.id, status, ready: false });
        continue;
      }
      if (status === "closed" && !["completed", "deferred"].includes(challenge.phase)) {
        const handled = await this.handleExternallyClosedEnvironment(challenge, actual, online);
        results.push({ challenge_id: challenge.id, status,
          deferred: this.board.challenge(challenge.id).phase === "deferred",
          released: handled?.released ?? this.board.challenge(challenge.id).members.length === 0 });
        continue;
      }
      const ready = status === "active" && !["completed", "deferred"].includes(challenge.phase);
      const changed = challenge.environment.status !== status
        || challenge.environment.online !== online
        || challenge.ready !== ready
        || JSON.stringify(challenge.environment.connection) !== JSON.stringify(actual);
      if (changed) {
        this.setEnvironment({ challenge_id: challenge.id, status, ready, online, connection: actual });
      }
      results.push({ challenge_id: challenge.id, status, ready });
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
    return this.saveListedFile(challenge, path);
  }

  async saveListedFile(challenge, path) {
    const directory = join(await this.challengeDirectory(challenge.id), "inputs");
    const digest = createHash("sha256").update(path).digest("hex").slice(0, 16);
    const destination = join(directory, `g${challenge.generation}-${digest}-${basename(path)}`);
    let previous = null;
    try {
      previous = await fileMetadata(destination);
    } catch (error) { if (error.code !== "ENOENT") throw error; }
    const response = await this.arena.readFile(challenge.unique_code, path, { signal: this.stopController.signal });
    if (!response.body) throw new Error("Handout response has no body");
    const temporary = `${destination}.${randomUUID()}.partial`;
    try {
      await pipeline(Readable.fromWeb(response.body), createWriteStream(temporary, { flags: "wx", mode: 0o600 }));
      const downloaded = await fileMetadata(temporary);
      if (previous?.size === downloaded.size && previous.sha256 === downloaded.sha256) {
        await unlink(temporary);
        return { path: destination, source_path: path, size: downloaded.size,
          sha256: downloaded.sha256, cached: true, changed: false };
      }
      await rename(temporary, destination);
      if (this.board.getMeta(preparationMarkerKey(challenge))) {
        await this.invalidatePreparedInputs(challenge, "attachment_content_changed");
      }
      return { path: destination, source_path: path, size: downloaded.size,
        sha256: downloaded.sha256, cached: false, changed: true };
    } catch (error) { await unlink(temporary).catch(() => {}); throw error; }
  }

  catalogDifficulty(item) {
    if (["easy", "medium", "hard"].includes(item.difficulty)) return item.difficulty;
    if (item.multi_stage === true) return "hard";
    const numeric = Number(item.difficulty);
    if (Number.isFinite(numeric)) {
      if (numeric <= 1) return "easy";
      if (numeric <= 2) return "medium";
      return "hard";
    }
    return "medium";
  }

  async prepareOffline() {
    await this.refreshCatalog();
    const results = [];
    const offline = this.catalog.filter(item => catalogRequiresEnvironment(item) === false && !challengeCompleted(item));
    for (const item of offline) {
      try {
        let challenge = this.challenges().find(value => value.unique_code === item.unique_code);
        if (!challenge) {
          challenge = this.board.execute("lead", "create_challenge", {
            request_id: `prepare-offline:${item.unique_code}`,
            unique_code: item.unique_code,
            title: item.name ?? item.title ?? item.unique_code,
            description: item.description ?? "",
            difficulty: this.catalogDifficulty(item),
            multi_stage: item.multi_stage === true,
            priority: 0,
            ready: false,
            requires_online_environment: false,
          });
        }
        if (challenge.phase === "deferred" || challenge.phase === "completed") {
          const validation = await this.validatePreparedInputs(challenge);
          if (!validation.valid) throw Object.assign(new Error("Deferred offline inputs are not valid"), {
            code: "invalid_prepared_inputs",
            details: validation,
          });
          results.push({ status: challenge.phase, challenge_id: challenge.id, unique_code: item.unique_code,
            files: validation.files ?? [] });
          continue;
        }
        await this.startChallenge(challenge.id, { dispatchReady: false });
        challenge = this.board.challenge(challenge.id);
        if (challenge.environment.status !== "active") {
          throw Object.assign(new Error("The offline environment is not ready yet"), {
            code: "offline_environment_pending",
            details: { challenge_id: challenge.id, status: challenge.environment.status },
          });
        }
        const prepared = await this.prepareChallengeInputs(challenge, { markReady: true, refresh: true });
        const files = prepared.files;
        const directory = await this.challengeDirectory(challenge.id);
        results.push({
          status: "ready",
          challenge_id: challenge.id,
          unique_code: item.unique_code,
          difficulty: this.board.challenge(challenge.id).difficulty,
          challenge_root: directory,
          inputs: join(directory, "inputs"),
          files,
        });
      } catch (error) {
        results.push({
          status: "error",
          unique_code: item.unique_code,
          error: failure(error),
        });
      }
    }
    return {
      offline_total: offline.length,
      prepared: results.filter(item => item.status === "ready").length,
      failed: results.filter(item => item.status === "error").length,
      tasks: results,
    };
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
      tool("board_act", "Update the blackboard. request_id is automatic. Priority is 0 highest through 3 lowest. "
        + "claim: challenge_id,expected_revision. "
        + "create_challenge: unique_code,title,description,difficulty(easy|medium|hard),multi_stage,priority. "
        + "update_challenge: challenge_id,expected_revision plus title,description,difficulty,multi_stage or priority. "
        + "post_fact: challenge_id,expected_revision,statement,kind(observation|inference|structure),"
        + "evidence[{type:text,summary}|{artifact_id,summary}],derived_from; close held Intent with "
        + "resolves:intent_id,result:confirmed|rejected|inconclusive. "
        + "post_intent: challenge_id,expected_revision,statement,based_on:[fact_id],expected,method, "
        + "optional claim/parallel/parallel_key/depends_on/retry_of. "
        + "claim_intent: challenge_id,expected_revision,intent_id,expected_version. "
        + "release_intent: challenge_id,expected_revision,intent_id,note. "
        + "request_assistance: challenge_id,expected_revision,reason,additional_workers. "
        + "request_deferral: challenge_id,expected_revision,reason. "
        + "submit_review: challenge_id,expected_revision,flag,summary,fact_refs. "
        + "reject_review: challenge_id,expected_revision,review_revision,reason. "
        + "Read the returned revision before another task write.",
        Type.Object({ action: Type.Union((role === "lead" ? LEAD_ACTIONS : WORKER_ACTIONS).map(action => Type.Literal(action))), args: Type.Record(Type.String(), Type.Unknown()) }),
        (args, execution) => {
          if (args.action === "create_challenge" && !this.catalog?.some(item => item.unique_code === args.args.unique_code)) throw new Error("Create tasks using an actual code from arena_list");
          const input = { ...args.args, request_id: execution.callId };
          if (args.action === "create_challenge") {
            const catalog = this.catalog.find(item => item.unique_code === input.unique_code);
            input.ready = false;
            input.description ??= catalog.description;
            input.requires_online_environment = catalogRequiresEnvironment(catalog);
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
      tool("arena_list", "Fetch the actual competition challenge catalog and progress. Use container=false to identify offline challenges. Offline-first order is a Lead strategy; the coordinator does not enforce it as a local gate.",
        Type.Object({}), () => this.refreshCatalog()),
      tool("arena_reconcile", "Read official progress to resolve pending environment outcomes and flag receipts.", Type.Object({}), async () => ({
        environments: await this.reconcileEnvironments(), submissions: await this.reconcilePendingSubmissions(),
      })),
      tool("arena_prepare_offline", "Explicitly prepare unfinished offline challenges in one call. It creates missing board tasks, starts them, downloads every listed attachment into the shared inputs directory, writes a generation-bound SHA-256 manifest, and marks tasks ready only after preparation. Idempotent; retry failed tasks.",
        Type.Object({}), () => this.prepareOffline()),
      tool("arena_start", "Start one created task through the official API. The coordinator does not enforce local offline prerequisites, online instance counts, or quota policy. API errors such as environment_limit or quota failures are preserved for Lead. Pending environments need a later retry.",
        challengeParameter, args => this.startChallenge(args.challenge_id)),
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
