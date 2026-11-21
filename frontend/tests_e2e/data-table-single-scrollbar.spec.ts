import { test, expect, type Page } from '@playwright/test';
import { ok, withAdmin } from './helpers';

// ---------------------------------------------------------------------------
// One vertical scrollbar per list page (issue #11233, data-tables.md "One
// scrollbar"): the records table's scroll area takes the room the window has
// left, so the page never scrolls as well. jsdom cannot measure layout, so the
// unit tests cannot see it. /agents gets enough rows to scroll; the other list
// pages are checked for no page scroll with whatever rows the instance holds.
// ---------------------------------------------------------------------------

const stamp = Date.now();
const AGENT_COUNT = 30;
const agentIds: string[] = [];

const VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 1280, height: 720 },
];
const LIST_PAGES: Array<{ path: string; view?: string }> = [
  { path: '/presentations', view: 'presentations' },
  { path: '/users-and-groups/users' },
  { path: '/observability/traces' },
];

async function pageOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const root = document.scrollingElement ?? document.documentElement;
    return root.scrollHeight - window.innerHeight;
  });
}

test.describe.serial('Records table single scrollbar', () => {
  test.beforeAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      for (let i = 0; i < AGENT_COUNT; i += 1) {
        const created = await ok(await apiCtx.post('/api/v1/agents', {
          headers,
          data: { name: `E2E one scrollbar ${stamp} ${String(i).padStart(2, '0')}`, persona: 'Temp agent for the single scrollbar check.' },
        }), 'create agent');
        agentIds.push((await created.json()).id);
      }
    });
  });

  test.afterAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      for (const id of agentIds) await apiCtx.delete(`/api/v1/agents/${id}`, { headers });
    }).catch(() => undefined);
  });

  for (const viewport of VIEWPORTS) {
    test(`the agents list scrolls inside its table and never the page at ${viewport.width}x${viewport.height}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(() => localStorage.setItem('copilot-view:agents', 'list'));
      await page.goto('/agents');

      const scroller = page.getByTestId('data-table-scroll').first();
      await expect(scroller.locator('tbody tr').first()).toBeVisible({ timeout: 15_000 });
      await expect(scroller).toHaveAttribute('data-scroll-fit', 'viewport');

      // The rows overflow the table's scroll area, not the page.
      await expect.poll(() => scroller.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
      await expect.poll(() => pageOverflow(page)).toBeLessThanOrEqual(0);

      // The last row and the pager are reachable: scrolled to the end, the last row is inside the area.
      await scroller.evaluate((el) => { el.scrollTop = el.scrollHeight; });
      const area = (await scroller.boundingBox())!;
      const last = (await scroller.locator('tbody tr').last().boundingBox())!;
      expect(last.y + last.height).toBeLessThanOrEqual(area.y + area.height + 1);
      expect(area.y + area.height).toBeLessThanOrEqual(viewport.height);
    });

    test(`a long-form page keeps the table header under the top bar at ${viewport.width}x${viewport.height}`, async ({ page }) => {
      // A window too short for the floor: the table is not bounded, the page scrolls.
      await page.setViewportSize({ width: viewport.width, height: 380 });
      await page.addInitScript(() => localStorage.setItem('copilot-view:agents', 'list'));
      await page.goto('/agents');

      const scroller = page.getByTestId('data-table-scroll').first();
      await expect(scroller.locator('tbody tr').first()).toBeVisible({ timeout: 15_000 });
      await expect(scroller).toHaveAttribute('data-scroll-fit', 'page');
      // One scrollbar: the area draws none of its own.
      expect(await scroller.evaluate((el) => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(1);

      await page.evaluate(() => window.scrollTo(0, 600));
      const barBottom = await page.locator('#app-content').evaluate((main) => {
        const bar = main.parentElement?.firstElementChild;
        return bar && bar !== main ? bar.getBoundingClientRect().bottom : 0;
      });
      const head = page.getByTestId('data-table-head').first();
      await expect.poll(async () => Math.round((await head.boundingBox())!.y)).toBeGreaterThanOrEqual(Math.floor(barBottom) - 1);
      await expect.poll(async () => Math.round((await head.boundingBox())!.y)).toBeLessThanOrEqual(Math.ceil(barBottom) + 1);
    });

    for (const { path, view } of LIST_PAGES) {
      test(`${path} never shows a page scrollbar beside its table at ${viewport.width}x${viewport.height}`, async ({ page }) => {
        await page.setViewportSize(viewport);
        if (view) await page.addInitScript((key) => localStorage.setItem(`copilot-view:${key}`, 'list'), view);
        await page.goto(path);
        await expect(page.locator('#app-content')).toBeVisible({ timeout: 15_000 });
        // Wait for the table (or its empty state) to settle, then measure.
        await page.waitForLoadState('networkidle');
        const tables = page.getByTestId('data-table-scroll');
        if ((await tables.count()) === 1) {
          await expect(tables.first()).not.toHaveAttribute('data-scroll-fit', 'capped');
          const fit = await tables.first().getAttribute('data-scroll-fit');
          expect(fit === 'viewport' || fit === 'page').toBe(true);
          if (fit === 'viewport') await expect.poll(() => pageOverflow(page)).toBeLessThanOrEqual(0);
          // Long-form: the page scrolls alone, the table draws no scroller of its own.
          else expect(await tables.first().evaluate((el) => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(1);
        }
      });
    }
  }
});
