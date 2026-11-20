import { expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { CTF_ID, installCtfApi, openCtfMenu } from './ctf-fixtures';

for (const width of [1440, 390]) {
  test(`peer board ${width}px shows shared directions, evidence and assistant execution`, async ({ page }) => {
    const mock = await installCtfApi(page);
    const challenge = mock.challenges[0];
    Object.assign(challenge, { work_status: 'awaiting_review', collaboration: {
      difficulty: 'hard', multi_stage: true, assistance_target: 1, review_revision: 1,
      solve_elapsed_ms: 93000, solve_started_at: null, assistance_reminded: false,
    } });
    Object.assign(mock.members[2], { lifecycle: 'active', run_state: 'running' });
    const fact = { id: 'F1', kind: 'observation', statement: '已保存可复核的脚本快照', author: 'member-2',
      version: 15, status: 'proposed', provenance: 'tool_backed', derived_from: [], disputes: [],
      evidence: [{ type: 'file', path: '/workspace/shared/challenges/C1/artifacts/solve.py',
        uri: `evidence/${CTF_ID}/snapshot.py`, sha256: 'b'.repeat(64), call_id: 'capture-call-1', summary: '读取时的脚本快照' }] };
    const intent = { id: 'I1', statement: '核对输入边界', author: 'member-1', holder: 'member-2',
      version: 16, status: 'claimed', method: '比较两组本地输入', expected: '明确边界条件', based_on: ['F1'], result_facts: [] };
    let reads = 0;
    await page.route('**/ctf/knowledge/C1', route => {
      reads += 1;
      return route.fulfill({ json: { challenge_id: 'C1', facts: { F1: fact }, intents: { I1: intent }, version: 16 } });
    });
    await page.setViewportSize({ width, height: 950 });
    await page.goto(`/tasks/${CTF_ID}`);
    await expect(page.getByRole('button', { name: 'Agent：Lead', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '定位节点', exact: true }).click();
    await page.getByRole('menuitem', { name: challenge.title, exact: true }).click();
    const detail = page.getByRole('complementary', { name: `题目详情：${challenge.title}`, exact: true });
    const node = page.getByRole('button', { name: `任务：${challenge.title}`, exact: true });
    if (await node.isVisible()) await node.press('Enter');
    await expect(detail).toBeVisible();
    await expect(detail.getByLabel('协作状态')).toContainText('困难');
    await expect(detail.getByLabel('协作状态')).toContainText('2/10 位 Worker');
    await expect(detail.getByLabel('协作状态')).toContainText('累计解题 1 分 33 秒');
    await expect(detail.getByLabel('协作状态')).toContainText('等待 Lead 核验');
    await expect(detail.getByLabel('协作状态')).not.toContainText('招募');
    await expect(detail.getByRole('button', { name: '分配任务', exact: true })).toHaveCount(0);
    await detail.getByRole('tab', { name: '小黑板', exact: true }).click();
    const board = detail.getByLabel('题内小黑板');
    await expect(board).toContainText('事实 1');
    await expect(board).toContainText('核对输入边界');
    await expect(board.getByRole('button', { name: '密码助手 · 援助 · 持有 1/3', exact: true })).toBeVisible();
    await board.getByRole('button', { name: '证据 1', exact: true }).click();
    await expect(board).toContainText('/workspace/shared/challenges/C1/artifacts/solve.py');
    await expect(board).toContainText('b'.repeat(64));
    await mkdir('../.data/ctf-worker-board-ui', { recursive: true });
    await page.screenshot({ path: `../.data/ctf-worker-board-ui/board-${width}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await board.getByRole('button', { name: '密码助手 · 援助 · 持有 1/3', exact: true }).click();
    const session = page.getByRole('complementary', { name: '密码助手 会话' });
    await expect(session).toBeVisible();
    await expect(session.getByRole('navigation', { name: '正在处理的任务' })).toContainText(challenge.title);
    await expect(session).toContainText('member-2 的历史工作记录');
    await session.getByRole('navigation', { name: '正在处理的任务' }).getByRole('button').click();
    Object.assign(challenge, { work_status: 'completed', verification: { source: 'lead', status: 'accepted' } });
    await openCtfMenu(page);
    await page.getByRole('menuitem', { name: '刷新', exact: true }).click();
    await expect(detail.locator('header')).toContainText('已解出');
    await detail.getByRole('tab', { name: '小黑板', exact: true }).click();
    await expect(board).toContainText('历史认领 1');
    expect(reads).toBeGreaterThan(0);
    expect(mock.sent).toEqual([]);
    expect(mock.unexpected).toEqual([]);
  });
}
