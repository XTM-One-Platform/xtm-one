import { test, expect } from '@playwright/test';
import { ok, withAdmin } from './helpers';

// ---------------------------------------------------------------------------
// The records table keeps its header in view while its rows scroll (issue
// #9378, data-tables.md s.3 "Sticky header"). jsdom cannot measure layout, so
// the unit tests cannot see it: UX round 1 on #9434 measured the header leaving
// the screen with the rows. The /agents list view is the EntityTable preset of
// the one DataTable; enough agents are seeded for its rows to outgrow a short
// viewport.
// ---------------------------------------------------------------------------

const stamp = Date.now();
const AGENT_COUNT = 24;
const agentIds: string[] = [];

test.describe.serial('Records table sticky header', () => {
  test.beforeAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      for (let i = 0; i < AGENT_COUNT; i += 1) {
        const created = await ok(await apiCtx.post('/api/v1/agents', {
          headers,
          data: { name: `E2E sticky ${stamp} ${String(i).padStart(2, '0')}`, persona: 'Temp agent for the sticky header check.' },
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

  test('the header stays at the top of the table while its rows scroll', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 700 });
    await page.addInitScript(() => localStorage.setItem('copilot-view:agents', 'list'));
    await page.goto('/agents');

    const scroller = page.getByTestId('data-table-scroll').first();
    await expect(scroller.locator('tbody tr').first()).toBeVisible({ timeout: 15_000 });

    // A bounded scroll area: the rows overflow it rather than the page.
    const bounded = await scroller.evaluate((el) => el.scrollHeight > el.clientHeight);
    expect(bounded).toBe(true);

    await scroller.evaluate((el) => { el.scrollTop = 400; });
    await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);

    const boxTop = (await scroller.boundingBox())!.y;
    const headTop = (await scroller.locator('thead').boundingBox())!.y;
    // Stuck at the top of its scroll area (1 px of rounding allowed), never scrolled away above it.
    expect(Math.abs(headTop - boxTop)).toBeLessThanOrEqual(1);
  });
});
