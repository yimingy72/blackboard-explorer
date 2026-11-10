import { expect, test } from '@playwright/test';
import { installMockApi } from './fixtures';

test('模型演练结果断线后按原编号读取，费用未知，不再次发送', async ({ page }) => {
  const mock = await installMockApi(page);
  const model = {
    id: 'fixture-model', label: '合成模型', model_revision: 3, enabled: true, has_secret: true, tool_call: true,
    upstream_model: 'fixture-physical-model', usd_rates: null, context_window: 8192, max_output_tokens: 1024, input_modalities: ['text'],
  };
  await page.route('**/api/platform/pi-models', route => route.fulfill({
    json: { enabled: true, can_manage: true, can_test: true, models: [model] },
  }));
  const writes: Array<{ request_id: string; model_id: string; model_revision: number; prompt: string }> = [];
  await page.route('**/api/platform/pi-model-tests', async route => {
    writes.push(route.request().postDataJSON());
    await route.abort('failed');
  });
  const reads: string[] = [];
  await page.route('**/api/platform/pi-model-tests/*', route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    reads.push(id);
    return route.fulfill({ json: {
      request_id: id, model_id: model.id, model_revision: 3, state: 'complete',
      result_text: '合成答复', amount_usd: null, usage: { input: 5, output: 2, cacheRead: 0, cacheWrite: 0 },
    } });
  });

  await page.goto('/profiles?section=models');
  await expect(page.getByText('费率待配置', { exact: true })).toBeVisible();
  await expect(page.getByText('AgentGateway', { exact: false })).toHaveCount(0);
  await page.getByRole('button', { name: '测试模型', exact: true }).click();
  const testDialog = page.getByRole('dialog', { name: '测试模型：合成模型 · v3' });
  await testDialog.getByLabel('测试消息').fill('合成问题');
  await testDialog.getByRole('button', { name: '发送测试', exact: true }).click();
  await expect(testDialog.getByRole('alert')).toContainText('请读取原请求结果');
  await expect(testDialog.getByRole('button', { name: '发送测试', exact: true })).toBeDisabled();
  await testDialog.getByRole('button', { name: '读取原请求结果', exact: true }).click();
  await expect(testDialog.getByText('合成答复', { exact: true })).toBeVisible();
  await expect(testDialog.getByText('费用未知', { exact: true })).toBeVisible();
  await expect(testDialog.getByText(/观察用量：输入 5 · 输出 2/)).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ model_id: model.id, model_revision: 3, prompt: '合成问题' });
  expect(reads).toEqual([writes[0].request_id]);
  expect(mock.unexpected).toEqual([]);
});
