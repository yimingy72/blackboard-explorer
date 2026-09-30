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
    await page.getByRole('button', { name: 'MCP 工具', exact: true }).click();
    const list = page.getByRole('region', { name: 'MCP 服务列表' });
    const editor = page.getByRole('region', { name: 'MCP 服务编辑' });
    await list.getByRole('button', { name: /资料检索/ }).click();
    await editor.getByRole('button', { name: '查看工具清单' }).click();
    await list.getByRole('button', { name: /分析工具/ }).click();
    await editor.getByRole('button', { name: '查看工具清单' }).click();
    await expect(editor.getByText('current_tool', { exact: true })).toBeVisible();
    const response = page.waitForResponse((value) => value.url().includes('/reference/versions/1/tools'));
    releaseTools(); await response;
    await expect(editor.getByText('current_tool', { exact: true })).toBeVisible();
    await expect(editor.getByText('outdated_tool', { exact: true })).toHaveCount(0);
    await editor.getByLabel('显示名称').fill('分析工具 · 调整');
    await editor.getByRole('button', { name: '保存', exact: true }).click();
    await expect(editor.getByLabel('显示名称')).toBeDisabled();
    await expect(list.getByRole('button', { name: /资料检索/ })).toBeDisabled();
    await expect(editor.getByRole('button', { name: '正在保存…' })).toBeDisabled();
    releaseSave();
    await expect(editor.getByRole('status')).toContainText('已保存');
    await expect(editor.getByLabel('显示名称')).toHaveValue('分析工具 · 调整');
    await expect(editor.getByLabel('显示名称')).toBeEnabled();
    expect(mock.mcpSaves).toHaveLength(1);
    expect(mock.unexpected).toEqual([]);
  } finally { releaseTools(); releaseSave(); }
});
