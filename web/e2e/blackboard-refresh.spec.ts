import { expect, test } from '@playwright/test';
import { TASK_ID, installMockApi, pushMockBoardEvent } from './fixtures';
import { installConversationFixture } from './conversation-fixture';

test('拖动坐标刷新保留，持续进度不丢自动布局恢复请求',async({page})=>{
  const mock=await installMockApi(page);
  await page.setViewportSize({width:1440,height:900});
  await page.goto(`/tasks/${TASK_ID}`);
  const node=page.locator('.react-flow__node[data-id="F1"]');
  await expect(node).toBeVisible();
  const before=await node.getAttribute('style'), box=(await node.boundingBox())!;
  await page.mouse.move(box.x+box.width/2,box.y+box.height/2);
  await page.mouse.down();await page.mouse.move(box.x+box.width/2+110,box.y+box.height/2+50,{steps:10});await page.mouse.up();
  await expect(node).not.toHaveAttribute('style',before!);
  await expect(page.getByRole('button',{name:'恢复 F1 自动布局',exact:true})).toBeVisible();
  const key=`bbx:blackboard:${TASK_ID}`;
  const saved=await page.evaluate(key=>JSON.parse(localStorage.getItem(key)!).positions.F1,key);
  await page.reload();await expect(node).toBeVisible();
  await expect.poll(()=>page.evaluate(key=>JSON.parse(localStorage.getItem(key)!).positions.F1,key)).toEqual(saved);
  await page.getByRole('button',{name:'恢复 F1 自动布局',exact:true}).click();
  for(let index=0;index<10;index++){
    await pushMockBoardEvent(page,{task_id:TASK_ID,version:20+index,type:'budget.updated',actor:'system',object_id:null,addressed_to:null,created_at:new Date().toISOString(),payload:{usage:{cost:.02}}});
    await page.waitForTimeout(40);
  }
  await expect.poll(()=>page.evaluate(key=>JSON.parse(localStorage.getItem(key)!).positions.F1,key)).not.toEqual(saved);
  expect(mock.unexpected).toEqual([]);
});

for(const width of [1440,390]) test(`普通黑板 ${width}px 明亮画布与紧凑会话保持草稿和真实推理`,async({page})=>{
  const mock=await installConversationFixture(page,{running:true});
  await page.setViewportSize({width,height:900});await page.goto(`/tasks/${TASK_ID}`);
  const canvas=page.getByRole('region',{name:'黑板关系图',exact:true}).last();
  await expect(canvas).toBeVisible();
  await expect(page.getByRole('complementary',{name:'对象详情'})).toHaveCount(0);
  const agent=page.getByRole('group',{name:'全部 Agent'}).getByRole('button',{name:/Agent 1/});
  await agent.click();
  const chat=page.getByRole('complementary',{name:'Agent 1 对话记录',exact:true});
  await chat.getByRole('textbox').fill('草稿：保留已核对依据');
  await expect(chat.getByRole('button',{name:/思考/}).first()).toBeVisible();
  await page.screenshot({path:`../.data/blackboard-refresh/conversation-${width}.png`,fullPage:true});
  await chat.getByRole('button',{name:'关闭对话',exact:true}).click();await agent.click();
  await expect(chat.getByRole('textbox')).toHaveValue('草稿：保留已核对依据');
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)).toBeLessThanOrEqual(1);
  expect(mock.unexpected).toEqual([]);
});

test('增量事件会显示下一轮Derive，历史打开不回放通信',async({page})=>{
  const mock=await installMockApi(page);
  mock.events.push({...mock.events[2],version:15,type:'agent.spawned',payload:{id:'agent-2',task_type:'derive'},object_id:'agent-2'});
  mock.events.push({...mock.events[2],version:16,type:'agent.finished',payload:{agent_id:'agent-2',end_reason:'normal'},object_id:'agent-2'});
  await page.goto(`/tasks/${TASK_ID}`);
  const button=page.getByRole('group',{name:'全部 Agent'}).getByRole('button',{name:/Agent 2/});
  await expect(button).toContainText('已结束');
  await expect(page.locator('[data-blackboard-pulse]')).toHaveCount(0);
  await pushMockBoardEvent(page,{...mock.events[2],version:17,type:'agent.reactivated',payload:{id:'agent-2',derive_parallel:true},object_id:'agent-2',created_at:new Date().toISOString()});
  await expect(button).toContainText('运行中');
  await button.click();await expect(page.getByRole('complementary',{name:'Agent 2 对话记录'})).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});
