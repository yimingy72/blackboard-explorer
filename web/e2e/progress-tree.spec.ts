import { expect, test, type Page } from '@playwright/test';
import { installConversationFixture } from './conversation-fixture';
import { TASK_ID, pushMockBoardEvent } from './fixtures';
import type { ProgressMessage, ProgressNode, ProgressView } from '../src/progress/types';

const ref = { kind: 'fact' as const, id: 'F1', version: 4 };
function node(id: string, parent: string | null, title: string): ProgressNode {
  return { id, parent_id: parent, order: 0, kind: parent === null ? 'goal' : parent === 'goal' ? 'direction' : 'finding', title,
    question: '延迟是否与连接池等待有关？', summary: '高峰请求持续等待连接，现有日志支持进一步检查连接释放路径。', significance: '将排查重点从响应解析转向连接资源。', next_step: '补齐等待时长与释放记录的对应关系。', limitations: '尚未获得修复后的对照结果。', key_steps: [{ text: '复核三次请求的等待记录，排除单次波动。', source_refs: [ref] }], source_refs: parent ? [ref] : [], effective_state: 'supported', contributors: ['agent-1'] };
}
async function fixture(page: Page, enabled = true) {
  const board = await installConversationFixture(page, { running: true });
  const state: ProgressView = { enabled, status: 'idle', revision: 1, source_version: 20, desired_source_version: 20, published_version: 21, run_number: 1, overview: '已确认请求在连接获取阶段等待；解析结构保持一致。主要缺口是连接释放路径与修复后的对照证据。', nodes: enabled ? [node('goal', null, '查明延迟并保留复现依据'), node('connections', 'goal', '连接资源是当前优先方向'), node('wait', 'connections', '高峰请求在获取连接时等待')] : [], budget: { max_cost: '2', spent: '0.08', reserved: '0', unknown: '0' }, updated_at: '2026-10-19T00:00:00Z', error: null };
  const messages: ProgressMessage[] = [];
  const sent: Array<{ id: string; content: string; node_id: string | null }> = [];
  const configs: unknown[] = [], reads: string[] = [], refreshes: unknown[] = [];
  let failure = false;
  await page.route(`**/api/tasks/${TASK_ID}/progress**`, route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.split('/progress')[1];
    if (path === '') return route.fulfill({ json: state });
    if (path === '/settings') {
      const body = request.postDataJSON() as { enabled: boolean; max_cost: string };
      configs.push(body); state.enabled = body.enabled; state.budget.max_cost = body.max_cost;
      if (body.enabled && !state.nodes.length) state.nodes = [node('goal', null, '查明延迟并保留复现依据'), node('connections', 'goal', '连接资源是当前优先方向')];
      return route.fulfill({ json: state });
    }
    if (path === '/refresh') { refreshes.push(request.postDataJSON()); return route.fulfill({ json: { status: 'queued' } }); }
    if (path === '/sources') { reads.push(request.url()); return route.fulfill({ json: { object: { id: 'F1', statement: '数据库连接池出现等待', version: 4, author: 'agent-1', evidence: [{ type: 'text', summary: '登记输出', registered_uri: `evidence/${TASK_ID}/safe.txt`, registration_version: 4, sha256: 'a'.repeat(64) }] } } }); }
    if (path === '/evidence') { reads.push(request.url()); return route.fulfill({ json: { content: 'pool_wait=1200ms\nconnection_released=false', offset: 0, next_offset: 50, has_more: false } }); }
    if (path === '/messages') {
      if (request.method() === 'GET') return route.fulfill({ json: { messages } });
      const body = request.postDataJSON() as typeof sent[number]; sent.push(body);
      if (failure) { failure = false; return route.abort('failed'); }
      const now = '2026-10-19T00:00:00Z';
      const message: ProgressMessage = { ...body, task_id: TASK_ID, agent_id: '__progress__', role: 'user', status: 'completed', reply_to: null, usage: null, created_at: now, updated_at: now };
      if (!messages.some(item => item.id === body.id)) messages.push(message, { ...message, id: `reply-${body.id}`, role: 'assistant', content: '目前缺少释放路径的对照。[查看依据](fact:F1)，整理截至 v20。', usage: { source_version: 20 } });
      return route.fulfill({ json: message });
    }
    return route.fulfill({ status: 404, json: { detail: 'Unknown progress fixture route' } });
  });
  return { board, state, configs, sent, reads, refreshes, failSend: () => { failure = true; } };
}
async function selectFinding(page: Page) {
  await page.getByRole('button', { name: /^阶段发现：高峰请求在获取连接时等待/ }).click();
  return page.getByRole('complementary', { name: '成果详情与对话', exact: true });
}

test('成果树保留具体认识与缺口，详情引用使用截至版本和校验证据接口', async ({ page }) => {
  const mock = await fixture(page);
  await page.goto(`/tasks/${TASK_ID}/progress`);
  await expect(page.getByRole('complementary', { name: '成果详情与对话', exact: true })).toHaveCount(0);
  await expect(page.getByText(mock.state.overview, { exact: true })).toBeVisible();
  const panel = await selectFinding(page);
  await expect(panel).toContainText('将排查重点从响应解析转向连接资源。');
  await expect(panel).toContainText('补齐等待时长与释放记录的对应关系。');
  await panel.getByRole('button', { name: '查看对应版本的原始记录' }).first().click();
  const drawer = page.getByRole('dialog', { name: 'F1 · 原始依据' });
  await expect(drawer).toContainText('数据库连接池出现等待');
  await drawer.getByRole('button', { name: '登记输出' }).click();
  await expect(drawer).toContainText('pool_wait=1200ms');
  expect(mock.reads.some(url => url.includes('through_version=20'))).toBe(true);
  expect(mock.reads.some(url => url.includes('/progress/evidence?') && url.includes('sha256='))).toBe(true);
  expect(mock.board.unexpected).toEqual([]);
});

test('成果问答回车、重试同ID、引用和双视图草稿保持', async ({ page }) => {
  const mock = await fixture(page); mock.failSend();
  await page.addInitScript(() => Object.defineProperty(Crypto.prototype, 'randomUUID', { value: undefined }));
  await page.goto(`/tasks/${TASK_ID}/progress`);
  await page.getByRole('button', { name: '问整理 Agent', exact: true }).click();
  const panel = page.getByRole('complementary', { name: '成果详情与对话', exact: true }), input = panel.getByRole('textbox');
  await input.fill('现在缺什么证据？'); await input.press('Shift+Enter'); await input.pressSequentially('more');
  expect(mock.sent).toHaveLength(0); await input.press('Enter');
  await expect(panel.getByRole('alert')).toContainText('内容已保留');
  await panel.getByRole('button', { name: '关闭详情', exact: true }).click();
  await page.getByRole('button', { name: '问整理 Agent', exact: true }).click();
  await input.press('Enter'); await expect.poll(() => mock.sent.length).toBe(2);
  expect(mock.sent[1].id).toBe(mock.sent[0].id);
  await expect(panel.getByRole('button', { name: '查看依据', exact: true })).toBeVisible();
  await input.fill('切换视图后保留的草稿');
  await page.getByRole('link', { name: '协作黑板', exact: true }).click();
  await page.getByRole('link', { name: '成果树', exact: true }).click();
  await expect(panel.getByRole('textbox')).toHaveValue('切换视图后保留的草稿');
  expect(mock.board.unexpected).toEqual([]);
});

test('旧任务必须显式启用，独立金额不改变原求解预算', async ({ page }) => {
  const mock = await fixture(page, false), budget = structuredClone(mock.board.taskFixture.budget);
  mock.state.nodes = [node('goal', null, '查明延迟并保留复现依据')]; mock.state.revision = 0;
  await page.goto(`/tasks/${TASK_ID}/progress`);
  await expect(page.getByRole('heading', { name: '把探索记录整理成可读成果' })).toBeVisible();
  expect(mock.configs).toEqual([]);
  await page.getByLabel('独立整理费用上限', { exact: true }).fill('3');
  await page.getByRole('button', { name: '启用成果整理', exact: true }).click();
  await expect(page.getByRole('region', { name: '成果树画布' })).toBeVisible();
  expect(mock.configs).toEqual([{ enabled: true, max_cost: '3' }]);
  expect(mock.board.taskFixture.budget).toEqual(budget);
});

test('拖动坐标刷新保持，来源变化不重排已固定节点', async ({ page }) => {
  const mock = await fixture(page); await page.setViewportSize({ width: 1500, height: 980 });
  await page.goto(`/tasks/${TASK_ID}/progress`);
  const item = page.getByRole('button', { name: /^阶段发现：高峰请求在获取连接时等待/ });
  await expect(item).toBeVisible();
  const box = await item.boundingBox(); expect(box).not.toBeNull();
  await page.mouse.move(box!.x + 50, box!.y + 45); await page.mouse.down(); await page.mouse.move(box!.x + 120, box!.y + 90, { steps: 8 }); await page.mouse.up();
  const saved = await page.evaluate(id => localStorage.getItem(`bbx:progress-tree:${id}`), TASK_ID);
  expect(saved).toContain('wait'); await page.reload();
  await expect(item).toBeVisible();
  expect(await page.evaluate(id => localStorage.getItem(`bbx:progress-tree:${id}`), TASK_ID)).toBe(saved);
  mock.state.nodes.at(-1)!.effective_state = 'sources_changed'; mock.state.revision += 1;
  const event = { ...mock.board.events[0], version: 1000, type: 'progress.tree.updated', payload: { revision: 2 } };
  await pushMockBoardEvent(page, event);
  await expect(item).toContainText('依据有变化');
});

test('窄屏详情与对话可关闭，无横向溢出', async ({ page }) => {
  await fixture(page); await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/tasks/${TASK_ID}/progress`);
  await page.getByRole('button', { name: '问整理 Agent', exact: true }).click();
  const panel = page.getByRole('complementary', { name: '成果详情与对话', exact: true });
  await expect(panel).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await panel.getByRole('button', { name: '关闭详情', exact: true }).click();
  await expect(panel).toHaveCount(0);
});
