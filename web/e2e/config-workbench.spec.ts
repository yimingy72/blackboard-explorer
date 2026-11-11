import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { installMockApi } from './fixtures';
import { fixtureToolCatalog as catalog } from './settings-fixture';

const original = '# 角色\r\n\r\n原始职责 {{ goal }}\r\n\r\n# 工作规则\r\n{% if goal %}\r\n# 不是分段\r\n核对资料\r\n{% endif %}\r\n';
const profile = (prompt = original) => ({
  prompt_templates: { explore: prompt, derive: '复核任务材料。', close: '核对验收状态。' },
  worker_tools: { explore: { builtin: ['get', 'search', 'read_evidence', 'post_fact', 'post_intent', 'claim', 'release', 'execute_command', 'view_image'], mcp_servers: [] } },
  params: { explore_max_steps: 60, seed_max_steps: 20, close_reserve_ratio: '0.05', derive_enabled: true, context_threshold: 128000 },
  exec_resources: { cpus: 2, mem: '4g', pids: 256 },
});
const model = { id: 'synthetic', label: '测试模型', upstream_model: 'synthetic-model', base_url: 'https://fixture.invalid/v1',
  model_revision: 3, enabled: true, has_secret: true, context_window: 32768, max_output_tokens: 4096,
  input_modalities: ['text'], reasoning: false, tool_call: true, usd_rates: { input: '1', output: '2', cache_read: '0.2', cache_write: '1' },
  pricing_source: '测试报价', confirmed_free: false };
const server = { name: 'fixture-docs', label: '任务资料索引', version: 2, url: 'https://fixture.invalid/mcp',
  has_secret: true, auth_header: 'Authorization', auth_scheme: 'Bearer', enabled: true, published: false, published_version: 1, published_roles: ['explore'] };
const defaultProgress = {
  revision: 4, prompt: '', default_prompt: '# 职责\n\n阅读任务资料。\n\n# 交付\n\n维护候选成果树。\n',
  enabled_tools: catalog.roles.progress.tools.map(tool => tool.name),
};
async function configFixture(page: Page) {
  const mock = await installMockApi(page);
  await page.route('**/api/settings/tool-catalog', route => route.fulfill({ json: catalog }));
  await page.route('**/api/settings/workers', route => route.fulfill({ json: { revision: 6, profile: profile() } }));
  await page.route('**/api/settings/ctf/workers', route => route.fulfill({
    json: { published: true, revision: 3, task_network: true, prompt_templates: { lead: '真实发布 Lead', teammate: '真实发布 Worker' },
      business_tools: { lead: ['board_finish_task'], teammate: ['board_post_fact'] } },
  }));
  await page.route('**/api/progress/settings', route => route.fulfill({ json: defaultProgress }));
  await page.route('**/api/platform/pi-models', route => route.fulfill({ json: { enabled: true, can_manage: true, can_test: true, models: [model] } }));
  await page.route('**/api/platform/mcp-servers', route => route.fulfill({ json: [server] }));
  return mock;
}

test('Worker 分段无损、执行权限整组、离开保护、409 保留草稿', async ({ page }) => {
  const mock = await configFixture(page);
  let put: { prompt: string; tools: { builtin: string[] }; expected_revision: number } | undefined;
  await page.route('**/api/settings/workers/explore', route => {
    put = route.request().postDataJSON();
    return route.fulfill({ status: 409, json: { detail: 'revision conflict' } });
  });
  await page.goto('/profiles');
  await expect(page.getByRole('heading', { name: '核心职责' })).toBeVisible();
  await page.getByRole('button', { name: '系统提示词', exact: true }).click();
  const section = page.getByRole('textbox', { name: '角色', exact: true });
  await section.fill('\n新的职责 {{ goal }}');
  await page.getByRole('button', { name: '完整预览' }).click();
  await expect(page.getByRole('dialog')).toContainText('不是分段');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '工具权限', exact: true }).click();
  const group = page.getByRole('switch', { name: '执行环境整组授权' });
  await expect(group).toBeChecked();
  await group.click();
  await expect(page.getByText('execute_command', { exact: true })).toHaveCount(0);
  await expect(page.getByText('view_image', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: /运行命令.*exec_command/ }).click();
  await expect(page.getByRole('dialog')).toContainText('参数 JSON 示例');
  await page.keyboard.press('Escape');
  await page.getByRole('link', { name: 'CTF', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '离开配置页？' })).toBeVisible();
  await page.getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page).toHaveURL(/\/profiles$/);
  await page.getByRole('button', { name: '保存角色配置' }).click();
  await expect(page.getByRole('alert')).toContainText('草稿已保留');
  expect(put).toEqual({ prompt: original.replace('原始职责', '新的职责'), tools: {
    builtin: ['get', 'search', 'read_evidence', 'post_fact', 'post_intent', 'claim', 'release', 'view_image'], mcp_servers: [],
  }, expected_revision: 6 });
  await page.getByRole('button', { name: '系统提示词', exact: true }).click();
  await expect(section).toHaveValue('\n新的职责 {{ goal }}');
  expect(mock.unexpected).toEqual([]);
});

test('未知自定义提示词全量往返，目录缺失时禁止保存', async ({ page }) => {
  await configFixture(page);
  const custom = '{% raw %}\r\n# 自定义 #\r\n{% endraw %}\n{{ goal }}\r\n';
  await page.route('**/api/settings/workers', route => route.fulfill({ json: { revision: 6, profile: profile(custom) } }));
  let put: { prompt: string } | undefined;
  await page.route('**/api/settings/workers/explore', route => {
    put = route.request().postDataJSON();
    return route.fulfill({ json: { revision: 7, profile: profile(custom) } });
  });
  await page.goto('/profiles');
  await page.getByRole('button', { name: '系统提示词', exact: true }).click();
  await expect(page.getByLabel('完整系统提示词')).toHaveValue(custom.replaceAll('\r\n', '\n'));
  await page.getByRole('button', { name: '工具权限', exact: true }).click();
  await page.getByRole('switch', { name: '执行环境整组授权' }).click();
  await page.getByRole('button', { name: '保存角色配置' }).click();
  await expect.poll(() => put?.prompt).toBe(custom);
  await expect(page.getByText('已保存角色配置。', { exact: false })).toBeVisible();
  await page.route('**/api/settings/tool-catalog', route => route.fulfill({ json: { roles: {} } }));
  await page.reload();
  await page.getByRole('button', { name: '系统提示词', exact: true }).click();
  await page.getByLabel('完整系统提示词').fill('保留的新草稿');
  await expect(page.getByRole('button', { name: '保存角色配置' })).toBeDisabled();
  await page.getByRole('button', { name: '工具权限', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('未能读取此角色的工具目录');
});

test('CTF 只读；成果整理工具保存保持默认提示词未覆盖', async ({ page }) => {
  await configFixture(page);
  let put: { prompt: string; default_prompt?: string; enabled_tools: string[] } | undefined;
  await page.route('**/api/progress/settings', route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: defaultProgress });
    put = route.request().postDataJSON();
    return route.fulfill({ json: { ...defaultProgress, enabled_tools: put!.enabled_tools, revision: 5 } });
  });
  await page.goto('/profiles?section=ctf');
  await page.getByRole('button', { name: '系统提示词', exact: true }).click();
  await expect(page.getByText('真实发布 Lead')).toBeVisible();
  await expect(page.getByRole('textbox')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '保存角色配置' })).toHaveCount(0);
  await page.getByRole('button', { name: /^Worker/ }).click();
  await expect(page.getByText('真实发布 Worker')).toBeVisible();
  await page.getByRole('link', { name: '成果整理', exact: true }).click();
  await expect(page.getByRole('heading', { name: '成果整理 Agent', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '系统提示词', exact: true }).click();
  await expect(page.getByLabel('职责', { exact: true })).toHaveValue('\n阅读任务资料。');
  await page.getByRole('button', { name: '工具权限', exact: true }).click();
  await expect(page.getByText('系统必需', { exact: false })).toHaveCount(3);
  await page.getByRole('switch', { name: '允许搜索资料' }).click();
  await page.getByRole('button', { name: '保存成果整理' }).click();
  await expect.poll(() => put?.prompt).toBe('');
  expect(put?.default_prompt).toBeUndefined();
  expect(put?.enabled_tools).not.toContain('search');
});

test('旧目录入口替换地址，浏览器回退与刷新保护草稿', async ({ page }) => {
  await configFixture(page);
  await page.goto('/profiles');
  await page.getByRole('link', { name: 'CTF', exact: true }).click();
  await page.getByRole('link', { name: 'Worker', exact: true }).click();
  await page.getByRole('button', { name: '系统提示词', exact: true }).click();
  await page.getByRole('textbox', { name: '角色', exact: true }).fill('保留直到用户确认');
  await page.evaluate(() => history.back());
  await expect(page.getByRole('dialog', { name: '离开配置页？' })).toBeVisible();
  await page.getByRole('button', { name: '继续编辑', exact: true }).click();
  page.once('dialog', async dialog => {
    expect(dialog.type()).toBe('beforeunload');
    await dialog.dismiss();
  });
  await page.evaluate(() => window.location.reload());
  await expect(page.getByRole('textbox', { name: '角色', exact: true })).toHaveValue('保留直到用户确认');
  await page.getByRole('link', { name: 'CTF', exact: true }).click();
  await page.getByRole('button', { name: '放弃修改并离开', exact: true }).click();
  await expect(page).toHaveURL(/section=ctf$/);
  await page.goto('/profiles?section=models');
  await expect(page).toHaveURL(/\/models$/);
  await expect(page.getByRole('heading', { name: '模型管理' })).toBeVisible();
  await page.goto('/profiles?section=mcp');
  await expect(page).toHaveURL(/\/mcp$/);
  await expect(page.getByRole('heading', { name: 'MCP 管理' })).toBeVisible();
});

for (const width of [1440, 768, 390]) {
  test(`五个配置页在 ${width}px 无横向溢出`, async ({ page }) => {
    await configFixture(page);
    await page.setViewportSize({ width, height: 960 });
    const directory = process.env.BBX_CONFIG_SHOTS;
    if (directory) await mkdir(directory, { recursive: true });
    for (const [id, path, title] of [
      ['worker', '/profiles', 'Explore'], ['ctf', '/profiles?section=ctf', 'Lead'],
      ['progress', '/profiles?section=progress', '成果整理 Agent'], ['models', '/models', '测试模型'], ['mcp', '/mcp', '任务资料索引'],
    ]) {
      await page.goto(path);
      await expect(page.getByRole('heading', { name: title, exact: false }).first()).toBeVisible();
      if (id === 'worker' || id === 'ctf' || id === 'progress') {
        await expect(page.getByRole('button', { name: /平台黑板.*项能力/ })).toBeVisible();
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
      if (directory) await page.screenshot({ path: join(directory, `${id}-${width}.png`), fullPage: true, animations: 'disabled' });
    }
  });
}
