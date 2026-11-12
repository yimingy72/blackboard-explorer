import { expect, test } from '@playwright/test';
import { installMockApi } from './fixtures';

test('MCP 草稿不遮蔽发布版本，发现后逐个批准，工具 Schema 与角色可查', async ({ page }) => {
  const mock = await installMockApi(page);
  const server = {
    name: 'reference', version: 2, label: '资料检索', url: 'https://catalog.example.invalid/mcp',
    auth_header: 'Authorization', auth_scheme: 'Bearer', has_secret: true, enabled: true,
    published: false, published_version: 1, published_roles: ['explore'],
  };
  const tool = {
    name: 'docs.find', description: '查找资料',
    input_schema: { type: 'object', required: ['phrase'], properties: {
      phrase: { type: 'string', description: '要查找的短语' }, limit: { type: 'integer' },
    } },
    output_schema: { type: 'object' },
    annotations: { readOnlyHint: true },
  };
  const discoveries: number[] = [];
  let publication: unknown;
  const evidence = {
    status: 'ready', version: 2, published: false, discovery_digest: 'fixture-digest',
    tools: [tool, { ...tool, name: 'docs.other' }],
  };
  await page.route('**/api/platform/mcp-servers', route => route.fulfill({ json: [server] }));
  await page.route('**/api/platform/mcp-servers/reference/versions/1/tools', route =>
    route.fulfill({ json: { ...evidence, version: 1, published: true, roles: ['explore'], tools: [tool] } }));
  await page.route('**/api/platform/mcp-servers/reference/versions/2/discover', route => {
    discoveries.push(2);
    return route.fulfill({ json: evidence });
  });
  await page.route('**/api/platform/mcp-servers/reference/versions/2/publish', async route => {
    publication = route.request().postDataJSON();
    Object.assign(server, { version: 3, published_version: 3, published: true, published_roles: ['explore', 'lead'] });
    return route.fulfill({ json: server });
  });

  await page.goto('/profiles?section=mcp');
  await expect(page.getByRole('region', { name: 'MCP 服务编辑' }).getByText('草稿 · v2', { exact: true })).toBeVisible();
  await expect(page.getByText('当前发布版本：v1', { exact: false })).toBeVisible();
  await expect(page.getByLabel('认证密钥', { exact: true })).toHaveAttribute('placeholder', '留空沿用当前密钥');
  await page.getByRole('button', { name: '查看已发布工具', exact: true }).click();
  await expect(page.getByText('v1 · 已发布', { exact: true })).toBeVisible();
  expect(discoveries).toEqual([]);

  await page.getByRole('link', { name: '前往 Explore 工具权限', exact: true }).click();
  await page.getByRole('checkbox', { name: '资料检索', exact: true }).check();
  await expect(page.getByText('v1 · 已发布', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '读取 资料检索 工具清单', exact: true }).click();
  const roleTool = page.getByRole('checkbox', { name: /docs\.find/ });
  await expect(roleTool).not.toBeChecked();
  await roleTool.check();
  await page.getByRole('button', { name: '保存角色配置', exact: true }).click();
  await expect.poll(() => mock.workerSaves[0]?.tools).toMatchObject({
    mcp_servers: [{ name: 'reference', version: 1, allowed_tools: ['docs.find'], catalog_version: 'p6b' }],
  });
  await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: 'MCP 管理', exact: true }).click();

  await page.getByRole('button', { name: '测试连接并发现工具' }).click();
  await page.getByRole('button', { name: '查看 Schema', exact: true }).first().click();
  const details = page.getByRole('dialog', { name: 'docs.find' });
  await expect(details.getByText('输入 Schema', { exact: true })).toBeVisible();
  await expect(details.locator('pre').filter({ hasText: '要查找的短语' })).toBeVisible();
  await expect(details.getByText('输出 Schema', { exact: true })).toBeVisible();
  await expect(details.getByText('服务声明', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  const publish = page.getByRole('button', { name: '发布已选工具' });
  await expect(publish).toBeDisabled();
  await page.getByRole('checkbox', { name: '批准 docs.find', exact: true }).check();
  await expect(publish).toBeDisabled();
  await page.getByRole('checkbox', { name: '普通探索 Worker', exact: true }).check();
  await page.getByRole('checkbox', { name: 'CTF Lead', exact: true }).check();
  await expect(page.getByRole('checkbox', { name: 'CTF Worker', exact: true })).not.toBeChecked();
  await publish.click();
  await expect(page.getByText('当前发布版本：v3', { exact: false })).toBeVisible();
  expect(publication).toEqual({ discovery_digest: 'fixture-digest', tools: ['docs.find'], roles: ['explore', 'lead'] });
  expect(discoveries).toEqual([2]);
});
