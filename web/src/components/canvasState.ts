export type CanvasViewState = {
  positions: Record<string, { x: number; y: number }>;
  pinned: string[];
  viewport?: { x: number; y: number; zoom: number };
  collapsed: boolean;
};
type CanvasKind = 'ctf-board' | 'blackboard';
const patterns = { 'ctf-board': /^(?:agent|challenge):[^\s]{1,256}$/, blackboard: /^(?:goal|[FI]\d+|agent-\d+)$/ };
const coordinate = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e7;
const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
function validate(kind: CanvasKind, value: unknown): CanvasViewState | null {
  if (!object(value) || !object(value.positions) || !Array.isArray(value.pinned) || typeof value.collapsed !== 'boolean') return null;
  const entries = Object.entries(value.positions);
  if (entries.length > 2000 || value.pinned.length > 2000) return null;
  const positions: CanvasViewState['positions'] = {};
  for (const [id, point] of entries) {
    if (!patterns[kind].test(id) || !object(point) || !coordinate(point.x) || !coordinate(point.y)) return null;
    positions[id] = { x: point.x, y: point.y };
  }
  if (!value.pinned.every((id): id is string => typeof id === 'string' && patterns[kind].test(id))) return null;
  const state: CanvasViewState = { positions, pinned: [...new Set(value.pinned)], collapsed: value.collapsed };
  if (value.viewport !== undefined) {
    const view = value.viewport;
    if (!object(view) || !coordinate(view.x) || !coordinate(view.y) || typeof view.zoom !== 'number' || !Number.isFinite(view.zoom) || view.zoom < 0.15 || view.zoom > 1.5) return null;
    state.viewport = { x: view.x, y: view.y, zoom: view.zoom };
  }
  return state;
}
export function loadCanvasState(kind: CanvasKind, taskId: string, storage?: Pick<Storage, 'getItem'>): CanvasViewState | null {
  try {
    const raw = (storage ?? globalThis.localStorage)?.getItem(`bbx:${kind}:${encodeURIComponent(taskId)}`);
    if (!raw || raw.length > 1_000_000) return null;
    const value: unknown = JSON.parse(raw);
    return object(value) && value.version === 1 ? validate(kind, value) : null;
  } catch { return null; }
}
export function saveCanvasState(kind: CanvasKind, taskId: string, value: CanvasViewState, storage?: Pick<Storage, 'setItem'>): boolean {
  try {
    const state = validate(kind, value), target = storage ?? globalThis.localStorage;
    if (!state || !target) return false;
    const raw = JSON.stringify({ version: 1, ...state });
    if (raw.length > 1_000_000) return false;
    target.setItem(`bbx:${kind}:${encodeURIComponent(taskId)}`, raw);
    return true;
  } catch { return false; }
}
