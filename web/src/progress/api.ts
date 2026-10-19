import { request } from '../api/client';
import type { ProgressMessage, ProgressSettings, ProgressView } from './types';

const path = (id: string) => `/tasks/${encodeURIComponent(id)}/progress`;
const json = (method: string, value: unknown, signal?: AbortSignal): RequestInit => ({ method, body: JSON.stringify(value), signal });
export const progressApi = {
  get: (id: string, signal?: AbortSignal, atVersion?: number) => request<ProgressView>(`${path(id)}${atVersion === undefined ? '' : `?at_version=${atVersion}`}`, { signal }),
  configure: (id: string, value: { enabled: boolean; max_cost: string; model_id?: string | null }, signal?: AbortSignal) => request<ProgressView>(`${path(id)}/settings`, json('PUT', value, signal)),
  refresh: (id: string, requestId: string) => request(`${path(id)}/refresh`, json('POST', { request_id: requestId })),
  messages: (id: string, signal?: AbortSignal) => request<{ messages: ProgressMessage[] }>(`${path(id)}/messages`, { signal }),
  source: (id: string, objectId: string, through: number, signal?: AbortSignal) => request<{ object: Record<string, unknown> }>(`${path(id)}/sources?object_id=${encodeURIComponent(objectId)}&through_version=${through}`, { signal }),
  send: (id: string, value: { id: string; content: string; node_id?: string | null }) => request<ProgressMessage>(`${path(id)}/messages`, json('POST', value)),
  settings: (signal?: AbortSignal) => request<ProgressSettings>('/progress/settings', { signal }),
  saveSettings: (value: ProgressSettings) => request<ProgressSettings>('/progress/settings', json('PUT', { ...value, expected_revision: value.revision })),
};
