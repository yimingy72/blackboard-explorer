import { expect, test, type Page } from '@playwright/test';
import { installMockApi, TASK_ID } from './fixtures';

const model = { model_id: 'fixture-model', model_revision: 1 };

async function mockModel(page: Page) {
  await page.route('**/api/platform/pi-models', route => route.fulfill({
    json: { enabled: true, can_manage: false, models: [{
      id: model.model_id, model_revision: model.model_revision, label: '合成模型', enabled: true, has_secret: true,
      context_window: 8192, max_output_tokens: 1024, input_modalities: ['text'],
      tool_call: true, reasoning: true, usd_rates: { input: '1', output: '2', cache_read: '1', cache_write: '1' },
    }] },
  }));
}

test('同一个创建入口按模式提交，结果不明时重试原输入', async ({ page }) => {
  const mock = await installMockApi(page);
  await mockModel(page);
  const native = {
    id: 'isolated-ctf', revision: 3, publication_stamp: 'a'.repeat(64),
    label: '隔离 CTF', external_mcp: true,
  };
  const sourceId = '22222222-2222-4222-8222-222222222222';
  const privateUploads: string[] = [];
  const privateDeletes: string[] = [];
  const requests: Record<string, unknown>[] = [];
  let available = false;
  let inputsEnabled = false;
  await page.route('**/api/task-creation-capabilities', (route) =>
    route.fulfill({ json: { mode: 'legacy', blackboard: { available: true }, native_ctf: available ? { ...native, ...(inputsEnabled && {
      shared_inputs: { profile: 'shared-inputs-v1', max_file_bytes: 16 * 1024 * 1024, max_total_bytes: 24 * 1024 * 1024, max_files: 20 },
      vpn: { profile: 'openvpn-task-ipv4-tcp-v1', id: 'task-vpn', label: '共享 VPN' },
    }) } : null } }));
  await page.route(/\/api\/task-input-groups\/[^/]+\/private-vpn-source(?:\?|$)/, route => {
    if (route.request().method() === 'DELETE') {
      privateDeletes.push(new URL(route.request().url()).searchParams.get('source_id') ?? '');
      return privateDeletes.length === 1
        ? route.fulfill({ status: 503, json: { detail: '无法确认删除' } })
        : route.fulfill({ json: { deleted: true } });
    }
    expect(route.request().method()).toBe('PUT');
    privateUploads.push(route.request().postData() ?? '');
    return route.fulfill({ json: { source_id: sourceId } });
  });
  await page.route('**/api/tasks', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    requests.push(route.request().postDataJSON());
    if (requests.length === 2) {
      available = false;
      return route.fulfill({ status: 409, json: { detail: 'Publication changed' } });
    }
    if (requests.length === 3) return route.fulfill({ status: 503, json: { detail: 'Result unavailable' } });
    return route.fulfill({ json: { id: TASK_ID, agent_profile: 'ctf-task-settings', agent_profile_version: 3 } });
  });

  await page.goto('/tasks/new');
  const ctf = page.getByText('CTF 模式', { exact: true });
  const blackboard = page.getByText('黑板模式', { exact: true });
  const create = page.getByRole('button', { name: '创建任务', exact: true });
  await ctf.click();
  await expect(page.getByText('当前没有已发布的原生 CTF 环境')).toBeVisible();
  await expect(create).toHaveCount(0);
  await blackboard.click();
  await page.getByLabel('任务目标').fill('整理合成材料');
  await page.getByLabel('验收条件 1', { exact: true }).fill('发布一条事实');
  await page.getByLabel('领域背景').fill('合成黑板背景');
  await page.getByLabel('金额阈值（USD）').fill('1');
  await create.click();
  await expect(page).toHaveURL(`/tasks/${TASK_ID}`);
  expect(requests[0]).toMatchObject({ mode: 'blackboard', pi_model: model, domain_context: '合成黑板背景', acceptance: [{ id: 'A1', desc: '发布一条事实' }] });
  expect(requests[0]).not.toHaveProperty('native_environment');
  expect(requests[0]).not.toHaveProperty('public_input_ids');
  expect(requests[0]).not.toHaveProperty('reasoning_effort');

  available = true;
  await page.goto('/tasks/new');
  await ctf.click();
  await expect(page.getByText(native.label, { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('领域背景')).toHaveCount(0);
  await expect(page.getByText(/外部 MCP 工具|私有 VPN 配置/)).toHaveCount(0);
  await expect(page.getByText('此环境暂不支持初始附件。')).toBeVisible();
  await expect(page.locator('input[type=file]')).toHaveCount(0);
  inputsEnabled = true;
  await page.reload();
  await ctf.click();
  await expect(page.getByText(/附件对任务团队可见/)).toBeVisible();
  const picker = page.locator('input[type=file]');
  await picker.setInputFiles({ name: 'too-large.bin', mimeType: 'application/octet-stream', buffer: Buffer.alloc(16 * 1024 * 1024 + 1) });
  await expect(page.getByRole('alert').filter({ hasText: /每个文件最多 16 MiB/ })).toBeVisible();
  const sharedFile = { name: 'shared.bin', mimeType: 'application/octet-stream', buffer: Buffer.from([0, 255, 10]) };
  const privateFile = { name: 'private.OVPN', mimeType: 'application/octet-stream', buffer: Buffer.from('synthetic private bytes') };
  await picker.setInputFiles([sharedFile, privateFile]);
  await expect(page.getByText(/已上传/)).toHaveCount(2);
  await expect(page.getByRole('radio', { name: '黑板模式' })).toBeDisabled();
  await picker.setInputFiles({ ...privateFile, name: 'extra.ovpn' });
  await expect(page.getByRole('alert').filter({ hasText: /只可添加一个私有/ })).toBeVisible();
  await page.getByRole('button', { name: '移除附件 private.OVPN' }).click();
  await expect(page.getByText('无法确认删除')).toBeVisible();
  await expect(create).toBeDisabled();
  await page.getByRole('button', { name: '移除附件 private.OVPN' }).click();
  await expect(page.getByText('private.OVPN')).toHaveCount(0);
  await page.getByLabel('任务目标').fill('查看合成材料');
  await page.getByLabel('金额阈值（USD）').fill('1');
  await create.click();
  await expect(page.getByText('当前没有已发布的原生 CTF 环境')).toBeVisible();
  await page.getByRole('button', { name: '移除附件', exact: true }).click();
  await blackboard.click();
  await expect(page.getByLabel('验收条件 1', { exact: true })).toBeVisible();
  await ctf.click();
  available = true;
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await picker.setInputFiles([sharedFile, privateFile]);
  await expect(page.getByText(/已上传/)).toHaveCount(2);
  await page.getByLabel('思考强度').click();
  await page.getByRole('option', { name: '高', exact: true }).click();
  await create.click();
  await expect(page.getByText(/创建结果未确认/)).toBeVisible();
  await expect(page.getByLabel('任务目标')).toBeDisabled();
  await expect(page.getByLabel('思考强度')).toBeDisabled();
  await expect(page.getByRole('radio', { name: '黑板模式' })).toBeDisabled();
  await create.click();
  await expect(page).toHaveURL(`/tasks/${TASK_ID}`);

  expect(requests).toHaveLength(4);
  expect(requests[1]).not.toHaveProperty('native_vpn_source_id');
  expect(requests[1]).not.toHaveProperty('reasoning_effort');
  expect(requests[1].input_group_id).not.toEqual(requests[2].input_group_id);
  expect(requests[2]).toEqual(requests[3]);
  expect(requests[2]).toMatchObject({
    mode: 'ctf', pi_model: model,
    native_environment: { id: native.id, revision: native.revision, publication_stamp: native.publication_stamp },
    input_group_id: expect.any(String), input_file_ids: [expect.any(String)], egress_allowlist: [],
    native_vpn_source_id: sourceId, reasoning_effort: 'high',
  });
  const saved = requests[2];
  expect(saved.public_input_ids).toEqual(saved.input_file_ids);
  for (const key of ['acceptance', 'domain_context', 'native_mcp']) {
    expect(saved).not.toHaveProperty(key);
  }
  expect(privateUploads).toEqual(['synthetic private bytes', 'synthetic private bytes']);
  expect(privateDeletes).toEqual([sourceId, sourceId]);
  expect(mock.unexpected).toEqual([]);
});
