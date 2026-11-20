import { expect, test } from '@playwright/test';
import { CTF_ID, installCtfApi, openCtfMenu } from './ctf-fixtures';

test('native workbench assigns a Worker, shows original execution, and refreshes fees', async ({ page }) => {
  const mock = await installCtfApi(page);
  Object.assign(mock.task, { pi_model: { model_id: 'synthetic', model_revision: 1 }, cost_currency: 'USD' });
  const challenge = mock.challenges[0];
  Object.assign(challenge, { owner_id: null, collaborator_ids: [], work_status: 'pending', target: {} });
  mock.members[1].run_state = 'idle';
  mock.records.length = 0;
  let fee = '0.01';
  await page.route('**/pi-fees', route => route.fulfill({ json: { enabled: true, agents: { 'member-1': { estimated_usd: fee, unknown_calls: 0 } }, solver: { estimated_usd: fee, unknown_calls: 0 }, progress: { estimated_usd: '0', unknown_calls: 0 }, review: { estimated_usd: '0', unknown_calls: 0 } } }));
  await page.route('**/ctf/challenges/*/dispatch', async route => {
    expect(route.request().postDataJSON()).toMatchObject({ action: 'ready', worker_id: 'member-1' });
    Object.assign(challenge, { owner_id: 'member-1', work_status: 'in_progress', dispatch_status: 'assigned', revision: 4 });
    mock.members[1].run_state = 'running';
    mock.records.push({ id: 'fact:F1', knowledge_id: 'F1', challenge_id: challenge.id, kind: 'fact', body: 'Worker 已验证本地命令', author_id: 'member-1', created_version: 13, artifact_refs: [] });
    await route.fulfill({ json: { challenge, dispatch_status: 'assigned' } });
  });
  await page.route('**/agents/*/session*', route => route.fulfill({ json: { revision: 3, session: {}, native_entries: [
    { type: 'message', id: 'assistant', message: { role: 'assistant', content: [
      { type: 'thinking', thinking: 'Inspect the local workspace' },
      { type: 'text', text: 'Running the local verification' },
      { type: 'toolCall', id: 'tool', name: 'exec_command', arguments: { command: 'pwd' } },
    ] } },
    { type: 'message', id: 'result', message: { role: 'toolResult', toolCallId: 'tool', isError: false, content: [{ type: 'text', text: '/workspace/agents/member-1' }] } },
  ] } }));
  await page.goto(`/tasks/${CTF_ID}`);
  await expect(page.getByText(/估算.*0.01/)).toBeVisible();
  await page.getByRole('button', { name: `任务：${challenge.title}`, exact: true }).click();
  await page.getByRole('combobox', { name: '领取任务的 Worker' }).click();
  await page.getByRole('option', { name: 'Web 分析员 · 空闲', exact: true }).click();
  await page.getByRole('button', { name: '分配任务', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Agent：Web 分析员', exact: true })).toContainText(challenge.title);
  await page.getByRole('button', { name: 'Agent：Web 分析员', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Web 分析员 会话' });
  await expect(panel.getByRole('navigation', { name: '正在处理的任务' })).toContainText(challenge.title);
  await panel.getByRole('button', { name: '思考 · 模型返回', exact: true }).click();
  await expect(panel).toContainText('Inspect the local workspace');
  await panel.getByRole('button', { name: 'exec_command 已完成' }).click();
  await expect(panel).toContainText('/workspace/agents/member-1');
  fee = '0.07';
  await expect(page.getByText(/估算.*0.07/)).toBeVisible();
  await panel.getByRole('navigation', { name: '正在处理的任务' }).getByRole('button').click();
  await expect(page.getByRole('complementary', { name: /^题目详情/ })).toContainText('Worker 已验证本地命令');
  mock.finish();
  await openCtfMenu(page);
  await page.getByRole('menuitem', { name: '刷新', exact: true }).click();
  await expect(page.getByRole('complementary', { name: /^题目详情/ })).toContainText('已中断');
  await expect(page.getByRole('complementary', { name: /^题目详情/ })).not.toContainText('解题仍在进行');
  expect(mock.unexpected).toEqual([]);
});
