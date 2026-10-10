import type { ELK } from 'elkjs/lib/elk.bundled.js'

import type { GraphEdge, GraphNode } from '../board/graph'

let engine: Promise<ELK> | undefined
function loadEngine(): Promise<ELK> {
  return engine ??= import('elkjs/lib/elk.bundled.js').then(({ default: ELK }) => new ELK()).catch((error: unknown) => { engine = undefined; throw error })
}

export const NODE_SIZE: Record<string, { width: number; height: number }> = {
  goal: { width: 260, height: 176 },
  fact: { width: 280, height: 144 },
  intent: { width: 280, height: 164 },
  agent: { width: 152, height: 68 },
}

const sizeOf = (node: GraphNode) => NODE_SIZE[node.type] ?? NODE_SIZE.fact
const versionOf = (node: GraphNode) => Number(node.data.version ?? 0)

function linksOf(id: string, edges: GraphEdge[]): string {
  return edges
    .filter((edge) => edge.source === id || edge.target === id)
    .map((edge) => `${edge.source}:${edge.target}:${String(edge.data?.relation ?? '')}`)
    .sort()
    .join('|')
}

function overlaps(a: GraphNode, b: GraphNode): boolean {
  const first = sizeOf(a)
  const second = sizeOf(b)
  const gap = 18
  return (
    a.position.x < b.position.x + second.width + gap &&
    a.position.x + first.width + gap > b.position.x &&
    a.position.y < b.position.y + second.height + gap &&
    a.position.y + first.height + gap > b.position.y
  )
}

export function sameTopology(
  nodes: GraphNode[],
  edges: GraphEdge[],
  previousNodes: GraphNode[],
  previousEdges: GraphEdge[],
): boolean {
  return (
    nodes.length === previousNodes.length &&
    edges.length === previousEdges.length &&
    nodes.every((node) => previousNodes.some((previous) => previous.id === node.id)) &&
    edges.every((edge) => previousEdges.some((previous) => previous.id === edge.id))
  )
}

export async function layoutGraph(
  nodes: GraphNode[],
  edges: GraphEdge[],
  previousNodes: GraphNode[] = [],
  previousEdges: GraphEdge[] = [],
): Promise<GraphNode[]> {
  if (!nodes.length) return []

  const ordered = [...nodes].sort((a, b) => versionOf(a) - versionOf(b) || a.id.localeCompare(b.id))
  const nodeIds = new Set(nodes.map((node) => node.id))
  const elk = await loadEngine()
  const layout = await elk.layout({
    id: 'board',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.spacing.nodeNode': '54',
      'elk.layered.spacing.nodeNodeBetweenLayers': '100',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
    },
    children: ordered.map((node) => ({ id: node.id, ...sizeOf(node) })),
    edges: edges
      .filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
      .map((edge) => ({ id: edge.id, sources: [edge.source], targets: [edge.target] })),
  })

  const children = new Map(layout.children?.map((child) => [child.id, child]))
  const otherX = Math.min(
    ...nodes.filter((node) => node.type !== 'goal').map((node) => children.get(node.id)?.x ?? 0),
  )
  const prior = new Map(previousNodes.map((node) => [node.id, node]))
  const placed: GraphNode[] = []
  const pending: GraphNode[] = []

  for (const node of ordered) {
    const old = prior.get(node.id)
    if (old && linksOf(node.id, edges) === linksOf(node.id, previousEdges)) {
      placed.push({ ...node, position: old.position })
    } else {
      const position = children.get(node.id)
      pending.push({
        ...node,
        position: node.type === 'goal'
          ? { x: 0, y: 0 }
          : { x: (position?.x ?? 0) - otherX + 340, y: position?.y ?? 0 },
      })
    }
  }

  // New agents sit beside their held intent; existing unaffected nodes keep their coordinates.
  pending.sort((a, b) => Number(a.type === 'agent') - Number(b.type === 'agent'))
  for (const node of pending) {
    if (node.type === 'agent' && typeof node.data.intentId === 'string') {
      const intent = placed.find((item) => item.id === node.data.intentId)
      if (intent) {
        node.position = {
          x: intent.position.x - NODE_SIZE.agent.width - 22,
          y: intent.position.y + (sizeOf(intent).height - NODE_SIZE.agent.height) / 2,
        }
      }
    }
    while (placed.some((item) => overlaps(node, item))) {
      node.position = { ...node.position, y: node.position.y + sizeOf(node).height + 20 }
    }
    placed.push(node)
  }

  const byId = new Map(placed.map((node) => [node.id, node]))
  return nodes.map((node) => byId.get(node.id) ?? node)
}
