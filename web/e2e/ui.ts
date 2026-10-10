import { expect, type Locator, type Page } from '@playwright/test';

/** Select options live in a portal, outside the form or inspector that owns the field. */
export async function selectOption(page: Page, field: Locator, label: string | RegExp) {
  await field.click();
  const option = page.getByRole('option', { name: label, exact: typeof label === 'string' });
  await option.click();
  await expect(option).toBeHidden();
}

/** Verify the selected option rather than the empty search input of an AntD Select. */
export async function expectSelectedOption(page: Page, field: Locator, label: string | RegExp) {
  await field.click();
  await expect(page.getByRole('option', { name: label, exact: typeof label === 'string' }))
    .toHaveAttribute('aria-selected', 'true');
  await field.press('Escape');
}

export async function expectOption(page: Page, field: Locator, label: string | RegExp) {
  await field.click();
  await expect(page.getByRole('option', { name: label, exact: typeof label === 'string' })).toHaveCount(1);
  await field.press('Escape');
}

export async function selectMode(page: Page, label: string) {
  await page.getByText(label, { exact: true }).click();
  await expect(page.getByRole('radio', { name: label, exact: true })).toBeChecked();
}

export async function expandSection(control: Locator) {
  if (await control.getAttribute('aria-expanded') !== 'true') await control.click();
  await expect(control).toHaveAttribute('aria-expanded', 'true');
}

export async function taskAction(page: Page, name: string) {
  await page.getByRole('button', { name: '更多任务操作', exact: true }).click();
  const item = page.getByRole('menuitem', { name, exact: true });
  await item.click();
  await expect(item).toBeHidden();
}

export async function closeRecords(page: Page) {
  const dialog = page.getByRole('dialog', { name: '复盘记录', exact: true });
  await settleModal(dialog);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
}

export async function settleModal(dialog: Locator) {
  await expect.poll(() => dialog.evaluate((element) => element.getAnimations({ subtree: true })
    .some((animation) => animation.playState === 'running'))).toBe(false);
}

export async function conversationAction(page: Page, scope: Locator, name: string) {
  await scope.getByRole('button', { name: '更多会话操作', exact: true }).click();
  const item = page.getByRole('menuitem', { name, exact: true });
  await item.click();
  await expect(item).toBeHidden();
}
