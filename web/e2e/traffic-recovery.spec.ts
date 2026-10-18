import { expect, test, type Page } from '@playwright/test';
import { installMockApi, TASK_ID } from './fixtures';
import { expectSelectedOption } from './ui';

async function installTraffic(page: Page, options: { failBuild?: boolean; expireFrame?: boolean; failCheck?: boolean } = {}) {
  const mock = await installMockApi(page);
  let opened = 0, expired = false, checks = 0;
  const columns = (number: number) => [String(number), String(1792285200 + number), '192.0.2.1', '192.0.2.2', 'TCP', '4', 'Synthetic packet'];
  const nativePacket = (number: number) => ({ source_id: 'source-a', source_number: number, agent_id: 'agent-1', attribution: 'exact', columns: columns(number) });
  const packet = { id: 'r-1', seq: 1, timestamp: '2026-10-18T01:00:01Z', agent_id: 'agent-1', attribution: 'exact', record_kind: 'packet', protocol: 'tcp', source_ip: '192.0.2.1', destination_ip: '192.0.2.2', summary: 'Synthetic packet', plaintext: 'encrypted', packet: { source_id: 'source-a', number: 1, frame_length: 4, captured_length: 4, info: 'Synthetic packet' } };
  const frame = (number: number) => ({ source_id: 'source-a', source_number: number, revision: 'r1', agent_id: 'agent-1', columns: columns(number), data_sources: [
    { id: 0, name: 'Frame', length: 4, raw_hex: '00010203', truncated: false },
    { id: 1, name: 'Decrypted TLS', length: 4, raw_hex: '41424344', truncated: false },
  ], layers: [{ name: 'tcp', label: 'TCP', data_source_id: 0, children: [{ name: 'tcp.port', label: 'Synthetic port', offset: 1, length: 1, data_source_id: 1, filter_expr: 'tcp.port == 443' }] }], tree_truncated: false });
  const view = (building = false) => ({ id: options.expireFrame && opened > 1 ? 'query-2' : 'query-1', status: building ? 'building' : options.failBuild && opened === 1 ? 'error' : 'ready', revision: building || options.failBuild && opened === 1 ? null : 'r1', requested_revision: 'r1', matched_packets: 2, coverage: [], results_truncated: false, engine_version: 'fixture', error: !building && options.failBuild && opened === 1 ? 'Synthetic decoder failed' : null });
  await page.route(`**/api/tasks/${TASK_ID}/traffic**`, route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/native/check')) {
      checks++;
      if (options.failCheck && checks === 1) return route.fulfill({ status: 503, json: { detail: '解析器忙碌' } });
      return route.fulfill({ json: { valid: true } });
    }
    if (path.endsWith('/native/queries') && route.request().method() === 'POST') { opened++; return route.fulfill({ status: 202, json: view(true) }); }
    if (path.includes('/frames/')) {
      if (options.expireFrame && !expired) { expired = true; return route.fulfill({ status: 404, json: { detail: 'Query expired' } }); }
      return route.fulfill({ json: frame(Number(path.split('/').at(-1))) });
    }
    if (path.endsWith('/packets')) return route.fulfill({ json: { revision: 'r1', records: [nativePacket(1), nativePacket(2)], total: 2 } });
    if (path.includes('/native/queries/')) return route.fulfill({ json: view() });
    if (path.includes('/records/')) return route.fulfill({ json: { ...packet, packet_detail: { layers: [], raw_hex: '00010203' } } });
    return route.fulfill({ json: { task_id: TASK_ID, run: 1, availability: 'recorded', capture: { status: 'partial', error: 'Capture incomplete', record_count: 1, captured_bytes: 4, preview_dropped: 61, dropped_packets: 0, finished_at: packet.timestamp }, records: [packet], files: [], has_more: false, snapshot_seq: 1 } });
  });
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByRole('button', { name: '流量', exact: true }).click();
  return { drawer: page.getByRole('dialog', { name: '任务流量' }), mock, opens: () => opened, checks: () => checks };
}

test('过滤器校验失败可原地重试，校验完成前不启动分析', async ({ page }) => {
  const { drawer, opens, checks } = await installTraffic(page, { failCheck: true });
  await drawer.getByLabel('Wireshark 显示过滤器').fill('tcp');
  const retry = drawer.getByRole('button', { name: '重试校验', exact: true });
  await expect(retry).toBeVisible();
  expect(opens()).toBe(0);
  await retry.click();
  const apply = drawer.getByRole('button', { name: '应用', exact: true });
  await expect(apply).toBeEnabled();
  await apply.click();
  await expect(drawer.getByRole('button', { name: '查看流量 p:source-a:2' })).toBeVisible();
  expect(checks()).toBe(2);
});

test('终态原生解析失败可显式重试，同一查询恢复后展示结果', async ({ page }) => {
  const { drawer, opens, mock } = await installTraffic(page, { failBuild: true });
  await drawer.getByLabel('Wireshark 显示过滤器').fill('tcp');
  await drawer.getByRole('button', { name: '应用', exact: true }).click();
  await expect(drawer.getByText('原生筛选失败', { exact: true })).toBeVisible();
  expect(opens()).toBe(1);
  await drawer.getByRole('button', { name: '重试', exact: true }).click();
  await expect(drawer.getByRole('button', { name: '查看流量 p:source-a:2' })).toBeVisible();
  await expect(drawer.getByText('原生筛选失败', { exact: true })).toHaveCount(0);
  expect(opens()).toBe(2);
  expect(mock.unexpected).toEqual([]);
});

test('历史包详情的查询淘汰后自动重新打开', async ({ page }) => {
  const { drawer, opens } = await installTraffic(page, { expireFrame: true });
  await drawer.getByRole('button', { name: '查看流量 p:source-a:1' }).click();
  await expect(drawer.getByRole('region', { name: '协议层详情' }).getByText('Wireshark', { exact: true })).toBeVisible();
  expect(opens()).toBe(2);
  await expect(drawer.getByText('Query expired', { exact: true })).toHaveCount(0);
});

test('相同解析版本切包后清空字段及解密数据源选择', async ({ page }) => {
  const { drawer } = await installTraffic(page);
  await drawer.getByLabel('Wireshark 显示过滤器').fill('tcp');
  await drawer.getByRole('button', { name: '应用', exact: true }).click();
  await drawer.getByRole('button', { name: '查看流量 p:source-a:1' }).click();
  await drawer.getByText('Synthetic port', { exact: true }).click();
  await expect(drawer.locator('mark').first()).toBeVisible();
  await expectSelectedOption(page, drawer.getByRole('combobox', { name: '字节数据源' }), 'Decrypted TLS');
  await drawer.getByRole('button', { name: '查看流量 p:source-a:2' }).click();
  await expect(drawer.locator('mark')).toHaveCount(0);
  await expect(drawer.getByRole('button', { name: '使用选中字段过滤' })).toBeDisabled();
  await expectSelectedOption(page, drawer.getByRole('combobox', { name: '字节数据源' }), 'Frame');
});
