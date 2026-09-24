import type { components } from './schema';

export type TaskView = components['schemas']['TaskView'];
export type TaskCreateInput = components['schemas']['TaskCreateBody'];
export type TaskCreated = components['schemas']['TaskCreated'];
export type ProfileName = components['schemas']['ProfileName'];
export type ProfileVersion = components['schemas']['ProfileVersion'];
export type BoardEvent = components['schemas']['Event'];
export type ObjectDetails = {
  object: Record<string, unknown>;
  related: Record<string, Record<string, unknown>>;
};

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    let message = `请求失败（${response.status}），请稍后重试。`;
    if (body && typeof body === 'object') {
      const detail = body as Record<string, unknown>;
      if (typeof detail.message === 'string') message = detail.message;
      else if (typeof detail.detail === 'string') message = detail.detail;
    }
    if (response.status === 401) {
      message = path === '/login' ? '账号或密码不正确。' : '登录已失效，请重新登录。';
    }
    throw new ApiError(response.status, message);
  }
  return body as T;
}

const taskPath = (id: string) => `/tasks/${encodeURIComponent(id)}`;

export const api = {
  login: (username: string, password: string) =>
    request<{ username: string }>('/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/logout', { method: 'POST' }),
  listTasks: () => request<TaskView[]>('/tasks'),
  createTask: (input: TaskCreateInput) =>
    request<TaskCreated>('/tasks', { method: 'POST', body: JSON.stringify(input) }),
  getTask: (id: string) => request<TaskView>(taskPath(id)),
  startTask: (id: string) =>
    request<components['schemas']['StatusResult']>(`${taskPath(id)}/start`, { method: 'POST' }),
  stopTask: (id: string) =>
    request<components['schemas']['StatusResult']>(`${taskPath(id)}/stop`, { method: 'POST' }),
  getEvents: (id: string, since = 0) =>
    request<BoardEvent[]>(`${taskPath(id)}/events?since=${since}`),
  getState: <T = Record<string, unknown>>(id: string) => request<T>(`${taskPath(id)}/state`),
  getObject: (id: string, objectId: string) =>
    request<ObjectDetails>(`${taskPath(id)}/objects/${encodeURIComponent(objectId)}?depth=1`),
  listProfiles: () => request<ProfileName[]>('/profiles'),
  listProfileVersions: (name: string) =>
    request<ProfileVersion[]>(`/profiles/${encodeURIComponent(name)}/versions`),
};
