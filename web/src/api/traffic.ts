import type { components } from './schema';
import { ApiError, request } from './client';

export type TrafficRecord = components['schemas']['TrafficRecordSummary'];
export type TrafficDetail = components['schemas']['TrafficRecordDetail'];
export type TrafficPage = components['schemas']['TrafficListResponse'];
export type TrafficMessage = NonNullable<TrafficDetail['request']>;
export type TrafficPacketNode = components['schemas']['TrafficPacketNode'];
export type TrafficPacketDirection = components['schemas']['TrafficPacketMeta']['direction'];
export type NativeTrafficQuery = components['schemas']['TrafficNativeQueryView'];
export type NativeTrafficPacket = components['schemas']['TrafficNativePacket'];
export type NativeTrafficPage = components['schemas']['TrafficNativePacketPage'];
export type NativeTrafficFrame = components['schemas']['TrafficNativeFrameDetail'];
export type NativeFilterCheck = { valid: boolean; error: string | null; engine_version: string | null };
export type TrafficFilters = {
  run: number;
  agentId?: string;
  protocol?: string;
  query?: string;
  before?: number;
  after?: number;
  kind?: 'packet' | 'http' | 'logical';
  direction?: TrafficPacketDirection;
  limit?: number;
};

function base(taskId: string) {
  return `/tasks/${encodeURIComponent(taskId)}/traffic`;
}

export const trafficApi = {
  list(taskId: string, filters: TrafficFilters, signal?: AbortSignal) {
    const params = new URLSearchParams({ run: String(filters.run), limit: String(filters.limit ?? 50) });
    if (filters.agentId) params.set('agent_id', filters.agentId);
    if (filters.protocol) params.set('protocol', filters.protocol);
    if (filters.query) params.set('q', filters.query);
    if (filters.before !== undefined) params.set('before', String(filters.before));
    if (filters.after !== undefined) params.set('after', String(filters.after));
    if (filters.kind) params.set('kind', filters.kind);
    if (filters.direction) params.set('direction', filters.direction);
    return request<TrafficPage>(`${base(taskId)}?${params}`, { signal });
  },
  detail(taskId: string, run: number, recordId: string, signal?: AbortSignal) {
    return request<TrafficDetail>(`${base(taskId)}/records/${encodeURIComponent(recordId)}?run=${run}`, { signal });
  },
  download(taskId: string, run: number, fileId: string) {
    return `/api${base(taskId)}/files/${encodeURIComponent(fileId)}?run=${run}`;
  },
  async checkFilter(taskId: string, expression: string, signal?: AbortSignal): Promise<NativeFilterCheck> {
    try {
      return await request<NativeFilterCheck>(`${base(taskId)}/native/check`, { method: 'POST', body: JSON.stringify({ display_filter: expression }), signal });
    } catch (error) {
      if (error instanceof ApiError && error.status === 422) return { valid: false, error: error.message, engine_version: null };
      throw error;
    }
  },
  openNativeQuery(taskId: string, run: number, agentId: string, expression: string, signal?: AbortSignal) {
    return request<NativeTrafficQuery>(`${base(taskId)}/native/queries`, { method: 'POST', body: JSON.stringify({ run, agent_id: agentId || null, display_filter: expression }), signal });
  },
  nativeQuery(taskId: string, queryId: string, seen?: string | null, signal?: AbortSignal) {
    const params = new URLSearchParams();
    if (seen) params.set('seen_revision', seen);
    return request<NativeTrafficQuery>(`${base(taskId)}/native/queries/${encodeURIComponent(queryId)}${params.size ? `?${params}` : ''}`, { signal });
  },
  nativePackets(taskId: string, queryId: string, revision: string, offset: number, signal?: AbortSignal) {
    const params = new URLSearchParams({ revision, offset: String(offset), limit: '200' });
    return request<NativeTrafficPage>(`${base(taskId)}/native/queries/${encodeURIComponent(queryId)}/packets?${params}`, { signal });
  },
  nativeFrame(taskId: string, queryId: string, revision: string, sourceId: string, number: number, signal?: AbortSignal) {
    const params = new URLSearchParams({ revision });
    return request<NativeTrafficFrame>(`${base(taskId)}/native/queries/${encodeURIComponent(queryId)}/sources/${encodeURIComponent(sourceId)}/frames/${number}?${params}`, { signal });
  },
};
