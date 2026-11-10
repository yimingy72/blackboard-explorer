import { expect, test } from '@playwright/test';
import { installMockApi, TASK_ID } from './fixtures';
import { selectOption, expandSection } from './ui';

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
  await selectOption(page, page.getByRole('combobox', { name: 'MCP 服务', exact: true }), '资料检索');
  await expect(page.getByText('草稿 v2', { exact: true })).toBeVisible();
  await expect(page.getByText('当前已发布 v1', { exact: true })).toBeVisible();
  await expect(page.getByText('已保存认证密钥', { exact: true })).toBeVisible();
  await expandSection(page.getByRole('button', { name: '工具清单', exact: true }));
  await page.getByRole('button', { name: '查看已发布工具', exact: true }).click();
  await expect(page.getByText('v1 · 已发布', { exact: true })).toBeVisible();
  expect(discoveries).toEqual([]);

  await page.getByRole('tab', { name: 'Worker 配置', exact: true }).click();
  await page.getByRole('checkbox', { name: '资料检索', exact: true }).check();
  await expect(page.getByText('v1 · 已发布', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '读取 资料检索 工具清单', exact: true }).click();
  const roleTool = page.getByRole('checkbox', { name: /docs\.find/ });
  await expect(roleTool).not.toBeChecked();
  await roleTool.check();
  await page.getByRole('button', { name: '保存并应用', exact: true }).click();
  await expect.poll(() => mock.workerSaves[0]?.tools).toMatchObject({
    mcp_servers: [{ name: 'reference', version: 1, allowed_tools: ['docs.find'], catalog_version: 'p6b' }],
  });
  await page.getByRole('tab', { name: 'MCP 工具', exact: true }).click();
  await selectOption(page, page.getByRole('combobox', { name: 'MCP 服务', exact: true }), '资料检索');
  await expandSection(page.getByRole('button', { name: '工具清单', exact: true }));

  await page.getByRole('button', { name: '测试连接并发现工具' }).click();
  const details = page.locator('details').filter({ has: page.getByText('工具详情：docs.find', { exact: true }) });
  await details.locator('summary').first().click();
  await expect(details.getByText('phrase · string · 必填', { exact: true })).toBeVisible();
  await expect(details.getByText('要查找的短语', { exact: true })).toBeVisible();
  await expect(details.getByText('limit · integer · 可选', { exact: true })).toBeVisible();
  await details.getByText('完整输入 Schema', { exact: true }).click();
  await expect(details.locator('pre').filter({ hasText: '"required"' })).toBeVisible();
  await expect(details.getByText('完整输出 Schema', { exact: true })).toBeVisible();
  await expect(details.getByText('服务声明（不代表平台权限）', { exact: true })).toBeVisible();
  const publish = page.getByRole('button', { name: '发布已选工具' });
  await expect(publish).toBeDisabled();
  await page.getByRole('checkbox', { name: '批准 docs.find', exact: true }).check();
  await expect(publish).toBeDisabled();
  await page.getByRole('checkbox', { name: '普通探索 Worker', exact: true }).check();
  await page.getByRole('checkbox', { name: 'CTF Lead', exact: true }).check();
  await expect(page.getByRole('checkbox', { name: 'CTF Worker', exact: true })).not.toBeChecked();
  await publish.click();
  await expect(page.getByText('已发布 v3', { exact: true })).toBeVisible();
  expect(publication).toEqual({ discovery_digest: 'fixture-digest', tools: ['docs.find'], roles: ['explore', 'lead'] });
  expect(discoveries).toEqual([2]);
});

test('原生新任务明确冻结 Lead 和 Worker 各自授权的外部工具', async ({ page }) => {
  await installMockApi(page);
  const environment = { id: 'fixture-native', revision: 2, publication_stamp: 'a'.repeat(64), label: '合成原生环境', external_mcp: true };
  const base = { url: 'https://fixture.example/mcp', auth_header: 'Authorization', auth_scheme: 'Bearer', enabled: true, has_secret: true };
  await page.route('**/api/task-creation-capabilities', route => route.fulfill({
    json: { mode: 'native-ctf', native_ctf: environment },
  }));
  await page.route('**/api/platform/pi-models', route => route.fulfill({
    json: { enabled: true, can_manage: true, models: [{
      id: 'fixture-model', model_revision: 1, label: '合成模型', enabled: true, has_secret: true, tool_call: true,
      usd_rates: { input: '1', output: '2', cache_read: '1', cache_write: '1' },
    }] },
  }));
  await page.route('**/api/platform/mcp-servers', route => route.fulfill({ json: [
    { ...base, name: 'lead-docs', label: 'Lead 资料', version: 4, published_version: 3, published: false, published_roles: ['lead'] },
    { ...base, name: 'worker-docs', label: 'Worker 资料', version: 2, published_version: 2, published: true, published_roles: ['teammate'] },
    { ...base, name: 'draft', label: '尚未发布', version: 1, published: false, published_version: null },
  ] }));
  for (const [name, version, role, tool] of [
    ['lead-docs', 3, 'lead', 'docs.lead'],
    ['worker-docs', 2, 'teammate', 'docs.worker'],
  ] as const) {
    await page.route(`**/api/platform/mcp-servers/${name}/versions/${version}/tools`, route => route.fulfill({
      json: { status: 'ready', version, published: true, roles: [role], tools: [
        { name: tool, description: '无害的资料查询' }, { name: 'docs.unselected', description: '未授权工具' },
      ] },
    }));
  }
  let creation: Record<string, unknown> | undefined;
  await page.route('**/api/tasks', route => {
    if (route.request().method() !== 'POST') return route.fallback();
    creation = route.request().postDataJSON();
    return route.fulfill({ status: 201, json: { id: TASK_ID } });
  });

  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto('/tasks/new');
  await expect(page.getByText('合成模型 · fixture-model', { exact: true })).toBeVisible();
  await page.getByLabel('任务目标', { exact: true }).fill('查询合成资料');
  await page.getByLabel('金额阈值（USD）', { exact: true }).fill('1');
  await expandSection(page.getByRole('button', { name: '外部 MCP 工具（可选）' }));
  await expect(page.getByRole('checkbox', { name: 'Lead 资料', exact: true })).not.toBeChecked();
  await expect(page.getByRole('checkbox', { name: 'Worker 资料', exact: true })).toHaveCount(0);
  await expect(page.getByRole('checkbox', { name: '尚未发布', exact: true })).toHaveCount(0);
  await page.getByRole('checkbox', { name: 'Lead 资料', exact: true }).check();
  await page.getByRole('button', { name: '创建任务', exact: true }).click();
  await expect(page.getByText('请为 Lead 和 Worker 已选的每项 MCP 服务至少勾选一个工具，或取消选择该服务。')).toBeVisible();
  expect(creation).toBeUndefined();
  await page.getByRole('button', { name: '读取 Lead 资料 工具清单', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: /docs.lead/ })).not.toBeChecked();
  await page.getByRole('checkbox', { name: /docs.lead/ }).check();

  await page.getByText('CTF Worker', { exact: true }).click();
  await expect(page.getByRole('checkbox', { name: 'Lead 资料', exact: true })).toHaveCount(0);
  await page.getByRole('checkbox', { name: 'Worker 资料', exact: true }).check();
  await page.getByRole('button', { name: '读取 Worker 资料 工具清单', exact: true }).click();
  await page.getByRole('checkbox', { name: /docs.worker/ }).check();
  await expect(page.getByRole('checkbox', { name: /docs.unselected/ })).not.toBeChecked();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);

  await page.getByRole('button', { name: '创建任务', exact: true }).click();
  await expect.poll(() => creation?.native_mcp).toEqual({
    lead: [{ name: 'lead-docs', version: 3, allowed_tools: ['docs.lead'] }],
    teammate: [{ name: 'worker-docs', version: 2, allowed_tools: ['docs.worker'] }],
  });
  expect(creation?.native_environment).toEqual({ id: environment.id, revision: environment.revision, publication_stamp: environment.publication_stamp });
});
