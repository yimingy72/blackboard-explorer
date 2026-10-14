import { expect, test, type Page } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { installMockApi } from './fixtures';
import { expandSection, expectSelectedOption, selectOption } from './ui';

const screenshots = '../.data/ui-detail-review/forms';

async function capture(page: Page, filename: string) {
  await mkdir(screenshots, { recursive: true });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: `${screenshots}/${filename}`, animations: 'disabled', fullPage: true });
}

test('创建页添加模型打开独立模型页，原目标与附件保留并可刷新模型', async ({ page }) => {
  const mock = await installMockApi(page);
  const availableModel = structuredClone(mock.platformModels[0]);
  mock.platformModels.length = 0;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/tasks/new');
  await expect(page.getByRole('alert')).toContainText('当前没有可用模型');
  await page.getByLabel('任务目标', { exact: true }).fill('保留尚未创建的目标');
  await page.locator('input[type="file"]').setInputFiles({ name: 'draft.txt', mimeType: 'text/plain', buffer: Buffer.from('draft attachment') });
  await expect(page.getByText(/已上传/)).toHaveCount(1);
  await capture(page, 'no-model-warning-390.png');
  const documents: string[] = [];
  page.on('request', request => { if (request.resourceType() === 'document') documents.push(request.url()); });
  const popupPromise = page.waitForEvent('popup');
  await page.getByText('添加模型', { exact: true }).click();
  const popup = await popupPromise;
  await expect(popup).toHaveURL(/\/profiles\?section=models$/);
  await expect(popup.getByRole('tab', { name: '模型配置', exact: true })).toHaveAttribute('aria-selected', 'true');
  const editor = popup.getByRole('region', { name: '模型配置编辑', exact: true });
  await expect(editor.getByRole('button', { name: /新增模型/ })).toBeVisible();
  await expect(page).toHaveURL(/\/tasks\/new$/);
  await expect(page.getByLabel('任务目标', { exact: true })).toHaveValue('保留尚未创建的目标');
  await expect(page.getByText('draft.txt', { exact: true })).toBeVisible();
  mock.platformModels.push(availableModel);
  await page.getByRole('button', { name: '刷新模型', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expectSelectedOption(page, page.getByRole('combobox', { name: '平台模型', exact: true }), '审查模型 · 默认');
  await expect(page.getByRole('option', { name: '审查模型 · 默认', exact: true })).toBeHidden();
  await expect(page.getByRole('button', { name: '创建任务', exact: true })).toBeEnabled();
  await expect(page.getByText('draft.txt', { exact: true })).toBeVisible();
  expect(documents).toEqual([]);
  await capture(page, 'no-model-entry-390.png');
  expect(mock.unexpected).toEqual([]);
});

test('新增模型缺少价格时展开计费并定位首个无效字段，补齐零单价可以保存', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/profiles');
  await page.getByRole('tab', { name: '模型配置', exact: true }).click();
  const editor = page.getByRole('region', { name: '模型配置编辑', exact: true });
  await editor.getByRole('button', { name: /新增模型/ }).click();
  await editor.getByLabel('显示名称', { exact: true }).fill('零价本地模型');
  await selectOption(page, editor.getByRole('combobox', { name: 'Provider', exact: true }), 'Local');
  await editor.getByLabel('模型 ID', { exact: true }).fill('local-check');
  const billing = editor.getByRole('button', { name: '人民币计费', exact: true });
  await expect(billing).toHaveAttribute('aria-expanded', 'true');
  await billing.click();
  await expect(billing).toHaveAttribute('aria-expanded', 'false');
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  await expect(billing).toHaveAttribute('aria-expanded', 'true');
  const firstPrice = editor.getByLabel('缓存命中输入', { exact: true });
  await expect(firstPrice).toBeVisible();
  await expect(firstPrice).toBeInViewport({ ratio: 1 });
  await expect(firstPrice).toBeFocused();
  await expect(firstPrice).toHaveAttribute('aria-invalid', 'true');
  expect(mock.modelSaves).toEqual([]);
  await capture(page, 'price-validation-390.png');
  for (const field of ['缓存命中输入', '缓存未命中输入', '输出']) {
    await editor.getByLabel(field, { exact: true }).fill('0');
  }
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  await expect(editor.getByRole('status')).toContainText('已保存');
  expect(mock.modelSaves).toHaveLength(1);
  expect(mock.modelSaves[0]).toMatchObject({ price: { cache_hit_per_m: 0, cache_miss_per_m: 0, output_per_m: 0 } });
  expect(mock.unexpected).toEqual([]);
});

test('Worker 保存后重新编辑时撤下旧成功提示，第二次保存提交新修订', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  const prompt = page.getByLabel('完整系统提示词', { exact: true });
  await prompt.fill('已保存的第一版提示词');
  const save = page.getByRole('button', { name: '保存并应用', exact: true });
  await save.click();
  await expect(page.getByRole('status')).toContainText('已保存并应用');
  await expect(save).toHaveAttribute('aria-busy', 'false');
  await prompt.fill('尚未保存的第二版提示词');
  await expect(page.getByRole('status')).toHaveCount(0);
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.getByRole('status')).toContainText('已保存并应用');
  expect(mock.workerSaves).toHaveLength(2);
  expect(mock.workerSaves[1]).toMatchObject({ expected_revision: 3, prompt: '尚未保存的第二版提示词' });
  expect(mock.unexpected).toEqual([]);
});

for (const kind of ['models', 'mcp'] as const) {
  test(`${kind} 保存后重新编辑时撤下旧成功提示，第二次保存使用新草稿`, async ({ page }) => {
    const mock = await installMockApi(page);
    await page.goto('/profiles');
    await page.getByRole('tab', { name: kind === 'models' ? '模型配置' : 'MCP 工具', exact: true }).click();
    const editor = page.getByRole('region', { name: kind === 'models' ? '模型配置编辑' : 'MCP 服务编辑', exact: true });
    const label = editor.getByLabel('显示名称', { exact: true });
    await label.fill('连接第一版');
    const save = editor.getByRole('button', { name: '保存', exact: true });
    await save.click();
    await expect(editor.getByRole('status')).toContainText('已保存');
    await expect(save).toHaveAttribute('aria-busy', 'false');
    await label.fill('连接第二版');
    await expect(editor.getByRole('status')).toHaveCount(0);
    await expect(save).toBeEnabled();
    await save.click();
    await expect(editor.getByRole('status')).toContainText('已保存');
    const saves = kind === 'models' ? mock.modelSaves : mock.mcpSaves;
    expect(saves).toHaveLength(2);
    expect(saves[1]).toMatchObject({ label: '连接第二版' });
    expect(mock.unexpected).toEqual([]);
  });
}

test('CTF 保存后重新编辑时撤下旧成功提示，第二次保存使用新修订', async ({ page }) => {
  const mock = await installMockApi(page);
  const profile = {
    prompt_templates: { lead: 'Lead original', teammate: 'Teammate original' },
    worker_tools: { lead: { builtin: ['list_members'], mcp_servers: [{ name: 'reference', version: 1, allowed_tools: ['status'] }] }, teammate: { builtin: ['list_members'], mcp_servers: [] } },
    platform_tools: [{ server_name: 'reference', server_version: 1, tool_name: 'status', purpose: 'status', result_adapter: 'none', read_only: true }],
  };
  let revision = 3;
  const saves: Record<string, unknown>[] = [];
  await page.route('**/api/settings/ctf/workers', route => route.fulfill({ json: { revision, profile } }));
  await page.route('**/api/settings/ctf/workers/lead', route => {
    const body = route.request().postDataJSON();
    saves.push(body);
    profile.prompt_templates.lead = body.prompt;
    return route.fulfill({ json: { revision: ++revision, profile } });
  });
  await page.goto('/profiles');
  await page.getByRole('tab', { name: 'CTF 配置', exact: true }).click();
  const prompt = page.getByLabel('Lead完整系统提示词', { exact: true });
  await prompt.fill('Lead 第一版');
  const save = page.getByRole('button', { name: '保存 CTF 配置', exact: true });
  await save.click();
  await expect(page.getByRole('status')).toContainText('CTF 配置已保存');
  await expect(save).toHaveAttribute('aria-busy', 'false');
  await selectOption(page, page.getByRole('combobox', { name: 'status 用途', exact: true }), '提交候选答案');
  await expect(page.getByRole('status')).toHaveCount(0);
  await prompt.fill('Lead 第二版');
  await expect(page.getByRole('status')).toHaveCount(0);
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.getByRole('status')).toContainText('CTF 配置已保存');
  expect(saves).toHaveLength(2);
  expect(saves[1]).toMatchObject({ expected_revision: 4, prompt: 'Lead 第二版', platform_tools: [{ purpose: 'submit', read_only: false }] });
  expect(mock.unexpected).toEqual([]);
});

test('MCP 未保存连接变更时清除旧工具清单并停止读取，保存后按新版本读取', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  const reads: string[] = [];
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/platform/mcp-servers/reference/versions/*/tools', async route => {
    const version = new URL(route.request().url()).pathname.split('/').at(-2)!;
    reads.push(version);
    if (reads.length === 2) await delayed;
    await route.fulfill({ json: { tools: [{ name: version === '1' ? 'old_endpoint_tool' : 'new_endpoint_tool', description: `服务版本 ${version}` }] } });
  });
  try {
    await page.goto('/profiles');
    await page.getByRole('tab', { name: 'MCP 工具', exact: true }).click();
    const editor = page.getByRole('region', { name: 'MCP 服务编辑', exact: true });
    await expandSection(editor.getByRole('button', { name: '工具清单', exact: true }));
    const inspect = editor.getByRole('button', { name: '查看工具清单', exact: true });
    await inspect.click();
    await expect(editor.getByText('old_endpoint_tool', { exact: true })).toBeVisible();
    await expect(inspect).toHaveAttribute('aria-busy', 'false');
    await inspect.click();
    await expect.poll(() => reads.length).toBe(2);
    await expect(inspect).toHaveAttribute('aria-busy', 'true');
    await editor.getByLabel('Streamable HTTP URL', { exact: true }).fill('https://changed.example.invalid/mcp');
    await expect(inspect).toBeDisabled();
    await expect(editor.getByText(/先保存修改，再读取工具清单/)).toBeVisible();
    await expect(editor.getByText('old_endpoint_tool', { exact: true })).toHaveCount(0);
    const response = page.waitForResponse(value => value.url().includes('/reference/versions/1/tools'));
    release();
    await response;
    await expect(editor.getByText('old_endpoint_tool', { exact: true })).toHaveCount(0);
    await expect(inspect).toBeDisabled();
    await capture(page, 'mcp-unsaved-connection-1440.png');
    await editor.getByRole('button', { name: '保存', exact: true }).click();
    await expect(editor.getByRole('status')).toContainText('已保存');
    await expect(inspect).toBeEnabled();
    await inspect.click();
    await expect(editor.getByText('new_endpoint_tool', { exact: true })).toBeVisible();
    expect(reads).toEqual(['1', '1', '2']);
    expect(mock.mcpSaves).toHaveLength(1);
    expect(mock.mcpSaves[0]).toMatchObject({ url: 'https://changed.example.invalid/mcp' });
    expect(mock.unexpected).toEqual([]);
  } finally { release(); }
});

test('旧 USD 模型价格保持未知，改名保存前必须重新填写人民币单价', async ({ page }) => {
  const mock = await installMockApi(page);
  Object.assign(mock.platformModels[0].config, { price: { currency: 'USD', cache_hit_per_m: 0.2, cache_miss_per_m: 2, output_per_m: 3 } });
  await page.goto('/profiles?section=models');
  const editor = page.getByRole('region', { name: '模型配置编辑', exact: true });
  await expect(editor.getByRole('button', { name: '人民币计费', exact: true })).toHaveAttribute('aria-expanded', 'true');
  for (const field of ['缓存命中输入', '缓存未命中输入', '输出']) {
    await expect(editor.getByLabel(field, { exact: true })).toBeEmpty();
  }
  await editor.getByLabel('显示名称', { exact: true }).fill('USD 模型重新命名');
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  await expect(editor.getByLabel('缓存命中输入', { exact: true })).toHaveAttribute('aria-invalid', 'true');
  expect(mock.modelSaves).toEqual([]);
  for (const field of ['缓存命中输入', '缓存未命中输入', '输出']) {
    await editor.getByLabel(field, { exact: true }).fill('2');
  }
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  await expect(editor.getByRole('status')).toContainText('已保存');
  expect(mock.modelSaves).toHaveLength(1);
  expect(mock.modelSaves[0]).toMatchObject({ price: { currency: 'CNY', cache_hit_per_m: 2, cache_miss_per_m: 2, output_per_m: 2 } });
  expect(mock.unexpected).toEqual([]);
});

test('配置页签写入 URL，侧栏离开与浏览器后退都保护未保存草稿', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/tasks');
  await page.getByRole('button', { name: '展开导航', exact: true }).click();
  const nav = page.getByRole('navigation', { name: '主导航' });
  const documents: string[] = [];
  page.on('request', request => { if (request.resourceType() === 'document') documents.push(request.url()); });
  await nav.getByRole('link', { name: 'Agent 配置', exact: true }).click();
  await page.getByRole('tab', { name: '模型配置', exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('section')).toBe('models');
  await page.getByRole('tab', { name: 'Worker 配置', exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('section')).toBe('worker');
  const prompt = page.getByLabel('完整系统提示词', { exact: true });
  await prompt.fill('配置页离开前的未保存草稿');
  await nav.getByRole('link', { name: '任务', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '离开配置页？', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(prompt).toHaveValue('配置页离开前的未保存草稿');
  await page.evaluate(() => history.back());
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(prompt).toHaveValue('配置页离开前的未保存草稿');
  await page.evaluate(() => history.back());
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '放弃修改并离开', exact: true }).click();
  await expect(page).toHaveURL(/\/tasks$/);
  expect(documents).toEqual([]);
  expect(mock.workerSaves).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

test('配置保存中离开操作不可放弃草稿，保存成功后继续原导航', async ({ page }) => {
  const mock = await installMockApi(page);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  let started = false;
  await page.route('**/api/settings/workers/explore', async route => {
    started = true;
    await pending;
    await route.fallback();
  });
  try {
    await page.goto('/profiles');
    await page.getByRole('button', { name: '展开导航', exact: true }).click();
    await page.getByLabel('完整系统提示词', { exact: true }).fill('离开前完成保存');
    await page.getByRole('button', { name: '保存并应用', exact: true }).click();
    await expect.poll(() => started).toBe(true);
    await expect(page.getByRole('button', { name: '保存并应用', exact: true })).toHaveAttribute('aria-busy', 'true');
    await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: '任务', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '离开配置页？', exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('button', { name: '放弃修改并离开', exact: true })).toBeDisabled();
    release();
    await expect(page).toHaveURL(/\/tasks$/);
    expect(mock.workerSaves).toHaveLength(1);
    expect(mock.workerSaves[0]).toMatchObject({ expected_revision: 2, prompt: '离开前完成保存' });
    expect(mock.unexpected).toEqual([]);
  } finally { release(); }
});
