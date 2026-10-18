import { expect, test, type Locator, type Page } from '@playwright/test';
import type { AgentMessage } from '../src/api/client';
import { installConversationFixture } from './conversation-fixture';
import { CTF_ID, installCtfApi } from './ctf-fixtures';
import { TASK_ID } from './fixtures';

test.beforeEach(async ({ page }) => {
  // Public HTTP origins expose getRandomValues but not randomUUID.
  await page.addInitScript(() => Object.defineProperty(Crypto.prototype, 'randomUUID', { value: undefined }));
});

async function checkKeyboard(draft: Locator, sent: () => number) {
  await expect(draft).toBeEnabled();
  await draft.fill('第一行');
  await draft.press('Shift+Enter');
  await draft.pressSequentially('second line');
  await expect(draft).toHaveValue('第一行\nsecond line');
  expect(sent()).toBe(0);
  await draft.dispatchEvent('compositionstart');
  await draft.press('Enter');
  expect(sent()).toBe(0);
  await draft.dispatchEvent('compositionend');
  await draft.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 229, isComposing: false, bubbles: true });
  expect(sent()).toBe(0);
  await draft.fill('确认后发送');
  await draft.press('Enter');
  await expect.poll(sent).toBe(1);
  await expect(draft).toHaveValue('');
}

async function openLead(page: Page) {
  await page.goto(`/tasks/${CTF_ID}`);
  await page.getByRole('button', { name: 'Agent：Lead', exact: true }).click();
  return page.getByRole('complementary', { name: 'Lead 会话', exact: true });
}

for (const status of ['running', 'agent-finished', 'task-finished'] as const) {
  test(`ordinary ${status}: HTTP-compatible chat, Enter send, Shift+Enter and IME`, async ({ page }) => {
    const mock = await installConversationFixture(page, { running: status !== 'task-finished' });
    if (status === 'agent-finished') mock.events.push({
      ...mock.events[0], version: mock.events.length + 1, type: 'agent.finished',
      object_id: 'agent-1', payload: { agent_id: 'agent-1', end_reason: 'normal' },
    });
    const rows: AgentMessage[] = [];
    const sent: Array<{ id: string; content: string }> = [];
    await page.route(`**/api/tasks/${TASK_ID}/agents/agent-1/messages`, route => {
      if (route.request().method() === 'GET') return route.fulfill({ json: {
        messages: rows, session_available: true, session_origin: 'native', mode: status === 'running' ? 'active' : 'review',
      } });
      const body = route.request().postDataJSON() as typeof sent[number];
      sent.push(body);
      const message: AgentMessage = {
        ...body, task_id: TASK_ID, agent_id: 'agent-1', role: 'user', status: 'completed', reply_to: null, usage: null,
        created_at: '2026-10-18T08:00:00Z', updated_at: '2026-10-18T08:00:00Z',
      };
      rows.push(message, { ...message, id: `answer-${sent.length}`, role: 'assistant', reply_to: message.id, content: '已收到你的问题。' });
      return route.fulfill({ json: message });
    });
    await page.goto(`/tasks/${TASK_ID}`);
    await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
    const chat = page.getByRole('complementary', { name: 'Agent 1 对话记录', exact: true });
    const draft = chat.getByRole('textbox');
    await checkKeyboard(draft, () => sent.length);
    expect(sent[0].id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    await expect(chat.getByText('已收到你的问题。', { exact: true })).toBeVisible();
    await draft.fill('点击发送');
    await chat.getByRole('button', { name: '发送', exact: true }).click();
    await expect.poll(() => sent.length).toBe(2);
    expect(sent[1].id).not.toBe(sent[0].id);
    expect(mock.unexpected).toEqual([]);
  });
}

for (const finished of [false, true]) {
  test(`CTF ${finished ? 'finished review' : 'active turn'}: HTTP-compatible chat and keyboard`, async ({ page }) => {
    const mock = await installCtfApi(page);
    if (finished) mock.finish();
    const chat = await openLead(page);
    await checkKeyboard(chat.getByRole('textbox'), () => mock.sent.length);
    expect(mock.sent[0]).toMatchObject({ content: '确认后发送', purpose: finished ? 'review' : 'execution' });
    expect(mock.sent[0].id).toMatch(/^[0-9a-f-]{36}$/);
    await chat.getByRole('textbox').fill('保留原快捷键');
    await chat.getByRole('textbox').press('Control+Enter');
    await expect.poll(() => mock.sent.length).toBe(2);
    expect(mock.operations).toEqual([]);
    expect(mock.resumes).toEqual([]);
    expect(mock.unexpected).toEqual([]);
  });
}

test('HTTP chat retry keeps the request ID and draft', async ({ page }) => {
  const mock = await installCtfApi(page);
  mock.failSend();
  const chat = await openLead(page);
  const draft = chat.getByRole('textbox');
  await draft.fill('失败后重试同一条消息');
  await draft.press('Enter');
  await expect(chat.getByRole('alert')).toContainText('消息内容已保留');
  await expect(draft).toHaveValue('失败后重试同一条消息');
  await draft.press('Enter');
  await expect.poll(() => mock.sent.length).toBe(2);
  expect(mock.sent[1].id).toBe(mock.sent[0].id);
  await expect(draft).toHaveValue('');
  expect(mock.unexpected).toEqual([]);
});
