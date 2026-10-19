import { describe, expect, it } from 'vitest';
import { defaultExpanded, layoutProgressTree, progressViewportForNode, PROGRESS_NODE_SIZE, visibleProgressNodes } from './layout';
import type { ProgressNode } from './types';

function node(id: string, parent_id: string | null = null, kind: ProgressNode['kind'] = 'finding', order = 0): ProgressNode {
  return { id, parent_id, kind, order, title: id, question: '', summary: '', significance: '', next_step: '', limitations: '', key_steps: [], source_refs: [] };
}

describe('progress tree layout', () => {
  it('starts with a readable core and lets each direction expand independently', () => {
    const nodes = [node('goal', null, 'goal'), ...Array.from({ length: 4 }, (_, index) => node(`direction-${index}`, 'goal', 'direction', index)), ...Array.from({ length: 24 }, (_, index) => node(`finding-${index}`, `direction-${Math.floor(index / 6)}`))];
    const expanded = defaultExpanded(nodes);
    const visible = visibleProgressNodes(nodes, expanded);
    expect(visible.length).toBeGreaterThanOrEqual(8);
    expect(visible.length).toBeLessThanOrEqual(20);
    expect(expanded.has('goal')).toBe(true);
    expanded.add('direction-3');
    expect(visibleProgressNodes(nodes, expanded).some(item => item.id === 'finding-23')).toBe(true);
    expanded.delete('direction-3');
    expect(visibleProgressNodes(nodes, expanded).some(item => item.id === 'finding-23')).toBe(false);
  });
  it('centers parents over ordered children without overlap', () => {
    const nodes = [node('goal', null, 'goal'), node('second', 'goal', 'direction', 2), node('first', 'goal', 'direction', 1), node('a', 'first'), node('b', 'first')];
    const positions = layoutProgressTree(nodes);
    expect(positions.first.y).toBeLessThan(positions.second.y);
    expect(positions.b.y - positions.a.y).toBeGreaterThan(PROGRESS_NODE_SIZE.height);
    expect(positions.goal.x).toBeLessThan(positions.first.x);
    expect(positions.first.x).toBeLessThan(positions.a.x);
  });
  it('preserves manual positions when a sibling or a whole branch is added', () => {
    const base = [node('goal', null, 'goal'), node('direction', 'goal', 'direction'), node('finding', 'direction')];
    const previous = layoutProgressTree(base);
    previous.direction = { x: 600, y: 90 };
    const next = layoutProgressTree([...base, node('new', 'direction'), node('another', 'goal', 'direction')], previous);
    for (const item of base) expect(next[item.id]).toEqual(previous[item.id]);
    expect(next.new.x).toBe(600 + PROGRESS_NODE_SIZE.width + 96);
    expect(next.new).not.toEqual(next.finding);
  });
  it('ignores archived nodes and traverses deep trees without recursion', () => {
    const nodes = Array.from({ length: 12000 }, (_, index) => node(`n${index}`, index ? `n${index - 1}` : null));
    const expanded = new Set(nodes.map(item => item.id));
    expect(visibleProgressNodes(nodes, expanded)).toHaveLength(12000);
    expect(Object.keys(layoutProgressTree(nodes))).toHaveLength(12000);
    expect(visibleProgressNodes([...nodes.slice(0, 2), { ...node('old', 'n0'), archived: true }], expanded)).toHaveLength(2);
    expect(visibleProgressNodes([node('a', 'b'), node('b', 'a')], new Set(['a', 'b']))).toEqual([]);
  });
  it('supports valid ids that coincide with object prototype properties', () => {
    const positions = layoutProgressTree([node('constructor', null, 'goal'), node('__proto__', 'constructor')]);
    expect(positions.constructor).toEqual({ x: 0, y: 0 });
    expect(positions.__proto__.x).toBeGreaterThan(0);
  });
});

describe('revealing the selected node beside the inspector', () => {
  it('keeps the current viewport when the selected node is already visible', () => {
    const viewport = { x: 0, y: 0, zoom: .8 };
    expect(progressViewportForNode({ x: 100, y: 100 }, viewport, { width: 900, height: 600 })).toBe(viewport);
  });
  it('only pans enough to reveal a clipped node, preserving manual zoom and positions', () => {
    const position = { x: 700, y: 100 }, viewport = { x: 0, y: 0, zoom: .8 };
    const next = progressViewportForNode(position, viewport, { width: 700, height: 600 });
    expect(next.zoom).toBe(.8);
    expect(next.x).toBeCloseTo(-112.8);
    expect(next.y).toBe(0);
    expect(position).toEqual({ x: 700, y: 100 });
    expect(viewport).toEqual({ x: 0, y: 0, zoom: .8 });
  });
});
