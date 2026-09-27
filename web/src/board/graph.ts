import { MarkerType, type Edge, type Node } from '@xyflow/react';

import type { BoardState } from './types';
import { agentColors, agentLabel, agentStyle } from './agents';

export type GraphRelation =
  | 'derived_from'
  | 'based_on'
  | 'resolves'
  | 'disputes'
  | 'retry_of'
  | 'claim';

export interface GraphNodeData extends Record<string, unknown> {
  label: string;
  ownerLabel?: string;
  ownerAccent?: string;
  version?: number;
  status?: string;
  kind?: string;
  author?: string;
  provenance?: string;
  reliedBy?: number;
  attempts?: number;
  holder?: string | null;
  result?: string | null;
  isSeed?: boolean;
  taskType?: string;
  intentId?: string | null;
  steps?: number;
  contextTokens?: number;
  acceptance?: Array<{ id: string; status: 'met' | 'unmet' }>;
}

export interface GraphEdgeData extends Record<string, unknown> {
  relation: GraphRelation;
}

export type GraphNode = Node<GraphNodeData, 'goal' | 'fact' | 'intent' | 'agent'>;
export type GraphEdge = Edge<GraphEdgeData>;

export function deriveGraph(state: BoardState, numbers: Record<string, number> = {}): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  const edge = (relation: GraphRelation, source: string, target: string, color: string, dash?: string) => {
    edges.push({
      id: `${relation}:${source}:${target}`,
      source,
      target,
      type: 'smoothstep',
      data: { relation },
      style: { stroke: color, strokeWidth: relation === 'disputes' ? 2 : 1.5, strokeDasharray: dash },
      markerEnd: { type: MarkerType.ArrowClosed, color },
    });
  };

  if (state.task) {
    nodes.push({
      id: 'goal',
      type: 'goal',
      position: { x: 0, y: 0 },
      data: {
        label: state.task.goal,
        status: state.task.status,
        acceptance: state.task.acceptance.map((item) => ({
          id: item.id,
          status: state.acceptance[item.id]?.status ?? 'unmet',
        })),
      },
    });
  }

  Object.values(state.facts).forEach((fact, index) => {
    nodes.push({
      id: fact.id,
      type: 'fact',
      position: { x: 320, y: index * 120 },
      className: [
        `fact-${fact.kind}`,
        fact.status === 'disputed' ? 'fact-disputed' : '',
        fact.provenance === 'self_reported' ? 'fact-self-reported' : '',
      ].filter(Boolean).join(' '),
      data: {
        label: fact.statement,
        version: fact.version,
        status: fact.status,
        kind: fact.kind,
        author: fact.author,
        provenance: fact.provenance,
        reliedBy: fact.reliedBy,
      },
    });
    for (const source of fact.derivedFrom) {
      if (state.facts[source]) edge('derived_from', source, fact.id, 'var(--color-subtle)');
    }
    for (const target of fact.disputes) {
      if (state.facts[target]) edge('disputes', fact.id, target, 'var(--color-danger)');
    }
    if (fact.resolves && state.intents[fact.resolves]) {
      const color = fact.result === 'confirmed'
        ? 'var(--color-success)'
        : fact.result === 'rejected'
          ? 'var(--color-danger)'
          : 'var(--color-subtle)';
      edge('resolves', fact.resolves, fact.id, color);
    }
  });

  Object.values(state.intents).forEach((intent, index) => {
    nodes.push({
      id: intent.id,
      type: 'intent',
      position: { x: 640, y: index * 140 },
      className: `intent-${intent.status}`,
      data: {
        label: intent.statement,
        version: intent.version,
        status: intent.status,
        author: intent.author,
        attempts: intent.attempts,
        holder: intent.holder ? agentLabel(intent.holder, numbers) : null,
        result: intent.result,
      },
    });
    for (const source of intent.basedOn) {
      if (state.facts[source]) edge('based_on', source, intent.id, 'var(--color-subtle)', '5 4');
    }
    if (intent.retryOf && state.intents[intent.retryOf]) {
      edge('retry_of', intent.retryOf, intent.id, 'var(--color-subtle)', '5 4');
    }
  });

  Object.values(state.agents)
    .filter((agent) => agent.status === 'running' || agent.status === 'concluding')
    .forEach((agent, index) => {
      nodes.push({
        id: agent.id,
        type: 'agent',
        position: { x: 960, y: index * 80 },
        className: `agent-${agent.status}`,
        data: {
          label: agentLabel(agent.id, numbers),
          status: agent.status,
          isSeed: agent.isSeed,
          taskType: agent.taskType,
          intentId: agent.intentId,
          steps: agent.steps,
          contextTokens: agent.contextTokens,
        },
      });
      if (agent.intentId && state.intents[agent.intentId]) {
        edge('claim', agent.id, agent.intentId, 'var(--color-muted)', '2 4');
      }
    });

  for (const node of nodes) {
    const owner = node.type === 'agent' ? node.id : node.data.author;
    if (owner && numbers[owner]) {
      node.style = agentStyle(numbers[owner]);
      node.data.ownerLabel = agentLabel(owner, numbers);
      node.data.ownerAccent = agentColors(numbers[owner]).accent;
    }
  }
  return { nodes, edges };
}
