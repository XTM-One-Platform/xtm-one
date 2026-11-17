import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Agents list view fits its card.
//
// On scope All at 1440 px the agents table scrolled sideways inside its card
// (scrollWidth 1253 against clientWidth 1194). The Name cell now puts its
// badges under the name, Resources shows from 1280 px, Model from 1440 px
// (with Model the table was 1121 px in a 1034 px card at
// 1280) and the Assignments column (also counted in Resources) from 1680 px.
// With the "Net value (30d)" column (#9836 R6b, 148 px), Tags shows from
// 1440 px at 120 px (the rest folded into "+N").
//
// The real agents list is fetched and a copy of its first agent is added with
// a long name and every badge the Name cell can carry (company-managed,
// shared, channels, web exposure), so the widest row is on screen whatever
// the deployment holds. Nothing is written.
// ---------------------------------------------------------------------------

const LONG_NAME = 'Threat intelligence triage assistant for the Paris security operations centre';

async function openListView(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('copilot-view:agents', 'list');
  });
  await page.route(
    (url) => url.pathname.endsWith('/api/v1/agents'),
    async (route: Route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const agents = (await response.json()) as Record<string, unknown>[];
      const base = agents[0] ?? {};
      const wide = {
        ...base,
        id: '00000000-0000-4000-8000-000000007848',
        name: LONG_NAME,
        description: 'Reads every inbound advisory and files the ones that matter for the team',
        enabled: true,
        is_company_managed: true,
        user_id: '00000000-0000-4000-8000-0000000078ff',
        owner_name: 'Another owner',
        web_enabled: true,
        channel_config: { slack: {} },
        tags: ['triage', 'intel', 'soc', 'paris'],
      };
      await route.fulfill({ response, json: [...agents, wide] });
    },
  );
  await page.goto('/agents?vis=all');
}

test.describe('Agents list, table fits its card', () => {
  for (const width of [1024, 1280, 1366, 1440]) {
    test(`no horizontal scroll with a long agent name at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openListView(page);
      // The list pages at 25 rows (standard v2, #10916): search the wide agent so its row
      // is on page 1 whatever the deployment holds.
      await page.getByRole('searchbox', { name: 'Search agents...' }).fill(LONG_NAME);

      const longName = page.getByTestId('entity-name').filter({ hasText: 'Threat intelligence triage' });
      await expect(longName).toBeVisible({ timeout: 15_000 });
      await expect(longName).toHaveAttribute('title', LONG_NAME);

      // The badges sit under the name, not beside it.
      const cell = page.getByTestId('entity-name-cell').filter({ has: longName });
      const nameBox = await longName.boundingBox();
      const badgesBox = await cell.getByTestId('entity-name-badges').boundingBox();
      expect(nameBox).not.toBeNull();
      expect(badgesBox).not.toBeNull();
      expect(badgesBox!.y).toBeGreaterThanOrEqual(nameBox!.y + nameBox!.height);

      // The table fits its card, and the page fits the viewport.
      const scroller = page.locator('table').locator('xpath=..');
      const overflow = await scroller.evaluate((el) => el.scrollWidth - el.clientWidth);
      expect(overflow).toBeLessThanOrEqual(1);
      const pageOverflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(pageOverflow).toBeLessThanOrEqual(1);
    });
  }

  // Standard v2, UX round 1 on #10920 (B1): one rendered row height per table, the
  // two-line token (h-14, 56 px), a row with badges on line 2 and a row without alike.
  test('renders every row at the two-line row height, badge or not', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openListView(page);
    await expect(page.getByTestId('entity-name-text').first()).toBeVisible({ timeout: 15_000 });

    const heights = await page.locator('table tbody tr').evaluateAll((rows) =>
      rows
        .filter((row) => row.querySelector('[data-testid="entity-name-cell"]'))
        .map((row) => Math.round(row.getBoundingClientRect().height)),
    );
    expect(heights.length).toBeGreaterThan(0);
    // One height for every row; the token is 56 px (the collapsed 1 px row border may add one).
    const distinct = [...new Set(heights)];
    expect(distinct, `row heights ${heights.join(',')}`).toHaveLength(1);
    expect(Math.abs(distinct[0] - 56), `row height ${distinct[0]} against the 56 px token`).toBeLessThanOrEqual(1);
  });

  // Standard v2, UX round 2 on #10920 (B1): line 2 never clips a badge; the ones that
  // do not fit fold into "+N", so no line 2 overflows at any width.
  for (const width of [1024, 1440]) {
    test(`no line 2 clips its badges at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openListView(page);
      await expect(page.getByTestId('entity-name-text').first()).toBeVisible({ timeout: 15_000 });
      const overflows = await page.getByTestId('entity-name-line2').evaluateAll((lines) =>
        lines.map((line) => line.scrollWidth - line.clientWidth),
      );
      expect(overflows.length).toBeGreaterThan(0);
      for (const overflow of overflows) expect(overflow).toBeLessThanOrEqual(1);
    });
  }

  // Standard v2, UX round 1 on #10920 (B2): the scope switcher is never a filter.
  test('shows no Clear all on scope All with no filter, and clearing keeps the scope', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openListView(page);
    await expect(page.getByTestId('entity-name-text').first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Clear all filters' })).toHaveCount(0);

    await page.getByRole('searchbox', { name: 'Search agents...' }).fill(LONG_NAME);
    await page.getByRole('button', { name: 'Clear all filters' }).first().click();
    await expect(page).toHaveURL(/[?&]vis=all\b/);
    await expect(page.getByRole('button', { name: 'Clear all filters' })).toHaveCount(0);
  });

  // #10746: the name and the description sat above the row's centre, under
  // them an empty badge line. Every row's visible text block is centred on
  // its icon tile, with badges or without.
  test('centres the name block of every row on its icon tile', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openListView(page);
    await expect(page.getByTestId('entity-name-text').first()).toBeVisible({ timeout: 15_000 });

    const offsets = await page.getByTestId('entity-name-cell').evaluateAll((cells) =>
      cells.map((cell) => {
        const tile = cell.firstElementChild!.getBoundingClientRect();
        const text = cell.querySelector('[data-testid="entity-name-text"]')!.getBoundingClientRect();
        return Math.abs(text.top + text.height / 2 - (tile.top + tile.height / 2));
      }),
    );
    expect(offsets.length).toBeGreaterThan(0);
    for (const offset of offsets) expect(offset).toBeLessThanOrEqual(1);
  });
});
