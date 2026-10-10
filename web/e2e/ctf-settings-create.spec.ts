import { selectOption, expectSelectedOption, selectMode } from './ui';
import { expect, test } from '@playwright/test';
import type { CtfPlatformToolBinding, WorkerTools } from '../src/api/client';
import { TASK_ID, installMockApi } from './fixtures';

test('CTF 创建允许零队友和自由完成要求，沿用模型附件预算', async ({ page }) => {
  const mock = await installMockApi(page);
  let submitted: Record<string, unknown> | undefined;
  await page.route('**/api/tasks', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    submitted = route.request().postDataJSON();
    await route.fulfill({ json: { id: TASK_ID, agent_profile: 'ctf-task-settings', agent_profile_version: 3 } });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/tasks/new');
  await selectMode(page, 'CTF 团队');
  await expect(page.getByLabel('队友上限（不含 Lead）')).toHaveValue('4');
  await expect(page.getByLabel('验收条件 1', { exact: true })).toHaveCount(0);
  await page.getByLabel('任务名', { exact: true }).fill('离线 CTF');
  await page.getByLabel('任务目标').fill('检查两道模拟题并保留复现脚本');
  await page.getByRole('textbox', { name: '完成要求', exact: true }).fill('先提交证据；未完成题说明失败条件。\n需要 Lead 总结。');
  await page.getByLabel('队友上限（不含 Lead）').fill('0');
  await page.locator('input[type="file"]').setInputFiles({ name: 'challenge.txt', mimeType: 'text/plain', buffer: Buffer.from('offline fixture') });
  await expect(page.getByText(/已上传/)).toHaveCount(1);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.getByRole('button', { name: /创建任务/ }).click();
  await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
  expect(submitted).toMatchObject({
    mode: 'ctf', name: '离线 CTF', goal: '检查两道模拟题并保留复现脚本',
    completion_requirements: '先提交证据；未完成题说明失败条件。\n需要 Lead 总结。',
    ctf_options: { max_teammates: 0 }, budget: { max_cost: '10', max_minutes: 60 },
    agent_profile: 'ctf', model_id: 'review-model', model_version: 1,
  });
  expect(submitted).not.toHaveProperty('acceptance');
  expect(submitted?.budget).not.toHaveProperty('max_concurrent_agents');
  expect(submitted?.input_file_ids).toHaveLength(1);
  expect(mock.unexpected).toEqual([]);
});

test('CTF 设置用途与模拟适配显式保存，冲突保留草稿和另一角色绑定', async ({ page }) => {
  const mock = await installMockApi(page);
  const kept: CtfPlatformToolBinding = { server_name: 'analysis', server_version: 1, tool_name: 'status', purpose: 'status', result_adapter: 'none', read_only: true };
  const profile = {
    prompt_templates: { lead: 'Lead original', teammate: 'Teammate original' },
    worker_tools: {
      lead: { builtin: ['list_members', 'send_message', 'finish_task'], mcp_servers: [] } as WorkerTools,
      teammate: { builtin: ['list_members'], mcp_servers: [{ name: 'analysis', version: 1, allowed_tools: ['status'] }] } as WorkerTools,
    },
    platform_tools: [kept],
  };
  const saves: Array<{ expected_revision: number; prompt: string; tools: WorkerTools; platform_tools: CtfPlatformToolBinding[] }> = [];
  await page.route('**/api/settings/ctf/workers', (route) => route.fulfill({ json: { revision: 3, profile } }));
  await page.route('**/api/platform/mcp-servers/reference/versions/1/tools', (route) => route.fulfill({ json: { tools: [{ name: 'lookup', description: 'Read simulated target connection' }] } }));
  await page.route('**/api/settings/ctf/workers/lead', async (route) => {
    const body = route.request().postDataJSON();
    saves.push(body);
    if (saves.length === 2) return route.fulfill({ status: 409, json: { detail: 'Concurrent revision' } });
    await route.fulfill({ json: { revision: 4, profile: {
      ...profile, prompt_templates: { ...profile.prompt_templates, lead: body.prompt },
      worker_tools: { ...profile.worker_tools, lead: body.tools }, platform_tools: body.platform_tools,
    } } });
  });
  await page.goto('/profiles');
  await page.getByRole('tab', { name: /CTF 配置$/ }).click();
  await selectMode(page, '队友 · Teammate');
  await page.getByLabel('队友完整系统提示词').fill('Unsaved teammate draft');
  await selectMode(page, 'Lead · 团队负责人');
  await expect.poll(async () => (await page.getByLabel('Lead完整系统提示词').boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(300);
  await page.getByLabel('Lead完整系统提示词').fill('Lead new snapshot');
  await page.getByRole('checkbox', { name: /资料检索/ }).check();
  await page.getByRole('button', { name: '读取 资料检索 工具清单' }).click();
  await page.getByRole('checkbox', { name: /lookup/ }).check();
  await page.getByRole('button', { name: /保存 CTF 配置/ }).click();
  await expect(page.getByRole('alert')).toContainText('指定用途');
  expect(saves).toHaveLength(0);
  await selectOption(page, page.getByLabel('lookup 用途'), '连接信息');
  await selectOption(page, page.getByLabel('lookup 结果适配'), '模拟 CTF v1（仅测试）');
  await page.getByRole('checkbox', { name: '只读调用（仅连接或查询）' }).check();
  await page.getByRole('button', { name: /保存 CTF 配置/ }).click();
  await expect(page.getByRole('status')).toContainText('仅用于之后创建的任务');
  expect(saves[0]).toMatchObject({ expected_revision: 3, prompt: 'Lead new snapshot', tools: { mcp_servers: [{ name: 'reference', version: 1, allowed_tools: ['lookup'] }] } });
  expect(saves[0].platform_tools).toContainEqual(kept);
  expect(saves[0].platform_tools).toContainEqual({ server_name: 'reference', server_version: 1, tool_name: 'lookup', purpose: 'connect', result_adapter: 'fake_ctf_v1', read_only: true });
  await page.getByLabel('Lead完整系统提示词').fill('Concurrent local Lead draft');
  await page.getByRole('button', { name: /保存 CTF 配置/ }).click();
  await expect(page.getByRole('alert')).toContainText('当前草稿已保留');
  expect(saves[1].expected_revision).toBe(4);
  await expect(page.getByLabel('Lead完整系统提示词')).toHaveValue('Concurrent local Lead draft');
  await expectSelectedOption(page, page.getByLabel('lookup 用途'), '连接信息');
  await selectMode(page, '队友 · Teammate');
  await expect(page.getByLabel('队友完整系统提示词')).toHaveValue('Unsaved teammate draft');
  await expectSelectedOption(page, page.getByLabel('status 用途'), '查询验证状态');
  expect(mock.workerSaves).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

test('同名任务显示模式并保持各自 ID 链接', async ({ page }) => {
  const mock = await installMockApi(page);
  const other = '22222222-2222-4222-8222-222222222222';
  await page.route('**/api/tasks', (route) => route.fulfill({ json: [TASK_ID, other].map((id, index) => ({
    id, name: '同名任务', goal: 'Shared title', mode: index ? 'ctf' : 'blackboard', status: 'created',
    acceptance_state: {}, usage: {}, created_at: '2026-10-08T00:00:00Z',
  })) }));
  await page.goto('/tasks');
  const ctf = page.getByRole('row').filter({ hasText: 'CTF 团队' });
  const blackboard = page.getByRole('row').filter({ hasText: '黑板探索' });
  await expect(ctf.getByRole('link', { name: '同名任务', exact: true })).toHaveAttribute('href', `/tasks/${other}`);
  await expect(blackboard.getByRole('link', { name: '同名任务', exact: true })).toHaveAttribute('href', `/tasks/${TASK_ID}`);
  expect(mock.unexpected).toEqual([]);
});
