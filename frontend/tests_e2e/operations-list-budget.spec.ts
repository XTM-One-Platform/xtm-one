import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Header and filter row budget of the Operations list (#10215, header and
// hero density sweep, criteria 2 and 6).
//
// At 1440 px the list page header, from the top of its title row to the top
// of the tab row under it, is at most 96 px: the page's help sentence lives in
// the title's "i", never in a paragraph under the h1. Each Operations list
// draws ONE filter row (search and visibility on the row, every other filter
// in the Filters pill), so the row is one control tall at 1440 and at 1024.
// Nothing is written: the lists are read as the deployment holds them.
// ---------------------------------------------------------------------------

const LIST_HEADER_BUDGET_PX = 96;
// One toolbar control (32 px) plus the focus ring room; a second row would
// be at least twice that.
const ONE_ROW_PX = 40;

const LISTS = [
  // On the shared DataTable (#9378, #11894) the Executions filter row is the toolbar lead that
  // holds the Status pill (the right group may take a line of its own, so the toolbar is not measured).
  { tab: 'executions', path: '/operations/executions', row: 'executions-status-lead' },
  { tab: 'background-tasks', path: '/operations/background-tasks', row: 'background-tasks-filter-row' },
  // Work items and Shared state carry their Status filter in the toolbar lead: the lead is
  // the filter row, and the DataTable moves its right group (total, page size, view) to a
  // line of its own when that lead crowds it (leadCrowds), so the toolbar is not measured.
  // Work items moved to the Tasks page (#775): the same list, measured on its new route.
  { tab: 'work-items', path: '/tasks/work-items', row: 'work-items-filter-row' },
  { tab: 'shared-state', path: '/operations/shared-state', row: 'shared-state-filter-row' },
  // On the shared DataTable (slice 8b, #9378) the Worksets filter row is the table's toolbar.
  { tab: 'worksets', path: '/operations/worksets', row: 'data-table-toolbar' },
] as const;

async function listHeaderHeight(page: Page): Promise<number> {
  const header = page.locator('[data-list-page-header]').first();
  await expect(header).toBeVisible({ timeout: 15_000 });
  return header.evaluate((el) => {
    const next = el.nextElementSibling as HTMLElement | null;
    const top = el.getBoundingClientRect().top;
    const bottom = next ? next.getBoundingClientRect().top : el.getBoundingClientRect().bottom;
    return Math.round(bottom - top);
  });
}

async function filterRowHeight(page: Page, testId: string): Promise<number> {
  const row =
    testId === 'executions-status-lead'
      ? page
          .getByTestId('data-table-toolbar')
          .locator(':scope > div', { has: page.getByRole('button', { name: 'Status', exact: true }) })
      : page.getByTestId(testId);
  await expect(row).toBeVisible({ timeout: 15_000 });
  return row.evaluate((el) => Math.round(el.getBoundingClientRect().height));
}

test.describe('Operations list: header and filter row budget', () => {
  test('the Operations header stays within the list budget at 1440 px', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/operations/executions');
    // The help sentence is the title's "i", not a paragraph under the h1.
    await expect(page.getByRole('img', { name: 'About operations' })).toBeVisible({ timeout: 15_000 });
    expect(await listHeaderHeight(page)).toBeLessThanOrEqual(LIST_HEADER_BUDGET_PX);
  });

  for (const width of [1440, 1024]) {
    for (const { tab, path, row } of LISTS) {
      test(`the ${tab} list draws one filter row at ${width} px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await page.goto(path);
        expect(await filterRowHeight(page, row)).toBeLessThanOrEqual(ONE_ROW_PX);
      });
    }
  }
});
