import { selectOption, expandSection } from './ui';
import { expect, test } from '@playwright/test';
import { installMockApi } from './fixtures';

test('切换MCP服务拒绝旧清单，保存时保护输入和资源选择', async ({ page }) => {
  const mock = await installMockApi(page);
  let releaseTools!: () => void;
  let releaseSave!: () => void;
  const slowTools = new Promise<void>((resolve) => { releaseTools = resolve; });
  const slowSave = new Promise<void>((resolve) => { releaseSave = resolve; });
  await page.route('**/api/platform/mcp-servers/reference/versions/1/tools', async (route) => {
    await slowTools;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ tools: [{ name: 'outdated_tool' }] }) });
  });
  await page.route('**/api/platform/mcp-servers/analysis/versions/1/tools', (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ tools: [{ name: 'current_tool' }] }) }));
  await page.route('**/api/platform/mcp-servers/analysis', async (route) => {
    if (route.request().method() === 'POST') await slowSave;
    await route.fallback();
  });
  try {
    await page.goto('/profiles');
    await page.getByRole('tab', { name: /MCP 工具$/ }).click();
    const connection = page.getByRole('combobox', { name: 'MCP 服务', exact: true });
    const editor = page.getByRole('region', { name: 'MCP 服务编辑' });
    await selectOption(page, connection, '资料检索');
    await expandSection(editor.getByRole('button', { name: '工具清单', exact: true }));
    await editor.getByRole('button', { name: '查看工具清单' }).click();
    await selectOption(page, connection, '分析工具');
    await editor.getByRole('button', { name: '查看工具清单' }).click();
    await expect(editor.getByText('current_tool', { exact: true })).toBeVisible();
    const response = page.waitForResponse((value) => value.url().includes('/reference/versions/1/tools'));
    releaseTools(); await response;
    await expect(editor.getByText('current_tool', { exact: true })).toBeVisible();
    await expect(editor.getByText('outdated_tool', { exact: true })).toHaveCount(0);
    await editor.getByLabel('显示名称').fill('分析工具 · 调整');
    await editor.getByRole('button', { name: /保存$/ }).click();
    await expect(editor.getByLabel('显示名称')).toBeDisabled();
    await expect(connection).toBeDisabled();
    await expect(page.getByRole('tab', { name: /模型配置$/ })).toBeDisabled();
    await expect(editor.getByRole('button', { name: /保存/ })).toBeDisabled();
    releaseSave();
    await expect(editor.getByRole('status')).toContainText('已保存');
    await expect(editor.getByLabel('显示名称')).toHaveValue('分析工具 · 调整');
    await expect(editor.getByLabel('显示名称')).toBeEnabled();
    expect(mock.mcpSaves).toHaveLength(1);
    expect(mock.unexpected).toEqual([]);
  } finally { releaseTools(); releaseSave(); }
});

test('新增模型未选 Provider 时零提交，补齐连接后只提交一次', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  await page.getByRole('tab', { name: /模型配置$/ }).click();
  const editor = page.getByRole('region', { name: '模型配置编辑' });
  await editor.getByRole('button', { name: /新增模型/ }).click();
  await editor.getByLabel('显示名称').fill('本地验证模型');
  await editor.getByLabel('模型 ID', { exact: true }).fill('local-check');
  await expandSection(editor.getByRole('button', { name: '人民币计费', exact: true }));
  for (const field of ['缓存命中输入', '缓存未命中输入', '输出']) {
    await editor.getByLabel(field, { exact: true }).fill('0');
  }
  await editor.getByRole('button', { name: /保存/ }).click();
  await expect(editor.getByRole('alert')).toContainText('请选择有效的 Provider');
  expect(mock.modelSaves).toEqual([]);
  await selectOption(page, editor.getByRole('combobox', { name: 'Provider', exact: true }), 'Local');
  await editor.getByRole('button', { name: /保存/ }).click();
  await expect(editor.getByRole('status')).toContainText('已保存');
  expect(mock.modelSaves).toHaveLength(1);
  expect(mock.modelSaves[0]).toMatchObject({
    provider: 'foundry_local', model: 'local-check', label: '本地验证模型',
    base_url: 'http://host.docker.internal:8000/v1', credentials: {},
  });
  expect(mock.unexpected).toEqual([]);
});
