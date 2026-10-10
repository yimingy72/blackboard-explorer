import { selectOption } from './ui';
import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';
import { TASK_ID, installMockApi } from './fixtures';

test('模型标题、启用与保存同行，表单紧凑且手机可用', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/profiles');
  await page.getByRole('tab', { name: /模型配置$/ }).click();
  await selectOption(page, page.getByRole('combobox', { name: '模型连接', exact: true }), '审查模型');
  const editor = page.getByRole('region', { name: '模型配置编辑' });
  const title = await editor.getByRole('combobox', { name: '模型连接', exact: true }).boundingBox();
  const connectionLayout = await editor.getByRole('combobox', { name: '模型连接', exact: true }).evaluate((input) => {
    const picker = input.closest<HTMLElement>('[class*="connectionPicker"]');
    const select = input.closest<HTMLElement>('[class*="resourceSelect"]');
    if (!picker || !select) throw new Error('Missing application-owned connection picker');
    const style = getComputedStyle(picker);
    return { display: style.display, flexWrap: style.flexWrap, picker: picker.getBoundingClientRect().toJSON(),
      select: select.getBoundingClientRect().toJSON(), actions: [...picker.querySelectorAll('button')].map(button => ({text:button.innerText,rect:button.getBoundingClientRect().toJSON()})) };
  });
  await test.info().attach('model-connection-layout', { body: JSON.stringify(connectionLayout), contentType: 'application/json' });
  if (process.env.BBX_E2E_LAYOUT_REPORT === '1') console.log('model-connection-layout', JSON.stringify(connectionLayout));
  const enabled = await editor.getByLabel('启用此模型').boundingBox();
  const save = await editor.getByRole('button', { name: /保存$/ }).boundingBox();
  expect(Math.abs((title?.y ?? 0) - (enabled?.y ?? 0))).toBeLessThan(20);
  expect(Math.abs((title?.y ?? 0) - (save?.y ?? 0))).toBeLessThan(20);
  const label = await editor.getByLabel('显示名称').boundingBox();
  const provider = await editor.getByLabel('Provider').boundingBox();
  const model = await editor.getByLabel('模型 ID', { exact: true }).boundingBox();
  const middle = (bounds: typeof label) => bounds ? bounds.y + bounds.height / 2 : NaN;
  expect(Math.abs(middle(label) - middle(provider))).toBeLessThanOrEqual(1);
  expect(Math.abs(middle(provider) - middle(model))).toBeLessThanOrEqual(1);
  await page.screenshot({ path: test.info().outputPath('compact-model-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await editor.getByLabel('显示名称').fill('紧凑模型');
  await editor.getByRole('button', { name: /保存$/ }).click();
  expect(mock.modelSaves[0].label).toBe('紧凑模型');
  expect(mock.unexpected).toEqual([]);
});

test('列表仅一次总数，名称与ID同行，完整长目标按需展开', async ({ page }) => {
  const mock = await installMockApi(page);
  const goal = '完整目标内容和不同资料的核对要求。\n'.repeat(100) + '目标末尾标记';
  const task = { id: TASK_ID, name: '月度核对', goal, status: 'finished', acceptance_state: {}, usage: {}, cost_currency: 'CNY', budget: { max_cost: 10 }, created_at: '2026-09-30T08:00:00Z', agent_profile: 'default', agent_profile_version: 2 };
  await page.route('**/api/tasks', (route) => route.fulfill({ json: Array.from({ length: 8 }, (_, i) => ({ ...task, id: i ? `22222222-2222-4222-8222-${String(i).padStart(12, '0')}` : TASK_ID, name: i ? `核对 ${i}` : task.name })) }));
  await page.route(`**/api/tasks/${TASK_ID}`, (route) => route.fulfill({ json: task }));
  mock.events[0].payload = { ...mock.events[0].payload, name: task.name, goal };
  await page.goto('/tasks');
  await expect(page.getByText('8 项任务', { exact: true })).toHaveCount(1);
  const row = page.getByRole('row').filter({ hasText: '月度核对' });
  const id = await row.getByText(TASK_ID.slice(0, 8)).boundingBox();
  const name = await row.getByRole('link', { name: task.name, exact: true }).boundingBox();
  expect(Math.abs((id?.y ?? 0) - (name?.y ?? 0))).toBeLessThan(6);
  await page.screenshot({ path: test.info().outputPath('named-tasks-desktop.png'), fullPage: true });
  await page.getByLabel('查找任务').fill(TASK_ID.slice(0, 8));
  await expect(page.getByRole('row')).toHaveCount(2);
  await row.getByRole('link', { name: task.name, exact: true }).click();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(task.name);
  await page.getByRole('button', { name: '查看任务目标', exact: true }).click();
  const detail = page.getByRole('region', { name: '任务目标', exact: true });
  await expect(detail).not.toContainText('目标末尾标记');
  await detail.getByRole('button', { name: '展开全文' }).click();
  await expect(detail).toContainText('目标末尾标记');
  await detail.getByRole('button', { name: '收起全文' }).click();
  await expect(detail).not.toContainText('目标末尾标记');
  expect(mock.unexpected).toEqual([]);
});

test('文件上传、移除和命名创建，只绑定明确选中的文件', async ({ page }) => {
  const mock = await installMockApi(page);
  let submitted: Record<string, unknown> | undefined;
  await page.route('**/api/tasks', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    submitted = route.request().postDataJSON();
    await route.fulfill({ json: { id: TASK_ID, agent_profile: 'default', agent_profile_version: 2 } });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/tasks/new');
  await page.getByLabel('任务名', { exact: true }).fill('初始资料核对');
  await page.getByLabel('任务目标').fill('核对附件中的数字，说明资料差异');
  await page.getByLabel('验收条件 1', { exact: true }).fill('给出可复核结果');
  await page.locator('input[type="file"]').setInputFiles([
    { name: '资料.csv', mimeType: 'text/csv', buffer: Buffer.from('value\n10') },
    { name: '不要保留.txt', mimeType: 'text/plain', buffer: Buffer.from('remove') },
  ]);
  await expect(page.getByText('已上传', { exact: false })).toHaveCount(2);
  await page.getByRole('button', { name: '移除附件 不要保留.txt' }).click();
  await expect(page.getByText('不要保留.txt', { exact: true })).toHaveCount(0);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: test.info().outputPath('create-inputs-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: /创建任务/ }).click();
  await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
  expect(submitted?.name).toBe('初始资料核对');
  expect(submitted?.input_group_id).toMatch(/^33333333/);
  expect(submitted?.input_file_ids).toHaveLength(1);
  expect(mock.unexpected).toEqual([]);
});

test('同名同大小同修改时间但内容不同的附件全部保留并绑定', async ({ page }) => {
  const mock = await installMockApi(page);
  const uploads: string[] = [];
  let submitted: Record<string, unknown> | undefined;
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().includes('/files?filename=')) uploads.push(request.postDataBuffer()!.toString());
  });
  await page.route('**/api/tasks', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    submitted = route.request().postDataJSON();
    return route.fulfill({ json: { id: TASK_ID, agent_profile: 'default', agent_profile_version: 2 } });
  });
  await page.goto('/tasks/new');
  await page.getByLabel('任务目标').fill('核对两份同名资料');
  await page.getByLabel('验收条件 1', { exact: true }).fill('分别核对两份文件');
  await page.locator('input[type="file"]').evaluate((input: HTMLInputElement) => {
    const files = new DataTransfer();
    files.items.add(new File(['first'], 'same.txt', { lastModified: 123 }));
    files.items.add(new File(['other'], 'same.txt', { lastModified: 123 }));
    input.files = files.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await expect(page.getByText(/已上传/)).toHaveCount(2);
  await expect(page.getByText('same.txt', { exact: true })).toHaveCount(2);
  await page.getByRole('button', { name: /创建任务/ }).click();
  await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
  expect(uploads).toEqual(['first', 'other']);
  expect(submitted?.input_file_ids).toHaveLength(2);
  expect(new Set(submitted?.input_file_ids as string[]).size).toBe(2);
  expect(mock.unexpected).toEqual([]);
});

test('创建响应丢失后冻结原请求，模型停用也能重试且验收条件不可删除', async ({ page }) => {
  const mock = await installMockApi(page);
  const attempts: Record<string, unknown>[] = [];
  await page.route('**/api/tasks', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    attempts.push(route.request().postDataJSON());
    if (attempts.length === 1) return route.abort('failed');
    return route.fulfill({ json: { id: TASK_ID, agent_profile: 'default', agent_profile_version: 2 } });
  });
  await page.goto('/tasks/new');
  await page.getByLabel('任务名', { exact: true }).fill('不重复创建');
  await page.getByLabel('任务目标').fill('核对');
  await page.getByLabel('验收条件 1', { exact: true }).fill('结果可检查');
  await page.getByRole('button', { name: /添加验收条件/ }).click();
  await page.getByLabel('验收条件 2', { exact: true }).fill('全部附件已核对');
  await page.getByRole('button', { name: /创建任务/ }).click();
  await expect(page.getByRole('alert')).toContainText('创建结果未确认');
  await expect(page.getByLabel('任务目标')).toBeDisabled();
  await expect(page.getByRole('button', { name: '删除验收条件 2', exact: true })).toBeDisabled();
  mock.platformModels[0].enabled = false;
  await page.evaluate(() => window.dispatchEvent(new Event('offline')));
  const refreshed = page.waitForResponse((response) => response.url().endsWith('/api/platform/models'));
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await refreshed;
  await expect(page.getByText(/当前没有可用模型/)).toBeVisible();
  await expect(page.getByRole('button', { name: /创建任务/ })).toBeEnabled();
  await page.getByRole('button', { name: /创建任务/ }).click();
  await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
  expect(attempts).toHaveLength(2); expect(attempts[0]).toEqual(attempts[1]);
  expect(mock.unexpected).toEqual([]);
});

test('取消离页后已绑定组保持保护，延迟创建响应不把页面拉回详情', async ({ page }) => {
  const mock = await installMockApi(page);
  let release!: () => void;
  const responseReady = new Promise<void>((resolve) => { release = resolve; });
  let bound = false;
  let deletions = 0;
  await page.route('**/api/task-input-groups/*', (route) => {
    if (bound && route.request().method() === 'DELETE') {
      deletions++;
      return route.fulfill({ status: 409, json: { detail: '附件组已绑定任务，不能修改' } });
    }
    return route.fallback();
  });
  await page.route('**/api/tasks', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    bound = true;
    await responseReady;
    return route.fulfill({ json: { id: TASK_ID, agent_profile: 'default', agent_profile_version: 2 } });
  });
  await page.goto('/tasks/new');
  await page.getByLabel('任务目标').fill('核对');
  await page.getByLabel('验收条件 1', { exact: true }).fill('可复核');
  await page.getByRole('button', { name: /创建任务/ }).click();
  await expect.poll(() => bound).toBe(true);
  await page.getByRole('button', { name: /^取\s*消$/ }).click();
  await expect(page).toHaveURL('/tasks');
  await expect.poll(() => deletions).toBe(1);
  const response = page.waitForResponse((reply) => reply.request().method() === 'POST' && reply.url().endsWith('/api/tasks'));
  release();
  await (await response).finished();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page).toHaveURL('/tasks');
  expect(mock.unexpected).toEqual([]);
});

for (const status of [404, 410]) {
  test(`附件组${status}后重建组并重传本地文件，完整草稿保留`, async ({ page }) => {
    const mock = await installMockApi(page);
    const groups: Array<{ id: string; expires_at: string; files: Array<{ id: string; filename: string; path: string; uri: string; size: number; sha256: string }> }> = [];
    const uploaded: string[] = [];
    let expired = false;
    let submitted: Record<string, unknown> | undefined;
    await page.route('**/api/task-input-groups**', (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === 'POST' && url.pathname === '/api/task-input-groups') {
        const group = { id: `33333333-3333-4333-8333-${String(groups.length + 1).padStart(12, '0')}`, expires_at: '2026-10-01T00:00:00Z', files: [] };
        groups.push(group);
        return route.fulfill({ json: group });
      }
      const group = groups.find((item) => url.pathname.includes(item.id));
      if (!group) return route.fulfill({ status: 404, json: { detail: '附件组不存在' } });
      if (expired && group === groups[0]) return route.fulfill({ status, json: { detail: '附件组已取消或过期，请重新上传' } });
      if (request.method() === 'POST' && url.pathname.endsWith('/files')) {
        const bytes = request.postDataBuffer()!;
        uploaded.push(bytes.toString());
        const id = `44444444-4444-4444-8444-${String(uploaded.length).padStart(12, '0')}`;
        const filename = url.searchParams.get('filename')!;
        const file = { id, filename, path: `/workspace/shared/inputs/${id}/${filename}`, uri: `inputs/${group.id}/${id}/${filename}`, size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
        group.files.push(file);
        return route.fulfill({ json: file });
      }
      return route.fulfill({ json: group });
    });
    await page.route('**/api/tasks', (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      submitted = route.request().postDataJSON();
      if (submitted?.input_group_id === groups[0].id) return route.fulfill({ status, json: { detail: '附件组已取消或过期，请重新上传' } });
      return route.fulfill({ json: { id: TASK_ID, agent_profile: 'default', agent_profile_version: 2 } });
    });
    await page.goto('/tasks/new');
    await page.getByLabel('任务名', { exact: true }).fill('保留草稿');
    await page.getByLabel('任务目标').fill('完整目标不得丢失');
    await page.getByLabel('领域背景').fill('原始领域资料');
    await page.getByLabel('验收条件 1', { exact: true }).fill('核对附件并给出结果');
    await page.getByLabel('金额上限（人民币元）').fill('12.5');
    await page.locator('input[type="file"]').setInputFiles({ name: 'source.txt', mimeType: 'text/plain', buffer: Buffer.from('original input') });
    await expect(page.getByText(/已上传/)).toBeVisible();
    expired = true;
    await page.getByRole('button', { name: /创建任务/ }).click();
    await page.getByRole('button', { name: '重新上传附件', exact: true }).click();
    await expect.poll(() => uploaded.length).toBe(2);
    await expect(page.getByText(/已上传/)).toBeVisible();
    await expect(page.getByLabel('任务名', { exact: true })).toHaveValue('保留草稿');
    await expect(page.getByLabel('任务目标')).toHaveValue('完整目标不得丢失');
    await expect(page.getByLabel('领域背景')).toHaveValue('原始领域资料');
    await expect(page.getByLabel('验收条件 1', { exact: true })).toHaveValue('核对附件并给出结果');
    await expect(page.getByLabel('金额上限（人民币元）')).toHaveValue('12.5');
    await page.getByRole('button', { name: /创建任务/ }).click();
    await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
    expect(groups).toHaveLength(2);
    expect(uploaded).toEqual(['original input', 'original input']);
    expect(submitted?.input_group_id).toBe(groups[1].id);
    expect(submitted?.input_file_ids).toEqual([groups[1].files[0].id]);
    expect(mock.unexpected).toEqual([]);
  });
}

for (const retry of [true, false]) {
  test(`上传已落库但响应丢失时${retry ? '恢复同一附件' : '移除后不绑定残留附件'}`, async ({ page }) => {
    const mock = await installMockApi(page);
    const group = { id: '33333333-3333-4333-8333-999999999999', expires_at: '2026-10-01T00:00:00Z', files: [] as Array<{ id: string; filename: string; path: string; uri: string; size: number; sha256: string }> };
    let uploads = 0;
    let submitted: Record<string, unknown> | undefined;
    await page.route('**/api/task-input-groups**', async (route) => {
      const request = route.request();
      if (request.method() === 'POST' && request.url().includes('/files?')) {
        uploads++;
        const bytes = request.postDataBuffer()!;
        const id = '44444444-4444-4444-8444-999999999999';
        group.files.push({ id, filename: '资料.txt', path: `/workspace/shared/inputs/${id}/资料.txt`, uri: `inputs/${group.id}/${id}/资料.txt`, size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') });
        return route.abort('failed');
      }
      return route.fulfill({ json: group });
    });
    await page.route('**/api/tasks', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      submitted = route.request().postDataJSON();
      return route.fulfill({ json: { id: TASK_ID, agent_profile: 'default', agent_profile_version: 2 } });
    });
    await page.goto('/tasks/new');
    await page.getByLabel('任务目标').fill('核对');
    await page.getByLabel('验收条件 1', { exact: true }).fill('可复核');
    await page.locator('input[type="file"]').setInputFiles({ name: '资料.txt', mimeType: 'text/plain', buffer: Buffer.from('original input') });
    await expect(page.getByRole('button', { name: '重试上传' })).toBeVisible();
    await expect(page.getByRole('button', { name: /创建任务/ })).toBeDisabled();
    if (retry) {
      await page.getByRole('button', { name: '重试上传' }).click();
      await expect(page.getByText(/已上传/)).toBeVisible();
    } else await page.getByRole('button', { name: '移除附件 资料.txt' }).click();
    await page.getByRole('button', { name: /创建任务/ }).click();
    await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
    expect(uploads).toBe(1);
    expect(submitted?.input_file_ids).toEqual(retry ? [group.files[0].id] : []);
    expect(mock.unexpected).toEqual([]);
  });
}

test('任务内容展示初始附件路径并可下载完整原件', async ({ page }) => {
  const mock = await installMockApi(page);
  const bytes = Buffer.from('source,value\noriginal,42\n');
  const id = '44444444-4444-4444-8444-888888888888';
  const file = { id, filename: 'input.csv', path: `/workspace/shared/inputs/${id}/input.csv`, uri: `inputs/${TASK_ID}/${id}/input.csv`, size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
  const mockApi = process.env.BBX_E2E_MOCK_API_URL;
  if (!mockApi) throw new Error('Run E2E through node e2e/run.cjs to isolate native browser downloads.');
  const registered = await page.request.post(`${mockApi}/__e2e/download`, { data: {
    uri: file.uri, filename: file.filename, contentType: 'text/csv', body: bytes.toString('base64'),
  } });
  expect(registered.status()).toBe(204);
  mock.events[0].payload = { ...mock.events[0].payload, initial_attachments: [file] };
  await page.context().route('**/api/evidence?*', (route) => new URL(route.request().url()).searchParams.get('uri') === file.uri
    ? route.fulfill({ body: bytes, headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="input.csv"', 'X-Content-Type-Options': 'nosniff' } })
    : route.fallback());
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('button', { name: '查看任务目标', exact: true }).click();
  const detail = page.getByRole('region', { name: '任务目标', exact: true });
  await detail.getByRole('button', { name: `预览附件 ${file.filename}`, exact: true }).click();
  const attachment = page.getByRole('dialog', { name: file.filename, exact: true });
  await expect(attachment.getByText(`路径：${file.path}`, { exact: true })).toBeVisible();
  const downloaded = page.waitForEvent('download');
  await attachment.getByRole('link', { name: '下载完整证据', exact: true }).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe(file.filename);
  const { readFile } = await import('node:fs/promises');
  expect(await readFile((await download.path())!)).toEqual(bytes);
  expect(mock.unexpected).toEqual([]);
});
