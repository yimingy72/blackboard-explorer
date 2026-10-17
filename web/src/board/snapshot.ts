import type {
  BoardAcceptance,
  BoardAgent,
  BoardFact,
  BoardIntent,
  BoardState,
  BoardTask,
  TaskStatus,
} from './types';

type JsonObject = Record<string, unknown>;

export interface BoardSnapshot {
  task: JsonObject;
  facts: Record<string, JsonObject>;
  intents: Record<string, JsonObject>;
  agents: Record<string, JsonObject>;
  last_change_version?: number;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function nullableText(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function amount(value: unknown): number {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

function usage(value: unknown): Record<string, number> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value as JsonObject).map(([key, item]) => [key, amount(item)]),
  );
}

function taskFromSnapshot(task: JsonObject): BoardTask {
  const acceptance = Array.isArray(task.acceptance)
    ? task.acceptance
        .filter((item): item is JsonObject => Boolean(item) && typeof item === 'object')
        .map((item) => ({ id: text(item.id), desc: text(item.desc) }))
    : [];
  return {
    name: nullableText(task.name),
    initialAttachments: Array.isArray(task.initial_attachments)
      ? (task.initial_attachments as BoardTask['initialAttachments'])
      : [],
    goal: text(task.goal),
    status: text(task.status) as TaskStatus,
    domain_context: nullableText(task.domain_context),
    acceptance,
    budget: (task.budget ?? {}) as Record<string, unknown>,
    usage: usage(task.usage),
    report_uri: nullableText(task.report_uri),
    workspace_uri: nullableText(task.workspace_uri),
    fail_reason: nullableText(task.fail_reason),
    version: amount(task.version ?? task.last_change_version),
    params: (task.params ?? {}) as Record<string, unknown>,
    closingReason: nullableText(task.closing_reason),
    startedAt: nullableText(task.started_at),
    finishedAt: nullableText(task.finished_at),
    runNumber: amount(task.run_number) || 1,
    activeSeconds: amount(task.active_seconds),
    activeSince: nullableText(task.active_since),
  };
}

function acceptanceFromSnapshot(task: JsonObject): Record<string, BoardAcceptance> {
  const descriptions = new Map<string, string>();
  if (Array.isArray(task.acceptance)) {
    for (const item of task.acceptance) {
      if (!item || typeof item !== 'object') continue;
      const value = item as JsonObject;
      descriptions.set(text(value.id), text(value.desc));
    }
  }
  const states = (task.acceptance_state ?? {}) as Record<string, JsonObject>;
  return Object.fromEntries(
    [...descriptions].map(([id, desc]) => {
      const value = states[id] ?? {};
      return [
        id,
        {
          id,
          desc,
          status: value.status === 'met' ? 'met' : 'unmet',
          reason: nullableText(value.reason),
          missing: nullableText(value.missing),
          evidence_facts: strings(value.evidence_facts),
          judged_version:
            typeof value.judged_version === 'number' ? value.judged_version : null,
          completion_basis: text(value.completion_basis) || 'inferred',
          completion_reason: nullableText(value.completion_reason),
        } satisfies BoardAcceptance,
      ];
    }),
  );
}

export function boardFromSnapshot(snapshot: BoardSnapshot): { state: BoardState; version: number } {
  const task = taskFromSnapshot(snapshot.task ?? {});
  const facts = Object.fromEntries(
    Object.entries(snapshot.facts ?? {}).map(([id, value]) => [
      id,
      {
        id,
        version: amount(value.version),
        kind: text(value.kind) as BoardFact['kind'],
        statement: text(value.statement),
        author: text(value.author),
        provenance: text(value.provenance) as BoardFact['provenance'],
        status: value.status === 'disputed' ? 'disputed' : 'proposed',
        reliedBy: amount(value.relied_by),
        derivedFrom: strings(value.derived_from),
        disputes: strings(value.disputes),
        resolves: nullableText(value.resolves),
        result: (value.result ?? null) as BoardFact['result'],
        satisfies: strings(value.satisfies),
        evidence: Array.isArray(value.evidence) ? value.evidence : [],
      } satisfies BoardFact,
    ]),
  );
  const intents = Object.fromEntries(
    Object.entries(snapshot.intents ?? {}).map(([id, value]) => [
      id,
      {
        id,
        version: amount(value.version),
        statement: text(value.statement),
        basedOn: strings(value.based_on),
        expected: text(value.expected),
        method: text(value.method),
        relatesTo: strings(value.relates_to),
        retryOf: nullableText(value.retry_of),
        author: text(value.author),
        status: ['open', 'claimed', 'closed'].includes(text(value.status))
          ? (text(value.status) as BoardIntent['status'])
          : 'open',
        holder: nullableText(value.holder),
        result: (value.result ?? null) as BoardIntent['result'],
        closedBy: nullableText(value.closed_by),
        resultFacts: strings(value.result_facts),
        attempts: amount(value.attempts),
        notes: Array.isArray(value.notes) ? (value.notes as BoardIntent['notes']) : [],
      } satisfies BoardIntent,
    ]),
  );
  const agents = Object.fromEntries(
    Object.entries(snapshot.agents ?? {}).map(([id, value]) => [
      id,
      {
        id,
        taskType: text(value.task_type) as BoardAgent['taskType'],
        isSeed: value.is_seed === true,
        deriveParallel: value.derive_parallel === true,
        deriveReview: value.derive_review === true,
        closeMode: (value.close_mode ?? null) as BoardAgent['closeMode'],
        status: text(value.status) as BoardAgent['status'],
        steps: amount(value.steps),
        contextTokens: amount(value.context_tokens),
        intentId: nullableText(value.intent_id),
        usage: usage(value.usage),
        lastSeenVersion: amount(value.last_seen_version),
        graceCallsLeft:
          typeof value.grace_calls_left === 'number' ? value.grace_calls_left : null,
        endReason: nullableText(value.end_reason),
        receipt: value.receipt ?? null,
        concludeReason: nullableText(value.conclude_reason),
        startedAt: nullableText(value.started_at),
        finishedAt: nullableText(value.finished_at),
      } satisfies BoardAgent,
    ]),
  );
  const version = Math.max(
    amount(snapshot.task?.version),
    amount(snapshot.last_change_version),
    0,
  );
  return {
    version,
    state: {
      task,
      facts,
      intents,
      agents,
      acceptance: acceptanceFromSnapshot(snapshot.task ?? {}),
      toolCalls: {},
    },
  };
}
