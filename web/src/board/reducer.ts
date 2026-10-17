import type {
  BoardAcceptance,
  BoardAgent,
  BoardEvent,
  BoardFact,
  BoardIntent,
  BoardState,
  BoardTask,
} from './types';

type Payload = Record<string, unknown>;

export const emptyBoard = (): BoardState => ({
  task: null,
  facts: {},
  intents: {},
  agents: {},
  acceptance: {},
  toolCalls: {},
});

export function orderedEvents(events: readonly BoardEvent[]): BoardEvent[] {
  const versions = new Map<number, BoardEvent>();
  for (const event of events) {
    if (!versions.has(event.version)) versions.set(event.version, event);
  }
  return [...versions.values()].sort((left, right) => left.version - right.version);
}

function payload(event: BoardEvent): Payload {
  return (event.payload ?? {}) as Payload;
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

function numbers(value: unknown): Record<string, number> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value).map(([key, amount]) => [key, Number(amount) || 0]),
  );
}

export function advanceBoard(previous: BoardState, events: readonly BoardEvent[]): BoardState {
  const state = { ...previous, task: previous.task ? { ...previous.task } : null };
  const factMap = () => { if (state.facts === previous.facts) state.facts = { ...state.facts }; };
  const intentMap = () => { if (state.intents === previous.intents) state.intents = { ...state.intents }; };
  const agentMap = () => { if (state.agents === previous.agents) state.agents = { ...state.agents }; };
  const acceptanceMap = () => { if (state.acceptance === previous.acceptance) state.acceptance = { ...state.acceptance }; };
  const factAt = (id: string) => {
    if (!state.facts[id]) return undefined;
    factMap(); return state.facts[id] = { ...state.facts[id] };
  };
  const intentAt = (id: string) => {
    if (!state.intents[id]) return undefined;
    intentMap(); const value = state.intents[id];
    return state.intents[id] = { ...value, resultFacts: [...value.resultFacts] };
  };
  const agentAt = (id: string) => {
    if (!state.agents[id]) return undefined;
    agentMap(); const value = state.agents[id];
    return state.agents[id] = { ...value, usage: { ...value.usage } };
  };
  const acceptanceAt = (id: string) => {
    if (!state.acceptance[id]) return undefined;
    acceptanceMap(); return state.acceptance[id] = { ...state.acceptance[id] };
  };
  for (const event of events) {
    const p = payload(event);
    switch (event.type) {
      case 'task.created': {
        const acceptance = (p.acceptance ?? []) as Array<{ id: string; desc: string }>;
        const initial = (p.acceptance_state ?? {}) as Record<
          string,
          Partial<BoardAcceptance>
        >;
        state.task = {
          name: nullableText(p.name),
          initialAttachments: (p.initial_attachments ?? []) as import('../api/client').InitialAttachment[],
          goal: text(p.goal),
          status: 'created',
          domain_context: nullableText(p.domain_context),
          acceptance,
          budget: (p.budget ?? {}) as Record<string, unknown>,
          usage: numbers(p.usage),
          report_uri: null,
          workspace_uri: null,
          fail_reason: null,
          version: event.version,
          params: (p.params ?? {}) as Record<string, unknown>,
          closingReason: null,
          startedAt: null,
          finishedAt: null,
          runNumber: 1,
          activeSeconds: 0,
          activeSince: null,
        };
        for (const item of acceptance) {
          const saved = initial[item.id];
          acceptanceMap();
          state.acceptance[item.id] = {
            id: item.id,
            desc: item.desc,
            status: saved?.status ?? 'unmet',
            reason: saved?.reason ?? null,
            missing: saved?.missing ?? null,
            evidence_facts: saved?.evidence_facts ?? [],
            judged_version: saved?.judged_version ?? null,
            completion_basis: saved?.completion_basis ?? 'inferred',
            completion_reason: saved?.completion_reason ?? null,
          };
        }
        break;
      }
      case 'task.provisioning':
      case 'task.running':
      case 'task.closing':
      case 'task.finished':
      case 'task.failed':
      case 'task.stopped':
        if (state.task) {
          state.task.status = event.type.split('.')[1] as BoardTask['status'];
          if (event.type === 'task.failed') state.task.fail_reason = nullableText(p.reason);
          if (event.type === 'task.running') {
            state.task.startedAt ??= event.created_at;
            state.task.activeSince = event.created_at;
          }
          if (event.type === 'task.closing' || event.type === 'task.stopped') state.task.closingReason = nullableText(p.reason);
          if (['task.finished', 'task.failed', 'task.stopped'].includes(event.type)) {
            state.task.finishedAt = event.created_at;
            if (state.task.activeSince) {
              state.task.activeSeconds = (state.task.activeSeconds ?? 0) + Math.max(0,
                Math.floor((Date.parse(event.created_at) - Date.parse(state.task.activeSince)) / 1000));
              state.task.activeSince = null;
            }
          }
        }
        break;
      case 'task.resumed':
        if (state.task) {
          state.task.status = 'provisioning';
          state.task.runNumber = Number(p.run_number) || 1;
          state.task.budget = (p.budget ?? state.task.budget) as Record<string, unknown>;
          state.task.report_uri = null;
          state.task.workspace_uri = null;
          state.task.finishedAt = null;
          state.task.fail_reason = null;
          state.task.closingReason = null;
        }
        break;
      case 'task.report':
        if (state.task) state.task.report_uri = nullableText(p.uri);
        break;
      case 'task.archived':
        if (state.task) state.task.workspace_uri = nullableText(p.uri);
        break;
      case 'budget.updated':
        if (state.task) {
          state.task.usage = numbers(p.usage);
          if (p.budget && typeof p.budget === 'object') state.task.budget = p.budget as Record<string, unknown>;
        }
        break;
      case 'cost.reconciled':
        if (state.task) state.task.usage = { ...state.task.usage, cost: Number(p.cost) || 0 };
        if (p.agent_costs && typeof p.agent_costs === 'object') {
          for (const [id, cost] of Object.entries(p.agent_costs)) {
            const agent = agentAt(id);
            if (agent) agent.usage.cost = Number(cost) || 0;
          }
        }
        break;
      case 'fact.posted': {
        const id = text(p.id);
        const derivedFrom = strings(p.derived_from);
        const author = text(p.author);
        const fact: BoardFact = {
          id,
          version: event.version,
          kind: p.kind as BoardFact['kind'],
          statement: text(p.statement),
          author,
          provenance: p.provenance as BoardFact['provenance'],
          status: 'proposed',
          reliedBy: 0,
          derivedFrom,
          disputes: strings(p.disputes),
          resolves: nullableText(p.resolves),
          result: (p.result ?? null) as BoardFact['result'],
          satisfies: strings(p.satisfies),
          evidence: Array.isArray(p.evidence) ? p.evidence : [],
        };
        factMap();
        state.facts[id] = fact;
        for (const sourceId of new Set(derivedFrom)) {
          const source = factAt(sourceId);
          if (source && source.author !== author) source.reliedBy += 1;
        }
        break;
      }
      case 'fact.disputed':
      case 'fact.undisputed': {
        const fact = factAt(text(p.fact_id));
        if (fact) fact.status = event.type === 'fact.disputed' ? 'disputed' : 'proposed';
        break;
      }
      case 'intent.posted': {
        const id = text(p.id);
        const claimed = p.claim === true;
        const author = text(p.author);
        intentMap();
        state.intents[id] = {
          id,
          version: event.version,
          statement: text(p.statement),
          basedOn: strings(p.based_on),
          expected: text(p.expected),
          method: text(p.method),
          relatesTo: strings(p.relates_to),
          retryOf: nullableText(p.retry_of),
          author,
          status: claimed ? 'claimed' : 'open',
          holder: claimed ? author : null,
          result: null,
          closedBy: null,
          resultFacts: [],
          attempts: 0,
          notes: [],
        };
        if (claimed) { const agent = agentAt(author); if (agent) agent.intentId = id; }
        break;
      }
      case 'intent.claimed': {
        const id = text(p.intent_id);
        const intent = intentAt(id);
        const holder = text(p.holder);
        if (intent) {
          intent.status = 'claimed';
          intent.holder = holder;
        }
        const agent = agentAt(holder); if (agent) agent.intentId = id;
        break;
      }
      case 'intent.released': {
        const intent = intentAt(text(p.intent_id));
        const holder = text(p.holder);
        if (intent) {
          intent.status = 'open';
          intent.holder = null;
          if (p.counted === true) intent.attempts += 1;
          intent.notes = [...(intent.notes ?? []), { by: holder, at: event.created_at, text: text(p.note) }];
        }
        const agent = agentAt(holder); if (agent) agent.intentId = null;
        break;
      }
      case 'intent.closed': {
        const intent = intentAt(text(p.intent_id));
        if (intent) {
          if (intent.holder && state.agents[intent.holder]) {
            const agent = agentAt(intent.holder); if (agent) agent.intentId = null;
          }
          intent.status = 'closed';
          intent.holder = null;
          intent.result = p.result as BoardIntent['result'];
          intent.closedBy = nullableText(p.by);
          if (intent.closedBy && intent.closedBy !== 'system') {
            intent.resultFacts.push(intent.closedBy);
          }
        }
        break;
      }
      case 'agent.spawned': {
        const id = text(p.id);
        const agent: BoardAgent = {
          id,
          taskType: p.task_type as BoardAgent['taskType'],
          isSeed: p.is_seed === true,
          deriveParallel: p.derive_parallel === true,
          deriveReview: p.derive_review === true,
          closeMode: (p.close_mode ?? null) as BoardAgent['closeMode'],
          status: 'running',
          steps: 0,
          contextTokens: 0,
          intentId: null,
          usage: {},
          lastSeenVersion: 0,
          graceCallsLeft: null,
          endReason: null,
          receipt: null,
          concludeReason: null,
          startedAt: event.created_at,
          finishedAt: null,
        };
        agentMap();
        state.agents[id] = agent;
        break;
      }
      case 'agent.progress': {
        const agent = agentAt(text(p.agent_id));
        if (agent) {
          if (p.usage_known === false) { agent.usageIncomplete = true; if (state.task) state.task.usageIncomplete = true; }
          if (p.accounting_only !== true && typeof p.steps === 'number') agent.steps += p.steps;
          if (p.accounting_only !== true && typeof p.context_tokens === 'number') agent.contextTokens = p.context_tokens;
          if (p.accounting_only !== true && typeof p.last_seen_version === 'number') {
            agent.lastSeenVersion = Math.max(agent.lastSeenVersion, p.last_seen_version);
          }
          if (p.usage) {
            for (const [key, amount] of Object.entries(numbers(p.usage))) {
              agent.usage[key] = (agent.usage[key] ?? 0) + amount;
            }
          }
          if (p.accounting_only !== true && typeof p.grace_left === 'number') agent.graceCallsLeft = p.grace_left;
        }
        break;
      }
      case 'agent.reactivated': {
        const agent = agentAt(text(p.id));
        if (agent) {
          agent.status = 'running';
          agent.deriveParallel = p.derive_parallel === true;
          agent.deriveReview = p.derive_review === true;
          agent.intentId = null;
          agent.endReason = null;
          agent.receipt = null;
          agent.concludeReason = null;
          agent.graceCallsLeft = null;
          agent.startedAt = event.created_at;
          agent.finishedAt = null;
        }
        break;
      }
      case 'agent.conclude_requested': {
        const agent = agentAt(text(p.agent_id));
        if (agent) { agent.status = 'concluding'; agent.concludeReason = nullableText(p.reason); }
        break;
      }
      case 'agent.finished': {
        const agent = agentAt(text(p.agent_id));
        if (agent) {
          agent.status = p.end_reason === 'runtime_error' ? 'failed' : 'finished';
          agent.endReason = nullableText(p.end_reason);
          agent.intentId = null;
          agent.receipt = p.receipt ?? null;
          agent.finishedAt = event.created_at;
        }
        break;
      }
      case 'tool_call.recorded': {
        const id = text(p.id);
        if (state.toolCalls === previous.toolCalls) state.toolCalls = { ...state.toolCalls };
        state.toolCalls ??= {};
        state.toolCalls[id] = {
          id, agentId: text(p.agent_id), tool: text(p.tool),
          args: (p.args ?? {}) as Record<string, unknown>,
          resultHead: text(p.result_head), resultUri: nullableText(p.result_uri),
          createdAt: event.created_at, version: event.version,
        };
        break;
      }
      case 'acceptance.judged': {
        const verdicts = (p.verdicts ?? []) as Array<{
          id: string;
          verdict: BoardAcceptance['status'];
          reason: string;
          missing?: string | null;
          evidence_facts?: string[];
          completion_basis?: string;
          completion_reason?: string | null;
        }>;
        for (const verdict of verdicts) {
          const item = acceptanceAt(verdict.id);
          if (!item) continue;
          item.status = verdict.verdict;
          item.reason = verdict.reason;
          item.missing = verdict.missing ?? null;
          item.evidence_facts = verdict.evidence_facts ?? [];
          item.judged_version = typeof p.judge_from_version === 'number' ? p.judge_from_version : null;
          item.completion_basis = verdict.completion_basis === 'explicit' ? 'explicit' : 'inferred';
          item.completion_reason = typeof verdict.completion_reason === 'string' ? verdict.completion_reason : null;
        }
        break;
      }
      case 'acceptance.reverted': {
        const item = acceptanceAt(text(p.id));
        if (item) {
          item.status = 'unmet';
          item.missing = `支撑事实 ${text(p.fact_id)} 被争议`;
          item.evidence_facts = [];
        }
        break;
      }
    }
    if (state.task) state.task.version = event.version;
  }
  return state;
}

export function reduce(events: readonly BoardEvent[]): BoardState {
  return advanceBoard(emptyBoard(), orderedEvents(events));
}

export const reduceBoard = reduce;
