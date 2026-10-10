import { ApiError, request } from '../api/client';
import type { CtfArtifact, CtfChallenge, CtfEvent, CtfMessage, CtfRecord, CtfSession, CtfState } from './types';

const path = (taskId: string) => `/tasks/${encodeURIComponent(taskId)}`;
const body = (value: unknown, signal?: AbortSignal): RequestInit => ({ method: 'POST', body: JSON.stringify(value), signal });
export const ctfApi = {
  state: (id: string, signal?: AbortSignal) => request<CtfState>(`${path(id)}/ctf/state`, { signal }),
  challenges: (id: string, signal?: AbortSignal) => request<{ challenges: CtfChallenge[]; next_offset?: number | null }>(`${path(id)}/ctf/challenges?limit=500`, { signal }),
  events: (id: string, since = 0, signal?: AbortSignal) => request<CtfEvent[]>(`${path(id)}/events?since=${since}`, { signal }),
  messages: (id: string, member: string, signal?: AbortSignal) => request<{ messages: CtfMessage[] }>(`${path(id)}/agents/${encodeURIComponent(member)}/messages`, { signal }),
  session: async (id: string, member: string, signal?: AbortSignal): Promise<CtfSession | null> => {
    try { return await request<CtfSession>(`${path(id)}/agents/${encodeURIComponent(member)}/session`, { signal }); }
    catch (error) { if (error instanceof ApiError && error.status === 404) return null; throw error; }
  },
  send: (id: string, member: string, message: { id: string; content: string }, signal?: AbortSignal) => request<CtfMessage>(`${path(id)}/agents/${encodeURIComponent(member)}/messages`, body(message, signal)),
  member: (id: string, member: string, operation: 'stop' | 'resume' | 'remove', requestId: string, signal?: AbortSignal) => request(`${path(id)}/ctf/members/${encodeURIComponent(member)}/${operation}`, body({ request_id: requestId }, signal)),
  status: (id: string, operation: 'start' | 'stop', signal?: AbortSignal) => request(`${path(id)}/${operation}`, body({}, signal)),
  resume: (id: string, value: { request_id: string; additional_cost: string; additional_minutes: number; refresh_tools: boolean }, signal?: AbortSignal) => request(`${path(id)}/resume`, body(value, signal)),
  records: (id: string, challenge: string, signal?: AbortSignal, offset = 0) => request<{ records: CtfRecord[]; next_offset?: number | null }>(`${path(id)}/ctf/challenges/${encodeURIComponent(challenge)}/records?limit=100&offset=${offset}`, { signal }),
  artifact: (id: string, artifact: string, signal?: AbortSignal) => request<CtfArtifact>(`${path(id)}/ctf/artifacts/${encodeURIComponent(artifact)}`, { signal }),
};
