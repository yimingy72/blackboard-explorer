import { expect, test } from '@playwright/test';
import { TASK_ID, installMockApi } from './fixtures';

test('终态任务填写追加额度后续跑同一任务并保留旧报告入口', async ({ page }) => {
  const mock = await installMockApi(page);
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('button', { name: '续跑任务' }).click();
  const dialog = page.getByRole('dialog', { name: '续跑任务' });
  await expect(dialog).toContainText('保留黑板和 Agent 会话');
  await dialog.getByLabel('追加金额（CNY）').fill('2.50');
  await dialog.getByLabel('追加运行分钟').fill('30');
  await dialog.getByLabel('采用当前 Worker 工具设置').check();
  await page.screenshot({ path: test.info().outputPath('resume-dialog.png') });
  await dialog.getByRole('button', { name: '确认续跑' }).click();
  await expect(dialog).not.toBeVisible();
  expect(mock.resumeSaves).toHaveLength(1);
  expect(mock.resumeSaves[0]).toMatchObject({ additional_cost: '2.50', additional_minutes: 30, refresh_tools: true });
  await page.reload();
  await expect(page.getByText('第 2 轮', { exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '历史轮次' }).getByRole('link', { name: '报告' })).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});
