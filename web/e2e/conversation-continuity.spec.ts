import { expect, test, type Page } from '@playwright/test';
import { installConversationFixture } from './conversation-fixture';
import { CTF_ID, installCtfApi } from './ctf-fixtures';
import { TASK_ID } from './fixtures';
import type { AgentMessage } from '../src/api/client';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

async function openLead(page: Page) {
  await page.getByRole('button', { name: 'Agent：Lead', exact: true }).click();
  const chat = page.getByRole('complementary', { name: 'Lead 会话', exact: true, includeHidden: true });
  await expect(chat).toBeVisible();
  return chat;
}

test('普通 Agent 的旧发送成功不会擦掉切回后新写的草稿', async ({ page }) => {
  const mock = await installConversationFixture(page, { running: true });
  mock.events.push({
    ...mock.events[0], version: mock.events.at(-1)!.version + 1,
    type: 'agent.spawned', object_id: 'agent-2',
    payload: { id: 'agent-2', task_type: 'derive' },
  });
  const held = deferred();
  const released = deferred();
  const attempts: Array<{ id: string; content: string }> = [];
  const rows: AgentMessage[] = [];
  await page.route(`**/api/tasks/${TASK_ID}/agents/agent-1/messages`, async (route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({ json: { messages: rows, session_available: true, session_origin: 'native', mode: 'active' } });
    }
    const body = route.request().postDataJSON() as typeof attempts[number];
    attempts.push(body);
    await held.promise;
    const message: AgentMessage = {
      ...body, task_id: TASK_ID, agent_id: 'agent-1', role: 'user', status: 'delivered',
      reply_to: null, usage: null,
      created_at: '2026-10-10T08:00:00Z', updated_at: '2026-10-10T08:00:00Z',
    };
    rows.push(message);
    try {
      await route.fulfill({ json: message });
    } catch (error) {
      if (!route.request().failure()) throw error;
    } finally {
      released.resolve();
    }
  });
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/tasks/${TASK_ID}`);
    const agents = page.getByRole('group', { name: '全部 Agent' });
    await agents.getByRole('button', { name: /Agent 1/ }).click();
    const chat = page.getByRole('complementary', { name: 'Agent 1 对话记录', exact: true });
    const draft = chat.getByRole('textbox');
    await draft.fill('已发送的模拟旧指令');
    await chat.getByRole('button', { name: '发送', exact: true }).click();
    await expect.poll(() => attempts.length).toBe(1);
    await agents.getByRole('button', { name: /Agent 2/ }).click();
    await expect(page.getByRole('complementary', { name: 'Agent 2 对话记录', exact: true })).toBeVisible();
    await agents.getByRole('button', { name: /Agent 1/ }).click();
    await draft.fill('切回后尚未发送的模拟新草稿');
    held.resolve();
    await released.promise;
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(draft).toHaveValue('切回后尚未发送的模拟新草稿');
    expect(attempts).toHaveLength(1);
    expect(rows).toHaveLength(1);
    expect(mock.unexpected).toEqual([]);
  } finally {
    held.resolve();
  }
});

for (const transition of ['resize', 'reopen'] as const) {
  test(`CTF 未知发送结果${transition === 'resize' ? '跨899px布局切换' : '关闭重开'}后重试同ID且不重复生效`, async ({ page }) => {
    const mock = await installCtfApi(page);
    const attempts: Array<{ id: string; content: string }> = [];
    const applied = new Map<string, { id: string; body: string; sender_id: string; sender_kind: string; recipient_id: string; kind: string; status: string; deferred: boolean; created_at: string }>();
    let sideEffects = 0;
    await page.route(`**/api/tasks/${CTF_ID}/agents/lead/messages`, async (route) => {
      if (route.request().method() === 'GET') return route.fulfill({ json: { messages: [...applied.values()] } });
      const body = route.request().postDataJSON() as typeof attempts[number];
      attempts.push(body);
      if (!applied.has(body.id)) {
        sideEffects += 1;
        applied.set(body.id, {
          id: body.id, body: body.content, sender_id: 'user', sender_kind: 'user', recipient_id: 'lead',
          kind: 'message', status: 'delivered', deferred: false, created_at: '2026-10-10T08:00:00Z',
        });
      }
      if (attempts.length === 1) return route.abort('failed');
      return route.fulfill({ json: applied.get(body.id) });
    });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/tasks/${CTF_ID}`);
    const chat = await openLead(page);
    await chat.getByRole('textbox').fill('只能生效一次的模拟指令');
    await chat.getByRole('button', { name: '发送消息', exact: true }).click();
    await expect.poll(() => attempts.length).toBe(1);
    await expect(chat.getByRole('alert')).toContainText('消息内容已保留');
    expect(sideEffects).toBe(1);
    if (transition === 'resize') {
      await page.setViewportSize({ width: 768, height: 900 });
      await expect(chat).toBeVisible();
      await expect(chat.getByRole('textbox')).toHaveValue('只能生效一次的模拟指令');
      await page.setViewportSize({ width: 1440, height: 900 });
    } else {
      await chat.getByRole('button', { name: '关闭详情', exact: true }).click();
      await expect(chat).toBeHidden();
      await openLead(page);
    }
    await expect(chat).toBeVisible();
    await expect(chat.getByRole('textbox')).toHaveValue('只能生效一次的模拟指令');
    await chat.getByRole('button', { name: /^(?:发送消息|重试发送)$/ }).click();
    await expect.poll(() => attempts.length).toBe(2);
    expect(attempts[1]).toEqual(attempts[0]);
    expect(sideEffects).toBe(1);
    expect(applied.size).toBe(1);
    await expect(chat.getByRole('textbox')).toHaveValue('');
    await expect(chat.getByText('只能生效一次的模拟指令', { exact: true })).toHaveCount(1);
    expect(mock.operations).toEqual([]);
    expect(mock.resumes).toEqual([]);
    expect(mock.unexpected).toEqual([]);
  });
}

test('手机 Goal 与附件 Modal 的 Esc 分层关闭，恢复焦点且不卸载草稿', async ({ page }) => {
  const mock = await installCtfApi(page);
  const file = {
    id: 'attachment-continuity', filename: 'source.txt', path: '/workspace/shared/inputs/source.txt',
    uri: `inputs/${CTF_ID}/attachment-continuity/source.txt`, size: 24, sha256: 'a'.repeat(64),
  };
  const task = { ...mock.task, completion_requirements: '模拟完成要求：保留可复核依据', initial_attachments: [file] };
  await page.route(`**/api/tasks/${CTF_ID}`, (route) => route.request().method() === 'GET' ? route.fulfill({ json: task }) : route.fallback());
  await page.route(`**/api/tasks/${CTF_ID}/ctf/state`, (route) => route.fulfill({ json: { task, members: mock.members, challenges: mock.challenges, records: mock.records, artifacts: [] } }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/tasks/${CTF_ID}`);
  const chat = await openLead(page);
  const draft = chat.getByRole('textbox', { includeHidden: true });
  await draft.fill('目标查看前未发送的模拟草稿');
  const opener = page.getByRole('button', { name: '查看任务目标', exact: true });
  await opener.click();
  const goal = page.getByRole('region', { name: '任务目标', exact: true });
  await expect(goal).toBeVisible();
  await expect(goal).toContainText('模拟完成要求：保留可复核依据');
  await expect(chat).toHaveCount(1);
  await expect(chat).toBeHidden();
  await expect(draft).toHaveValue('目标查看前未发送的模拟草稿');
  await goal.getByRole('button', { name: '预览附件 source.txt', exact: true }).click();
  const preview = page.getByRole('dialog', { name: 'source.txt', exact: true });
  await expect(preview).toBeVisible();
  await expect(preview.getByRole('link', { name: '下载完整证据', exact: true })).toHaveAttribute('href', /attachment-continuity/);
  await preview.press('Escape');
  await expect(preview).toBeHidden();
  await expect(goal).toBeVisible();
  await expect(chat).toHaveCount(1);
  await expect(chat).toBeHidden();
  const collapse = goal.getByRole('button', { name: '收起目标', exact: true });
  await collapse.focus();
  await collapse.press('Escape');
  await expect(goal).toBeHidden();
  await expect(opener).toBeFocused();
  await expect(chat).toBeVisible();
  await expect(draft).toHaveValue('目标查看前未发送的模拟草稿');
  expect(mock.sent).toEqual([]);
  expect(mock.operations).toEqual([]);
  expect(mock.resumes).toEqual([]);
  expect(mock.deletions).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});
