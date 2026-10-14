import { describe, expect, it } from 'vitest'

import type { GraphNode } from '../board/graph'
import { layoutGraph, sameTopology } from './layout'

const goal: GraphNode = {
  id: 'goal', type: 'goal', position: { x: 0, y: 0 }, data: { label: 'Find cause' },
}
const firstFact: GraphNode = {
  id: 'F1', type: 'fact', position: { x: 0, y: 0 }, data: { label: 'Observation', version: 1 },
}
const secondFact: GraphNode = {
  id: 'F2', type: 'fact', position: { x: 0, y: 0 }, data: { label: 'More evidence', version: 2 },
}

describe('incremental layout', () => {
  it('keeps existing node coordinates when a new object appears', async () => {
    const first = await layoutGraph([goal, firstFact], [])
    expect(first.find((node) => node.id === 'goal')?.position.x).toBe(0)
    const moved = first.map((node) => node.id === 'F1'
      ? { ...node, position: { x: 810, y: 140 } }
      : node)

    const next = await layoutGraph([goal, firstFact, secondFact], [], moved)
    expect(next.find((node) => node.id === 'F1')?.position).toEqual({ x: 810, y: 140 })
    expect(next.find((node) => node.id === 'F2')?.position).not.toEqual({ x: 810, y: 140 })
    expect(sameTopology([goal, firstFact], [], moved, [])).toBe(true)
  })

  it('distinguishes status updates from topology growth', () => {
    const disputed = { ...firstFact, data: { ...firstFact.data, status: 'disputed' } }
    expect(sameTopology([goal, disputed], [], [goal, firstFact], [])).toBe(true)
    expect(sameTopology([goal, firstFact, secondFact], [], [goal, firstFact], [])).toBe(false)
    expect(sameTopology([goal, firstFact], [
      { id: 'based_on:F1:I1', source: 'F1', target: 'I1' },
    ], [goal, firstFact], [])).toBe(false)
  })

  it('keeps dragged nodes when a new relation is added', async () => {
    const first = await layoutGraph([goal, firstFact], [])
    const moved = first.map(node => node.id === 'F1' ? { ...node, position: { x: 810, y: 140 } } : node)
    const next = await layoutGraph([goal, firstFact, secondFact], [{ id: 'supports:F1:F2', source: 'F1', target: 'F2' }], moved)
    for (const node of moved) expect(next.find(item => item.id === node.id)?.position).toEqual(node.position)
    const added = next.find(node => node.id === 'F2')!
    const existing = next.find(node => node.id === 'F1')!
    expect(added.position.x >= existing.position.x + 280 || added.position.x + 280 <= existing.position.x || added.position.y >= existing.position.y + 144 || added.position.y + 144 <= existing.position.y).toBe(true)
  })
})
