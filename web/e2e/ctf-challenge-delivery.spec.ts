import { expect, test, type Page } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import type { CtfRecord } from '../src/ctf/types';
import { CTF_ID, installCtfApi, openCtfMenu } from './ctf-fixtures';

const screenshots = '../.data/ctf-challenge-ui';
function solutionRecord(id = 'S-current', challengeId = 'C1', conclusion = '当前公开阶段结论'): CtfRecord {
  return {
    id, challenge_id: challengeId, kind: 'solution', author_id: 'member-1', created_version: 25,
    created_at: '2026-10-14T08:00:00Z', solution_run_number: 1, solution_challenge_revision: 4, artifact_refs: [], body: conclusion,
    solution: { conclusion, approach: '先核对输入边界，再对照公开观察。', reproduction: '使用已登记脚本与记录中的合成输入复现。', remaining: ['仍缺独立外部复核'], steps: [
      { kind: 'attempt', title: '排除初始假设', result: 'ruled_out', summary: '两组输入均未支持原假设。', record_ids: ['R-proof'], artifact_ids: [] },
      { kind: 'breakthrough', title: '定位真实边界', result: 'supported', summary: '独立比较明确了有效边界。', record_ids: [], artifact_ids: [] },
      { kind: 'result', title: '保存阶段成果', result: 'inconclusive', summary: '保留未定项供继续复核。', record_ids: [], artifact_ids: [] },
    ] },
  };
}
async function openChallenge(page: Page, title = '模拟 Web 题') {
  await page.goto(`/tasks/${CTF_ID}`);
  await expect(page.getByRole('button', { name: 'Agent：Lead', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '定位节点', exact: true }).click();
  await page.getByRole('menuitem', { name: title, exact: true }).click();
  const detail = page.getByRole('complementary', { name: `题目详情：${title}`, exact: true });
  const node = page.getByRole('button', { name: `任务：${title}`, exact: true });
  if (await node.isVisible()) await node.press('Enter');
  await expect(detail).toBeVisible();
  return detail;
}
async function refresh(page: Page) {
  await openCtfMenu(page);
  await page.getByRole('menuitem', { name: '刷新', exact: true }).click();
  await expect(page.getByRole('menuitem', { name: '刷新', exact: true })).toBeHidden();
}

for (const width of [1440, 390]) {
  test(`题目公开流程 ${width}px 精确结论、结构化取舍、引用定位与标题去重`, async ({ page }) => {
    const mock = await installCtfApi(page);
    const title = 'Boundary [crypto/medium]';
    Object.assign(mock.challenges[0], { title, external_id: 'Boundary', work_status: 'completed', solution_record_id: 'S-current' });
    mock.records.push(solutionRecord(), solutionRecord('S-foreign', 'C2', '他题结论不得串入'), solutionRecord('S-later', 'C1', '未指向的较新结论'));
    mock.records.at(-1)!.created_version = 30;
    const proof: CtfRecord = { id: 'R-proof', challenge_id: 'C1', kind: 'note', author_id: 'member-1', body: '独立对照的公开原始依据', created_version: 20, artifact_refs: [] };
    const reads: string[] = [];
    await page.route(`**/api/tasks/${CTF_ID}/ctf/challenges/C1/records/R-proof`, route => {
      reads.push(route.request().method()); return route.fulfill({ json: proof });
    });
    await page.setViewportSize({ width, height: 900 });
    const detail = await openChallenge(page, title);
    await expect(detail.getByRole('heading', { name: 'Boundary', exact: true })).toHaveCount(1);
    await expect(detail).toContainText('crypto · medium');
    await expect(detail).toContainText('当前公开阶段结论');
    await expect(detail).not.toContainText('他题结论不得串入');
    await expect(detail).not.toContainText('未指向的较新结论');
    await expect(detail).toContainText('已交付');
    await expect(detail.getByRole('button', { name: /与 Lead 讨论/ })).toHaveCount(0);
    await detail.getByRole('tab', { name: '解题流程', exact: true }).click();
    await expect(detail).toContainText('先核对输入边界');
    await expect(detail).toContainText('排除初始假设');
    await expect(detail).toContainText('排除');
    await expect(detail).toContainText('定位真实边界');
    await expect(detail).toContainText('突破点');
    await expect(detail).toContainText('未定');
    await expect(detail).toContainText('仍缺独立外部复核');
    await detail.getByRole('button', { name: '依据 1', exact: true }).click();
    const reference = page.getByRole('dialog', { name: '依据记录', exact: true });
    await expect(reference).toContainText(proof.body);
    expect(reads).toEqual(['GET']);
    await reference.getByRole('button', { name: '关闭', exact: true }).click();
    await expect(reference).not.toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await mkdir(screenshots, { recursive: true });
    await page.screenshot({ path: `${screenshots}/delivery-${width}.png`, fullPage: true });
    expect(mock.operations).toEqual([]); expect(mock.sent).toEqual([]); expect(mock.unexpected).toEqual([]);
  });
}

test('流程不在state时精确GET回读，缺失错误可重试，重开只保留上一轮', async ({ page }) => {
  const mock = await installCtfApi(page);
  Object.assign(mock.challenges[0], { work_status: 'completed', solution_record_id: 'S-current' });
  const report = solutionRecord();
  const reads: string[] = [];
  let fail = true;
  await page.route(`**/api/tasks/${CTF_ID}/ctf/challenges/C1/records/S-current`, route => {
    reads.push(route.request().method());
    return fail ? route.fulfill({ status: 404, json: { detail: 'Record not found' } }) : route.fulfill({ json: report });
  });
  const detail = await openChallenge(page);
  await expect(detail.getByRole('alert')).toContainText('解题流程读取失败');
  await expect(detail).not.toContainText('当前公开阶段结论');
  fail = false;
  await detail.getByRole('button', { name: '重试', exact: true }).click();
  await expect(detail).toContainText('当前公开阶段结论');
  expect(reads.every(method => method === 'GET')).toBe(true);
  mock.records.push(report);
  Object.assign(mock.challenges[0], { work_status: 'in_progress', solution_record_id: null, revision: 5 });
  await refresh(page);
  await expect(detail).toContainText('尚未提交最终流程');
  await expect(detail).not.toContainText('当前公开阶段结论');
  await detail.getByRole('tab', { name: '解题流程', exact: true }).click();
  await expect(detail).toContainText('负责人尚未提交解题流程');
  await expect(detail.getByText('当前公开阶段结论', { exact: true })).toBeHidden();
  await detail.getByRole('button', { name: '上一轮结论', exact: true }).click();
  await expect(detail.getByText('当前公开阶段结论', { exact: true })).toBeVisible();
  expect(mock.operations).toEqual([]); expect(mock.unexpected).toEqual([]);
});

test('错误本题结论指针不把普通记录冒充最终流程', async ({ page }) => {
  const mock = await installCtfApi(page);
  Object.assign(mock.challenges[0], { work_status: 'completed', solution_record_id: 'R2' });
  const detail = await openChallenge(page);
  await detail.getByRole('tab', { name: '解题流程', exact: true }).click();
  await expect(detail).not.toContainText('FLAG{mock-first}');
  await expect(detail.getByText('正在读取解题流程…', { exact: true })).toHaveCount(0);
  await expect(detail.getByRole('alert')).toBeVisible();
  expect(mock.operations).toEqual([]); expect(mock.unexpected).toEqual([]);
});

test('六种状态遵守completed和真实非模拟验收边界', async ({ page }) => {
  const mock = await installCtfApi(page);
  mock.challenges.splice(1);
  for (const [work_status, verification, label] of [
    ['pending', {}, '待分配'], ['in_progress', { source: 'platform', status: 'accepted' }, '解题中'],
    ['blocked', {}, '受阻'], ['completed', { source: 'agent', status: 'candidate' }, '已交付'],
    ['completed', { source: 'platform', status: 'accepted', test_only: true }, '已交付'],
    ['completed', { source: 'agent', status: 'accepted' }, '已交付'],
    ['completed', { source: 'platform', status: 'accepted', test_only: false }, '已解出'],
    ['completed', { source: 'user', status: 'accepted' }, '已解出'], ['cancelled', {}, '已取消'],
  ] as const) {
    Object.assign(mock.challenges[0], { work_status, verification });
    const detail = await openChallenge(page);
    await expect(detail.locator('header')).toContainText(label);
    if (label !== '已解出') await expect(detail.locator('header')).not.toContainText('已解出');
  }
  expect(mock.operations).toEqual([]); expect(mock.unexpected).toEqual([]);
});
