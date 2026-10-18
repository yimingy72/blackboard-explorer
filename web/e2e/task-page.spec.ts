import { expect, test } from '@playwright/test';
import { installMockApi } from './fixtures';
import { taskListRoute, taskListResponse } from './task-list-fixture';

test('server pages can find tasks beyond the old 100 row limit', async ({ page }) => {
  const mock=await installMockApi(page);
  const requests: string[]=[];
  const items=Array.from({length:301},(_,index)=>({ ...mock.taskFixture,id:`33333333-3333-4333-8333-${String(index).padStart(12,'0')}`,name:index===300?'较早的归档任务':`分析 ${index}`,goal:`输入 ${index}` }));
  await page.route(taskListRoute, route=>{requests.push(route.request().url());return route.fulfill({json:taskListResponse(items,route.request().url())});});
  await page.goto('/tasks');
  await expect(page.getByRole('list',{name:'任务列表',exact:true}).getByRole('listitem')).toHaveCount(20);
  await page.getByLabel('查找任务').fill('较早的归档任务');
  await expect(page.getByRole('link',{name:'较早的归档任务',exact:true})).toBeVisible();
  expect(requests.every(url=>new URL(url).pathname==='/api/tasks/page')).toBe(true);
  expect(requests.some(url=>new URL(url).searchParams.get('q')==='较早的归档任务')).toBe(true);
  expect(requests.every(url=>new URL(url).searchParams.get('limit')==='20')).toBe(true);
});

test('removing the last task of a page returns to the existing previous page',async({page})=>{
  const mock=await installMockApi(page);
  const items=Array.from({length:21},(_,index)=>({...mock.taskFixture,id:`33333333-3333-4333-8333-${String(index).padStart(12,'0')}`,name:`清理 ${index}`}));
  await page.route(taskListRoute,route=>route.fulfill({json:taskListResponse(items,route.request().url())}));
  await page.route('**/api/tasks/33333333-3333-4333-8333-000000000020',route=>{if(route.request().method()!=='DELETE')return route.fulfill({json:items.at(-1)});items.pop();return route.fulfill({json:{deleting:true}});});
  await page.goto('/tasks?page=2');
  await page.getByRole('button',{name:'任务操作：清理 20'}).click();
  await page.getByRole('menuitem',{name:'删除任务'}).click();
  await page.getByRole('button',{name:'确认删除'}).click();
  await expect(page).toHaveURL(/\/tasks$/);
  await expect(page.getByRole('list',{name:'任务列表',exact:true}).getByRole('listitem')).toHaveCount(20);
});
