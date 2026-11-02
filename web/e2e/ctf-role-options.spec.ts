import { expect, test } from '@playwright/test';
import type { CtfRoleRunOptions } from '../src/api/client';
import { installMockApi } from './fixtures';
import { expectSelectedOption, selectMode, selectOption } from './ui';

test('角色覆盖独立保存，编辑提示词不清空参数，恢复继承可显式提交', async ({ page }, testInfo) => {
  const mock = await installMockApi(page);
  let revision = 1;
  const profile = {
    model: { provider: 'deepseek', model: 'deepseek-flash', reasoning_effort: 'max', max_output_tokens: 393216 },
    role_run_options: { lead: { reasoning_effort: 'high', max_output_tokens: 65536 } } as Partial<Record<'lead' | 'teammate', CtfRoleRunOptions>>,
    prompt_templates: { lead: 'Lead original', teammate: 'Teammate original' },
    worker_tools: {
      lead: { builtin: ['list_members', 'send_message', 'create_teammate', 'finish_task'], mcp_servers: [] },
      teammate: { builtin: ['list_members', 'send_message'], mcp_servers: [] },
    },
    platform_tools: [],
  };
  const saves: Array<{ role: string; run_options?: CtfRoleRunOptions | null }> = [];
  await page.route('**/api/settings/ctf/workers', route => route.fulfill({ json: { revision, profile } }));
  await page.route('**/api/settings/ctf/workers/*', async route => {
    const role = route.request().url().split('/').at(-1) as 'lead' | 'teammate';
    const body = route.request().postDataJSON();
    saves.push({ ...body, role });
    if (Object.hasOwn(body, 'run_options')) {
      if (body.run_options === null) delete profile.role_run_options[role];
      else profile.role_run_options[role] = body.run_options;
    }
    profile.prompt_templates[role] = body.prompt;
    profile.worker_tools[role] = body.tools;
    await route.fulfill({ json: { revision: ++revision, profile } });
  });
  await page.goto('/profiles');
  await page.getByRole('tab', { name: 'CTF 配置', exact: true }).click();
  await expectSelectedOption(page, page.getByLabel('Lead推理强度'), 'high');
  await expect(page.getByLabel('Lead单次输出上限')).toHaveValue('65536');
  await page.getByLabel('Lead完整系统提示词').fill('Lead updated');
  const save = page.getByRole('button', { name: '保存 CTF 配置', exact: true });
  await save.click();
  await expect(save).toBeDisabled();
  expect(saves[0]).not.toHaveProperty('run_options');

  await selectMode(page, '队友');
  await selectOption(page, page.getByLabel('队友推理强度'), 'max');
  await page.getByLabel('队友单次输出上限').fill('131072');
  await selectMode(page, 'Lead');
  await selectOption(page, page.getByLabel('Lead推理强度'), 'low');
  await page.getByLabel('Lead单次输出上限').fill('32768');
  await save.click();
  await expect(save).toBeDisabled();
  expect(saves[1]).toMatchObject({ role: 'lead', run_options: { reasoning_effort: 'low', max_output_tokens: 32768 } });
  await selectMode(page, '队友');
  await expectSelectedOption(page, page.getByLabel('队友推理强度'), 'max');
  await expect(page.getByLabel('队友单次输出上限')).toHaveValue('131072');
  await save.click();
  await expect(save).toBeDisabled();
  expect(saves[2]).toMatchObject({ role: 'teammate', run_options: { reasoning_effort: 'max', max_output_tokens: 131072 } });

  await selectMode(page, 'Lead');
  await selectOption(page, page.getByLabel('Lead推理强度'), '继承模型');
  await page.getByLabel('Lead单次输出上限').fill('');
  await save.click();
  await expect(save).toBeDisabled();
  expect(saves[3]).toMatchObject({ role: 'lead', run_options: null });
  await page.screenshot({ path: testInfo.outputPath('role-options-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(mock.unexpected).toEqual([]);
});
