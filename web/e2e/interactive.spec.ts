import { expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { installMockApi, TASK_ID, GOAL } from './fixtures';
import { installConversationFixture } from './conversation-fixture';
import type { AgentMessage } from '../src/api/client';

function userMessage(id: string, content: string, status: AgentMessage['status']): AgentMessage {
  return { id, content, status, task_id: TASK_ID, agent_id: 'agent-1', role: 'user', reply_to: null, usage: null, created_at: '2026-09-27T09:00:00Z', updated_at: '2026-09-27T09:00:00Z' };
}

test('运行中的消息可发送并显示投递状态，网络重试沿用同一个消息ID', async ({ page }) => {
  const mock = await installConversationFixture(page, { running: true });
  const rows: AgentMessage[] = [];
  const ids: string[] = [];
  await page.route(`**/api/tasks/${TASK_ID}/agents/agent-1/messages`, async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { messages: rows, session_available: true, session_origin: 'native', mode: 'active' } });
    const body = route.request().postDataJSON() as { id: string; content: string };
    ids.push(body.id);
    if (ids.length === 1) return route.abort();
    const message = userMessage(body.id, body.content, 'delivered');
    rows.push(message);
    return route.fulfill({ json: message });
  });
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
  const pane = page.getByRole('complementary', { name: 'Agent 1 对话记录' });
  await pane.getByRole('textbox').fill('优先核对日期口径，已有中间发现请及时共享。');
  await pane.getByRole('button', { name: '发送', exact: true }).click();
  await expect(pane.getByRole('alert')).toBeVisible();
  await pane.getByRole('button', { name: '发送', exact: true }).click();
  await expect(pane.getByText('已送达', { exact: true })).toBeVisible();
  expect(ids).toHaveLength(2);
  expect(ids[0]).toBe(ids[1]);
  await expect(pane.getByRole('textbox')).toHaveValue('');
  expect(mock.unexpected).toEqual([]);
});

test('结束后继续复盘问答并在刷新后保留，历史回放不可发送', async ({ page }) => {
  const mock = await installMockApi(page);
  const rows: AgentMessage[] = [];
  await page.route(`**/api/tasks/${TASK_ID}/agents/agent-1/messages`, async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { messages: rows, session_available: rows.length > 0, session_origin: rows.length ? 'legacy' : null, mode: 'review' } });
    const body = route.request().postDataJSON() as { id: string; content: string };
    const question = userMessage(body.id, body.content, 'completed');
    rows.push(question, { ...question, id: 'reply-1', role: 'assistant', reply_to: body.id, content: '结论依据来自 F1 的连接池配置与工具记录。', usage: { cost: 0.001 }, created_at: '2026-09-27T09:00:01Z' });
    return route.fulfill({ json: question });
  });
  await page.goto(`/tasks/${TASK_ID}?focus=agent-1`);
  const pane = page.getByRole('complementary', { name: 'Agent 1 对话记录' });
  await expect(pane).toContainText('旧会话未保存');
  await pane.getByRole('textbox').fill('请解释这次结论的依据。');
  await pane.getByRole('button', { name: '发送', exact: true }).click();
  await expect(pane).toContainText('结论依据来自 F1');
  await mkdir('../.data/qa', { recursive: true });
  await page.screenshot({ path: '../.data/qa/interactive-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: '../.data/qa/interactive-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.reload();
  await expect(pane).toContainText('请解释这次结论的依据。');
  await expect(pane).toContainText('此复盘会话基于旧任务记录初始化');
  await page.getByRole('button', { name: '复盘记录', exact: true }).click();
  await page.getByRole('tab', { name: '时间轴' }).click();
  const slider = page.getByRole('slider', { name: '回放版本' });
  await slider.focus(); await slider.press('Home');
  for (let index = 0; index < 2; index++) await slider.press('ArrowRight');
  await page.getByRole('button', { name: '关闭复盘记录' }).click();
  await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
  await expect(pane.getByRole('textbox')).toBeDisabled();
  await expect(pane).not.toContainText('结论依据来自 F1');
  expect(mock.unexpected).toEqual([]);
});

test('删除任务须确认清除所有会话，取消不发请求', async ({ page }) => {
  const mock = await installMockApi(page);
  let removed = false;
  await page.route(`**/api/tasks/${TASK_ID}`, async (route) => {
    if (route.request().method() !== 'DELETE') return route.fallback();
    removed = true;
    return route.fulfill({ status: 202, json: { deleting: true } });
  });
  await page.route('**/api/tasks', (route) => removed ? route.fulfill({ json: [] }) : route.fallback());
  await page.goto('/tasks');
  await page.getByRole('button', { name: `删除任务：${GOAL}` }).click();
  const dialog = page.getByRole('dialog', { name: '删除任务' });
  await expect(dialog).toContainText('所有 Agent 会话');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  expect(removed).toBe(false);
  await page.getByRole('button', { name: `删除任务：${GOAL}` }).click();
  await dialog.getByRole('button', { name: '确认删除' }).click();
  await expect(page.getByRole('heading', { name: '还没有任务' })).toBeVisible();
  expect(removed).toBe(true);
  expect(mock.unexpected).toEqual([]);
});
