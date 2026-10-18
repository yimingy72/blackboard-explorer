import { expect, test } from '@playwright/test';
import { installMockApi } from './fixtures';

test('模型删除确认、取消、删除默认模型与创建任务选项同步', async ({ page }) => {
  const mock = await installMockApi(page);
  mock.platformModels.push({ ...structuredClone(mock.platformModels[0]), name: 'other', label: '保留模型', is_default: false });
  await page.goto('/profiles');
  await page.getByRole('tab', { name: '模型配置', exact: true }).click();
  await page.getByLabel('显示名称').fill('未保存修改');
  await page.getByRole('region', { name: '模型配置编辑' }).getByRole('button', { name: '删除模型', exact: true }).click();
  const confirm = page.getByRole('dialog', { name: '删除模型「审查模型」？' });
  await expect(confirm).toContainText('同时取消平台默认模型');
  await expect(confirm).toContainText('当前未保存的修改也会丢弃');
  await confirm.getByRole('button', { name: '取消', exact: true }).click();
  expect(mock.modelDeletes).toEqual([]);
  await expect(page.getByLabel('显示名称')).toHaveValue('未保存修改');
  await page.getByRole('region', { name: '模型配置编辑' }).getByRole('button', { name: '删除模型', exact: true }).click();
  await confirm.getByRole('button', { name: '删除模型', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已删除 审查模型');
  await expect(page.getByLabel('显示名称')).toHaveValue('保留模型');
  expect(mock.modelDeletes).toEqual(['review-model']);
  await page.getByRole('button', { name: '新建任务', exact: true }).first().click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('combobox', { name: '平台模型', exact: true }).click();
  await expect(page.getByRole('option', { name: /审查模型/ })).toHaveCount(0);
  await expect(page.getByRole('option', { name: /保留模型/ })).toBeAttached();
  expect(mock.unexpected).toEqual([]);
});

test('删除中的页面锁定、最后一个模型空态及窄屏布局', async ({ page }) => {
  const mock = await installMockApi(page);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/platform/models/review-model', async route => {
    if (route.request().method() === 'DELETE') await pending;
    return route.fallback();
  });
  await page.setViewportSize({ width: 390, height: 844 });
  try {
    await page.goto('/profiles');
    await page.getByRole('tab', { name: '模型配置', exact: true }).click();
    await expect(page.getByRole('region', { name: '模型配置编辑' }).getByRole('button', { name: '删除模型', exact: true })).toBeVisible();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: '../.data/model-delete-mobile.png', fullPage: true });
    await page.getByRole('region', { name: '模型配置编辑' }).getByRole('button', { name: '删除模型', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: '删除模型', exact: true }).click();
    await expect(page.getByLabel('显示名称')).toBeDisabled();
    await expect(page.getByRole('tab', { name: 'Worker 配置', exact: true })).toBeDisabled();
    release();
    await expect(page.getByText('添加或选择模型', { exact: true })).toBeVisible();
    await expect(page.getByRole('region', { name: '模型配置编辑' }).getByRole('button', { name: '删除模型', exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: /新增模型$/ }).click();
    await expect(page.getByLabel('显示名称')).toBeEmpty();
    await expect(page.getByLabel('显示名称')).toBeEnabled();
    expect(mock.modelDeletes).toEqual(['review-model']);
    expect(mock.unexpected).toEqual([]);
  } finally { release(); }
});

test('删除失败保留模型与草稿并允许重试', async ({ page }) => {
  const mock = await installMockApi(page);
  let fail = true;
  await page.route('**/api/platform/models/review-model', async route => {
    if (route.request().method() === 'DELETE' && fail) {
      fail = false;
      return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '暂时无法删除，请重试' }) });
    }
    return route.fallback();
  });
  await page.goto('/profiles');
  await page.getByRole('tab', { name: '模型配置', exact: true }).click();
  await page.getByLabel('显示名称').fill('保留草稿');
  await page.getByRole('region', { name: '模型配置编辑' }).getByRole('button', { name: '删除模型', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '删除模型', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('暂时无法删除，请重试');
  await expect(page.getByLabel('显示名称')).toHaveValue('保留草稿');
  expect(mock.platformModels).toHaveLength(1);
  await page.getByRole('region', { name: '模型配置编辑' }).getByRole('button', { name: '删除模型', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '删除模型', exact: true }).click();
  await expect(page.getByText('添加或选择模型', { exact: true })).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});
