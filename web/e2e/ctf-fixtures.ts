import type { Page } from '@playwright/test';
import type { CtfChallenge, CtfRecord } from '../src/ctf/types';

export const CTF_ID = '55555555-5555-4555-8555-555555555555';
export const SECOND_ID = '66666666-6666-4666-8666-666666666666';
const at = '2026-10-08T08:00:00Z';
export async function openCtfMenu(page: Page) {
  const summary = page.locator('summary[aria-label="更多任务操作"]');
  if (!await summary.evaluate((element) => (element.parentElement as HTMLDetailsElement).open)) await summary.click();
}

export async function installCtfApi(page: Page, options: { evidenceText?: string } = {}) {
  const task = {
    id: CTF_ID, mode: 'ctf', name: '模拟验收 · 团队协作', goal: '完成两道模拟题并保留可复核记录',
    status: 'running', ctf_phase: 'running', ctf_options: { max_teammates: 4 }, ctf_conclusion: null as Record<string, unknown> | null,
    budget: { max_cost: '10', max_minutes: 60 }, usage: { cost: '0.04' }, cost_currency: 'CNY',
    active_seconds: 120, active_since: null, run_number: 1, runs: [] as Array<{ run_number: number; report_uri: string | null; workspace_uri: string | null }>, cleanup_ready: false, deleting: false, ctf_cleanup: {} as Record<string, unknown>,
    created_at: at, updated_at: at, initial_attachments: [], report_uri: null as string | null, workspace_uri: null as string | null, version: 12,
  };
  const members = [
    { id: 'lead', role: 'lead', display_name: 'Lead', lifecycle: 'active', run_state: 'idle', generation: 1 },
    { id: 'member-1', role: 'teammate', display_name: 'Web 分析员', lifecycle: 'active', run_state: 'running', generation: 1 },
    { id: 'member-2', role: 'teammate', display_name: '密码助手', lifecycle: 'stopped', run_state: 'idle', generation: 2 },
    { id: 'member-3', role: 'teammate', display_name: '历史队友', lifecycle: 'removed', run_state: 'idle', generation: 2 },
  ];
  const artifact = { id: 'artifact-1', filename: 'probe.py', path: '/workspace/shared/probe.py', uri: `evidence/${CTF_ID}/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/probe.py`, size: 32, sha256: 'a'.repeat(64), created_version: 8 };
  const challenges: CtfChallenge[] = [
    { id: 'C1', title: '模拟 Web 题', description: '分析本地模拟响应；不连接真实靶场。', connection: 'fake://local/web', requirements: '保留失败条件和复现脚本', owner_id: 'member-1', collaborator_ids: ['member-2'], work_status: 'blocked', revision: 3, verification: { source: 'platform', status: 'rejected', required: true, test_only: true }, target: { id: 'fake-target', test_only: true } },
    { id: 'C2', title: '模拟编码题', description: '已完成的分析题', owner_id: 'member-2', collaborator_ids: [], work_status: 'completed', revision: 2, verification: { source: 'agent', status: 'candidate', required: false }, target: {} },
  ];
  const records: CtfRecord[] = [{ id: 'R1', challenge_id: 'C1', author_id: 'member-1', kind: 'help_request', body: '需要协作者复核解析过程。', created_version: 9, artifact_refs: [artifact], attempted_routes: '检查响应编码与边界输入', observations_and_basis: '模拟响应在空输入时不一致', failure_conditions: '空输入导致解码失败', current_blocker: '尚未确定填充规则', help_needed: '请密码助手复核填充规则' }, { id: 'R2', challenge_id: 'C1', author_id: 'member-1', kind: 'verification', body: '候选答案：FLAG{mock-first}', created_version: 10, artifact_refs: [], verification: { source: 'agent', status: 'candidate', candidate: 'FLAG{mock-first}', summary: '首个候选，尚未独立验证。' } }, { id: 'R3', challenge_id: 'C1', author_id: 'member-2', kind: 'verification', body: '候选答案：FLAG{mock-second}', created_version: 11, artifact_refs: [], verification: { source: 'agent', status: 'candidate', candidate: 'FLAG{mock-second}', summary: '第二个候选，保留用于复核。' } }];
  const events = [{ task_id: CTF_ID, version: 7, type: 'ctf.turn.started', actor: 'member-1', payload: { agent_id: 'member-1', turn_id: 'turn-1', generation: 1 }, created_at: at }, { task_id: CTF_ID, version: 8, type: 'agent.trace.recorded', actor: 'member-1', payload: { agent_id: 'member-1', kind: 'model_output', uri: 'traces/' + CTF_ID + '/member-1/model.json', summary: 'model_output' }, created_at: at }, { task_id: CTF_ID, version: 9, type: 'tool_call.recorded', actor: 'member-1', payload: { agent_id: 'member-1', tool: 'read_artifact', result_uri: 'traces/' + CTF_ID + '/member-1/tool.json', result_head: '公开结果已登记。' }, created_at: at }, { task_id: CTF_ID, version: 10, type: 'ctf.turn.finished', actor: 'member-1', payload: { agent_id: 'member-1', status: 'completed', summary: '回合已结束' }, created_at: at }];
  const messages: Record<string, object[]> = { lead: [], 'member-1': [], 'member-2': [], 'member-3': [] };
  const secondTask = { ...structuredClone(task), id: SECOND_ID, name: '另一个模拟任务', goal: '第二个任务的独立目标' };
  const tasks = new Map([[CTF_ID, task], [SECOND_ID, secondTask]]);
  const memberSets = new Map([[CTF_ID, members], [SECOND_ID, structuredClone(members)]]);
  const mailboxes = new Map([[CTF_ID, messages], [SECOND_ID, { lead: [], 'member-1': [], 'member-2': [], 'member-3': [] } as Record<string, object[]>]]);
  const sent: Array<{ task_id: string; member: string; id: string; content: string; purpose: string }> = [];
  const resumes: Array<{ task_id: string; request_id: string; additional_cost: string; additional_minutes: number; refresh_tools: boolean }> = [];
  const deletions: string[] = [];
  const reports: string[] = [];
  const appliedResumes = new Set<string>();
  let resumeFailures = 0;
  let resumeHold: Promise<void> | null = null;
  let resumeRelease: (() => void) | null = null;
  const operations: string[] = [];
  const unexpected: string[] = [];
  let failures = 0;
  let hold: Promise<void> | null = null;
  let release: (() => void) | null = null;
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api/, '');
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (path === '/tasks') return json([...tasks.values()]);
    if (path === '/billing/summary') return json({ currency: 'CNY', total_cost: '0.04' });
    if (path === '/evidence' || path.startsWith('/evidence/')) return route.fulfill({ body: JSON.stringify({ text: options.evidenceText ?? '# Mock public evidence\nresult: fixture', args: { input: 'synthetic' }, result: 'synthetic result' }), contentType: 'application/json' });
    const match = path.match(/^\/tasks\/([^/]+)(.*)$/);
    if (!match) { unexpected.push(path); return json({ detail: 'Unexpected mock route' }, 404); }
    const [, id, suffix] = match;
    if (!suffix && request.method() === 'DELETE') deletions.push(id);
    const current = tasks.get(id);
    if (!current) return json({ detail: 'Task not found' }, 404);
    const currentMembers = memberSets.get(id)!;
    const currentMessages = mailboxes.get(id)!;
    if (!suffix) {
      if (request.method() === 'DELETE') {
        tasks.delete(id); mailboxes.delete(id); memberSets.delete(id);
        return json({ deleting: true });
      }
      return json(current);
    }
    if (suffix === '/report') {
      reports.push(id);
      return route.fulfill({ contentType: 'text/markdown', body: `# 模拟 CTF 报告\n\n${current.name}\n\n仍有一道题待核实。` });
    }
    if (suffix === '/workspace') return route.fulfill({ contentType: 'application/zstd', body: 'isolated mock archive', headers: { 'Content-Disposition': 'attachment; filename="ctf-test.tar.zst"' } });
    if (suffix === '/resume') {
      const body = request.postDataJSON() as typeof resumes[number];
      resumes.push({ ...body, task_id: id });
      if (resumeHold) await resumeHold;
      if (resumeFailures-- > 0) return json({ detail: '模拟续跑暂时失败' }, 503);
      if (!current.cleanup_ready) return json({ detail: 'Cleanup is incomplete' }, 409);
      const key = `${id}:${body.request_id}`;
      if (!appliedResumes.has(key)) {
        appliedResumes.add(key);
        current.run_number += 1; current.status = 'provisioning'; current.ctf_phase = 'provisioning';
        current.cleanup_ready = false;
      }
      return json(current);
    }
    if (suffix === '/ctf/state') return json({ task: { ...current, cost_currency: undefined }, members: currentMembers, challenges: id === CTF_ID ? challenges : [], records: id === CTF_ID ? records : [], artifacts: id === CTF_ID ? [artifact] : [] });
    if (suffix === '/ctf/challenges') return json({ challenges: id === CTF_ID ? challenges : [], next_offset: null });
    if (suffix === '/events') return json(id === CTF_ID ? events : []);
    if (suffix === '/stream') return route.fulfill({ contentType: 'text/event-stream', body: ': mock keepalive\n\n' });
    if (suffix.endsWith('/records')) return json({ records: id === CTF_ID ? records.filter((record) => suffix.includes(`/challenges/${record.challenge_id}/`)) : [], next_offset: null });
    if (suffix === '/ctf/artifacts/artifact-1') return json(artifact);
    const memberRoute = suffix.match(/^\/agents\/([^/]+)\/(messages|session)$/);
    if (memberRoute) {
      const [, member, kind] = memberRoute;
      if (kind === 'session') {
        const sessionMessages: object[] = [
          { message_id: `${id}-${member}-system`, role: 'system', contents: [{ type: 'text', text: 'PRIVATE_SYSTEM_CONTEXT_SENTINEL' }] },
          { message_id: `${id}-${member}-answer`, role: 'assistant', contents: [{ type: 'text', text: id === SECOND_ID ? '第二任务会话' : member === 'lead' ? '已分派两道模拟题；困难题已安排增援。' : `${member} 的历史工作记录` }] },
        ];
        if (id === CTF_ID && member === 'member-1') sessionMessages.push(
          { message_id: 'mock-tool-call', role: 'assistant', contents: [
            { type: 'text_reasoning', text: '模拟 provider 原始 reasoning：先核对空输入。', additional_properties: { reasoning_text: true, protected_data: 'PRIVATE_REASONING_SENTINEL' } },
            { type: 'text_reasoning', text: '模拟 provider 摘要：检查编码边界。' },
            { type: 'text', text: '准备读取公开复现脚本。' },
            { type: 'function_call', call_id: 'mock-read', name: 'read_artifact', arguments: { path: '/workspace/shared/probe.py', protected_data: 'PRIVATE_ARGUMENT_SENTINEL' } },
          ] },
          { message_id: 'mock-tool-result', role: 'tool', contents: [{ type: 'function_result', call_id: 'mock-read', result: { stdout: options.evidenceText ?? '公开结果：空输入解码失败，等待协作者复核。', raw_representation: 'PRIVATE_TOOL_SENTINEL' } }] },
          { message_id: 'mock-tool-followup', role: 'assistant', contents: [{ type: 'text', text: '脚本与失败条件已保存，题目仍待独立验证。' }] },
        );
        return json({ revision: 1, session: { state: { in_memory: { messages: sessionMessages } } } });
      }
      if (request.method() === 'GET') return json({ messages: currentMessages[member] ?? [] });
      const body = request.postDataJSON() as { id: string; content: string };
      const purpose = ['finished', 'failed', 'stopped'].includes(current.status) ? 'review' : 'execution';
      sent.push({ task_id: id, member, purpose, ...body });
      if (hold) await hold;
      if (failures-- > 0) return json({ detail: '模拟发送暂时失败' }, 503);
      const message = { id: body.id, body: body.content, sender_id: 'user', sender_kind: 'user', recipient_id: member, kind: 'user', purpose, status: 'queued', deferred: current.ctf_phase === 'closing', created_at: at };
      if (!(currentMessages[member] ?? []).some((item) => (item as { id: string }).id === body.id)) currentMessages[member].push(message);
      return json(message);
    }
    const operation = suffix.match(/^\/ctf\/members\/([^/]+)\/(stop|resume|remove)$/);
    if (operation) {
      operations.push(`${operation[1]}:${operation[2]}`);
      const member = currentMembers.find((item) => item.id === operation[1])!;
      member.lifecycle = operation[2] === 'remove' ? 'removed' : operation[2] === 'stop' ? 'stopped' : 'active';
      member.run_state = 'idle'; return json(member);
    }
    if (suffix === '/stop') { operations.push('task:stop'); current.ctf_phase = 'closing'; current.status = 'closing'; return json({ status: 'closing' }); }
    unexpected.push(`${request.method()} ${path}`); return json({ detail: 'Unexpected mock route' }, 404);
  });
  return { task, secondTask, tasks, members, challenges, records, sent, resumes, deletions, reports, operations, unexpected,
    finish: (status = 'stopped', ready = true) => {
      task.status = status; task.ctf_phase = 'closed'; task.cleanup_ready = ready;
      task.ctf_conclusion = { end_reason: status === 'failed' ? 'system_failure' : 'partial', summary: status === 'failed' ? 'CTF 执行因内部错误停止；请使用失败追踪 ID 查询受控日志。' : '已保存模拟结论与证据，未完成题留待下一轮。', unresolved_items: ['模拟 Web 题仍需验证'], evidence_refs: [artifact.uri], lead_claim: false, verification_refs: [], ...(status === 'failed' ? { failure: { error_type: 'RuntimeError', phase: 'tick', occurred_at: at, correlation_id: '00000000-0000-4000-8000-000000000001', summary: 'CTF 执行因内部错误停止；请使用失败追踪 ID 查询受控日志。' } } : {}) };
      task.report_uri = ready ? `reports/${CTF_ID}/run-1.md` : null;
      task.workspace_uri = ready ? `workspace/${CTF_ID}/run-1.tar.zst` : null;
      task.runs = [{ run_number: 1, report_uri: task.report_uri, workspace_uri: task.workspace_uri }];
    },
    failResume: () => { resumeFailures = 1; },
    holdResume: () => { resumeHold = new Promise<void>((resolve) => { resumeRelease = resolve; }); },
    releaseResume: () => { resumeRelease?.(); resumeHold = null; },
    failSend: () => { failures = 1; },
    holdSend: () => { hold = new Promise<void>((resolve) => { release = resolve; }); },
    releaseSend: () => { release?.(); hold = null; },
  };
}
