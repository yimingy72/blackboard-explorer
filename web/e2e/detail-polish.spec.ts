import { mkdir } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import { installMockApi, TASK_ID } from './fixtures';

test('详情用 Markdown 展示正文和代码，缩略图紧凑且筛选全部移除', async ({ page }) => {
  const mock = await installMockApi(page);
  const source = [
    '已为任务准备共享资料，其他 Agent 可直接复用。',
    '',
    '1. 源文件位于 `/workspace/shared/monthly-reconciliation`。',
    '2. 版本字段保留原文：<version>3.8.9</version>。',
    '',
    '```python',
    'from pathlib import Path',
    'source = Path("/workspace/shared/monthly-reconciliation/source.csv")',
    'print(source.read_text())',
    '```',
    '',
    '![外部图片](https://images.invalid/never-load.png)',
    '<script>window.detailInjected = true</script>',
  ].join('\n');
  const events = mock.events.map((event) => event.type === 'fact.posted'
    ? { ...event, payload: { ...event.payload, kind: 'structure', statement: source } }
    : event.type === 'intent.posted'
      ? { ...event, payload: { ...event.payload, statement: '核对 **日期范围** 与汇总结果。', expected: '- 输出差异行\n- 保留原始编号', method: '执行下面的检查：\n\n```sh\npython verify.py --input source.csv\n```' } }
      : event);
  await page.route(`**/api/tasks/${TASK_ID}/events?*`, (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(events) }));
  const imageRequests: string[] = [];
  await page.route('https://images.invalid/**', (route) => { imageRequests.push(route.request().url()); return route.abort(); });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/tasks/${TASK_ID}`);
  const canvas = page.getByRole('region', { name: '黑板关系图' }).first();
  await canvas.getByRole('button', { name: /事实 F1，已提出/ }).click();
  const detail = page.getByRole('complementary', { name: '对象详情' });
  await expect(detail.getByRole('heading', { name: '结构事实 · F1' })).toBeVisible();
  await expect(detail.locator('ol').first().locator('li')).toHaveCount(2);
  await expect(detail.locator('code').filter({ hasText: '/workspace/shared/monthly-reconciliation' }).first()).toBeVisible();
  await expect(detail.locator('pre code')).toContainText('print(source.read_text())');
  await expect(detail).toContainText('<version>3.8.9</version>');
  await expect(detail.locator('img, script')).toHaveCount(0);
  expect(await page.evaluate(() => 'detailInjected' in window)).toBe(false);
  expect(imageRequests).toEqual([]);
  await expect(page.getByRole('combobox')).toHaveCount(0);
  await expect(page.getByRole('checkbox')).toHaveCount(0);
  await expect(page.locator('summary').filter({ hasText: '验收 1/1' })).toBeVisible();
  const minimap = await page.locator('.react-flow__minimap').boundingBox();
  expect(minimap?.width).toBeLessThanOrEqual(130);
  expect(minimap?.height).toBeLessThanOrEqual(86);
  await mkdir('../.data/qa', { recursive: true });
  await page.screenshot({ path: '../.data/qa/markdown-detail-desktop.png', fullPage: true });
  await canvas.getByRole('button', { name: /意图 I1/ }).click();
  await expect(detail.getByRole('heading', { name: '意图 · I1' })).toBeVisible();
  await expect(detail.getByRole('heading', { name: '调查方法' })).toBeVisible();
  await expect(detail.locator('pre code')).toContainText('python verify.py --input source.csv');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: '../.data/qa/markdown-detail-mobile.png', fullPage: true });
  expect(mock.unexpected).toEqual([]);
});
