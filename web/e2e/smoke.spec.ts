import { expect, test } from '@playwright/test';
import {
  COMMAND_SENTINEL,
  FUTURE_NOTE,
  FUTURE_RECEIPT,
  GOAL,
  TASK_ID,
  TEXT_SENTINEL,
  installMockApi,
} from './fixtures';

test('登录后显示任务列表', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/login');
  await page.getByLabel('账号').fill('tester');
  await page.getByLabel('密码').fill('fixture-password');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page).toHaveURL(/\/tasks$/);
  await expect(page.getByRole('heading', { name: '任务', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: `查看任务：${GOAL}` })).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('事实详情展示六类证据，文本与命令输出可预览', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto(`/tasks/${TASK_ID}`);
  const canvas = page.getByRole('region', { name: '黑板关系图' });
  await canvas.getByRole('button', { name: /事实 F1，已提出/ }).click();
  const detail = page.getByRole('complementary', { name: '对象详情' });
  await expect(detail.getByRole('heading', { name: '观察事实 · F1' })).toBeVisible();
  for (const kind of ['文本', '命令输出', 'HTTP', '日志', '代码引用', '脚本']) {
    await expect(detail.locator('summary').filter({ hasText: kind }).first()).toBeVisible();
  }
  await detail.locator('summary').filter({ hasText: '现场文本' }).click();
  await expect(detail.getByText(TEXT_SENTINEL)).toBeVisible();
  await detail.locator('summary').filter({ hasText: '命令执行记录' }).click();
  await expect(detail.getByText(COMMAND_SENTINEL)).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('历史时间轴不泄漏后续交接笔记和 Agent 回执，记录可回到实时', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('button', { name: '复盘记录', exact: true }).click();
  await page.getByRole('tab', { name: '时间轴' }).click();
  const slider = page.getByRole('slider', { name: '回放版本' });
  await slider.focus();
  await slider.press('Home');
  await expect(page.getByText('花费 ¥0.000 / ¥10.00', { exact: true })).toBeVisible();
  for (let index = 0; index < 4; index += 1) await slider.press('ArrowRight');
  await expect(page.getByText('历史回放 · v5').first()).toBeVisible();

  await page.getByRole('button', { name: '关闭复盘记录' }).click();
  const canvas = page.getByRole('region', { name: '黑板关系图' });
  await canvas.getByRole('button', { name: /意图 I1，调查中/ }).click();
  const detail = page.getByRole('complementary', { name: '历史快照详情' });
  await expect(detail.getByRole('heading', { name: '意图 · I1' })).toBeVisible();
  await expect(detail).not.toContainText(FUTURE_NOTE);

  await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
  const conversation = page.getByRole('complementary', { name: 'Agent 1 对话记录' });
  await expect(conversation).not.toContainText(FUTURE_RECEIPT);
  await page.getByRole('button', { name: '复盘记录', exact: true }).click();
  await page.getByRole('tab', { name: '时间轴' }).click();
  await page.getByRole('button', { name: '返回实时' }).last().click();
  await page.getByRole('button', { name: '关闭复盘记录' }).click();
  await expect(conversation).toContainText(FUTURE_RECEIPT);
  await page.getByRole('button', { name: '复盘记录', exact: true }).click();
  await page.getByRole('tab', { name: '裁定历史' }).click();
  await expect(page.getByRole('tabpanel')).toContainText('连接池证据充分');
  expect(mock.unexpected).toEqual([]);
});

test('工作区目录、文件预览与 Markdown 报告', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('button', { name: '复盘记录', exact: true }).click();
  await page.getByRole('tab', { name: '工作区' }).click();
  const tree = page.getByRole('region', { name: '工作区目录' });
  await tree.getByText('agents/').click();
  await tree.getByText('agent-1/').click();
  await tree.getByRole('button', { name: 'notes.txt' }).click();
  await expect(page.getByRole('region', { name: '文件预览' })).toContainText('归档文件预览内容');
  await page.getByRole('button', { name: '关闭复盘记录' }).click();
  await page.getByRole('link', { name: '查看报告' }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}/report$`));
  await expect(page.getByRole('article', { name: '最终报告正文' }).getByRole('heading', { name: '最终报告' })).toBeVisible();
  await expect(page.getByRole('article', { name: '最终报告正文' })).toContainText('连接池上限');
  expect(mock.unexpected).toEqual([]);
});

test('Profile YAML 校验、发布与本浏览器默认', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  await expect(page.getByRole('heading', { name: 'Agent 配置' })).toBeVisible();
  await page.getByRole('button', { name: /^v1/ }).click();
  await expect(page.getByRole('button', { name: '对比版本' })).toHaveCount(0);
  await page.getByRole('button', { name: '编辑并发布新版本' }).click();
  await page.getByRole('button', { name: '高级 YAML', exact: true }).click();
  const editor = page.getByLabel('完整 AgentProfile YAML');
  const source = await editor.inputValue();
  await editor.fill('models: [');
  await page.getByRole('button', { name: '检查格式' }).click();
  await expect(page.getByRole('alert')).toContainText('YAML 格式错误');
  await editor.fill(source.replace('旧版探索模板', '已调整的探索模板'));
  await page.getByRole('button', { name: '发布新版本' }).click();
  await expect(page.getByRole('heading', { name: 'default · v3' })).toBeVisible();
  await page.getByRole('button', { name: '设为本浏览器默认' }).click();
  await expect(page.getByRole('status')).toContainText('本浏览器的新建任务默认');
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('bbx.default-profile.v1') ?? 'null')))
    .toEqual({ name: 'default', version: 3 });
  await page.goto('/tasks/new');
  await expect(page.getByLabel('版本', { exact: true })).toHaveValue('3');
  expect(mock.unexpected).toEqual([]);
});

test('完整展示超过 300 个对象且无筛选，手机视口无页面横向溢出', async ({ page }) => {
  test.setTimeout(90_000);
  const mock = await installMockApi(page, { largeGraph: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/tasks/${TASK_ID}`);
  await expect(page.getByRole('checkbox', { name: '折叠已关闭分支' })).toHaveCount(0);
  await expect(page.getByRole('combobox')).toHaveCount(0);
  await expect(page.getByText(/对象 302/)).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth))
    .toBeLessThanOrEqual(1);
  expect(mock.unexpected).toEqual([]);
});


test('worker 提示词完整编辑、跨模式保留、发布并重新读取', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  await expect(page.locator('#prompt-explore')).toHaveText('新版探索模板');
  await page.getByRole('button', { name: '编辑并发布新版本' }).click();
  const explore = '完整探索规则\n{{ goal }}\n{% if current_intent %}继续{% else %}种子{% endif %}';
  await page.getByLabel('完整系统提示词 · explore').fill(explore);
  await page.getByRole('button', { name: 'derive · 推导', exact: true }).click();
  await page.getByLabel('完整系统提示词 · derive').fill('新的推导规则\n{{ facts_text }}');
  await page.getByRole('button', { name: 'Close · 裁定与终结', exact: true }).click();
  await page.getByLabel('完整系统提示词 · close').fill('新的裁定规则\n{{ mode }}');
  await page.getByRole('button', { name: '高级 YAML', exact: true }).click();
  const yaml = page.getByLabel('完整 AgentProfile YAML');
  const source = await yaml.inputValue();
  expect(source).toContain('新的推导规则');
  await yaml.fill('models: [');
  await page.getByRole('button', { name: 'Worker 配置', exact: true }).last().click();
  await expect(page.getByRole('status')).toContainText('草稿已保留');
  await yaml.fill(source);
  await page.getByRole('button', { name: 'Worker 配置', exact: true }).last().click();
  await page.getByRole('button', { name: 'Explore · 探索', exact: true }).click();
  await expect(page.getByLabel('完整系统提示词 · explore')).toHaveValue(explore);
  await page.getByRole('button', { name: '发布新版本', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'default · v3' })).toBeVisible();
  await page.reload();
  await expect(page.locator('#prompt-explore')).toHaveText(explore);
  expect((mock.profiles.get(2)?.prompt_templates as Record<string, string>).explore).toBe('新版探索模板');
  expect((mock.profiles.get(3)?.prompt_templates as Record<string, string>).close).toContain('{{ mode }}');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: '../.data/checkpoints/cny-worker-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: '../.data/checkpoints/cny-worker-desktop.png', fullPage: true });
  expect(mock.unexpected).toEqual([]);
});

test('平台模型与 MCP 密钥只写、工具清单可读取', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  await page.getByRole('button', { name: '平台模型' }).click();
  await page.getByRole('button', { name: /审查模型/ }).click();
  const modelEditor = page.getByRole('region', { name: '平台模型编辑' });
  await expect(modelEditor.getByLabel('API 密钥')).toHaveValue('');
  await modelEditor.getByLabel('显示名称').fill('审查模型新版');
  await modelEditor.getByRole('button', { name: '保存新版本' }).click();
  await expect(modelEditor.getByRole('status')).toContainText('v2');
  expect(mock.modelSaves[0]).not.toHaveProperty('api_key');
  expect(mock.modelSaves[0]).toHaveProperty('price.currency', 'CNY');
  await page.getByRole('button', { name: 'MCP 工具' }).click();
  await page.getByRole('button', { name: /资料检索/ }).click();
  const mcpEditor = page.getByRole('region', { name: 'MCP 服务编辑' });
  await expect(mcpEditor.getByLabel('认证密钥')).toHaveValue('');
  await mcpEditor.getByRole('button', { name: /查看 v1 工具清单/ }).click();
  await expect(mcpEditor.getByText('search_docs')).toBeVisible();
  await mcpEditor.getByLabel('显示名称').fill('资料检索新版');
  await mcpEditor.getByRole('button', { name: '保存新版本' }).click();
  expect(mock.mcpSaves[0]).not.toHaveProperty('secret');
  await page.screenshot({ path: '../.data/checkpoints/platform-config/mcp-desktop.png', fullPage: true });
  expect(mock.unexpected).toEqual([]);
});

test('Worker 表单保存模型快照、MCP 白名单及调度环境，切换草稿受保护', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  await page.getByRole('button', { name: '编辑并发布新版本' }).click();
  await page.getByLabel('完整系统提示词 · explore').fill('未保存的探索提示词');
  page.once('dialog', (dialog) => void dialog.dismiss());
  await page.getByRole('button', { name: '平台模型' }).click();
  await expect(page.getByLabel('完整系统提示词 · explore')).toHaveValue('未保存的探索提示词');
  await page.getByLabel('平台模型').selectOption('review-model@1');
  await page.getByLabel('资料检索 · v1').check();
  await page.getByLabel('分析工具 · v1').check();
  await page.getByRole('button', { name: '读取 资料检索 工具清单' }).click();
  await page.getByLabel('read_docs').uncheck();
  await page.getByText('调度参数与执行环境').click();
  await page.getByLabel('CPU').fill('3');
  await page.screenshot({ path: '../.data/checkpoints/platform-config/worker-desktop.png', fullPage: true });
  await page.getByRole('button', { name: '发布新版本' }).click();
  await expect(page.getByRole('heading', { name: 'default · v3' })).toBeVisible();
  const published = mock.profiles.get(3)!;
  expect(published.models.explore).toHaveProperty('platform_id', 'review-model');
  expect(published.models.explore).toHaveProperty('platform_version', 1);
  expect(published.worker_tools?.explore?.mcp_servers).toEqual([
    { name: 'reference', version: 1, allowed_tools: ['search_docs'] },
    { name: 'analysis', version: 1, allowed_tools: null },
  ]);
  expect(published.exec_resources.cpus).toBe(3);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.data/checkpoints/platform-config/worker-mobile.png', fullPage: true });
  expect(mock.unexpected).toEqual([]);
});

test('人民币预算按页面已展示的固定版本提交，避免最新版本变化改变币种', async ({ page }) => {
  const mock = await installMockApi(page);
  let submitted: Record<string, unknown> | undefined;
  await page.route('**/api/tasks', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    submitted = route.request().postDataJSON();
    await route.fulfill({ json: { id: TASK_ID, agent_profile: 'default', agent_profile_version: 2 } });
  });
  await page.goto('/tasks/new');
  await expect(page.getByLabel('金额上限（人民币元）')).toBeVisible();
  await page.getByLabel('任务目标', { exact: false }).fill('核对报表');
  await page.getByLabel('验收条件 1', { exact: true }).fill('列出差异');
  mock.profiles.set(3, structuredClone(mock.profiles.get(2)!));
  await page.getByRole('button', { name: '创建任务', exact: true }).click();
  await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
  expect(submitted?.profile_version).toBe(2);
  expect(submitted?.budget).toMatchObject({ max_cost: '10' });
  expect(mock.unexpected).toEqual([]);
});
