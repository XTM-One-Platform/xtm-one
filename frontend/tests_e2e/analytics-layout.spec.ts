import { test, expect } from '@playwright/test';

// ---------------------------------------------------------------------------
// LLM Analytics: every bar carries its figure, so a long window gives a
// chart wider than the card. That chart scrolls inside its card; the page
// itself never scrolls sideways, on a window narrower than the two-column
// breakpoint as on a wide one.
// ---------------------------------------------------------------------------

const DAYS = 365;
// Eight models: the stack keeps six and folds the rest into Other, the tallest tooltip.
const MODELS = ['model-a', 'model-b', 'model-c', 'model-d', 'model-e', 'model-f', 'model-g', 'model-h'];

function days(): string[] {
  const end = Date.UTC(2026, 9, 31);
  return Array.from({ length: DAYS }, (_, index) => new Date(end - (DAYS - 1 - index) * 86_400_000).toISOString().slice(0, 10));
}

const metrics = {
  total_cost: 61176.89,
  total_tokens: 36_500_000_000,
  trace_count: 110_600,
  avg_duration_ms: 6400,
  error_rate: 0.021,
  avg_feedback_score: 0.82,
  quota_consumed: 0,
  message_count: 0,
  avg_quota_per_message: null,
};

const summaryPayload = {
  ...metrics,
  previous: { ...metrics, total_cost: 34160.45 },
  last_aggregation_at: '2026-10-31T00:00:00Z',
  aggregation_stale: false,
  unknown_cost_trace_count: 0,
  stats_coverage_start: '2025-01-01',
  window_starts_before_coverage: false,
};

const costPayload = {
  series: days().flatMap((date, index) =>
    MODELS.map((model, rank) => ({ date, model, cost: 1000 / (rank + 1) + (index % 7) * 50 })),
  ),
};

const latencyPayload = {
  overall: { p50: 4200, p75: 7800, p90: 15400, p95: 23100, p99: 61000 },
  by_model: [],
  daily_median: days().map((date, index) => ({ date, p50: 3500 + (index % 25) * 100 })),
  approximate: false,
};

const pageScrollsSideways = () => document.documentElement.scrollWidth > document.documentElement.clientWidth;

test.describe('LLM Analytics layout', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/traces/analytics/**', async (route) => {
      const { pathname } = new URL(route.request().url());
      const body: Record<string, unknown> = {
        '/api/v1/traces/analytics/summary': summaryPayload,
        '/api/v1/traces/analytics/cost-over-time': costPayload,
        '/api/v1/traces/analytics/volume-over-time': { series: [] },
        '/api/v1/traces/analytics/latency': latencyPayload,
      };
      if (pathname in body) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body[pathname]) });
        return;
      }
      await route.continue();
    });
  });

  for (const width of [1024, 1440]) {
    test(`a year of daily bars scrolls inside its card, not the page, at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/observability?period=1y');
      await expect(page.getByText('Usage over time')).toBeVisible();
      await page.getByRole('group', { name: 'Interval' }).getByRole('button', { name: 'Day' }).click();

      const chart = page.locator('.recharts-surface').first();
      await expect(chart.locator('text[transform]')).toHaveCount(DAYS);
      expect(await page.evaluate(pageScrollsSideways)).toBe(false);

      // The card's own scroller holds the width the figures need.
      const scroller = chart.locator('xpath=ancestor::div[contains(@class, "overflow-x-auto")][1]');
      const { scrollWidth, clientWidth } = await scroller.evaluate((node) => ({
        scrollWidth: node.scrollWidth,
        clientWidth: node.clientWidth,
      }));
      expect(scrollWidth).toBeGreaterThan(clientWidth);
      expect(clientWidth).toBeLessThanOrEqual(width);
    });
  }

  // Hovering the bars never flashes a scrollbar. The tooltip stays inside the chart's
  // own box, so the scroller's overflow, width and height are the same before, during and
  // after the pointer rests on the last bar, where the tooltip opens at the right edge.
  for (const width of [1024, 1440, 1920]) {
    test(`hovering the last bar of a month never changes the scroller at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/observability?period=30d');
      await expect(page.getByText('Usage over time')).toBeVisible();
      await page.getByRole('group', { name: 'Interval' }).getByRole('button', { name: 'Day' }).click();

      const scroller = page.getByTestId('usage-over-time-scroller');
      const bars = scroller.locator('.recharts-bar-rectangle');
      await expect(bars.last()).toBeAttached();
      // The last bar sits at the scroller's right end, where the tooltip opens against the edge.
      await scroller.evaluate((node) => {
        node.scrollLeft = node.scrollWidth;
      });
      // At 1024 px the filters and KPI strips stack taller and push the card below the fold:
      // page.mouse works in viewport coordinates, so the bars must be on screen to be hovered.
      await scroller.scrollIntoViewIfNeeded();
      const measure = () =>
        scroller.evaluate((node) => ({
          overflowX: getComputedStyle(node).overflowX,
          scrollWidth: node.scrollWidth,
          clientWidth: node.clientWidth,
          scrollHeight: node.scrollHeight,
          clientHeight: node.clientHeight,
          offsetHeight: (node as HTMLElement).offsetHeight,
        }));
      const before = await measure();

      // Enter from below the bars, then rest on the last one.
      const box = await bars.last().boundingBox();
      expect(box).not.toBeNull();
      const x = box!.x + box!.width / 2;
      await page.mouse.move(x, box!.y + box!.height + 40);
      const samples: Awaited<ReturnType<typeof measure>>[] = [];
      for (const step of [0.9, 0.6, 0.3]) {
        await page.mouse.move(x, box!.y + box!.height * step, { steps: 4 });
        samples.push(await measure());
      }
      await expect(scroller.locator('.recharts-tooltip-wrapper')).toBeVisible();
      samples.push(await measure());

      for (const sample of samples) expect(sample).toEqual(before);
      // The whole tooltip shows: it fits inside the chart's own (clipped) box.
      const fits = await scroller.evaluate((node) => {
        const tip = node.querySelector('.recharts-default-tooltip')!.getBoundingClientRect();
        const chart = node.firstElementChild!.getBoundingClientRect();
        return tip.top >= chart.top && tip.bottom <= chart.bottom;
      });
      expect(fits).toBe(true);
      expect(await page.evaluate(pageScrollsSideways)).toBe(false);
    });
  }

  // The Y axis stays in view while the bars scroll, the scroller is a named region
  // reachable by Tab, and a bucket change brings the latest bars back into view.
  for (const width of [1024, 1440]) {
    test(`a year of daily bars keeps its Y axis in view and opens on the latest bars at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/observability?period=1y');
      await expect(page.getByText('Usage over time')).toBeVisible();
      const interval = page.getByRole('group', { name: 'Interval' });
      await interval.getByRole('button', { name: 'Day' }).click();

      const scroller = page.getByRole('region', { name: 'Usage over time chart, scrolls sideways' });
      await expect(scroller).toHaveAttribute('tabindex', '0');
      await expect(scroller.locator('.recharts-bar-rectangle').last()).toBeAttached();
      const atRightEnd = () =>
        scroller.evaluate((node) => Math.abs(node.scrollLeft + node.clientWidth - node.scrollWidth) <= 1);
      await expect.poll(atRightEnd).toBe(true);

      // Scrolled to the middle, the pinned axis still sits inside the card.
      await scroller.evaluate((node) => {
        node.scrollLeft = 1800;
      });
      const axis = page.getByTestId('usage-over-time-scroller-axis');
      // The tick labels (the axis chart's X axis draws none, so these are the Y ticks).
      const ticks = axis.locator('.recharts-cartesian-axis-tick-value');
      await expect(ticks.first()).toBeVisible();
      const [axisBox, scrollerBox] = [await axis.boundingBox(), await scroller.boundingBox()];
      expect(axisBox!.x).toBeGreaterThanOrEqual(0);
      expect(axisBox!.x + axisBox!.width).toBeLessThanOrEqual(scrollerBox!.x + 1);

      // Week then Day again: the latest bars are back in view.
      await interval.getByRole('button', { name: 'Week' }).click();
      await interval.getByRole('button', { name: 'Day' }).click();
      await expect.poll(atRightEnd).toBe(true);
      expect(await page.evaluate(pageScrollsSideways)).toBe(false);
    });
  }

  // With the Y axis pinned outside the scroller, the figure over the first
  // point of the Daily median line stays whole inside the scroller (the X axis padding), and
  // it is the formatted median ("3.5s"), as on the axis and the tooltip.
  for (const width of [1024, 1440]) {
    test(`the first Daily median figure is not cut by the scroller at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/observability?period=1y');
      await expect(page.getByText('Usage over time')).toBeVisible();
      await page.getByRole('button', { name: 'Daily median' }).click();

      const scroller = page.getByRole('region', { name: 'Daily median latency chart, scrolls sideways' });
      const figures = scroller.locator('.recharts-label-list text');
      await expect(figures.first()).toBeAttached();
      await scroller.evaluate((node) => {
        node.scrollLeft = 0;
      });
      const [scrollerBox, firstBox] = [await scroller.boundingBox(), await figures.first().boundingBox()];
      expect(firstBox!.x).toBeGreaterThanOrEqual(scrollerBox!.x);
      await expect(figures.first()).toHaveText('3.5s');
    });
  }

  // A month at 1024 px never widens the page (Cost breakdown, Latency).
  test('a month of daily bars never widens the page at 1024 px', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 900 });
    await page.goto('/observability?period=30d');
    await expect(page.getByText('Usage over time')).toBeVisible();
    await page.getByRole('group', { name: 'Interval' }).getByRole('button', { name: 'Day' }).click();
    await expect(page.getByTestId('usage-over-time-scroller').locator('.recharts-bar-rectangle').last()).toBeAttached();
    expect(await page.evaluate(pageScrollsSideways)).toBe(false);
  });
});
