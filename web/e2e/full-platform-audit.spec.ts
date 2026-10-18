import { expect, test, type Page } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import { TASK_ID } from './fixtures';
import { CTF_ID, installCtfApi } from './ctf-fixtures';
import { installConversationFixture } from './conversation-fixture';
import { taskAction } from './ui';

const directory = process.env.BBX_AUDIT_DIR ?? '../.data/full-platform-refinement/after';

test('audit every route and principal panel at desktop and phone widths', async ({ page }) => {
  test.setTimeout(180000);
  await mkdir(directory, { recursive: true });
  await page.clock.setFixedTime(new Date('2026-09-24T08:10:00Z'));
  const results: Array<Record<string, unknown>> = [];
  const capture = async (name: string, page: Page) => {
    await page.screenshot({ path: `${directory}/${name}.png`, fullPage: true, animations: 'disabled' });
    const measurements = await page.evaluate(() => ({
      width: innerWidth, overflow: document.documentElement.scrollWidth - innerWidth,
      elements: document.querySelectorAll('*').length,
      headings: [...document.querySelectorAll('h1,h2')].filter(e => (e as HTMLElement).getClientRects().length).map(e => e.textContent),
    }));
    results.push({ name, ...measurements });
    expect(measurements.overflow, name).toBeLessThanOrEqual(1);
  };
  await installConversationFixture(page, { running: true });
  await page.route('**/api/settings/ctf/workers', route => route.fulfill({ json: { revision: 1, profile: {
    prompt_templates: { lead: '协调成员并保存可复核的进展。', teammate: '执行已分派的工作，及时共享证据。' },
    worker_tools: { lead: { builtin: ['list_members','send_message','finish_task'], mcp_servers: [] }, teammate: { builtin: ['list_members'], mcp_servers: [] } }, platform_tools: [],
  } } }));
  for (const width of [1440, 390]) {
    if (width === 390) await page.evaluate(() => localStorage.clear());
    await page.setViewportSize({ width, height: 960 });
    for (const [name, url, ready] of [
      ['login', '/login', '进入工作台'], ['tasks', '/tasks', '任务'],
      ['create', '/tasks/new', '创建任务'], ['workers', '/profiles', 'Agent 配置'],
      ['models', '/profiles?section=models', 'Agent 配置'], ['mcp', '/profiles?section=mcp', 'Agent 配置'],
      ['ctf-settings', '/profiles?section=ctf', 'Agent 配置'], ['report', `/tasks/${TASK_ID}/report`, '最终报告'],
    ]) {
      await page.goto(url);
      await expect(page.getByRole('heading', { level: 1 }).filter({ hasText: ready }).first()).toBeVisible();
      if (name === 'workers' || name === 'ctf-settings') await expect(page.getByLabel('完整系统提示词')).toBeVisible();
      if (name === 'models' || name === 'mcp') await expect(page.getByLabel('显示名称', { exact: true })).toBeVisible();
      if (name === 'create') await expect(page.getByLabel('任务目标')).toBeVisible();
      await capture(`${width}-${name}`, page);
    }
    await page.goto(`/tasks/${TASK_ID}`);
    await expect(page.locator('.react-flow__node[data-id="F1"]')).toBeVisible();
    await capture(`${width}-blackboard`, page);
    await page.getByRole('group', { name: '全部 Agent' }).getByRole('button', { name: /Agent 1/ }).click();
    await expect(page.getByRole('complementary', { name: 'Agent 1 对话记录' })).toBeVisible();
    await capture(`${width}-blackboard-chat`, page);
    await page.getByRole('button', { name: '关闭对话', exact: true }).click();
    await page.getByRole('button', { name: /事实 F1，/ }).click();
    await expect(page.getByRole('complementary', { name: '对象详情' })).toBeVisible();
    await capture(`${width}-fact`, page);
    await page.goto(`/tasks/${TASK_ID}`);
    await taskAction(page, '复盘记录');
    await page.getByRole('tab', { name: '工作区', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '复盘记录' })).toBeVisible();
    await capture(`${width}-workspace`, page);
  }
  await installCtfApi(page);
  for (const width of [1440,390]) {
    if (width === 390) await page.evaluate(() => localStorage.clear());
    await page.setViewportSize({ width, height: 960 });
    await page.goto(`/tasks/${CTF_ID}`);
    await expect(page.getByRole('button', { name: 'Agent：Lead' })).toBeVisible();
    await capture(`${width}-ctf`, page);
    await page.getByRole('button', { name: 'Agent：Lead' }).click();
    await expect(page.getByRole('complementary', { name: 'Lead 会话' })).toBeVisible();
    await capture(`${width}-ctf-chat`, page);
    await page.goto(`/tasks/${CTF_ID}`);
    await page.getByRole('button', { name: '任务：模拟 Web 题' }).click();
    const panel=page.getByRole('complementary', { name: /^题目详情：/ });
    await expect(panel).toBeVisible();
    for (const tab of ['概览','解题流程','文件']) {
      await panel.getByRole('tab', { name: new RegExp(tab) }).click();
      await capture(`${width}-challenge-${tab}`, page);
    }
  }
  await writeFile(`${directory}/audit.json`, JSON.stringify(results,null,2));
});

test('traffic packets, filtering, protocol bytes, and fallback states remain readable', async ({ page }) => {
  test.setTimeout(90000);
  const { installMockApi } = await import('./fixtures');
  await installMockApi(page);
  const packet={id:'packet-1',seq:1,timestamp:'2026-10-18T01:00:00Z',agent_id:'agent-1',attribution:'exact',record_kind:'packet',protocol:'tcp',source_ip:'192.0.2.10',source_port:42000,destination_ip:'192.0.2.20',destination_port:443,summary:'443 → 42000 [ACK]',request_bytes:0,response_bytes:0,packet:{source_id:'agent-1-wire',number:1,frame_length:64,captured_length:64,info:'443 → 42000 [ACK]',interface:'eth0'}};
  const columns=['1','1792285200','192.0.2.10','192.0.2.20','TCP','64','443 → 42000 [ACK]'];
  const frame={source_id:'agent-1-wire',source_number:1,revision:'r1',agent_id:'agent-1',columns,data_sources:[{id:0,name:'Frame · 原始帧',length:64,raw_hex:'000102030405060708090a0b0c0d0e0f',truncated:false}],layers:[{name:'tcp',label:'Transmission Control Protocol',data_source_id:0,offset:0,length:16,generated:false,children:[{name:'tcp.port',label:'Destination Port: 443',value:'443',data_source_id:0,offset:2,length:2,generated:false,filter_expr:'tcp.port == 443'}]}],tree_truncated:false};
  const analysis={id:'query-1',status:'complete',revision:'r1',matched_packets:1,coverage:[],engine_version:'fixture',results_truncated:false};
  await page.route(`**/api/tasks/${TASK_ID}/traffic**`,route=>{
    const url=new URL(route.request().url());
    if(url.pathname.endsWith('/native/check'))return route.fulfill({json:{valid:true,error:null,engine_version:'fixture'}});
    if(url.pathname.includes('/frames/'))return route.fulfill({json:frame});
    if(url.pathname.endsWith('/packets'))return route.fulfill({json:{records:[{source_id:'agent-1-wire',source_number:1,agent_id:'agent-1',attribution:'exact',columns}],total:1}});
    if(url.pathname.includes('/native/queries'))return route.fulfill({json:analysis});
    if(url.pathname.includes('/records/'))return route.fulfill({json:{...packet,packet_detail:{layers:frame.layers,raw_hex:frame.data_sources[0].raw_hex,raw_truncated:false}}});
    return route.fulfill({json:{task_id:TASK_ID,run:1,availability:'recorded',capture:{status:'complete',record_count:1,captured_bytes:64,finished_at:packet.timestamp},records:[packet],files:[],has_more:false,snapshot_seq:1}});
  });
  for(const width of [1440,390]){
    await page.setViewportSize({width,height:960});await page.goto(`/tasks/${TASK_ID}`);
    await page.getByRole('button',{name:'流量',exact:true}).click();
    const drawer=page.getByRole('dialog',{name:'任务流量'});
    await drawer.getByRole('button',{name:'查看流量 p:agent-1-wire:1'}).click();
    await expect(drawer.getByRole('region',{name:'协议层详情'})).toBeVisible();
    await expect(drawer.getByRole('region',{name:'数据包字节详情'})).toBeVisible();
    await drawer.getByText('Destination Port: 443',{exact:true}).click();
    await expect(drawer.locator('mark').first()).toBeVisible();
    await page.screenshot({path:`${directory}/${width}-traffic.png`,fullPage:true,animations:'disabled'});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)).toBeLessThanOrEqual(1);
    await drawer.getByRole('button',{name:'使用选中字段过滤'}).click();
    await expect(drawer.getByLabel('Wireshark 显示过滤器')).toHaveValue('tcp.port == 443');
  }
  await page.goto('/missing');
  await expect(page.getByText('页面不存在',{exact:true})).toBeVisible();
  await page.screenshot({path:`${directory}/404.png`,fullPage:true,animations:'disabled'});
  await page.route('**/api/tasks/page?*',route=>route.fulfill({json:{items:[],total:0,status_counts:{},limit:20,offset:0}}));
  await page.goto('/tasks');await expect(page.getByText('还没有任务',{exact:true})).toBeVisible();
  await page.screenshot({path:`${directory}/tasks-empty.png`,fullPage:true,animations:'disabled'});
});

test('finished tasks without captures stop idle traffic polling',async({page})=>{
  const {installMockApi}=await import('./fixtures');await installMockApi(page);
  let reads=0;
  await page.route(`**/api/tasks/${TASK_ID}/traffic?*`,route=>{reads++;return route.fulfill({json:{task_id:TASK_ID,run:1,availability:'not_recorded',capture:null,records:[],files:[],has_more:false,snapshot_seq:0}});});
  await page.goto(`/tasks/${TASK_ID}`);await page.getByRole('button',{name:'流量',exact:true}).click();
  await expect(page.getByText('本轮没有流量存档',{exact:true})).toBeVisible();
  const before=reads;expect(before).toBeGreaterThan(0);
  await page.clock.install();await page.clock.fastForward(15000);expect(reads).toBe(before);
});
