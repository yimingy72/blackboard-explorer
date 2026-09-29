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
  await expect(page.getByText('估算费用 ¥0.000 / ¥10.00', { exact: true })).toBeVisible();
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

test('Worker 直接保存提示词，冲突保留草稿', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  const explore = page.getByLabel('完整系统提示词');
  await expect(explore).toHaveValue('新版探索模板');
  await explore.fill('新探索规则\n{{ goal }}');
  await page.getByRole('button', { name: '保存并应用' }).click();
  await expect(page.getByRole('status')).toContainText('下一次模型调用生效');
  expect(mock.workerSaves[0]).toMatchObject({ role: 'explore', expected_revision: 2, prompt: '新探索规则\n{{ goal }}' });
  await page.getByRole('tab', { name: /Derive/ }).click();
  await page.getByLabel('完整系统提示词').fill('未保存的推导');
  mock.revision = 99;
  await page.getByRole('button', { name: '保存并应用' }).click();
  await expect(page.getByRole('alert')).toContainText('当前草稿已保留');
  await expect(page.getByLabel('完整系统提示词')).toHaveValue('未保存的推导');
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


test('Worker 工具与全局运行参数分别保存', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  await page.getByLabel('资料检索').check();
  await page.getByRole('button', { name: '读取 资料检索 工具清单' }).click();
  await page.getByLabel('read_docs').uncheck();
  await page.getByRole('button', { name: '保存并应用' }).click();
  expect(mock.workerSaves[0].tools).toMatchObject({ mcp_servers: [{ name: 'reference', version: 1, allowed_tools: ['search_docs'] }] });
  await page.getByText('全局调度与执行环境').click();
  await page.getByLabel('CPU').fill('3');
  await page.getByRole('button', { name: '保存全局设置' }).click();
  expect(mock.runtimeSaves[0]).toMatchObject({ expected_revision: 3, exec_resources: { cpus: 3 } });
  await page.screenshot({ path: '../.data/checkpoints/worker-settings/worker-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.data/checkpoints/worker-settings/worker-mobile.png', fullPage: true });
  expect(mock.unexpected).toEqual([]);
});

test('Provider 目录驱动模型字段、密钥只写、平台默认与 MCP 自动标识', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto('/profiles');
  await page.getByRole('button', { name: '模型配置' }).click();
  await page.getByRole('button', { name: /审查模型/ }).click();
  const editor = page.getByRole('region', { name: '模型配置编辑' });
  await expect(editor.getByLabel('API 密钥')).toHaveValue('');
  await expect(editor.getByText('凭据来源')).toHaveCount(0);
  await editor.getByLabel('显示名称').fill('审查模型新版');
  await editor.getByLabel('图片输入').selectOption('true');
  await editor.getByLabel('费用估算方式').selectOption('fixed');
  await page.screenshot({ path: test.info().outputPath('model-settings.png'), fullPage: true });
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  expect(mock.modelSaves[0]).not.toHaveProperty('credentials.api_key');
  expect(mock.modelSaves[0]).toMatchObject({ supports_vision: true, price: { billing_mode: 'fixed' } });
  await page.getByRole('button', { name: '＋ 新增模型' }).click();
  await editor.getByLabel('显示名称').fill('Azure 模型');
  await editor.getByLabel('Provider').selectOption('deepseek');
  await expect(editor.getByLabel('费用估算方式')).toHaveValue('deepseek_schedule');
  await expect(editor.getByLabel('Base URL')).toHaveCount(0);
  await expect(editor.getByLabel('推理强度')).toHaveValue('none');
  await editor.getByLabel('Provider').selectOption('azure_openai_chat');
  await expect(editor.getByLabel('费用估算方式')).toHaveValue('fixed');
  await expect(editor.getByLabel('推理强度')).toBeVisible();
  await editor.getByLabel('Provider').selectOption('foundry');
  await expect(editor.getByLabel('推理强度')).toHaveCount(0);
  await editor.getByLabel('模型 ID').fill('azure-chat');
  await editor.getByLabel('Base URL').fill('https://azure.example.invalid/');
  await editor.getByLabel('租户 ID').fill('tenant-fixture');
  await editor.getByLabel('客户端 ID').fill('client-fixture');
  await editor.getByLabel('客户端密钥').fill('secret-fixture');
  for (const field of ['缓存命中输入', '缓存未命中输入', '输出']) await editor.getByLabel(field).fill('1');
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  expect(mock.modelSaves[1]).toMatchObject({ provider: 'foundry', reasoning_effort: 'none', credentials: { tenant_id: 'tenant-fixture', client_id: 'client-fixture', client_secret: 'secret-fixture' } });
  expect(mock.modelSaves[1]).not.toHaveProperty('name');
  await editor.getByRole('button', { name: '设为默认模型' }).click();
  expect(mock.platformModels.find((item) => item.is_default)?.label).toBe('Azure 模型');
  await page.getByRole('button', { name: '＋ 新增模型' }).click();
  await editor.getByLabel('Provider').selectOption('openai_compatible');
  await expect(editor.getByText('推理强度需兼容接口支持')).toBeVisible();
  await editor.getByLabel('推理强度').selectOption('high');
  await editor.getByLabel('Provider').selectOption('bedrock');
  await expect(editor.getByLabel('推理强度')).toHaveCount(0);
  await expect(editor.getByText('该 Provider 当前使用模型默认推理设置')).toBeVisible();
  await expect(editor.getByLabel('区域')).toBeVisible();
  await expect(editor.getByLabel('Secret Access Key')).toBeVisible();
  await editor.getByLabel('Provider').selectOption('gemini_vertex');
  await expect(editor.getByLabel('服务账号 JSON')).toBeVisible();
  await editor.getByLabel('Provider').selectOption('foundry_local');
  await expect(editor.getByLabel('Base URL')).toHaveValue('http://host.docker.internal:8000/v1');
  await editor.getByLabel('显示名称').fill('本地模型');
  await editor.getByLabel('模型 ID').fill('local-chat');
  for (const field of ['缓存命中输入', '缓存未命中输入', '输出']) await editor.getByLabel(field).fill('0');
  await editor.getByRole('button', { name: '保存', exact: true }).click();
  expect(mock.modelSaves[2]).toMatchObject({ provider: 'foundry_local', reasoning_effort: 'none', credentials: {} });
  await page.getByRole('button', { name: 'MCP 工具' }).click();
  await page.getByRole('button', { name: '＋ 新增MCP 服务' }).click();
  const mcp = page.getByRole('region', { name: 'MCP 服务编辑' });
  await mcp.getByLabel('显示名称').fill('测试工具');
  await mcp.getByLabel('Streamable HTTP URL').fill('https://mcp.example.invalid/mcp');
  await mcp.getByRole('button', { name: '保存', exact: true }).click();
  expect(mock.mcpSaves[0]).not.toHaveProperty('name');
  await mcp.getByRole('button', { name: '查看工具清单' }).click();
  await expect(mcp.getByText('search_docs')).toBeVisible();
  await page.screenshot({ path: '../.data/checkpoints/worker-settings/catalog-desktop.png', fullPage: true });
  expect(mock.unexpected).toEqual([]);
});

test('新任务提交所选模型快照与人民币预算', async ({ page }) => {
  const mock = await installMockApi(page);
  let submitted: Record<string, unknown> | undefined;
  await page.route('**/api/tasks', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    submitted = route.request().postDataJSON();
    await route.fulfill({ json: { id: TASK_ID, agent_profile: 'default', agent_profile_version: 2 } });
  });
  await page.goto('/tasks/new');
  await expect(page.getByLabel('平台模型')).toHaveValue('review-model');
  await expect(page.getByLabel('金额上限（人民币元）')).toBeVisible();
  await page.getByLabel('任务目标', { exact: false }).fill('核对报表');
  await page.getByLabel('领域背景').fill('包含测试背景');
  await page.locator('summary').filter({ hasText: '目标域名' }).click();
  await page.getByLabel('任务所需域名（记录）').fill('example.com\nexample.org, example.com');
  await page.getByLabel('验收条件 1', { exact: true }).fill('列出差异');
  await page.getByRole('button', { name: '创建任务', exact: true }).click();
  await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
  expect(submitted).toMatchObject({ agent_profile: 'default', model_id: 'review-model', model_version: 1, domain_context: '包含测试背景', egress_allowlist: ['example.com', 'example.org'], budget: { max_cost: '10' } });
  expect(submitted).not.toHaveProperty('profile_version');
  expect(mock.unexpected).toEqual([]);
});
