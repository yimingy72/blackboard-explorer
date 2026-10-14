import { expect, test, type Page } from '@playwright/test';
import type { CtfPlatformToolBinding, WorkerTools } from '../src/api/client';
import { installMockApi } from './fixtures';
import { expectSelectedOption, selectMode, selectOption } from './ui';

type Role = 'lead' | 'teammate';
type Save = {
  role: Role;
  expected_revision: number;
  prompt: string;
  tools: WorkerTools;
  platform_tools: CtfPlatformToolBinding[];
};

const shared: CtfPlatformToolBinding = {
  server_name: 'reference', server_version: 1, tool_name: 'status',
  purpose: 'status', result_adapter: 'none', read_only: true,
};
const pending: CtfPlatformToolBinding = {
  server_name: 'analysis', server_version: 1, tool_name: 'submit',
  purpose: 'submit', result_adapter: 'none', read_only: false,
};
const reference = { name: 'reference', version: 1, allowed_tools: ['status'] };

async function installSettings(page: Page, teammateUsesReference: boolean) {
  const mock = await installMockApi(page);
  let revision = 3;
  const profile = {
    prompt_templates: { lead: 'Lead original', teammate: 'Teammate original' },
    worker_tools: {
      lead: { builtin: ['list_members', 'send_message', 'create_teammate', 'finish_task'], mcp_servers: [reference] } as WorkerTools,
      teammate: { builtin: ['list_members', 'send_message'], mcp_servers: teammateUsesReference ? [reference] : [] } as WorkerTools,
    },
    platform_tools: [shared],
  };
  const saves: Save[] = [];
  await page.route('**/api/settings/ctf/workers', route => route.fulfill({ json: { revision, profile } }));
  await page.route('**/api/settings/ctf/workers/*', async route => {
    const role = route.request().url().split('/').at(-1) as Role;
    const body = route.request().postDataJSON() as Omit<Save, 'role'>;
    saves.push({ ...body, role });
    profile.prompt_templates[role] = body.prompt;
    profile.worker_tools[role] = body.tools;
    profile.platform_tools = body.platform_tools;
    revision += 1;
    await route.fulfill({ json: { revision, profile } });
  });
  await page.route('**/api/platform/mcp-servers/analysis/versions/1/tools', route =>
    route.fulfill({ json: { tools: [{ name: 'submit', description: 'Submit mock candidate' }] } }));
  await page.goto('/profiles');
  await page.getByRole('tab', { name: 'CTF 配置', exact: true }).click();
  await expect(page.getByLabel('Lead完整系统提示词')).toHaveValue('Lead original');
  return { mock, saves };
}

test('CTF 解绑当前角色唯一 MCP 服务时不提交孤儿工具用途', async ({ page }) => {
  const { mock, saves } = await installSettings(page, false);
  await page.getByRole('checkbox', { name: /资料检索/ }).uncheck();
  await page.getByRole('button', { name: '保存 CTF 配置', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('CTF 配置已保存');
  expect(saves).toHaveLength(1);
  expect(saves[0].tools.mcp_servers).toEqual([]);
  expect(saves[0].platform_tools).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

test('CTF 保存保留另一角色共享用途，并延后提交其未保存工具用途', async ({ page }) => {
  const { mock, saves } = await installSettings(page, true);
  await selectMode(page, '队友 · Teammate');
  await page.getByRole('checkbox', { name: /分析工具/ }).check();
  await page.getByRole('button', { name: '读取 分析工具 工具清单', exact: true }).click();
  await page.getByRole('checkbox', { name: /submit/ }).check();
  await selectOption(page, page.getByLabel('submit 用途'), '提交候选答案');

  await selectMode(page, 'Lead · 团队负责人');
  await page.getByRole('checkbox', { name: /资料检索/ }).uncheck();
  await page.getByRole('button', { name: '保存 CTF 配置', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('CTF 配置已保存');
  expect(saves).toHaveLength(1);
  expect(saves[0]).toMatchObject({ role: 'lead', expected_revision: 3, tools: { mcp_servers: [] } });
  expect(saves[0].platform_tools).toEqual([shared]);

  await selectMode(page, '队友 · Teammate');
  await expect(page.getByRole('checkbox', { name: /分析工具/ })).toBeChecked();
  await expectSelectedOption(page, page.getByLabel('submit 用途'), '提交候选答案');
  await page.getByRole('button', { name: '保存 CTF 配置', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('CTF 配置已保存');
  expect(saves).toHaveLength(2);
  expect(saves[1]).toMatchObject({ role: 'teammate', expected_revision: 4 });
  expect(saves[1].tools.mcp_servers).toEqual([reference, { name: 'analysis', version: 1, allowed_tools: ['submit'] }]);
  expect(saves[1].platform_tools).toEqual([shared, pending]);
  expect(mock.unexpected).toEqual([]);
});
