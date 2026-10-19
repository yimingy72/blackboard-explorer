import type { ProgressNode } from './types';

export type Point = { x: number; y: number };
export const PROGRESS_NODE_SIZE = { width: 286, height: 164 };
const ROW_GAP = 28;
const COLUMN_GAP = 96;

export function progressTree(nodes: ProgressNode[]) {
  const active = nodes.filter(node => !node.archived);
  const byId = new Map(active.map(node => [node.id, node]));
  const children = new Map<string, ProgressNode[]>();
  const roots: ProgressNode[] = [];
  for (const node of active) {
    if (!node.parent_id || !byId.has(node.parent_id)) roots.push(node);
    else {
      const siblings = children.get(node.parent_id) ?? [];
      siblings.push(node);
      children.set(node.parent_id, siblings);
    }
  }
  const order = (a: ProgressNode, b: ProgressNode) => a.order - b.order || a.id.localeCompare(b.id);
  roots.sort(order);
  for (const siblings of children.values()) siblings.sort(order);
  return { roots, children };
}

export function defaultExpanded(nodes: ProgressNode[], budget = 20): Set<string> {
  const { roots, children } = progressTree(nodes);
  const expanded = new Set<string>();
  const queue = [...roots];
  let count = roots.length;
  for (let index = 0; index < queue.length; index++) {
    const node = queue[index];
    const descendants = children.get(node.id) ?? [];
    if (!descendants.length || (node.kind !== 'goal' && count + descendants.length > budget)) continue;
    expanded.add(node.id);
    count += descendants.length;
    queue.push(...descendants);
  }
  return expanded;
}

export function visibleProgressNodes(nodes: ProgressNode[], expanded: Set<string>): ProgressNode[] {
  const { roots, children } = progressTree(nodes);
  const queue = [...roots];
  const visited = new Set<string>();
  const visible: ProgressNode[] = [];
  for (let index = 0; index < queue.length; index++) {
    const node = queue[index];
    if (visited.has(node.id)) continue;
    visited.add(node.id);
    visible.push(node);
    if (expanded.has(node.id)) queue.push(...children.get(node.id) ?? []);
  }
  return visible;
}

// Keep every existing position; a new branch only occupies unused rows in its column.
export function layoutProgressTree(nodes: ProgressNode[], previous: Record<string, Point> = {}): Record<string, Point> {
  const { roots, children } = progressTree(nodes);
  const queue = roots.map(node => ({ node, depth: 0 }));
  const depth = new Map<string, number>();
  const ordered: ProgressNode[] = [];
  for (let index = 0; index < queue.length; index++) {
    const item = queue[index];
    if (depth.has(item.node.id)) continue;
    depth.set(item.node.id, item.depth);
    ordered.push(item.node);
    for (const child of children.get(item.node.id) ?? []) queue.push({ node: child, depth: item.depth + 1 });
  }
  const heights = new Map<string, number>();
  for (let index = ordered.length - 1; index >= 0; index--) {
    const node = ordered[index], descendants = children.get(node.id) ?? [];
    heights.set(node.id, Math.max(PROGRESS_NODE_SIZE.height, descendants.reduce((sum, child) => sum + (heights.get(child.id) ?? 0), 0) + Math.max(0, descendants.length - 1) * ROW_GAP));
  }
  const positions: Record<string, Point> = Object.create(null);
  const occupied = new Map<number, Array<{ x: number; y: number }>>();
  for (const node of ordered) {
    if (!Object.hasOwn(previous, node.id)) continue;
    positions[node.id] = previous[node.id];
    const column = depth.get(node.id)!;
    occupied.set(column, [...occupied.get(column) ?? [], previous[node.id]]);
  }
  const pending: Array<{ node: ProgressNode; top: number }> = [];
  let rootTop = 0;
  for (const node of roots) {
    pending.push({ node, top: rootTop });
    rootTop += (heights.get(node.id) ?? PROGRESS_NODE_SIZE.height) + ROW_GAP;
  }
  const placed = new Set<string>();
  for (let index = 0; index < pending.length; index++) {
    const { node, top } = pending[index];
    if (placed.has(node.id)) continue;
    placed.add(node.id);
    const column = depth.get(node.id) ?? 0;
    if (!positions[node.id]) {
      const parent = node.parent_id ? positions[node.parent_id] : undefined;
      const x = parent ? parent.x + PROGRESS_NODE_SIZE.width + COLUMN_GAP : column * (PROGRESS_NODE_SIZE.width + COLUMN_GAP);
      let y = (node.parent_id && Object.hasOwn(previous, node.parent_id) ? previous[node.parent_id].y : undefined) ?? top + ((heights.get(node.id) ?? PROGRESS_NODE_SIZE.height) - PROGRESS_NODE_SIZE.height) / 2;
      const rows = occupied.get(column) ?? [];
      for (const point of [...rows].sort((a, b) => a.y - b.y)) {
        if (Math.abs(x - point.x) < PROGRESS_NODE_SIZE.width + 18 && Math.abs(y - point.y) < PROGRESS_NODE_SIZE.height + ROW_GAP) y = point.y + PROGRESS_NODE_SIZE.height + ROW_GAP;
      }
      positions[node.id] = { x, y };
      rows.push(positions[node.id]);
      occupied.set(column, rows);
    }
    let childTop = top;
    for (const child of children.get(node.id) ?? []) {
      pending.push({ node: child, top: childTop });
      childTop += (heights.get(child.id) ?? PROGRESS_NODE_SIZE.height) + ROW_GAP;
    }
  }
  return positions;
}

export function progressViewportForNode(
  position: Point,
  viewport: Point & { zoom: number },
  size: { width: number; height: number },
): Point & { zoom: number } {
  const width = PROGRESS_NODE_SIZE.width * viewport.zoom;
  const height = PROGRESS_NODE_SIZE.height * viewport.zoom;
  const left = position.x * viewport.zoom + viewport.x;
  const top = position.y * viewport.zoom + viewport.y;
  const shiftX = size.width < width + 48
    ? (size.width - width) / 2 - left
    : left < 24 ? 24 - left : left + width > size.width - 24 ? size.width - 24 - left - width : 0;
  const shiftY = size.height < height + 144
    ? (size.height - height) / 2 - top
    : top < 56 ? 56 - top : top + height > size.height - 88 ? size.height - 88 - top - height : 0;
  return shiftX || shiftY ? { x: viewport.x + shiftX, y: viewport.y + shiftY, zoom: viewport.zoom } : viewport;
}
