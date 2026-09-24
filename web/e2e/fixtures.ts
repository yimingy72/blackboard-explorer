import type { Page, Route } from '@playwright/test';

export const TASK_ID = '11111111-1111-4111-8111-111111111111';
export const GOAL = '查明订单服务延迟的原因';
export const TEXT_SENTINEL = 'TEXT_EVIDENCE_SENTINEL';
export const COMMAND_SENTINEL = 'COMMAND_RESULT_SENTINEL';
export const FUTURE_NOTE = 'FUTURE_NOTE_SENTINEL';
export const FUTURE_RECEIPT = 'FUTURE_RECEIPT_SENTINEL';
const AT = '2026-09-24T08:00:00Z';

type MockEvent = {
  version: number;
  task_id: string;
  type: string;
  actor: string;
  object_id: string | null;
  payload: Record<string, unknown>;
  addressed_to: null;
  created_at: string;
};

type MockProfile = Record<string, unknown>;

function event(version: number, type: string, payload: Record<string, unknown>, object_id: string | null = null, actor = 'system'): MockEvent {
  return { version, task_id: TASK_ID, type, actor, object_id, payload, addressed_to: null, created_at: AT };
}

const created = event(1, 'task.created', {
  goal: GOAL,
  domain_context: '订单服务的测试环境',
  acceptance: [{ id: 'A1', desc: '给出可复核的原因' }],
  budget: { max_cost: 10, max_minutes: 60, max_concurrent_agents: 3 },
  usage: { cost: 0 },
  params: {},
});

const evidence = [
  { type: 'text', summary: '现场文本', uri: `evidence/${TASK_ID}/agent-1/text.txt` },
  { type: 'command_output', summary: '命令执行记录', uri: `toolcalls/${TASK_ID}/call-1.txt`, call_id: 'call-1', auto: true },
  { type: 'http', summary: 'HTTP 请求响应', uri: `evidence/${TASK_ID}/agent-1/http.txt` },
  { type: 'log', summary: '延迟日志', uri: `evidence/${TASK_ID}/agent-1/log.txt` },
  { type: 'code_ref', summary: '代码引用', uri: `evidence/${TASK_ID}/agent-1/code.txt`, path: 'src/order.py:18-19' },
  { type: 'script', summary: '复现脚本', uri: `evidence/${TASK_ID}/agent-1/script.txt` },
];

function ordinaryEvents(): MockEvent[] {
  return [
    created,
    event(2, 'task.running', {}),
    event(3, 'agent.spawned', { id: 'agent-1', task_type: 'explore', is_seed: true }, 'agent-1'),
    event(4, 'fact.posted', {
      id: 'F1', author: 'agent-1', kind: 'observation', statement: '数据库连接池出现等待',
      provenance: 'tool_backed', derived_from: [], disputes: [], satisfies: ['A1'], evidence,
    }, 'F1', 'agent-1'),
    event(5, 'intent.posted', {
      id: 'I1', author: 'agent-1', statement: '检查连接池上限', expected: '找到等待原因',
      method: '读取配置', based_on: ['F1'], relates_to: ['A1'], claim: true,
    }, 'I1', 'agent-1'),
    event(6, 'intent.released', { intent_id: 'I1', holder: 'agent-1', counted: false, note: FUTURE_NOTE }, 'I1', 'agent-1'),
    event(7, 'tool_call.recorded', {
      id: 'call-1', agent_id: 'agent-1', tool: 'exec_command', args: { command: 'cat config' },
      result_head: 'pool=4', result_uri: `toolcalls/${TASK_ID}/call-1.txt`,
    }, 'agent-1', 'agent-1'),
    event(8, 'agent.progress', { agent_id: 'agent-1', steps: 2, context_tokens: 1300, usage: { cost: 0.02 } }, 'agent-1'),
    event(9, 'agent.finished', { agent_id: 'agent-1', end_reason: 'normal', receipt: { note: FUTURE_RECEIPT } }, 'agent-1'),
    event(10, 'acceptance.judged', {
      mode: 'judge', judge_from_version: 9,
      verdicts: [{ id: 'A1', verdict: 'met', reason: '连接池证据充分', evidence_facts: ['F1'] }],
    }, 'A1', 'agent-2'),
    event(11, 'task.closing', { reason: 'accepted' }),
    event(12, 'task.report', { uri: `reports/${TASK_ID}/final.md` }),
    event(13, 'task.finished', {}),
    event(14, 'task.archived', { uri: `archives/${TASK_ID}/workspace.tar.zst` }),
  ];
}

function largeGraphEvents(): MockEvent[] {
  const events = [created];
  for (let index = 1; index <= 151; index += 1) {
    const id = `I${index}`;
    events.push(event(events.length + 1, 'intent.posted', {
      id, author: 'agent-1', statement: `已关闭方向 ${index}`,
      expected: '完成', method: '检查', based_on: [], relates_to: ['A1'], claim: false,
    }, id, 'agent-1'));
    events.push(event(events.length + 1, 'fact.posted', {
      id: `F${index}`, author: 'agent-1', kind: 'observation', statement: `方向 ${index} 的结果`,
      provenance: 'tool_backed', derived_from: [], disputes: [], resolves: id,
      result: 'confirmed', satisfies: [], evidence: [],
    }, `F${index}`, 'agent-1'));
    events.push(event(events.length + 1, 'intent.closed', { intent_id: id, result: 'confirmed', by: `F${index}` }, id, 'agent-1'));
  }
  return events;
}

function profile(prompt: string): MockProfile {
  const model = {
    provider: 'deepseek', model: 'deepseek-chat', base_url: 'https://example.invalid/v1',
    reasoning_effort: 'medium', price: { currency: 'CNY', cache_hit_per_m: 0.2, cache_miss_per_m: 2, output_per_m: 3 },
  };
  return {
    models: { explore: model, derive: model, close: model }, params: {},
    prompts: { explore: 'prompts/explore.md.j2', derive: 'prompts/derive.md.j2', close: 'prompts/close.md.j2' },
    prompt_templates: { explore: prompt, derive: '推导模板', close: '收尾模板' },
    exec_image: 'bbx-exec-env:latest', exec_resources: { cpus: 2, mem: '4g', pids: 256 },
    privileged_allowlist: [],
  };
}

function profileDocument(version: number, body: MockProfile) {
  return { name: 'default', version, profile: body, created_by: 'tester', created_at: AT };
}

async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
}

/** Installs fixtures before navigation. Every /api request is handled here, including failures. */
export async function installMockApi(page: Page, options: { largeGraph?: boolean } = {}) {
  const unexpected: string[] = [];
  const events = options.largeGraph ? largeGraphEvents() : ordinaryEvents();
  const profiles = new Map<number, MockProfile>([[1, profile('旧版探索模板')], [2, profile('新版探索模板')]]);
  const task = {
    id: TASK_ID, goal: GOAL, status: 'finished', acceptance_state: { A1: { status: 'met' } },
    usage: { cost: 0.02 }, agents: [], report_uri: `reports/${TASK_ID}/final.md`,
    workspace_uri: `archives/${TASK_ID}/workspace.tar.zst`, agent_profile: 'default',
    agent_profile_version: 2, created_at: AT,
  };

  await page.addInitScript(() => {
    class MockEventSource extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readyState = 1;
      constructor() {
        super();
        queueMicrotask(() => this.dispatchEvent(new Event('open')));
      }
      close() { this.readyState = 2; }
    }
    Object.defineProperty(window, 'EventSource', { value: MockEventSource });
  });

  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (path === '/api/login' && method === 'POST') return json(route, { username: 'tester' });
    if (path === '/api/logout' && method === 'POST') return json(route, { ok: true });
    if (path === '/api/tasks' && method === 'GET') return json(route, [task]);
    if (path === `/api/tasks/${TASK_ID}` && method === 'GET') return json(route, task);
    if (path === `/api/tasks/${TASK_ID}/events` && method === 'GET') return json(route, events);
    if (path === `/api/tasks/${TASK_ID}/stream` && method === 'GET') {
      return route.fulfill({ contentType: 'text/event-stream', body: ': fixture\n\n' });
    }
    if (path === `/api/tasks/${TASK_ID}/report` && method === 'GET') {
      return route.fulfill({ contentType: 'text/markdown', body: '# 最终报告\n\n**连接池上限**导致等待。\n' });
    }
    if (path === `/api/tasks/${TASK_ID}/workspace/tree` && method === 'GET') {
      return json(route, { entries: [{ path: 'agents/agent-1/notes.txt', kind: 'file', size: 23 }] });
    }
    if (path === `/api/tasks/${TASK_ID}/workspace/file` && method === 'GET') {
      return json(route, { path: url.searchParams.get('path'), text: '归档文件预览内容', size: 23, truncated: false, binary: false });
    }
    if (path === `/api/tasks/${TASK_ID}/workspace` && method === 'GET') {
      return route.fulfill({ contentType: 'application/zstd', body: 'archive fixture' });
    }
    if (path === '/api/evidence' && method === 'GET') {
      const uri = url.searchParams.get('uri') ?? '';
      const body = uri.includes('call-1')
        ? JSON.stringify({ tool: 'exec_command', args: { command: 'cat config' }, result: { stdout: COMMAND_SENTINEL } })
        : uri.endsWith('/text.txt') ? TEXT_SENTINEL
        : uri.endsWith('/http.txt') ? 'GET /orders HTTP/1.1\nHTTP/1.1 200 OK'
        : uri.endsWith('/log.txt') ? '2026-09-24 延迟日志'
        : uri.endsWith('/code.txt') ? 'pool_size = 4\nmax_overflow = 0'
        : uri.endsWith('/script.txt') ? 'print("test")\n--- OUTPUT ---\n复现完成'
        : null;
      if (body !== null) return route.fulfill({ contentType: 'text/plain', body });
    }
    if (path === '/api/profiles' && method === 'GET') {
      return json(route, [{ name: 'default', latest_version: Math.max(...profiles.keys()) }]);
    }
    if (path === '/api/profiles/default/versions' && method === 'GET') {
      return json(route, [...profiles.keys()].reverse().map((version) => ({
        name: 'default', version, created_by: 'tester', created_at: AT,
      })));
    }
    const profileMatch = /^\/api\/profiles\/default\/versions\/(\d+)$/.exec(path);
    if (profileMatch && method === 'GET') {
      const version = Number(profileMatch[1]);
      const body = profiles.get(version);
      return json(route, body ? profileDocument(version, body) : { detail: 'Not found' }, body ? 200 : 404);
    }
    if (path === '/api/profiles/default/versions' && method === 'POST') {
      const version = Math.max(...profiles.keys()) + 1;
      const body = request.postDataJSON() as MockProfile;
      profiles.set(version, body);
      return json(route, profileDocument(version, body));
    }
    unexpected.push(`${method} ${path}`);
    return json(route, { detail: `Unexpected mock request: ${method} ${path}` }, 501);
  });
  return { unexpected, events, profiles };
}
