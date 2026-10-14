import { expect, test, type Page } from '@playwright/test';
import { createHash } from 'node:crypto';
import { mkdir, readFile } from 'node:fs/promises';
import { GOAL, TASK_ID, installMockApi } from './fixtures';
import { taskAction } from './ui';

const screenshots = '../.data/ui-review2';
const task = {
  id: TASK_ID, name: '合成阅读回归任务', goal: GOAL, status: 'finished',
  created_at: '2026-10-10T08:00:00Z', usage: { cost: 0.02 }, cost_currency: 'CNY',
  budget: { max_cost: 10, max_minutes: 60, max_concurrent_agents: 3 },
  run_number: 2, active_seconds: 600, active_since: null, cleanup_ready: true,
  report_uri: `reports/${TASK_ID}/run-2.md`, workspace_uri: `archives/${TASK_ID}/run-2.tar.zst`,
  runs: [1, 2].map(run_number => ({ run_number, report_uri: `reports/${TASK_ID}/run-${run_number}.md`, workspace_uri: `archives/${TASK_ID}/run-${run_number}.tar.zst` })),
};

async function capture(page: Page, name: string) {
  await mkdir(screenshots, { recursive: true });
  await page.screenshot({ path: `${screenshots}/${name}`, fullPage: true, animations: 'disabled' });
}

test('详情目标附件按持久化 URI 下载，任务 ID 不充当附件组 ID', async ({ page }) => {
  const mock = await installMockApi(page);
  const groupId = '33333333-3333-4333-8333-987654321000';
  const fileId = '44444444-4444-4444-8444-987654321000';
  const bytes = Buffer.from('source,value\nsynthetic,42\n');
  const file = { id: fileId, filename: 'review-source.csv', path: `/workspace/shared/inputs/${fileId}/review-source.csv`, uri: `inputs/${groupId}/${fileId}/review-source.csv`, size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
  mock.events[0].payload = { ...mock.events[0].payload, initial_attachments: [file] };
  const mockApi = process.env.BBX_E2E_MOCK_API_URL;
  if (!mockApi) throw new Error('Run through node e2e/run.cjs to isolate native downloads.');
  const registered = await page.request.post(`${mockApi}/__e2e/download`, { data: {
    uri: file.uri, filename: file.filename, contentType: 'text/csv', body: bytes.toString('base64'),
  } });
  expect(registered.status()).toBe(204);
  await page.context().route('**/api/evidence?*', route => new URL(route.request().url()).searchParams.get('uri') === file.uri
    ? route.fulfill({ body: bytes, headers: { 'Content-Type': 'text/csv', 'Content-Disposition': `attachment; filename="${file.filename}"` } })
    : route.fallback());
  await page.goto(`/tasks/${TASK_ID}?focus=goal`);
  const detail = page.getByRole('complementary', { name: '对象详情', exact: true });
  const link = detail.getByRole('link', { name: file.filename, exact: true });
  await expect(link).toHaveAttribute('href', `/api/evidence?uri=${encodeURIComponent(file.uri)}`);
  await capture(page, 'detail-input-download.png');
  const downloaded = page.waitForEvent('download');
  await link.click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe(file.filename);
  expect(await readFile((await download.path())!)).toEqual(bytes);
  await expect(detail).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('回放首轮归档时目录、同路径文件缓存和下载地址都保留轮次', async ({ page }) => {
  const mock = await installMockApi(page);
  const base = mock.events[0];
  mock.events.push(
    { ...base, version: 15, type: 'task.resumed', payload: { run_number: 2, budget: task.budget } },
    { ...base, version: 16, type: 'task.archived', payload: { uri: task.workspace_uri } },
  );
  await page.route(`**/api/tasks/${TASK_ID}`, route => route.fulfill({ json: task }));
  const requested: Array<{ endpoint: string; run: string | null; path: string | null }> = [];
  let releaseFirst!: () => void;
  const firstFile = new Promise<void>(resolve => { releaseFirst = resolve; });
  await page.route(`**/api/tasks/${TASK_ID}/workspace/*`, async route => {
    const url = new URL(route.request().url());
    const run = url.searchParams.get('run');
    const endpoint = url.pathname.split('/').at(-1)!;
    requested.push({ endpoint, run, path: url.searchParams.get('path') });
    if (endpoint === 'tree') return route.fulfill({ json: { entries: [{ path: 'notes.txt', kind: 'file', size: 32 }] } });
    if (run === '1') await firstFile;
    return route.fulfill({ json: { path: 'notes.txt', text: `ROUND_${run}_READING_SENTINEL`, size: 32, truncated: false, binary: false } });
  });
  await page.goto(`/tasks/${TASK_ID}`);
  await taskAction(page, '复盘记录');
  const records = page.getByRole('dialog', { name: '复盘记录', exact: true });
  const preview = records.getByRole('region', { name: '文件预览', exact: true });
  const openWorkspace = async () => {
    await records.getByRole('tab', { name: '工作区', exact: true }).click();
    await records.getByRole('tree', { name: '归档文件' }).getByText('notes.txt', { exact: true }).click();
  };
  await openWorkspace();
  await expect(preview.locator('pre')).toHaveText('ROUND_2_READING_SENTINEL');
  await expect(preview.getByRole('link', { name: '下载本轮归档', exact: true })).toHaveAttribute('href', `/api/tasks/${TASK_ID}/workspace?run=2`);
  await records.getByRole('tab', { name: '时间轴', exact: true }).click();
  const slider = records.getByRole('slider', { name: '回放版本' });
  await slider.press('End');
  await slider.press('ArrowLeft');
  await slider.press('ArrowLeft');
  await expect(records).toContainText('历史回放 · v14');
  const firstRequest = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname.endsWith('/workspace/file') && url.searchParams.get('run') === '1';
  });
  await openWorkspace();
  await firstRequest;
  try {
    await expect(preview).not.toContainText('ROUND_2_READING_SENTINEL');
    await expect(preview.getByRole('status')).toHaveText('正在读取文件…');
    await expect(preview.getByRole('link', { name: '下载本轮归档', exact: true })).toHaveAttribute('href', `/api/tasks/${TASK_ID}/workspace?run=1`);
  } finally { releaseFirst(); }
  await expect(preview.locator('pre')).toHaveText('ROUND_1_READING_SENTINEL');
  await capture(page, 'history-first-run-workspace.png');
  await records.getByRole('tab', { name: '时间轴', exact: true }).click();
  await records.getByRole('button', { name: '返回实时', exact: true }).click();
  await openWorkspace();
  await expect(preview.locator('pre')).toHaveText('ROUND_2_READING_SENTINEL');
  expect(requested).toEqual(expect.arrayContaining([
    { endpoint: 'tree', run: '1', path: null }, { endpoint: 'file', run: '1', path: 'notes.txt' },
    { endpoint: 'tree', run: '2', path: null }, { endpoint: 'file', run: '2', path: 'notes.txt' },
  ]));
  expect(requested.every(request => request.run === '1' || request.run === '2')).toBe(true);
  expect(mock.unexpected).toEqual([]);
});

test('报告正文成功但任务信息失败时显示短 ID 和可恢复错误', async ({ page }) => {
  const mock = await installMockApi(page);
  let failed = true;
  await page.route(`**/api/tasks/${TASK_ID}`, route => failed
    ? route.fulfill({ status: 500, json: { detail: '合成任务信息读取失败' } })
    : route.fulfill({ json: task }));
  await page.goto(`/tasks/${TASK_ID}/report?run=1`);
  await expect(page.getByRole('article')).toContainText('连接池上限');
  await expect(page.getByRole('alert')).toContainText('任务信息读取失败');
  await expect(page.locator('header').filter({ has: page.getByRole('heading', { name: '第 1 轮报告', exact: true }) })).toContainText(TASK_ID.slice(0, 8));
  await expect(page.getByText('正在读取任务…', { exact: true })).toHaveCount(0);
  await capture(page, 'report-task-error.png');
  failed = false;
  await page.getByRole('button', { name: '重试任务信息', exact: true }).click();
  await expect(page.getByText(task.name, { exact: true })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByRole('article')).toContainText('连接池上限');
  expect(mock.unexpected).toEqual([]);
});

test('历史报告未登录跳转后返回原来的 run 查询参数', async ({ page }) => {
  const mock = await installMockApi(page);
  let authenticated = false;
  await page.route(`**/api/tasks/${TASK_ID}/report?run=1`, route => authenticated
    ? route.fulfill({ contentType: 'text/markdown', body: '# 第一轮报告\n\n合成首轮正文。' })
    : route.fulfill({ status: 401, json: { detail: '请登录' } }));
  await page.route('**/api/login', route => { authenticated = true; return route.fallback(); });
  await page.goto(`/tasks/${TASK_ID}/report?run=1`);
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('账号', { exact: true }).fill('synthetic-reviewer');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}/report\\?run=1$`));
  await expect(page.getByRole('article')).toContainText('合成首轮正文');
  expect(mock.unexpected).toEqual([]);
});
