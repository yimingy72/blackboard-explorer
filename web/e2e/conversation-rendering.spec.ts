import { expect, test, type Page } from '@playwright/test';
import { installConversationFixture } from './conversation-fixture';
import { installCtfApi, CTF_ID } from './ctf-fixtures';
import { TASK_ID } from './fixtures';
import type { AgentMessage } from '../src/api/client';

async function instrument(page: Page, taskId: string) {
  await page.goto('/tasks');
  await page.evaluate(async () => {
    const probe = await import('/e2e/conversation-render-probe.ts');
    probe.installBubbleRenderProbe();
  });
  await page.locator(`a[href="/tasks/${taskId}"]`).first().click();
}

async function renderCount(page: Page) {
  return page.evaluate(async () => (await import('/e2e/conversation-render-probe.ts')).bubbleRenderCount());
}

test('ordinary long conversation does not render the message list while typing and retains retry and reasoning', async ({ page }) => {
  const mock = await installConversationFixture(page, { running: true });
  const rows: AgentMessage[] = Array.from({ length: 200 }, (_, index) => ({
    id: `history-${index}`, task_id: TASK_ID, agent_id: 'agent-1', role: index % 2 ? 'assistant' : 'user',
    content: `第 ${index} 条历史消息：核对证据与进展。`, status: index === 0 ? 'failed' : 'completed',
    reply_to: null, usage: null, created_at: '2026-10-10T08:00:00Z', updated_at: '2026-10-10T08:00:00Z',
  }));
  await page.route(`**/api/tasks/${TASK_ID}/agents/agent-1/messages`, route => route.fulfill({
    json: { messages: rows, session_available: true, session_origin: 'native', mode: 'active' },
  }));
  await instrument(page, TASK_ID);
  await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
  const chat = page.getByRole('complementary', { name: 'Agent 1 对话记录', exact: true });
  await expect(chat.getByRole('article', { name: '用户消息 history-0', exact: true })).toHaveCount(1);
  const reasoning = chat.getByRole('button', { name: '思考 · 模型返回', exact: true });
  await reasoning.click();
  await expect(reasoning).toHaveAttribute('aria-expanded', 'true');
  const before = await renderCount(page);
  const draft = chat.getByRole('textbox');
  await draft.pressSequentially('abcdefghijklmnopqrst');
  await expect(draft).toHaveValue('abcdefghijklmnopqrst');
  const additionalRenders = await renderCount(page) - before;
  console.log(JSON.stringify({ mode: 'ordinary', entries: rows.length, inputs: 20, additionalListRenders: additionalRenders }));
  expect(additionalRenders).toBe(0);
  await expect(reasoning).toHaveAttribute('aria-expanded', 'true');
  rows.push({ ...rows[1], id: 'live', content: '新增的普通会话回复' });
  await expect(chat.getByText('新增的普通会话回复', { exact: true })).toBeVisible();
  expect(await renderCount(page)).toBeGreaterThan(before);
  await expect(reasoning).toHaveAttribute('aria-expanded', 'true');
  await chat.getByRole('button', { name: '重新编辑', exact: true }).press('Enter');
  await expect(draft).toHaveValue(rows[0].content);
  expect(mock.unexpected).toEqual([]);
});

test('CTF long conversation does not render the message list while typing and keeps reasoning during live updates', async ({ page }) => {
  const mock = await installCtfApi(page);
  const rows = Array.from({ length: 200 }, (_, index) => ({
    id: `ctf-history-${index}`, body: `第 ${index} 条团队历史消息。`, sender_id: index % 2 ? 'lead' : 'user',
    sender_kind: index % 2 ? 'agent' : 'user', recipient_id: 'lead', kind: 'message',
    status: 'delivered', deferred: false, created_at: '2026-10-10T08:00:00Z',
  }));
  await page.route(`**/api/tasks/${CTF_ID}/agents/lead/messages`, route => route.fulfill({ json: { messages: rows } }));
  await page.route(`**/api/tasks/${CTF_ID}/agents/lead/session`, route => route.fulfill({ json: {
    revision: 1,
    session: { state: { in_memory: { messages: [{
      message_id: 'thinking', role: 'assistant', contents: [
        { type: 'text_reasoning', text: '先核对证据，再继续验证。', additional_properties: { reasoning_text: true } },
        { type: 'text', text: '保留完整连续会话。' },
      ],
    }] } } },
  } }));
  await instrument(page, CTF_ID);
  await page.getByRole('button', { name: 'Agent：Lead', exact: true }).click();
  const chat = page.getByRole('complementary', { name: 'Lead 会话', exact: true });
  const reasoning = chat.getByRole('button', { name: '思考 · 模型返回', exact: true });
  await reasoning.click();
  await expect(reasoning).toHaveAttribute('aria-expanded', 'true');
  const before = await renderCount(page);
  const draft = chat.getByRole('textbox');
  await draft.pressSequentially('abcdefghijklmnopqrst');
  await expect(draft).toHaveValue('abcdefghijklmnopqrst');
  const additionalRenders = await renderCount(page) - before;
  console.log(JSON.stringify({ mode: 'ctf', entries: rows.length, inputs: 20, additionalListRenders: additionalRenders }));
  expect(additionalRenders).toBe(0);
  rows.push({ ...rows[1], id: 'live', body: '新增的团队会话回复' });
  await expect(chat.getByText('新增的团队会话回复', { exact: true })).toBeVisible();
  expect(await renderCount(page)).toBeGreaterThan(before);
  await expect(reasoning).toHaveAttribute('aria-expanded', 'true');
  await expect(draft).toHaveValue('abcdefghijklmnopqrst');
  expect(mock.unexpected).toEqual([]);
});
