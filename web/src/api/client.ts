import type { components } from './schema';

export type TaskView = Omit<components['schemas']['TaskView'], 'budget' | 'runs'> & {
  budget: { max_cost: string | number; max_minutes: number; max_concurrent_agents: number };
  run_number: number;
  active_seconds: number;
  active_since: string | null;
  cleanup_ready: boolean;
  runs: Array<{ run_number: number; report_uri: string | null; workspace_uri: string | null }>;
};
export type TaskCreateInput = components['schemas']['TaskCreateBody'] & { model_id?: string; model_version?: number };
export type TaskCreated = components['schemas']['TaskCreated'];
export type ProfileName = components['schemas']['ProfileName'];
export type ProfileVersion = components['schemas']['ProfileVersion'];
export type ProfileDocument = components['schemas']['ProfileDocument'];
export type ProfileInput = components['schemas']['AgentProfile-Input'];
export type WorkerRole = 'explore' | 'derive' | 'close';
export type WorkerTools = components['schemas']['WorkerTools'];
export type WorkerSettings = { revision: number; profile: ProfileInput };
export type RuntimeInput = Pick<ProfileInput, 'params' | 'exec_image' | 'exec_resources' | 'privileged_allowlist'>;
export type ProviderField = { name: string; label?: string; required: boolean };
export type ProviderSpec = {
  id: string; label: string; options_fields: ProviderField[]; credential_fields: ProviderField[];
  allow_no_auth: boolean; default_base_url: string; base_url_required: boolean; supports_reasoning_effort: boolean;
  reasoning_efforts: string[];
};
export type PlatformModel = {
  name: string; version: number; label: string;
  config: ProfileInput['models']['explore'] & { provider_options?: Record<string, string>; platform_id?: string; platform_version?: number };
  credential_source: 'environment' | 'stored' | 'none'; has_secret: boolean; enabled: boolean;
  configured_credentials: string[]; is_default: boolean;
};
export type PlatformModelInput = {
  label: string; provider: string; model: string; base_url: string; reasoning_effort: string;
  supports_vision?: boolean | null;
  context_window?: number | null;
  price: ProfileInput['models']['explore']['price']; provider_options?: Record<string, string>;
  credentials?: Record<string, string>; api_key?: string; enabled: boolean;
};
export type McpServer = {
  name: string; version: number; label: string; url: string; auth_header: string;
  auth_scheme: string; has_secret: boolean; enabled: boolean;
};
export type McpServerInput = {
  label: string; url: string; auth_header: string; auth_scheme: string;
  secret?: string; clear_secret?: boolean; enabled: boolean;
};
export type McpTool = { name: string; description: string };
export type BoardEvent = components['schemas']['Event'];
export type PreviewData = { text: string; size: number; truncated: boolean; binary: boolean };
export type AgentMessage = {
  id: string; task_id: string; agent_id: string; role: 'user' | 'assistant'; content: string;
  status: 'queued' | 'processing' | 'delivered' | 'completed' | 'failed';
  reply_to: string | null; error?: string | null; usage: Record<string, unknown> | null;
  created_at: string; updated_at: string;
};
export type AgentMessages = {
  messages: AgentMessage[]; session_available: boolean; session_origin: 'native' | 'legacy' | null;
  mode: 'active' | 'review';
};
export type WorkspaceEntry = { path: string; kind: 'file' | 'directory' | 'link'; size: number };
export type WorkspaceTree = { entries: WorkspaceEntry[] };
export type WorkspacePreview = PreviewData & { path: string };
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
      else if (Array.isArray(detail.detail)) {
        message = detail.detail.map((error: { loc?: unknown[]; msg?: string }) =>
          `${error.loc?.filter((part) => part !== 'body').join('.') ?? '配置'}：${error.msg ?? '值不合法'}`,
        ).join('；');
      }
    }
    if (response.status === 401) {
      message = path === '/login' ? '账号或密码不正确。' : '登录已失效，请重新登录。';
    }
    throw new ApiError(response.status, message);
  }
  return body as T;
}

const taskPath = (id: string) => `/tasks/${encodeURIComponent(id)}`;

async function textResponse(path: string, signal?: AbortSignal): Promise<Response> {
  const response = await fetch(`/api${path}`, { credentials: 'include', signal });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string; message?: string } | null;
    throw new ApiError(response.status, body?.message ?? body?.detail ?? `读取失败（${response.status}）`);
  }
  return response;
}

export async function previewResponse(response: Response, limit = 200 * 1024): Promise<PreviewData> {
  const reader = response.body?.getReader();
  if (!reader) return { text: '', size: 0, truncated: false, binary: false };
  const half = Math.floor(limit / 2);
  let size = 0;
  let hasNull = false;
  let head = new Uint8Array(0);
  let tail = new Uint8Array(0);
  let complete: Uint8Array[] = [];
  const join = (chunks: Uint8Array[]) => {
    const output = new Uint8Array(chunks.reduce((total, chunk) => total + chunk.length, 0));
    let offset = 0;
    for (const chunk of chunks) { output.set(chunk, offset); offset += chunk.length; }
    return output;
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      hasNull ||= value.includes(0);
      if (size <= limit) complete.push(value);
      else complete = [];
      if (head.length < half) head = join([head, value.subarray(0, half - head.length)]);
      tail = join([tail, value.subarray(Math.max(0, value.length - half))]).slice(-half);
    }
  } finally { reader.releaseLock(); }
  const truncated = size > limit;
  const bytes = truncated ? join([head, tail]) : join(complete);
  const binary = hasNull || /^(image|audio|video)\//.test(response.headers.get('content-type') ?? '');
  const decoder = new TextDecoder();
  const text = binary ? '' : truncated
    ? `${decoder.decode(head)}\n\n… 已省略中间内容 …\n\n${decoder.decode(tail)}`
    : decoder.decode(bytes);
  return { text, size, truncated, binary };
}

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
  resumeTask: (id: string, input: { request_id: string; additional_cost: string; additional_minutes: number; refresh_tools: boolean }) =>
    request<TaskView>(`${taskPath(id)}/resume`, { method: 'POST', body: JSON.stringify(input) }),
  deleteTask: (id: string) => request<{ deleting: boolean }>(taskPath(id), { method: 'DELETE' }),
  getAgentMessages: (id: string, agent: string) => request<AgentMessages>(`${taskPath(id)}/agents/${encodeURIComponent(agent)}/messages`),
  sendAgentMessage: (id: string, agent: string, message: { id: string; content: string }) =>
    request<AgentMessage>(`${taskPath(id)}/agents/${encodeURIComponent(agent)}/messages`, { method: 'POST', body: JSON.stringify(message) }),
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
  getProfile: (name: string, version: number) =>
    request<ProfileDocument>(`/profiles/${encodeURIComponent(name)}/versions/${version}`),
  createProfileVersion: (name: string, profile: ProfileInput) =>
    request<ProfileDocument>(`/profiles/${encodeURIComponent(name)}/versions`, { method: 'POST', body: JSON.stringify(profile) }),
  getWorkerSettings: () => request<WorkerSettings>('/settings/workers'),
  saveWorkerSettings: (role: WorkerRole, expected_revision: number, prompt: string, tools: WorkerTools) =>
    request<WorkerSettings>(`/settings/workers/${role}`, { method: 'PUT', body: JSON.stringify({ expected_revision, prompt, tools }) }),
  saveRuntimeSettings: (expected_revision: number, input: RuntimeInput) =>
    request<WorkerSettings>('/settings/runtime', { method: 'PUT', body: JSON.stringify({ expected_revision, ...input }) }),
  listProviders: () => request<ProviderSpec[]>('/platform/providers'),
  listPlatformModels: () => request<PlatformModel[]>('/platform/models'),
  createPlatformModel: (input: PlatformModelInput) =>
    request<PlatformModel>('/platform/models', { method: 'POST', body: JSON.stringify(input) }),
  setDefaultPlatformModel: (name: string) =>
    request<{ default_model_id: string }>(`/platform/models/${encodeURIComponent(name)}/default`, { method: 'POST' }),
  savePlatformModel: (name: string, input: PlatformModelInput) =>
    request<PlatformModel>(`/platform/models/${encodeURIComponent(name)}`, { method: 'POST', body: JSON.stringify(input) }),
  listMcpServers: () => request<McpServer[]>('/platform/mcp-servers'),
  createMcpServer: (input: McpServerInput) =>
    request<McpServer>('/platform/mcp-servers', { method: 'POST', body: JSON.stringify(input) }),
  saveMcpServer: (name: string, input: McpServerInput) =>
    request<McpServer>(`/platform/mcp-servers/${encodeURIComponent(name)}`, { method: 'POST', body: JSON.stringify(input) }),
  listMcpTools: (name: string, version: number) =>
    request<{ tools: McpTool[] }>(`/platform/mcp-servers/${encodeURIComponent(name)}/versions/${version}/tools`),
  getReport: async (id: string, signal?: AbortSignal, run?: number) => (await textResponse(`${taskPath(id)}/report${run ? `?run=${run}` : ''}`, signal)).text(),
  getWorkspaceTree: (id: string) => request<WorkspaceTree>(`${taskPath(id)}/workspace/tree`),
  getWorkspaceFile: (id: string, path: string) => request<WorkspacePreview>(`${taskPath(id)}/workspace/file?path=${encodeURIComponent(path)}`),
  evidenceUrl: (uri: string) => `/api/evidence?uri=${encodeURIComponent(uri)}`,
  getEvidencePreview: async (uri: string, signal?: AbortSignal) =>
    previewResponse(await textResponse(`/evidence?uri=${encodeURIComponent(uri)}`, signal)),
};
