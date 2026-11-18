import { expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import type { SandboxCommandDetail } from '../src/api/client';
import { CTF_ID, installCtfApi } from './ctf-fixtures';

test('命令记录只读展示未知结果、截断输出，支持分页和 Agent 筛选', async ({ page }) => {
  const mock = await installCtfApi(page);
  const command: SandboxCommandDetail = {
    id: 'command-52', sequence: 52, task_id: CTF_ID, agent_id: 'member-1', role: 'worker',
    challenge_id: 'C1', assignment_id: 'assignment-1', generation: 2, epoch: 3,
    command: 'python probe.py --input "<script>fixture</script>"', cwd: '/workspace/ctf',
    background: true, state: 'unknown', execution_id: null, exit_code: null,
    created_at: '2026-11-07T08:00:00Z', started_at: null, finished_at: null,
    output_truncated: true, stdout: '<img src=x onerror="alert(1)"> fixture output', stderr: 'fixture stderr',
    stdout_bytes: 70000, stderr_bytes: 14,
  };
  const older = { ...command, id: 'command-51', sequence: 51, agent_id: 'lead', role: 'lead', state: 'completed', execution_id: 'exec-51', exit_code: 0 };
  const requests: URL[] = [];
  await page.route(`**/api/tasks/${CTF_ID}/sandbox/commands**`, route => {
    expect(route.request().method()).toBe('GET');
    const url = new URL(route.request().url());
    requests.push(url);
    if (url.pathname.endsWith('/command-52')) return route.fulfill({ json: command });
    if (url.searchParams.get('agent_id') === 'member-2') return route.fulfill({ json: { items: [], next_before: null } });
    const filtered = url.searchParams.has('agent_id');
    const before = url.searchParams.get('before');
    return route.fulfill({ json: { items: [before ? older : command], next_before: before || filtered ? null : 52 } });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/tasks/${CTF_ID}`);
  await page.getByRole('button', { name: '命令记录', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: '命令记录', exact: true });
  await expect(drawer).toContainText('不包含旧版执行通道，且未覆盖脚本内部所有子进程');
  await drawer.getByRole('button', { name: '查看命令 #52', exact: true }).click();
  const detail = drawer.getByRole('region', { name: '命令详情', exact: true });
  await expect(detail).toContainText('执行结果未确认');
  await expect(detail).toContainText('未确认');
  await expect(detail).toContainText('输出已截断');
  await expect(detail).toContainText(command.command);
  await expect(detail).toContainText(command.stdout);
  await expect(detail.locator('img, script')).toHaveCount(0);
  await expect(detail).not.toContainText('已完成');
  await mkdir('../.data/p3h-command-ui', { recursive: true });
  await page.screenshot({ path: '../.data/p3h-command-ui/desktop.png', fullPage: true });
  await drawer.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(drawer.getByRole('button', { name: '查看命令 #51', exact: true })).toBeVisible();
  await expect(detail).toContainText('选择一条命令');
  await expect(drawer.getByRole('button', { name: '下一页', exact: true })).toBeDisabled();
  await drawer.getByRole('button', { name: '上一页', exact: true }).click();
  await drawer.getByRole('combobox', { name: '按 Agent 筛选命令' }).click();
  await page.getByRole('option', { name: 'Web 分析员', exact: true }).click();
  await expect.poll(() => requests.at(-1)?.searchParams.get('agent_id')).toBe('member-1');
  await expect(drawer).toContainText('第 1 页');
  await drawer.getByRole('button', { name: '查看命令 #52', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(detail).toContainText('执行结果未确认');
  await expect.poll(async () => { const box = await drawer.boundingBox(); return box ? Math.round(box.x) : -1; }).toBe(0);
  expect(await drawer.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await page.screenshot({ path: '../.data/p3h-command-ui/mobile.png' });
  await expect(drawer.getByRole('button', { name: /执行命令|删除命令|取消命令/ })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await drawer.getByRole('combobox', { name: '按 Agent 筛选命令' }).click();
  await page.getByRole('option', { name: '密码助手', exact: true }).click();
  await expect(drawer).toContainText('此范围内暂无命令记录');
  await drawer.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(drawer).toBeHidden();
  expect(requests.some(url => url.searchParams.get('before') === '52')).toBe(true);
  expect(mock.unexpected).toEqual([]);
});
