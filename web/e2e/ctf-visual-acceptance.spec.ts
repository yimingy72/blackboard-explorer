import { expect, test, type Page } from '@playwright/test';
import { CTF_ID, installCtfApi, openCtfMenu } from './ctf-fixtures';

const screenshots = '../.data/ui-rebuild/e2e/ctf';

async function layoutMetrics(page: Page) {
  return page.evaluate(() => {
    const nodes = [...document.querySelectorAll('.react-flow__node')].map((node) => node.getBoundingClientRect());
    const minimap = document.querySelector('.react-flow__minimap')?.getBoundingClientRect();
    const overlapsMinimap = minimap ? nodes.some((node) => node.right > minimap.left && node.left < minimap.right && node.bottom > minimap.top && node.top < minimap.bottom) : false;
    const cardOverflow = [...document.querySelectorAll<HTMLElement>('.react-flow__node button')].filter((button) => {
      const bounds = button.getBoundingClientRect();
      const text = [...button.querySelectorAll<HTMLElement>('*')].filter((element) =>
        element.children.length === 0 && element.textContent?.trim() && element.getClientRects().length > 0);
      return button.scrollHeight > button.clientHeight + 1 || text.some((element) => {
        const rect = element.getBoundingClientRect();
        return rect.left < bounds.left - 1 || rect.right > bounds.right + 1 || rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1;
      });
    }).length;
    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      overflowY: document.documentElement.scrollHeight - document.documentElement.clientHeight,
      nodeCount: nodes.length,
      overlapsMinimap,
      cardOverflow,
      nestedScrollers: [...document.querySelectorAll<HTMLElement>('*')].filter((element) => {
        const style = getComputedStyle(element);
        return (style.overflowY === 'auto' || style.overflowY === 'scroll') && element.scrollHeight > element.clientHeight + 1;
      }).length,
    };
  });
}

test('CTF desktop 1920/1440 keeps the board readable and the inspector reversible', async ({ page }) => {
  const mock = await installCtfApi(page);
  await page.setViewportSize({ width: 1920, height: 1100 });
  await page.goto(`/tasks/${CTF_ID}`);
  await expect(page.getByRole('main', { name: 'CTF 团队协作画布' })).toBeVisible();
  await expect(page.getByRole('complementary', { name: /^题目详情：| 会话$/ })).toHaveCount(0);
  const default1920 = await layoutMetrics(page);
  expect(default1920.overflowX).toBeLessThanOrEqual(1);
  expect(default1920.overlapsMinimap).toBe(false);
  expect(default1920.nodeCount).toBe(6);
  expect(default1920.cardOverflow).toBe(0);
  await page.screenshot({ path: `${screenshots}/visual-1920-default.png`, fullPage: true });

  await page.getByRole('button', { name: 'Agent：Web 分析员' }).click();
  await expect(page.getByRole('complementary', { name: 'Web 分析员 会话' })).toBeVisible();
  const agentOpen = await layoutMetrics(page);
  expect(agentOpen.overflowX).toBeLessThanOrEqual(1);
  await page.screenshot({ path: `${screenshots}/visual-1920-agent-open.png`, fullPage: true });

  await page.getByRole('button', { name: '关闭详情' }).click();
  await expect(page.getByRole('complementary', { name: /^题目详情：| 会话$/ })).toHaveCount(0);
  await page.setViewportSize({ width: 1440, height: 900 });
  const default1440 = await layoutMetrics(page);
  expect(default1440.overflowX).toBeLessThanOrEqual(1);
  expect(default1440.overlapsMinimap).toBe(false);
  expect(default1440.cardOverflow).toBe(0);
  await page.screenshot({ path: `${screenshots}/visual-1440-closed.png`, fullPage: true });

  await page.getByRole('button', { name: '任务：模拟 Web 题' }).click();
  const detail = page.getByRole('complementary', { name: /^题目详情：/ });
  await detail.getByRole('tab', { name: /记录/ }).click();
  await expect(detail).toContainText('候选记录');
  await expect(detail).toContainText('FLAG{mock-second}');
  await page.screenshot({ path: `${screenshots}/visual-1440-task-open.png`, fullPage: true });
  await detail.getByRole('button', { name: '关闭详情' }).click();
  await expect(page.getByRole('complementary', { name: /^题目详情：| 会话$/ })).toHaveCount(0);
  expect(mock.unexpected).toEqual([]);
});

test('CTF long content, empty task, history and failure metadata remain readable', async ({ page }) => {
  const longEvidence = JSON.stringify({ args: '参数与结果均为公开模拟文本。'.repeat(160), result: '参数与结果均为公开模拟文本。'.repeat(160), note: '仅用于布局验收' });
  const mock = await installCtfApi(page, { evidenceText: longEvidence });
  mock.task.name = '一个非常长的团队协作验收任务名称，用于检查标题换行和顶部操作区不会互相遮挡';
  mock.members[0].display_name = 'Lead 负责协调与审阅的超长成员名称';
  mock.challenges[0].title = '一个非常长的题目节点名称，用于检查任务卡片不会遮挡验证状态';
  mock.challenges[0].description = '这是一个很长的题目说明，用来验证节点与检查器在桌面和窄屏中都能保持可读，并且不会把按钮或验证状态挤出容器。';
  mock.challenges.push({ id: 'C-empty', title: '空任务（无说明）', description: '', connection: null, requirements: null, owner_id: null, collaborator_ids: [], work_status: 'pending', revision: 1, verification: { status: 'unknown' }, target: {} });
  await page.route(`**/api/tasks/${CTF_ID}/agents/lead/session`, async (route) => {
    const messages = Array.from({ length: 12 }, (_, index) => ({ message_id: `long-${index}`, role: 'assistant', contents: [{ type: 'text', text: `第 ${index + 1} 条历史消息：${'用于检查消息历史换行、滚动和长文本布局。'.repeat(18)}` }] }));
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ revision: 2, session: { state: { in_memory: { messages } } } }) });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/tasks/${CTF_ID}`);
  await expect(page.getByRole('heading', { name: /一个非常长的团队协作验收任务名称/ })).toBeVisible();
  await expect(page.getByRole('button', { name: '任务：空任务（无说明）' })).toBeVisible();
  await page.getByRole('button', { name: '任务：空任务（无说明）' }).click();
  expect((await layoutMetrics(page)).cardOverflow).toBe(0);
  const empty = page.getByRole('complementary', { name: /^题目详情：/ });
  await expect(empty).toContainText('尚未分派');
  await empty.getByRole('tab', { name: /记录/ }).click();
  await expect(empty).toContainText('还没有共享记录');
  await empty.getByRole('tab', { name: /文件/ }).click();
  await expect(empty).toContainText('尚无登记文件');
  await empty.getByRole('button', { name: '关闭详情' }).click();
  await page.screenshot({ path: `${screenshots}/visual-long-and-empty.png`, fullPage: true });

  await page.getByRole('button', { name: /Lead 负责协调与审阅/ }).click();
  const lead = page.getByRole('complementary', { name: /Lead 负责协调与审阅/ });
  await expect(lead).toContainText('第 12 条历史消息');
  const history = await layoutMetrics(page);
  expect(history.overflowX).toBeLessThanOrEqual(1);
  await page.screenshot({ path: `${screenshots}/visual-long-history.png`, fullPage: true });

  await page.getByRole('button', { name: /Web 分析员/ }).click();
  const activity = page.getByRole('complementary', { name: 'Web 分析员 会话' });
  await expect(activity).toContainText('read_artifact');
  await expect(activity).toContainText('已完成');
  await activity.getByRole('button', { name: /^read_artifact/ }).click();
  const toolResult = activity.getByRole('region', { name: 'read_artifact 参数与结果', exact: true }).locator('pre').last();
  await expect(toolResult).toContainText('仅用于布局验收');
  await expect(toolResult).not.toContainText('Full payload in linked observation.');
  await page.screenshot({ path: `${screenshots}/visual-long-tool-result.png`, fullPage: true });

  mock.finish('failed', true);
  await page.getByRole('button', { name: '关闭详情' }).click();
  await openCtfMenu(page);
  await page.getByRole('menuitem', { name: '刷新', exact: true }).click();
  await expect(page.getByRole('menuitem', { name: '刷新', exact: true })).toBeHidden();
  await openCtfMenu(page);
  await page.getByRole('menuitem', { name: '收尾与恢复', exact: true }).click();
  const recovery = page.getByRole('region', { name: '收尾与恢复' });
  await expect(recovery).toContainText('失败诊断');
  await expect(recovery).toContainText('RuntimeError');
  await expect(recovery).toContainText('00000000-0000-4000-8000-000000000001');
  await page.screenshot({ path: `${screenshots}/visual-failure-diagnostic.png`, fullPage: true });
  expect(mock.unexpected).toEqual([]);
});
