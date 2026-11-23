import { test, expect, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Home "N work items are waiting for you" -> Review lands on every item counted.
//
// The notice counts the items waiting for the reader over every segment
// (Mine, Company, Shared). Review must open Tasks > Work Items on the
// "Waiting for me" view with the All scope named in the URL: without it the
// list landed on Mine and hid the company-managed items the notice counted.
//
// The count is a fixture; nothing is written.
// ---------------------------------------------------------------------------

const WAITING_COUNT_PATH = '/api/v1/work-items/waiting-for-me';

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('Review on the Home notice opens Waiting for me on the All scope', async ({ page }) => {
  await page.route(
    (url: URL) => url.pathname === WAITING_COUNT_PATH,
    (route: Route) =>
      route.fulfill({
        json: {
          count: 9,
          oldest: { id: '00000000-0000-4000-8000-000000009001', title: 'Pick a tier', reason: 'pick a pricing tier', since: '2026-11-23T08:00:00Z' },
        },
      }),
  );
  await page.goto('/');

  // The notice itself and its Review button open the same list.
  const notice = page.getByText('9 work items are waiting for you');
  await expect(notice).toBeVisible({ timeout: 15_000 });
  await notice.click();

  await expect(page).toHaveURL(/\/tasks\/work-items\?/);
  const url = new URL(page.url());
  expect(url.searchParams.get('view')).toBe('waiting');
  expect(url.searchParams.get('vis')).toBe('all');
});
