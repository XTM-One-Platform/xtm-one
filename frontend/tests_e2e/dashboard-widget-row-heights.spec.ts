import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Every widget card of one dashboard row is as tall as the row's tallest
// (Sam, 13 Nov 2026, #11470): the Inactive Users card ended short beside
// Lowest Activity, empty space under it. jsdom cannot measure layout, so the
// unit test (GapsTab.test.tsx) checks the structure and this spec the heights.
// ---------------------------------------------------------------------------

const VIEWPORTS = [
  { width: 1920, height: 1300 },
  { width: 1440, height: 900 },
  // UX round 2 B1: at 1080 a label beside a diff badge used to wrap.
  { width: 1728, height: 1080 },
];
const PAGES = ['/ai-adoption', '/ai-adoption/gaps'];

/** Per widget row, the heights of the cards that share a line (same top). */
async function rowHeights(page: Page): Promise<number[][]> {
  return page.evaluate(() => {
    const lines: number[][] = [];
    for (const row of Array.from(document.querySelectorAll('[data-widget-row]'))) {
      const byTop = new Map<number, number[]>();
      for (const card of Array.from(row.children)) {
        const box = card.getBoundingClientRect();
        if (box.height === 0) continue;
        const top = Math.round(box.top);
        byTop.set(top, [...(byTop.get(top) ?? []), Math.round(box.height)]);
      }
      for (const heights of byTop.values()) if (heights.length > 1) lines.push(heights);
    }
    return lines;
  });
}

/**
 * Per line of KPI cards (same card top), one box measure of the given part: the tops of
 * their values (a stretched card keeps its content top-aligned) or the heights of their
 * label rows (a card with a diff badge keeps the label row of a card without one).
 */
async function kpiPartPerLine(page: Page, testId: string, measure: 'top' | 'height'): Promise<number[][]> {
  return page.evaluate(([id, key]) => {
    const byTop = new Map<number, number[]>();
    for (const value of Array.from(document.querySelectorAll(`[data-widget-row] [data-testid="${id}"]`))) {
      const card = value.closest('[data-widget-row] > *');
      if (!card) continue;
      const cardTop = Math.round(card.getBoundingClientRect().top);
      const box = value.getBoundingClientRect();
      byTop.set(cardTop, [...(byTop.get(cardTop) ?? []), Math.round(key === 'top' ? box.top : box.height)]);
    }
    return Array.from(byTop.values()).filter((tops) => tops.length > 1);
  }, [testId, measure] as const);
}

/** The widest spread of one measure over the lines of KPI cards (0 when no line has two cards). */
async function kpiSpread(page: Page, testId: string, measure: 'top' | 'height'): Promise<number> {
  return Math.max(0, ...(await kpiPartPerLine(page, testId, measure)).map((values) => Math.max(...values) - Math.min(...values)));
}

test.describe('Dashboard widget rows', () => {
  for (const viewport of VIEWPORTS) {
    for (const path of PAGES) {
      test(`${path} draws the cards of a row at one height at ${viewport.width}x${viewport.height}`, async ({ page }) => {
        await page.setViewportSize(viewport);
        await page.goto(path);
        await expect(page.locator('[data-widget-row]').first()).toBeVisible({ timeout: 15_000 });
        await page.waitForLoadState('networkidle');
        // The cards fade in and their data arrives one by one: poll the widest spread.
        await expect.poll(async () => (await rowHeights(page)).length).toBeGreaterThan(0);
        await expect
          .poll(async () => Math.max(...(await rowHeights(page)).map((heights) => Math.max(...heights) - Math.min(...heights))))
          .toBeLessThanOrEqual(1);
        // UX round 1 B1: the values of one line of KPI cards share one top.
        await expect.poll(async () => kpiSpread(page, 'kpi-card-value', 'top')).toBeLessThanOrEqual(1);
        // UX round 2 B1: Overview mixes cards with a diff badge (Agents) and without (Conversations,
        // API Calls) on one line; their label rows keep one height, so no label wraps or grows.
        await expect.poll(async () => kpiSpread(page, 'kpi-card-label-row', 'height')).toBeLessThanOrEqual(1);
      });
    }
  }
});
