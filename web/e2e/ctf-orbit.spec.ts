import { expect, test, type Page } from '@playwright/test';
import type { CtfChallenge } from '../src/ctf/types';
import { CTF_ID, SECOND_ID, installCtfApi, openCtfMenu } from './ctf-fixtures';

const browserLogs = new WeakMap<Page, string[]>();
test.beforeEach(({ page }) => {
  const logs: string[] = []; browserLogs.set(page, logs);
  page.on('console', message => { if (['warning', 'error'].includes(message.type())) logs.push(message.text()); });
  page.on('pageerror', error => logs.push(error.message));
});
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === testInfo.expectedStatus) return;
  const diagnostic = await page.evaluate(() => ({
    viewport: document.querySelector('.react-flow__viewport')?.getAttribute('style'),
    nodes: [...document.querySelectorAll('.react-flow__node')].map(node => ({ id: node.getAttribute('data-id'), style: node.getAttribute('style'), handles: [...node.querySelectorAll('.react-flow__handle')].map(handle => ({ id: handle.getAttribute('data-handleid'), box: handle.getBoundingClientRect().toJSON() })) })),
    edges: [...document.querySelectorAll('.react-flow__edge')].map(edge => edge.getAttribute('data-id')),
  }));
  await testInfo.attach('orbit diagnostic', { body: JSON.stringify({ logs: browserLogs.get(page), diagnostic }, null, 2), contentType: 'application/json' });
});

async function installOrbit(page: Page, count = 2) {
  const mock = await installCtfApi(page);
  mock.members.splice(3);
  mock.members[2].lifecycle = 'active';
  mock.members[2].run_state = 'running';
  mock.challenges[1].work_status = 'in_progress';
  mock.challenges[1].verification = { source: 'agent', status: 'unknown', required: true };
  if (count > 2) {
    const seed = structuredClone(mock.challenges[1]);
    for (let index = 2; index < count; index++) mock.challenges.push({ ...structuredClone(seed), id: `C${index + 1}`, title: `环绕题 ${index + 1}`, external_id: `Q${index + 1}`, owner_id: index % 2 ? 'member-1' : 'member-2' });
  }
  // The second task deliberately reuses entity IDs: persistence must be task scoped.
  await page.route(`**/api/tasks/${SECOND_ID}/ctf/state`, route => route.fulfill({ json: { task: mock.secondTask, members: mock.members, challenges: mock.challenges, records: [], artifacts: [] } }));
  await page.route(`**/api/tasks/${SECOND_ID}/ctf/challenges?*`, route => route.fulfill({ json: { challenges: mock.challenges, next_offset: null } }));
  await page.setViewportSize({ width: 1440, height: 900 });
  return mock;
}

async function positions(page: Page) {
  return page.locator('.react-flow__node').evaluateAll(nodes => Object.fromEntries(nodes.map(node => [node.getAttribute('data-id')!, (node as HTMLElement).style.transform])));
}
async function ready(page: Page, count = 5) {
  await expect(page.locator('.react-flow__node')).toHaveCount(count);
  await expect.poll(() => page.locator('.react-flow__node').evaluateAll(nodes => nodes.every(node => node.getBoundingClientRect().width > 0))).toBe(true);
}
async function refresh(page: Page) {
  await openCtfMenu(page);
  await page.getByRole('menuitem', { name: '刷新', exact: true }).click();
  await expect(page.getByRole('menuitem', { name: '刷新', exact: true })).toBeHidden();
}
async function locate(page: Page, name: string) {
  await page.getByRole('button', { name: '定位节点', exact: true }).click();
  const item = page.getByRole('menuitem', { name, exact: true });
  await item.scrollIntoViewIfNeeded();
  await item.click();
  await expect(item).toBeHidden();
}
async function drag(page: Page, id: string) {
  const node = page.locator(`.react-flow__node[data-id="${id}"]`);
  const box = await node.boundingBox();
  if (!box) throw new Error(`Missing ${id}`);
  const before = await positions(page);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 90, box.y + box.height / 2 + 55, { steps: 8 });
  await page.mouse.up();
  await expect.poll(async () => (await positions(page))[id]).not.toBe(before[id]);
  await page.mouse.move(10, 10);
  await expect.poll(() => page.evaluate(nodeId => {
    const taskId = location.pathname.split('/tasks/')[1];
    const saved = JSON.parse(localStorage.getItem(`bbx:ctf-board:${encodeURIComponent(taskId)}`) ?? '{}') as { pinned?: string[] };
    return saved.pinned?.includes(nodeId) ?? false;
  }, id)).toBe(true);
  return (await positions(page))[id];
}

test('orbit keeps Lead in the center, one shared task and default short assignment edges', async ({ page }) => {
  const mock = await installOrbit(page);
  await page.goto(`/tasks/${CTF_ID}`);
  await ready(page);
  await expect(page.locator('.react-flow__node[data-id="challenge:C1"]')).toHaveCount(1);
  await expect(page.locator('.react-flow__edge[data-id^="claim:"]')).toHaveCount(2);
  await expect(page.locator('.react-flow__edge[data-id^="collaborate:"]')).toHaveCount(1);
  await expect(page.locator('.react-flow__edge[data-id^="team:"]')).toHaveCount(0);
  const centers = await page.locator('.react-flow__node[data-id^="agent:"]').evaluateAll(nodes => nodes.map(node => {
    const transform = new DOMMatrixReadOnly((node as HTMLElement).style.transform);
    return { id: node.getAttribute('data-id'), x: transform.m41 + (node as HTMLElement).offsetWidth / 2, y: transform.m42 + (node as HTMLElement).offsetHeight / 2 };
  }));
  const lead = centers.find(node => node.id === 'agent:lead')!;
  const teammates = centers.filter(node => node.id !== 'agent:lead');
  expect(Math.abs(teammates.reduce((sum, node) => sum + node.x, 0) / teammates.length - lead.x)).toBeLessThan(80);
  expect(Math.abs(teammates.reduce((sum, node) => sum + node.y, 0) / teammates.length - lead.y)).toBeLessThan(80);
  const before = await positions(page);
  await page.getByRole('button', { name: 'Agent：Lead', exact: true }).press('Enter');
  await expect(page.locator('.react-flow__edge[data-id^="team:"]')).toHaveCount(2);
  await expect(page.getByRole('complementary', { name: 'Lead 会话', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭详情', exact: true }).click();
  await page.getByRole('button', { name: '任务：模拟 Web 题', exact: true }).press('Enter');
  await expect(page.getByRole('complementary', { name: '题目详情：模拟 Web 题', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭详情', exact: true }).click();
  expect(await positions(page)).toEqual(before);
  expect(mock.unexpected).toEqual([]);
});

test('orbit drag survives progress polling, reload and switching tasks with reused node IDs', async ({ page }) => {
  const mock = await installOrbit(page);
  await page.goto(`/tasks/${CTF_ID}`);
  await ready(page);
  const initial = (await positions(page))['challenge:C1'];
  const dragged = await drag(page, 'challenge:C1');
  const stable = await positions(page);
  mock.records.push({ id: 'new-progress', challenge_id: 'C1', author_id: 'member-1', kind: 'note', body: '新增合成进展，坐标保持', created_version: 20, artifact_refs: [] });
  await refresh(page);
  await expect(page.getByRole('button', { name: '任务：模拟 Web 题', exact: true })).toContainText('新增合成进展，坐标保持');
  expect(await positions(page)).toEqual(stable);
  expect(await page.evaluate(taskId => {
    const saved = JSON.parse(localStorage.getItem(`bbx:ctf-board:${encodeURIComponent(taskId)}`) ?? '{}') as { positions: Record<string, { x: number; y: number }> };
    const transform = new DOMMatrixReadOnly((document.querySelector('.react-flow__node[data-id="challenge:C1"]') as HTMLElement).style.transform);
    const point = saved.positions['challenge:C1'];
    return Math.max(Math.abs(transform.m41 - point.x), Math.abs(transform.m42 - point.y));
  }, CTF_ID)).toBeLessThan(.01);
  await page.reload();
  await ready(page);
  await expect.poll(async () => (await positions(page))['challenge:C1']).toBe(dragged);
  await page.getByRole('button', { name: '返回任务', exact: true }).click();
  await page.getByRole('link', { name: '查看任务：另一个模拟任务', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(SECOND_ID));
  await ready(page);
  await expect.poll(async () => (await positions(page))['challenge:C1']).toBe(initial);
  const secondDragged = await drag(page, 'challenge:C1');
  await page.getByRole('button', { name: '返回任务', exact: true }).click();
  await page.getByRole('link', { name: `查看任务：${mock.task.name}`, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(CTF_ID));
  await ready(page);
  await expect.poll(async () => (await positions(page))['challenge:C1']).toBe(dragged);
  await page.goto(`/tasks/${SECOND_ID}`);
  await ready(page);
  await expect.poll(async () => (await positions(page))['challenge:C1']).toBe(secondDragged);
  expect(mock.unexpected).toEqual([]);
});

test('orbit reassignment only relocates automatic tasks and gives manual placement precedence', async ({ page }) => {
  const mock = await installOrbit(page, 3);
  await page.goto(`/tasks/${CTF_ID}`);
  await ready(page, 6);
  const manual = await drag(page, 'challenge:C1');
  const before = await positions(page);
  mock.challenges[0].owner_id = 'member-2';
  mock.challenges[0].collaborator_ids = [];
  mock.challenges[2].owner_id = 'member-1';
  await refresh(page);
  await expect(page.locator('.react-flow__edge[data-id="claim:member-1:C3"]')).toHaveCount(1);
  await expect.poll(async () => (await positions(page))['challenge:C3']).not.toBe(before['challenge:C3']);
  const after = await positions(page);
  expect(after['challenge:C1']).toBe(manual);
  for (const id of ['agent:lead', 'agent:member-1', 'agent:member-2', 'challenge:C2']) expect(after[id]).toBe(before[id]);
  expect(mock.unexpected).toEqual([]);
});

// Event feedback uses a stable data attribute because CSS module names are intentionally private.
for (const reducedMotion of ['no-preference', 'reduce'] as const) {
  test(`orbit skips historical beams and treats fresh candidate feedback honestly (${reducedMotion})`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion });
    const mock = await installOrbit(page);
    const history = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/events'));
    await page.goto(`/tasks/${CTF_ID}`);
    await history;
    await ready(page);
    await expect(page.locator('[data-ctf-pulse]')).toHaveCount(0);
    const before = await positions(page);
    mock.records.push({ id: 'fresh-candidate', challenge_id: 'C1', author_id: 'member-1', kind: 'verification', body: '新候选 FLAG{isolated}', created_version: 21, artifact_refs: [], verification: { source: 'agent', status: 'candidate', candidate: 'FLAG{isolated}' } });
    await refresh(page);
    const node = page.getByRole('button', { name: '任务：模拟 Web 题', exact: true });
    await expect(node).toContainText('新候选 FLAG{isolated}');
    await expect(node).not.toContainText('已验证');
    if (reducedMotion === 'reduce') await expect(page.locator('[data-ctf-pulse]')).toHaveCount(0);
    else {
      const pulse = page.locator('[data-ctf-pulse="progress"]');
      await expect(pulse).toHaveCount(1);
      await expect(pulse).not.toHaveAttribute('data-ctf-pulse', 'accepted');
      expect(await pulse.locator('circle').evaluateAll(circles => circles.every(circle => getComputedStyle(circle).fill === 'rgb(66, 136, 204)'))).toBe(true);
      await expect(pulse).toHaveCount(0, { timeout: 10_000 });
    }
    expect(await positions(page)).toEqual(before);
    expect(mock.unexpected).toEqual([]);
  });
}

test('orbit locates the final of 31 tasks and opens completed details after folding', async ({ page }) => {
  const mock = await installOrbit(page, 31);
  const final: CtfChallenge = mock.challenges[30];
  final.work_status = 'completed';
  final.verification = { source: 'platform', status: 'accepted', required: true, test_only: false };
  await page.goto(`/tasks/${CTF_ID}`);
  await ready(page, 33);
  await expect(page.getByRole('button', { name: '显示已完成题目 (1)', exact: true })).toBeVisible();
  await locate(page, final.title);
  const node = page.getByRole('button', { name: `任务：${final.title}`, exact: true });
  await expect(node).toBeInViewport();
  await expect(page.getByRole('complementary', { name: `题目详情：${final.title}`, exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭详情', exact: true }).click();
  await page.getByRole('button', { name: '折叠已完成题目', exact: true }).click();
  await expect(node).toHaveCount(0);
  await locate(page, final.title);
  await expect(node).toBeInViewport();
  await node.press('Enter');
  await expect(page.getByRole('complementary', { name: `题目详情：${final.title}`, exact: true })).toBeVisible();
  expect(mock.operations).toEqual([]);
  expect(mock.sent).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

test('orbit restores a dragged Agent when state arrives after challenge data', async ({ page }) => {
  const mock = await installOrbit(page);
  await page.goto(`/tasks/${CTF_ID}`);
  await ready(page);
  const dragged = await drag(page, 'agent:member-1');
  await page.route(`**/api/tasks/${CTF_ID}/ctf/state`, async route => {
    await new Promise(resolve => setTimeout(resolve, 500));
    await route.fulfill({ json: { task: mock.task, members: mock.members, challenges: mock.challenges, records: mock.records, artifacts: [] } });
  });
  await page.reload();
  await ready(page);
  await expect.poll(async () => (await positions(page))['agent:member-1']).toBe(dragged);
  expect(mock.unexpected).toEqual([]);
});

test('orbit reaches the new assignment after progress arrives during its movement', async ({ page }) => {
  const mock = await installOrbit(page, 3);
  await page.goto(`/tasks/${CTF_ID}`);
  await ready(page, 6);
  const stable = await positions(page);
  mock.challenges[2].owner_id = 'member-1';
  await refresh(page);
  await expect(page.locator('.react-flow__edge[data-id="claim:member-1:C3"]')).toHaveCount(1);
  mock.records.push({ id: 'during-assignment', challenge_id: 'C3', author_id: 'member-1', kind: 'note', body: '移动过程中新增合成记录', created_version: 30, artifact_refs: [] });
  await refresh(page);
  await expect(page.getByRole('button', { name: '任务：环绕题 3', exact: true })).toContainText('移动过程中新增合成记录');
  await expect.poll(() => page.evaluate(() => {
    const top = (id: string) => new DOMMatrixReadOnly((document.querySelector(`.react-flow__node[data-id="${id}"]`) as HTMLElement).style.transform).m42;
    return top('challenge:C3') - top('agent:member-1');
  })).toBeLessThan(-150);
  const final = await positions(page);
  for (const id of ['agent:lead', 'agent:member-1', 'agent:member-2', 'challenge:C1', 'challenge:C2']) expect(final[id]).toBe(stable[id]);
  expect(mock.unexpected).toEqual([]);
});
