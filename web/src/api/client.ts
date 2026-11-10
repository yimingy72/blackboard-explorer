import type { components } from './schema';

export type TaskView = Omit<components['schemas']['TaskView'], 'budget' | 'runs'> & {
  name?: string | null;
  initial_attachments?: InitialAttachment[];
  budget: { currency?: 'USD' | 'CNY' | null; max_cost: string | number; max_minutes: number; max_concurrent_agents: number };
  pi_model?: PiModelRef | null;
  run_number: number;
  active_seconds: number;
  active_since: string | null;
  cleanup_ready: boolean;
  runs: Array<{ run_number: number; report_uri: string | null; workspace_uri: string | null }>;
};
export type TaskPage = Omit<components['schemas']['TaskPage'], 'items'> & { items: TaskView[] };
export type BlackboardTaskCreateInput = components['schemas']['TaskCreateBody'] & { gateway_model?: GatewayModelRef; pi_model?: PiModelRef; name?: string | null; input_group_id?: string; input_file_ids?: string[] };
export type CtfTaskCreateInput = Omit<components['schemas']['CtfTaskCreate'], 'ctf_options'> & {
  gateway_model?: GatewayModelRef;
  pi_model?: PiModelRef;
  ctf_options?: Partial<components['schemas']['CtfOptions-Input']>;
};
export type TaskCreateInput = BlackboardTaskCreateInput | CtfTaskCreateInput;
export type InitialAttachment = { id: string; filename: string; path: string; uri: string; size: number; sha256: string };
export type TaskInputGroup = { id: string; expires_at: string; files: InitialAttachment[] };
export type TaskCreated = components['schemas']['TaskCreated'];
export type ProfileName = components['schemas']['ProfileName'];
export type ProfileVersion = components['schemas']['ProfileVersion'];
export type ProfileDocument = components['schemas']['ProfileDocument'];
export type ProfileInput = components['schemas']['AgentProfile-Input'];
export type WorkerRole = 'explore' | 'derive' | 'close';
export type WorkerTools = components['schemas']['WorkerTools'];
export type WorkerSettingsProfile = Omit<ProfileInput, 'models' | 'privileged_allowlist'>;
export type WorkerSettings = { revision: number; profile: WorkerSettingsProfile };
export type PublishedCtfSettings = { published: false } | {
  published: true; revision: number; task_network: boolean;
  prompt_templates: Record<'lead' | 'teammate', string>;
  business_tools: Record<'lead' | 'teammate', string[]>;
};
export type RuntimeInput = Pick<ProfileInput, 'params' | 'exec_resources'>;
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
  mode: 'active' | 'review' | 'unavailable';
};
export type WorkspaceEntry = { path: string; kind: 'file' | 'directory' | 'link'; size: number };
export type WorkspaceTree = { entries: WorkspaceEntry[] };
export type WorkspacePreview = PreviewData & { path: string };
export type ObjectDetails = {
  object: Record<string, unknown>;
  related: Record<string, Record<string, unknown>>;
};
export type SandboxCommandSummary = components['schemas']['SandboxCommandSummary'];
export type SandboxCommandPage = components['schemas']['SandboxCommandPage'];
export type SandboxCommandDetail = components['schemas']['SandboxCommandDetail'];

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
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

export type PiModelRef = { model_id: string; model_revision: number };
export type PiUsdRates = { input: string; output: string; cache_read: string; cache_write: string };
export type PiModelConfig = {
  label: string; upstream_model: string; base_url: string; context_window: number; max_output_tokens: number;
  input_modalities: ('text' | 'image')[]; reasoning: boolean; tool_call: boolean;
  usd_rates: PiUsdRates | null; pricing_source: string; confirmed_free: boolean; enabled: boolean;
};
export type PiModel = PiModelConfig & { id: string; model_revision: number; has_secret: boolean };
export type PiModelInput = PiModelConfig & { secret?: string; clear_secret?: boolean; expected_revision?: number };
export type PiModelDirectory = { enabled: boolean; can_manage: boolean; models: PiModel[] };
export type PiFeeSummary = { estimated_usd: string; unknown_calls: number };
type PiTaskFees = { enabled: false } | { enabled: true; currency: 'USD'; threshold_usd: string; solver: PiFeeSummary; progress: PiFeeSummary; agents?: Record<string, PiFeeSummary> };
type PiReviewFees = PiFeeSummary & { currency: 'USD'; sites: (PiFeeSummary & { site_id: string })[]; partial: boolean };

export type GatewayModelRef = { alias: string; catalog_revision: string };
type GatewayUsdRates = { input: string; output: string; cache_read: string; cache_write: string };
export type GatewayModel = GatewayModelRef & {
  label: string; context_window: number; max_output_tokens: number; max_input_tokens?: number;
  input_modalities: ('text' | 'image')[]; tool_call: boolean; reasoning?: boolean; usd_rates: GatewayUsdRates;
};
type GatewayDirectory = {
  status: 'ready' | 'unavailable'; models: GatewayModel[]; management_url?: string; maintenance?: boolean;
};
export type ReviewFees = {
  confirmed_usd: string | null; pending: number; more: boolean;
  calls: { attempt_id: string; site_id?: string; task_id?: string; agent_id: string; conversation_id: string; model_alias: string; at: string | null; cost_usd: string | null }[];
};

export const api = {
  login: (username: string, password: string) =>
    request<{ username: string }>('/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/logout', { method: 'POST' }),
  listTasks: (signal?: AbortSignal, limit = 100) => request<TaskView[]>(`/tasks?limit=${limit}`, { signal }),
  listTaskPage: (filters: { q: string; status: string; offset: number; limit: number }, signal?: AbortSignal) => {
    const params = new URLSearchParams({ limit: String(filters.limit), offset: String(filters.offset) });
    if (filters.q.trim()) params.set('q', filters.q.trim());
    if (filters.status !== 'all') params.set('status', filters.status);
    return request<TaskPage>(`/tasks/page?${params}`, { signal });
  },
  uploadPrivateVpnSource: (id: string, file: File) => request<{ source_id: string }>(
    `/task-input-groups/${encodeURIComponent(id)}/private-vpn-source`,
    { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: file },
  ),
  createInputGroup: () => request<TaskInputGroup>('/task-input-groups', { method: 'POST' }),
  getInputGroup: (id: string) => request<TaskInputGroup>(`/task-input-groups/${encodeURIComponent(id)}`),
  uploadInput: (group: string, file: File, signal?: AbortSignal) => request<InitialAttachment>(`/task-input-groups/${encodeURIComponent(group)}/files?filename=${encodeURIComponent(file.name)}`, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' }, signal }),
  deleteInput: (group: string, id: string) => request<{ deleted: boolean }>(`/task-input-groups/${encodeURIComponent(group)}/files/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  deleteInputGroup: (id: string) => request<{ deleted: boolean }>(`/task-input-groups/${encodeURIComponent(id)}`, { method: 'DELETE', keepalive: true }),
  piModels: () => request<PiModelDirectory>('/platform/pi-models'),
  savePiModel: (input: PiModelInput, id?: string) => request<PiModel>(
    id ? `/platform/pi-models/${encodeURIComponent(id)}` : '/platform/pi-models',
    { method: id ? 'PUT' : 'POST', body: JSON.stringify(input) },
  ),
  piTaskFees: (taskId: string) => request<PiTaskFees>(`${taskPath(taskId)}/pi-fees`),
  piReviewFees: () => request<PiReviewFees>('/platform/pi-review-fees'),
  gatewayModels: () => request<GatewayDirectory>('/platform/gateway-models'),
  gatewayReviewFees: (taskId?: string) => request<ReviewFees>(taskId ? `${taskPath(taskId)}/gateway-model/review-costs` : '/platform/gateway-models/review-costs'),
  taskCreationCapabilities: () => request<components['schemas']['TaskCreationCapabilities']>('/task-creation-capabilities'),
  createTask: (input: TaskCreateInput) =>
    request<TaskCreated>('/tasks', { method: 'POST', body: JSON.stringify(input) }),
  getTask: (id: string, signal?: AbortSignal) => request<TaskView>(taskPath(id), { signal }),
  listSandboxCommands: (id: string, filters: { agentId?: string; before?: number }, signal?: AbortSignal) => {
    const params = new URLSearchParams({ limit: '50' });
    if (filters.agentId) params.set('agent_id', filters.agentId);
    if (filters.before !== undefined) params.set('before', String(filters.before));
    return request<SandboxCommandPage>(`${taskPath(id)}/sandbox/commands?${params}`, { signal });
  },
  getSandboxCommand: (id: string, commandId: string, signal?: AbortSignal) =>
    request<SandboxCommandDetail>(`${taskPath(id)}/sandbox/commands/${encodeURIComponent(commandId)}`, { signal }),
  resumeTask: (id: string, input: { request_id: string; currency?: "USD"; additional_cost: string; additional_minutes: number; refresh_tools: boolean }) =>
    request<TaskView>(`${taskPath(id)}/resume`, { method: 'POST', body: JSON.stringify(input) }),
  deleteTask: (id: string) => request<{ deleting: boolean }>(taskPath(id), { method: 'DELETE' }),
  getAgentMessages: (id: string, agent: string, signal?: AbortSignal) => request<AgentMessages>(`${taskPath(id)}/agents/${encodeURIComponent(agent)}/messages`, { signal }),
  sendAgentMessage: (id: string, agent: string, message: { id: string; content: string }) =>
    request<AgentMessage>(`${taskPath(id)}/agents/${encodeURIComponent(agent)}/messages`, { method: 'POST', body: JSON.stringify(message) }),
  startTask: (id: string) =>
    request<components['schemas']['StatusResult']>(`${taskPath(id)}/start`, { method: 'POST' }),
  stopTask: (id: string) =>
    request<components['schemas']['StatusResult']>(`${taskPath(id)}/stop`, { method: 'POST' }),
  getEvents: (id: string, since = 0, signal?: AbortSignal) =>
    request<BoardEvent[]>(`${taskPath(id)}/events?since=${since}`, { signal }),
  getState: <T = Record<string, unknown>>(id: string, signal?: AbortSignal) => request<T>(`${taskPath(id)}/state`, { signal }),
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
  getCtfWorkerSettings: () => request<PublishedCtfSettings>('/settings/ctf/workers'),
  saveWorkerSettings: (role: WorkerRole, expected_revision: number, prompt: string, tools: WorkerTools) =>
    request<WorkerSettings>(`/settings/workers/${role}`, { method: 'PUT', body: JSON.stringify({ expected_revision, prompt, tools }) }),
  saveRuntimeSettings: (expected_revision: number, input: RuntimeInput) =>
    request<WorkerSettings>('/settings/runtime', { method: 'PUT', body: JSON.stringify({ expected_revision, ...input }) }),
  listMcpServers: () => request<McpServer[]>('/platform/mcp-servers'),
  createMcpServer: (input: McpServerInput) =>
    request<McpServer>('/platform/mcp-servers', { method: 'POST', body: JSON.stringify(input) }),
  saveMcpServer: (name: string, input: McpServerInput) =>
    request<McpServer>(`/platform/mcp-servers/${encodeURIComponent(name)}`, { method: 'POST', body: JSON.stringify(input) }),
  listMcpTools: (name: string, version: number) =>
    request<{ tools: McpTool[] }>(`/platform/mcp-servers/${encodeURIComponent(name)}/versions/${version}/tools`),
  getReport: async (id: string, signal?: AbortSignal, run?: number) => (await textResponse(`${taskPath(id)}/report${run ? `?run=${run}` : ''}`, signal)).text(),
  getWorkspaceTree: (id: string, run?: number) => request<WorkspaceTree>(`${taskPath(id)}/workspace/tree${run ? `?run=${run}` : ''}`),
  getWorkspaceFile: (id: string, path: string, run?: number) => request<WorkspacePreview>(`${taskPath(id)}/workspace/file?path=${encodeURIComponent(path)}${run ? `&run=${run}` : ''}`),
  evidenceUrl: (uri: string) => `/api/evidence?uri=${encodeURIComponent(uri)}`,
  getEvidencePreview: async (uri: string, signal?: AbortSignal) =>
    previewResponse(await textResponse(`/evidence?uri=${encodeURIComponent(uri)}`, signal)),
};
