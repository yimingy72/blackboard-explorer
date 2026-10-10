import { settleModal } from './ui';
import { expect, test } from '@playwright/test';
import { installConversationFixture } from './conversation-fixture';
import { CTF_ID, installCtfApi } from './ctf-fixtures';
import { TASK_ID } from './fixtures';

for (const mode of ['blackboard', 'ctf'] as const) for (const width of [1440, 390]) {
  test(`${mode} ${width}px 任务目标与会话草稿共存，附件与 Esc 只关闭当前区域`, async ({ page }) => {
    const goal = '# 原始任务目标\n\n' + '核对候选结果、来源和证据，记录未完成事项。'.repeat(24) + '\n\nGOAL_END_SENTINEL';
    const requirements = '完成要求必须保留来源与验证依据';
    const file = { id: '44444444-4444-4444-8444-777777777777', filename: 'goal-notes.md',
      path: '/workspace/shared/inputs/goal-notes.md', uri: 'inputs/goal-notes.md', size: 32, sha256: 'a'.repeat(64) };
    const mock = mode === 'ctf' ? await installCtfApi(page) : await installConversationFixture(page, { running: true });
    const taskId = mode === 'ctf' ? CTF_ID : TASK_ID;
    if ('task' in mock) Object.assign(mock.task, { goal, completion_requirements: requirements, initial_attachments: [file] });
    else Object.assign(mock.events[0].payload, { goal, acceptance: [{ id: 'A1', desc: requirements }], initial_attachments: [file] });
    await page.route('**/api/evidence?*', (route) => new URL(route.request().url()).searchParams.get('uri') === file.uri
      ? route.fulfill({ contentType: 'text/plain', body: '附件原文 GOAL_ATTACHMENT_SENTINEL' }) : route.fallback());
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`/tasks/${taskId}`);
    const agent = mode === 'ctf' ? page.getByRole('button', { name: 'Agent：Web 分析员', exact: true })
      : page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ });
    const chat = page.getByRole('complementary', { name: mode === 'ctf' ? 'Web 分析员 会话' : 'Agent 1 对话记录', exact: true });
    await page.getByRole('button', { name: '放大画布', exact: true }).click();
    await agent.click();
    await chat.getByRole('textbox').fill('未发送草稿 GOAL_DRAFT_SENTINEL');
    const viewport = page.locator('.react-flow__viewport');
    const transform = await viewport.getAttribute('style');
    const goalButton = page.getByRole('button', { name: '查看任务目标', exact: true });
    await goalButton.click();
    const summary = page.getByRole('region', { name: '任务目标', exact: true });
    await expect(summary).toContainText(requirements);
    await expect(summary).not.toContainText('GOAL_END_SENTINEL');
    await expect(page.getByRole('dialog', { name: /任务目标/ })).toHaveCount(0);
    if (width === 1440) await expect(chat).toBeVisible();
    else await expect(chat).toBeHidden();
    await expect(viewport).toHaveAttribute('style', transform ?? '');
    await summary.getByRole('button', { name: '展开全文', exact: true }).click();
    await expect(summary).toContainText('GOAL_END_SENTINEL');
    await summary.getByRole('button', { name: '原文', exact: true }).click();
    await expect(summary.locator('pre').first()).toHaveText(goal);
    await summary.getByRole('button', { name: '预览附件 goal-notes.md', exact: true }).click();
    const attachment = page.getByRole('dialog', { name: 'goal-notes.md', exact: true });
    await expect(attachment).toContainText('GOAL_ATTACHMENT_SENTINEL');
    await settleModal(attachment); await attachment.focus(); await attachment.press('Escape');
    await expect(attachment).toBeHidden();
    await expect(summary).toBeVisible();
    await summary.getByRole('button', { name: '收起目标', exact: true }).press('Escape');
    await expect(summary).toBeHidden();
    await expect(chat.getByRole('textbox')).toHaveValue('未发送草稿 GOAL_DRAFT_SENTINEL');
    await expect(viewport).toHaveAttribute('style', transform ?? '');
    if (width === 1440) {
      await goalButton.click();
      await chat.getByRole('textbox').press('Escape');
      await expect(chat).toBeHidden();
      await expect(summary).toBeVisible();
    } else {
      await chat.getByRole('textbox').press('Escape');
      await expect(chat).toBeHidden();
    }
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    expect(mock.unexpected).toEqual([]);
    if ('sent' in mock) expect(mock.sent).toEqual([]);
  });
}
