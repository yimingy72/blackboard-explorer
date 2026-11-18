import { expect, test } from '@playwright/test';
import { installMockApi, TASK_ID } from './fixtures';

test('平台模型编辑与冻结选模保留未知费用', async ({ page }) => {
  const mock = await installMockApi(page);
  let model: Record<string, unknown> | undefined;
  let saved: Record<string, unknown> | undefined;
  let task: Record<string, unknown> | undefined;
  await page.route(/\/api\/platform\/pi-models(?:\/fixture-model)?$/, async route => {
    const method = route.request().method();
    if (method === 'GET') {
      return route.fulfill({ json: { enabled: true, can_manage: true, models: model ? [model] : [] } });
    }
    saved = route.request().postDataJSON();
    const { secret, expected_revision: revision, ...config } = saved!;
    expect(method === 'PUT' ? revision : secret).toBe(method === 'PUT' ? 1 : 'synthetic-model-key');
    model = { ...config, id: 'fixture-model', model_revision: method === 'PUT' ? 2 : 1, has_secret: true };
    return route.fulfill({ json: model });
  });
  await page.route('**/api/tasks', async route => {
    if (route.request().method() !== 'POST') return route.fallback();
    task = route.request().postDataJSON();
    return route.fulfill({ status: 201, json: { id: TASK_ID } });
  });

  await page.goto('/profiles?section=models');
  await page.getByRole('button', { name: '添加模型', exact: true }).click();
  await page.getByLabel('显示名称', { exact: true }).fill('测试模型');
  await page.getByLabel('上游模型', { exact: true }).fill('physical-model');
  await page.getByLabel('HTTPS Base URL', { exact: true }).fill('https://fixture.example/v1');
  await page.getByLabel('上游 API 密钥', { exact: true }).fill('synthetic-model-key');
  await page.getByRole('button', { name: '保存模型', exact: true }).click();
  await expect(page.getByText('费率待配置', { exact: true })).toBeVisible();
  expect(saved?.usd_rates).toBeNull();
  await expect(page.getByLabel('上游 API 密钥', { exact: true })).toBeEmpty();

  await page.goto('/tasks/new');
  await expect(page.getByText('尚无可用于任务的模型', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '创建任务', exact: true })).toBeDisabled();

  await page.goto('/profiles?section=models');
  await page.getByLabel('输入', { exact: true }).fill('1.5');
  await page.getByRole('button', { name: '保存模型', exact: true }).click();
  await expect(page.getByText('请填写四项非负费率。', { exact: true })).toBeVisible();
  expect(saved?.usd_rates).toBeNull();
  for (const label of ['输入', '输出', '缓存读取', '缓存写入']) {
    await page.getByLabel(label, { exact: true }).fill('0');
  }
  await page.getByRole('button', { name: '保存模型', exact: true }).click();
  await expect(page.getByText('可用于任务', { exact: true })).toBeVisible();
  expect(saved?.usd_rates).toEqual({ input: '0', output: '0', cache_read: '0', cache_write: '0' });
  expect(saved?.secret).toBeUndefined();
  expect(saved).not.toHaveProperty('pricing_source');
  expect(saved).not.toHaveProperty('confirmed_free');
  await expect(page.getByText('复盘费用', { exact: true })).toHaveCount(0);
  await expect(page.getByText('模型版本', { exact: true })).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391);

  await page.goto('/tasks/new');
  await expect(page.getByText('测试模型 · fixture-model', { exact: true })).toBeVisible();
  await page.getByLabel('任务目标', { exact: true }).fill('验证已冻结的模型版本');
  await page.getByLabel('验收条件 1', { exact: true }).fill('创建请求记录模型版本');
  await page.getByLabel('金额阈值（USD）', { exact: true }).fill('1');
  await page.getByRole('button', { name: '创建任务', exact: true }).click();
  await expect.poll(() => task?.pi_model).toEqual({ model_id: 'fixture-model', model_revision: 2 });
  expect(task?.gateway_model).toBeUndefined();
  expect(mock.unexpected).toEqual([]);
});
