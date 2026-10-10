import { selectOption, expectSelectedOption, expectOption } from './ui';
import { expect, test, type Page } from '@playwright/test';
import { TASK_ID, installMockApi } from './fixtures';

async function setupModels(page: Page) {
  const mock = await installMockApi(page);
  const original = mock.platformModels[0];
  Object.assign(original.config, { provider: 'openai_responses', model: 'deepseek-flash', reasoning_effort: 'xhigh' });
  const native = { ...original, name: 'native', label: 'DeepSeek 官方', is_default: false,
    config: { ...original.config, provider: 'deepseek', reasoning_effort: 'low' } };
  const local = { ...original, name: 'local', label: '本地模型', is_default: false,
    config: { ...original.config, provider: 'foundry_local', model: 'local-chat', reasoning_effort: 'none' } };
  mock.platformModels.push(native, local);
  await page.route('**/api/platform/providers', (route) => route.fulfill({ json: [
    { id: 'openai_responses', label: 'OpenAI Responses', supports_reasoning_effort: true, reasoning_efforts: ['minimal', 'low', 'medium', 'high', 'xhigh'], options_fields: [], credential_fields: [{ name: 'api_key', label: 'API 密钥', required: true }], allow_no_auth: false, default_base_url: 'https://gateway.example.invalid', base_url_required: true },
    { id: 'deepseek', label: 'DeepSeek', supports_reasoning_effort: true, reasoning_efforts: ['low', 'high', 'max'], options_fields: [], credential_fields: [], allow_no_auth: false, default_base_url: 'provider-default', base_url_required: false },
    { id: 'foundry_local', label: 'Foundry Local', supports_reasoning_effort: false, reasoning_efforts: [], options_fields: [], credential_fields: [], allow_no_auth: true, default_base_url: 'provider-default', base_url_required: false },
  ] }));
  return mock;
}

for (const effort of ['', 'max', 'none']) {
  test(`创建任务的思考强度 ${effort || '继承'} 固定到任务，保留模型配置`, async ({ page }) => {
    const mock = await setupModels(page);
    let submitted: Record<string, unknown> | undefined;
    await page.route('**/api/tasks', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      submitted = route.request().postDataJSON();
      await route.fulfill({ json: { id: TASK_ID, agent_profile: 'task-settings', agent_profile_version: 3 } });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/tasks/new');
    const strength = page.getByLabel('推理强度');
    await expectSelectedOption(page, strength, '沿用模型配置（xhigh）');
    await expectOption(page, strength, 'max');
    if (effort) await selectOption(page, strength, effort === 'none' ? '模型默认（不传参数）' : effort);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
    await page.getByLabel('任务目标').fill('核对两份统计资料');
    await page.getByLabel('验收条件 1', { exact: true }).fill('列出可复核的差异');
    if (effort === 'max') await page.screenshot({ path: test.info().outputPath('create-max-mobile.png'), fullPage: true });
    await page.getByRole('button', { name: /创建任务/ }).click();
    await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
    if (effort) expect(submitted?.reasoning_effort).toBe(effort);
    else expect(submitted).not.toHaveProperty('reasoning_effort');
    expect(submitted).toMatchObject({ model_id: 'review-model', model_version: 1 });
    expect(mock.platformModels[0].config).toMatchObject({ reasoning_effort: 'xhigh' });
    expect(mock.modelSaves).toHaveLength(0);
    expect(mock.unexpected).toEqual([]);
  });
}

test('切换模型重置任务覆盖，DeepSeek原生支持max，不支持的Provider不显示强度', async ({ page }) => {
  const mock = await setupModels(page);
  await page.goto('/tasks/new');
  await selectOption(page, page.getByLabel('推理强度'), 'max');
  await selectOption(page, page.getByLabel('平台模型'), 'DeepSeek 官方');
  await expectSelectedOption(page, page.getByLabel('推理强度'), '沿用模型配置（low）');
  await selectOption(page, page.getByLabel('推理强度'), 'max');
  await selectOption(page, page.getByLabel('平台模型'), '本地模型');
  await expect(page.getByLabel('推理强度')).toHaveCount(0);
  await expect(page.getByText('该连接方式使用模型默认思考设置。')).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('模型配置保留既有兼容值，DeepSeek Responses可保存max', async ({ page }) => {
  const mock = await setupModels(page);
  await page.goto('/profiles');
  await page.getByRole('tab', { name: /模型配置$/ }).click();
  await selectOption(page, page.getByRole('combobox', { name: '模型连接', exact: true }), '审查模型');
  const editor = page.getByRole('region', { name: '模型配置编辑' });
  await expectSelectedOption(page, editor.getByLabel('推理强度'), 'xhigh（当前兼容设置）');
  await selectOption(page, editor.getByLabel('推理强度'), 'max');
  await page.screenshot({ path: test.info().outputPath('model-max-desktop.png'), fullPage: true });
  await editor.getByRole('button', { name: /保存$/ }).click();
  await expect(page.getByRole('status')).toContainText('已保存');
  expect(mock.modelSaves[0]).toMatchObject({ provider: 'openai_responses', model: 'deepseek-flash', reasoning_effort: 'max' });
  expect(mock.modelSaves[0]).not.toHaveProperty('credentials.api_key');
  expect(mock.unexpected).toEqual([]);
});
