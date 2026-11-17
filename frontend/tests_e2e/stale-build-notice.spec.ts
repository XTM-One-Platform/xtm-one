import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// A tab running the previous build (#4395): once `/auth/config` reports
// another `build_id` than the one the page booted (its index.html's
// `<meta name="build-id">`), coming back to the tab offers a reload, once per
// server build.
//
// The backend writes that meta into the index.html it serves (CI's production
// build) and the Vite dev server writes none, so the document's own meta, if
// any, is replaced here by the build the test says the page runs.
// ---------------------------------------------------------------------------

const PREVIOUS = 'e2e-build-previous';
const NEXT = 'e2e-build-next';
const LATER = 'e2e-build-later';

/** Past `/auth/config`'s `staleTime`, so coming back refetches it. */
const AWAY = '01:01';

/** An absence has no event to wait for: leave the page the time to raise a toast. */
const SETTLE_MS = 1_000;

/** The toast viewport: the only `<ol tabindex="-1">` on the page. */
const notices = (page: Page) =>
  page.locator('ol[tabindex="-1"] > li').filter({ hasText: 'A new version is available' });

/** Serve the page as build `builds.page` and report `builds.server` on `/auth/config`. */
async function serveBuilds(page: Page) {
  const builds = { page: PREVIOUS, server: PREVIOUS };
  await page.route(
    (url) => url.pathname === '/',
    async (route) => {
      if (route.request().resourceType() !== 'document') return route.fallback();
      const response = await route.fetch();
      const html = (await response.text())
        .replace(/\s*<meta\s+name="build-id"[^>]*>/gi, '')
        .replace(/<head>/i, `<head>\n    <meta name="build-id" content="${builds.page}" />`);
      await route.fulfill({ response, body: html });
    },
  );
  await page.route('**/api/v1/auth/config', async (route) => {
    const response = await route.fetch();
    const config = await response.json();
    await route.fulfill({ response, json: { ...config, build_id: builds.server } });
  });
  return builds;
}

/** Leave the tab long enough for its config to go stale, then come back to it. */
async function returnToTab(page: Page): Promise<void> {
  const refetched = page.waitForResponse((res) => new URL(res.url()).pathname === '/api/v1/auth/config');
  await page.clock.fastForward(AWAY);
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange', { bubbles: true })));
  await refetched;
}

test.describe('Stale build notice', () => {
  // The installed app's service worker (`public/sw.js`) answers page navigations
  // itself, out of `page.route`'s reach: the reload would get the real index.html.
  test.use({ serviceWorkers: 'block' });

  test('a tab on the previous build is offered a reload on focus, once per server build', async ({ page }) => {
    const builds = await serveBuilds(page);
    await page.clock.install();
    await page.goto('/');
    await expect(page.locator('meta[name="build-id"]')).toHaveAttribute('content', PREVIOUS);
    await expect(page.getByRole('link', { name: 'Home', exact: true })).toBeVisible();

    // The server serves the build the page runs: nothing to offer.
    await returnToTab(page);
    await page.waitForTimeout(SETTLE_MS);
    await expect(notices(page)).toHaveCount(0);

    // An upgrade: the next focus offers the reload.
    builds.server = NEXT;
    await returnToTab(page);
    await expect(notices(page)).toHaveCount(1);
    await expect(notices(page)).toContainText('This tab still runs the previous version.');
    await expect(notices(page).getByRole('button', { name: 'Reload' })).toBeVisible();

    // Closed, it is not offered again for the same build.
    await notices(page).getByRole('button', { name: 'Close notification' }).click();
    await expect(notices(page)).toHaveCount(0);
    await returnToTab(page);
    await page.waitForTimeout(SETTLE_MS);
    await expect(notices(page)).toHaveCount(0);

    // A later build is offered anew; Reload loads it, and nothing is left to offer.
    builds.server = LATER;
    await returnToTab(page);
    await expect(notices(page)).toHaveCount(1);
    builds.page = LATER;
    await Promise.all([
      page.waitForEvent('load'),
      notices(page).getByRole('button', { name: 'Reload' }).click(),
    ]);
    await expect(page.locator('meta[name="build-id"]')).toHaveAttribute('content', LATER);
    await expect(page.getByRole('link', { name: 'Home', exact: true })).toBeVisible();
    await page.waitForTimeout(SETTLE_MS);
    await expect(notices(page)).toHaveCount(0);
  });
});
