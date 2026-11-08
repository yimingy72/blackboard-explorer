import { expect, test } from '@playwright/test';
import { installMockApi, TASK_ID } from './fixtures';

test('原生 CTF 创建需要已发布环境，网络不明时重试同一输入组与配置', async ({ page }) => {
  const mock = await installMockApi(page);
  const native = {
    id: 'isolated-ctf', revision: 3, publication_stamp: 'a'.repeat(64),
    label: '隔离 CTF', model_id: 'fixed-model', model_version: 8,
  };
  const requests: unknown[] = [];
  let available = false;
  await page.route('**/api/task-creation-capabilities', (route) =>
    route.fulfill({ json: { mode: 'native-ctf', native_ctf: available ? native : null } }));
  await page.route('**/api/tasks', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    requests.push(route.request().postDataJSON());
    if (requests.length === 1) return route.fulfill({ status: 503, json: { detail: 'Result unavailable' } });
    return route.fulfill({ json: { id: TASK_ID, agent_profile: 'ctf-task-settings', agent_profile_version: 3 } });
  });

  await page.goto('/tasks/new');
  await expect(page.getByText('当前没有已发布的原生 CTF 环境')).toBeVisible();
  await expect(page.getByRole('button', { name: '创建任务', exact: true })).toHaveCount(0);
  available = true;
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await expect(page.getByText(native.label, { exact: true })).toBeVisible();
  await expect(page.getByText('此环境暂不支持初始附件。')).toBeVisible();
  await expect(page.locator('input[type=file]')).toHaveCount(0);
  await expect(page.getByText('黑板探索', { exact: true })).toHaveCount(0);
  await page.getByLabel('任务目标').fill('验证原生创建和流量归档');
  await page.screenshot({ path: test.info().outputPath('native-ctf-create.png') });
  const create = page.getByRole('button', { name: '创建任务', exact: true });
  await create.click();
  await expect(page.getByText(/创建结果未确认/)).toBeVisible();
  await expect(page.getByLabel('任务目标')).toBeDisabled();
  await create.click();
  await expect(page).toHaveURL(`/tasks/${TASK_ID}`);

  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  expect(requests[0]).toMatchObject({
    mode: 'ctf', model_id: native.model_id, model_version: native.model_version,
    native_environment: { id: native.id, revision: native.revision, publication_stamp: native.publication_stamp },
    input_group_id: expect.any(String), input_file_ids: [], egress_allowlist: [],
  });
  expect(requests[0]).not.toHaveProperty('reasoning_effort');
  expect(mock.unexpected).toEqual([]);
});
