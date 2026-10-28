import { expect, test, type Page } from '@playwright/test';
import { installMockApi } from './fixtures';
import { expandSection, selectOption } from './ui';

const discoveredModel = {
  id: 'gateway-model', name: '网关模型', context_window: 262144,
  max_input_tokens: 196608, max_output_tokens: 65536, supports_vision: true,
  metadata_source: 'provider_api',
};

async function openModel(page: Page) {
  await page.goto('/profiles?section=models');
  await selectOption(page, page.getByRole('combobox', { name: '模型连接', exact: true }), '审查模型');
  return page.getByRole('combobox', { name: '模型 ID', exact: true });
}

test('Responses 工具续传策略使用目录选项并保存到模型配置', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.route('**/api/platform/providers', route => route.fulfill({ json: [{
    id: 'openai_responses', label: 'OpenAI Responses', allow_no_auth: false,
    base_url_required: false, default_base_url: 'https://gateway.example.invalid/v1',
    supports_reasoning_effort: true, reasoning_efforts: ['low', 'high', 'max'],
    credential_fields: [{ name: 'api_key', label: 'API Key', required: true }],
    options_fields: [{ name: 'responses_replay_mode', label: 'Responses 工具续传', required: false, default_value: 'auto', options: [
      { value: 'auto', label: '自动识别（推荐）' },
      { value: 'native', label: '原生加密续传' },
      { value: 'tool_observations', label: '完整工具观察（兼容网关）' },
    ] }],
  }] }));
  await page.goto('/profiles?section=models');
  await selectOption(page, page.getByRole('combobox', { name: 'Provider', exact: true }), 'OpenAI Responses');
  const mode = page.getByRole('combobox', { name: 'Responses 工具续传', exact: true });
  await expect(page.getByText('自动识别（推荐）', { exact: true })).toBeVisible();
  await selectOption(page, mode, '完整工具观察（兼容网关）');
  await page.getByLabel('API Key', { exact: true }).fill('replay-fixture-key');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect.poll(() => mock.modelSaves.length).toBe(1);
  expect(mock.modelSaves[0]).toMatchObject({ provider: 'openai_responses', provider_options: { responses_replay_mode: 'tool_observations' } });
  expect(mock.unexpected).toEqual([]);
});

for (const width of [1440, 390]) {
  test(`模型配置在 ${width}px 保持紧凑且无横向溢出`, async ({ page }) => {
    await installMockApi(page);
    await page.setViewportSize({ width, height: 960 });
    await openModel(page);
    await expect(page.getByRole('heading', { name: '连接设置', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '模型与输出', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); window.scrollTo({ top: 0, behavior: 'instant' }); });
    await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
    await page.screenshot({ path: `../.data/model-config-discovery/layout-${width}.png`, fullPage: true });
  });
}

test('获取模型后选择 ID 回填能力上限，仍可手动修改', async ({ page }) => {
  const mock = await installMockApi(page), requests: unknown[] = [];
  await page.route('**/api/platform/models/discover', async route => {
    expect(route.request().method()).toBe('POST');
    requests.push(route.request().postDataJSON());
    await route.fulfill({ json: { models: [discoveredModel], warnings: [], supported: true } });
  });
  const id = await openModel(page);
  await expect(page.getByLabel('上下文大小（token）')).toHaveValue('1000000');
  await page.getByRole('button', { name: /获取模型$/ }).click();
  await expect(page.getByText('1 个可选模型', { exact: true })).toBeVisible();
  expect(requests).toEqual([expect.objectContaining({ provider: 'deepseek', saved_name: 'review-model', saved_version: 1 })]);
  await id.fill('gateway');
  await page.getByRole('option', { name: 'gateway-model · 网关模型', exact: true }).click();
  await expect(id).toHaveValue('gateway-model');
  await expect(page.getByLabel('上下文大小（token）')).toHaveValue('262144');
  await expect(page.getByLabel('最大输入（token）')).toHaveValue('196608');
  await expect(page.getByLabel('单次最大输出（token）')).toHaveValue('65536');
  await page.getByLabel('单次最大输出（token）').fill('32768');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect.poll(() => mock.modelSaves.length).toBe(1);
  expect(mock.modelSaves[0]).toMatchObject({ model: 'gateway-model', context_window: 262144, max_input_tokens: 196608, max_output_tokens: 32768, supports_vision: true });
  expect(mock.unexpected).toEqual([]);
});

test('修改连接后忽略迟到的模型发现响应', async ({ page }) => {
  const mock = await installMockApi(page);
  let release!: () => void, started = false, finished = false;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/platform/models/discover', async route => {
    started = true;
    await pending;
    await route.fulfill({ json: { models: [discoveredModel], warnings: [], supported: true } }).catch(() => {});
    finished = true;
  });
  try {
    const id = await openModel(page);
    await page.getByRole('button', { name: /获取模型$/ }).click();
    await expect.poll(() => started).toBe(true);
    await selectOption(page, page.getByRole('combobox', { name: 'Provider', exact: true }), 'OpenAI 兼容');
    await page.getByLabel('Base URL', { exact: true }).fill('https://new-gateway.example.invalid/v1');
    await id.fill('manual-new-model');
    release();
    await expect.poll(() => finished).toBe(true);
    await expect(page.getByRole('button', { name: /获取模型$/ })).toBeEnabled();
    await id.click();
    await expect(page.getByRole('option', { name: 'gateway-model · 网关模型', exact: true })).toHaveCount(0);
    await expect(page.getByText('1 个可选模型', { exact: true })).toHaveCount(0);
    await expect(id).toHaveValue('manual-new-model');
    await expect(page.getByLabel('上下文大小（token）')).toHaveValue('1000000');
    await expect(page.getByLabel('最大输入（token）')).toBeEmpty();
    await expect(page.getByLabel('单次最大输出（token）')).toBeEmpty();
    expect(mock.unexpected).toEqual([]);
  } finally { release(); }
});

test('公共模型目录仅在确认后补充参数，保留网关模型 ID 与人民币单价', async ({ page }) => {
  const mock = await installMockApi(page), requests: string[] = [];
  await page.route('**/api/platform/model-catalog*', async route => {
    const provider = new URL(route.request().url()).searchParams.get('provider');
    requests.push(provider ?? '');
    await route.fulfill({ json: {
      providers: [{ id: 'reference-provider', name: '参考厂商' }],
      models: provider ? [{ ...discoveredModel, metadata_source: 'models.dev', price: { currency: 'USD', cache_miss_per_m: '2', output_per_m: '8' } }] : [],
      warnings: [], provider,
    } });
  });
  const id = await openModel(page);
  await id.fill('gateway-private-alias');
  await expandSection(page.getByRole('button', { name: '从公共模型目录补充参数', exact: true }));
  await selectOption(page, page.getByRole('combobox', { name: '参考模型厂商', exact: true }), '参考厂商');
  await selectOption(page, page.getByRole('combobox', { name: '参考模型', exact: true }), 'gateway-model · 网关模型');
  await expect(page.getByLabel('上下文大小（token）')).toHaveValue('1000000');
  await page.getByRole('button', { name: '应用能力参数', exact: true }).click();
  await expect(id).toHaveValue('gateway-private-alias');
  await expect(page.getByLabel('上下文大小（token）')).toHaveValue('262144');
  await expect(page.getByLabel('最大输入（token）')).toHaveValue('196608');
  await expect(page.getByLabel('单次最大输出（token）')).toHaveValue('65536');
  await expect(page.getByText(/已参考 models.dev/)).toContainText('gateway-private-alias');
  await expect(page.getByText(/参考(?:基础)?价 · USD \/ 百万 token/)).toBeVisible();
  await expandSection(page.getByRole('button', { name: '人民币计费', exact: true }));
  await expect(page.getByLabel('缓存未命中输入', { exact: true })).toHaveValue('2');
  await expect(page.getByLabel('输出', { exact: true })).toHaveValue('3');
  expect(requests).toEqual(['', 'reference-provider']);
  expect(mock.unexpected).toEqual([]);
});

for (const outcome of ['unsupported', 'failure'] as const) {
  test(`模型发现 ${outcome} 时可手动填写并保存 ID`, async ({ page }) => {
    const mock = await installMockApi(page);
    await page.route('**/api/platform/models/discover', async route => {
      await route.fulfill(outcome === 'unsupported'
        ? { json: { models: [], warnings: ['该接口不支持模型发现，可手动填写模型 ID。'], supported: false } }
        : { status: 502, json: { detail: '模型列表暂时不可用' } });
    });
    const id = await openModel(page);
    await page.getByRole('button', { name: /获取模型$/ }).click();
    await expect(page.getByText(/可.*手动填写/)).toBeVisible();
    await id.fill('private-gateway-alias');
    await id.press('Tab');
    await page.getByRole('button', { name: '保存', exact: true }).click();
    await expect.poll(() => mock.modelSaves.length).toBe(1);
    expect(mock.modelSaves[0]).toMatchObject({ model: 'private-gateway-alias', context_window: 1000000 });
    expect(mock.unexpected).toEqual([]);
  });
}

test('美元参考价需手填汇率并显式确认才写入人民币单价', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.route('**/api/platform/models/discover', route => route.fulfill({ json: {
    models: [{ ...discoveredModel, price: { currency: 'USD', cache_hit_per_m: '0.25', cache_miss_per_m: '2', output_per_m: '8' } }], warnings: [], supported: true,
  } }));
  const id = await openModel(page);
  await page.getByRole('button', { name: /获取模型$/ }).click();
  await expect(page.getByText('1 个可选模型', { exact: true })).toBeVisible();
  await id.fill('gateway');
  await page.getByRole('option', { name: 'gateway-model · 网关模型', exact: true }).click();
  await expect(page.getByText(/参考(?:基础)?价 · USD \/ 百万 token/)).toBeVisible();
  await expandSection(page.getByRole('button', { name: '人民币计费', exact: true }));
  const inputPrice = page.getByLabel('缓存未命中输入', { exact: true });
  const outputPrice = page.getByLabel('输出', { exact: true });
  await expect(inputPrice).toHaveValue('2');
  await expect(outputPrice).toHaveValue('3');
  const apply = page.getByRole('button', { name: '填入人民币单价', exact: true });
  await expect(apply).toBeDisabled();
  await page.getByLabel('参考价格人民币汇率', { exact: true }).fill('7');
  await expect(apply).toBeEnabled();
  await expect(inputPrice).toHaveValue('2');
  await expect(outputPrice).toHaveValue('3');
  await apply.click();
  await expect(inputPrice).toHaveValue('14');
  await expect(outputPrice).toHaveValue('56');
  await expect(page.getByLabel('缓存命中输入', { exact: true })).toHaveValue('1.75');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect.poll(() => mock.modelSaves.length).toBe(1);
  expect(mock.modelSaves[0]).toMatchObject({ price: { currency: 'CNY', cache_hit_per_m: '1.75', cache_miss_per_m: '14', output_per_m: '56' } });
  expect(mock.unexpected).toEqual([]);
});
