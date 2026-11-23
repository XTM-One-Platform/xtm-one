import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { unzip } from './pptx-archive';

// ---------------------------------------------------------------------------
// Presentations - two units on one chart.
//
// A board dashboard plots a count next to a price, millions next to a ratio:
// a series on `axis: "secondary"` reads against a right-hand value axis that
// `secondary_format` / `secondary_unit` write. Asserted on the two charts the
// Q3 2026 Quarterly Review had to drop - "New logos and ASP" and "Burn
// and net new ARR" - on the stage (two value axes on one grid, each labelled
// in its own unit and in the ink of its series, the tooltip in each series'
// format, the dotted right-hand target named in the legend) and through both
// PPTX exports: the image and the editable one, whose charts carry a second
// value axis, while bars on two axes - which PowerPoint would draw one
// over the other - stay a picture the export log names.
//
// The deck is imported through the API from a bare spec (no LLM call), owned
// by the admin and deleted afterwards. The file runs serially: one import.
// ---------------------------------------------------------------------------

const QUARTERS = ['Q4 25', 'Q1 26', 'Q2 26', 'Q3 26'];

const NEW_LOGOS_AND_ASP = {
  type: 'chart',
  kind: 'combo',
  categories: QUARTERS,
  series: [
    { name: 'New logos', kind: 'bar', values: [24, 34, 42, 56] },
    { name: 'ASP', kind: 'line', axis: 'secondary', values: [240_000, 330_000, 420_000, 540_000] },
  ],
  secondary_format: 'currency',
  secondary_unit: 'EUR',
};

const BURN_AND_NET_NEW_ARR = {
  type: 'chart',
  kind: 'combo',
  categories: QUARTERS,
  format: 'currency',
  unit: 'EUR',
  series: [
    { name: 'Burn', kind: 'bar', tone: 'error', values: [3_400_000, 3_900_000, 4_600_000, 3_100_000] },
    { name: 'Net new ARR', kind: 'bar', tone: 'success', values: [2_100_000, 3_400_000, 4_900_000, 6_200_000] },
    { name: 'Burn multiple', kind: 'line', axis: 'secondary', values: [1.62, 1.15, 0.94, 0.5] },
  ],
  secondary_format: 'number',
  secondary_unit: 'x',
  secondary_target: 1,
};

const SPEC = {
  title: 'E2E chart axes',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    {
      id: 'a-board',
      layout: 'dashboard',
      title: 'Q3 2026 at a glance',
      tiles: [
        { value: '56', label: 'New logos', delta: '+14', trend: 'up' },
        { value: 'EUR 540k', label: 'ASP', trend: 'up' },
        { value: '0.5x', label: 'Burn multiple', trend: 'down', tone: 'success' },
      ],
      // One row of panels: room for the values on the bars and five ticks.
      panels: [
        { title: 'New logos and ASP', block: NEW_LOGOS_AND_ASP },
        { title: 'Burn and net new ARR', span: 2, block: BURN_AND_NET_NEW_ARR },
      ],
    },
    {
      id: 'a-bars',
      layout: 'content',
      title: 'Deals and pipeline',
      blocks: [
        {
          type: 'chart',
          kind: 'bar',
          fill: true,
          categories: QUARTERS,
          series: [
            { name: 'Deals', values: [31, 44, 52, 63] },
            { name: 'Pipeline', axis: 'secondary', values: [8_200_000, 9_400_000, 12_100_000, 15_800_000] },
          ],
          secondary_format: 'currency',
        },
      ],
    },
  ],
};

async function importDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: { name: 'e2e-chart-axes.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(SPEC), 'utf8') },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id } = (await resp.json()) as { id: string };
  return id;
}

/** Opens the stage on one slide (1-based) and waits for every drawing of it to be complete. */
async function openSlide(page: Page, deckId: string, slide: number): Promise<Locator> {
  await page.goto(`/presentations/${deckId}#/${slide - 1}`);
  await expect(page.locator('.reveal.ready')).toBeVisible();
  const present = page.locator('.reveal .slides > section.present');
  await expect(present).toBeVisible();
  await expect(present.locator('[data-deck-pending]')).toHaveCount(0);
  return present;
}

/** The tick labels of one axis of a chart: Recharts draws them apart from the axis, each carrying its axis' class. */
const ticks = (chart: Locator, axis: 'category' | 'primary' | 'secondary'): Locator =>
  chart.locator(`.deck-chart-tick-${axis}`);

/**
 * The ink of a value: an SVG text's box spans its font's ascent and descent,
 * its figures only the cap height over the baseline (IBM Plex Sans: ascent
 * 1.025, descent 0.275, cap height 0.698 of the size).
 */
function inkOf(box: { x: number; y: number; width: number; height: number } | null) {
  if (!box) throw new Error('A value has no box on the stage');
  const baseline = box.y + (box.height * 1.025) / 1.3;
  return { left: box.x, right: box.x + box.width, top: baseline - (box.height * 0.698) / 1.3, bottom: baseline };
}

/** The colour a `--deck-*` token resolves to on this deck, as a computed colour. */
async function resolvedToken(page: Page, variable: string): Promise<string> {
  return page.evaluate((name) => {
    const root = document.querySelector<HTMLElement>('.deck-root');
    if (!root) throw new Error('No deck root on the page');
    const probe = document.createElement('span');
    probe.style.color = `var(${name})`;
    root.appendChild(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }, variable);
}

/** The computed `fill` of the first element a locator matches, read where it is found. */
async function computedFill(target: Locator): Promise<string> {
  return target.evaluateAll((elements) => {
    if (elements.length === 0) throw new Error('The locator matched no element');
    return getComputedStyle(elements[0]).fill;
  });
}

async function exportDeck(deckId: string, format: 'pptx' | 'pptx_native') {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    const response = await apiCtx.post(`/api/v1/presentations/${deckId}/export`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { format },
      timeout: 150_000,
    });
    expect(response.ok(), `${format} export failed: ${response.status()} ${await response.text()}`).toBeTruthy();
    return { body: await response.body(), notes: response.headers()['x-export-notes'] ?? null };
  } finally {
    await apiCtx.dispose();
  }
}

/** The chart parts a slide of an exported PPTX holds, by their relationship from the slide. */
function slideCharts(entries: Map<string, Buffer>, slide: number): string[] {
  const rels = entries.get(`ppt/slides/_rels/slide${slide}.xml.rels`)?.toString('utf8') ?? '';
  return [...rels.matchAll(/Target="\.\.\/charts\/(chart\d+\.xml)"/g)].map(([, name]) => {
    const xml = entries.get(`ppt/charts/${name}`)?.toString('utf8');
    if (!xml) throw new Error(`Slide ${slide} names a chart part the archive lacks: ${name}`);
    return xml;
  });
}

test.describe('Presentations - two units on one chart', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      deckId = await importDeck(apiCtx, token);
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    if (!deckId) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should draw both board charts on dashboard panels with two labelled value axes', async ({ page }) => {
    const slide = await openSlide(page, deckId, 1);
    const panels = slide.locator('article.deck-panel');
    await expect(panels).toHaveCount(2);

    // New logos and ASP: a count on the left, a price on the right, each axis
    // in the ink of its one series.
    const logos = panels.nth(0).locator('.deck-chart[data-kind="combo"]');
    await expect(ticks(logos, 'category')).toHaveText(QUARTERS);
    await expect(ticks(logos, 'secondary').first()).toHaveText('€0');
    const countTicks = await ticks(logos, 'primary').allTextContents();
    const priceTicks = await ticks(logos, 'secondary').allTextContents();
    expect(countTicks.length).toBeGreaterThanOrEqual(3);
    expect(priceTicks.length).toBe(countTicks.length);
    for (const tick of countTicks) expect(tick).toMatch(/^\d+$/);
    for (const tick of priceTicks) expect(tick).toMatch(/^€\d+(\.\d)?k$|^€0$/);
    // The right-hand axis reaches the 540k price, on a round step.
    expect(['€600k', '€800k']).toContain(priceTicks.at(-1));
    expect(await computedFill(ticks(logos, 'primary'))).toBe(await resolvedToken(page, '--deck-chart-1'));
    expect(await computedFill(ticks(logos, 'secondary'))).toBe(await resolvedToken(page, '--deck-chart-2'));
    // The bars are labelled as counts, the price line in euros: it runs
    // through the bars' tops, so each price is written under its point, on its
    // bar with its halo, next to its dot and clear of every count.
    const counts = logos.locator('.deck-chart-value[data-series="0"]');
    await expect(counts).toHaveText(['24', '34', '42', '56']);
    const prices = logos.locator('.deck-chart-value[data-series="1"]');
    await expect(prices).toHaveText(['€240k', '€330k', '€420k', '€540k']);
    for (let category = 0; category < 4; category += 1) {
      await expect(prices.nth(category)).toHaveAttribute('data-placement', 'below');
      const price = inkOf(await prices.nth(category).boundingBox());
      for (let other = 0; other < 4; other += 1) {
        const count = inkOf(await counts.nth(other).boundingBox());
        const apart =
          price.right <= count.left || count.right <= price.left || price.bottom <= count.top || count.bottom <= price.top;
        expect(apart, `the price of ${QUARTERS[category]} clears the count of ${QUARTERS[other]}`).toBe(true);
      }
    }
    await expect(logos.locator('.deck-chart-facts')).toHaveAttribute('data-values-written', '8');
    await logos.locator('.recharts-bar-rectangle').first().hover();
    const tooltip = logos.locator('.deck-chart-tooltip');
    await expect(tooltip).toBeVisible();
    await expect(tooltip.getByRole('listitem')).toHaveText([/^New logos\s*24$/, /^ASP\s*€240k$/]);

    // Burn and net new ARR: millions on the left, the burn multiple on the
    // right, its ticks keeping their unit; the 1x line is the right axis' own.
    const burn = panels.nth(1).locator('.deck-chart[data-kind="combo"]');
    const moneyTicks = await ticks(burn, 'primary').allTextContents();
    const ratioTicks = await ticks(burn, 'secondary').allTextContents();
    for (const tick of moneyTicks) expect(tick).toMatch(/^€\d+(\.\d)?M$|^€0$/);
    for (const tick of ratioTicks) expect(tick).toMatch(/^\d+(\.\d+)?x$/);
    expect(ratioTicks).toContain('1x');
    // Every point of the burn multiple is written, as precisely as its smallest value needs.
    await expect(burn.locator('.deck-chart-value[data-series="2"]')).toHaveText(['1.62x', '1.15x', '0.94x', '0.5x']);
    await expect(burn.locator('.deck-chart-facts')).toHaveAttribute('data-values-written', '12');
    await expect(burn.locator('.deck-chart-legend-name')).toHaveText(['Burn', 'Net new ARR', 'Burn multiple', '1x']);
    await expect(burn.locator('.deck-chart-target[data-value-axis="secondary"]')).toHaveCount(1);
    await expect(burn.locator('.deck-chart-swatch-line[data-axis="secondary"]')).toHaveCount(1);
  });

  test('should carry both charts into both PPTX exports, the editable one on two value axes', async () => {
    // Two server-side renders of the deck through the platform's headless browser.
    test.setTimeout(280_000);

    const image = unzip((await exportDeck(deckId, 'pptx')).body);
    for (const slide of [1, 2]) {
      expect(image.get(`ppt/slides/slide${slide}.xml`)?.toString('utf8'), `slide ${slide}`).toContain('<p:pic>');
    }

    const native = await exportDeck(deckId, 'pptx_native');
    // Bars on two axes stay a picture, and only that chart is named in the log.
    const notes = JSON.parse(native.notes ?? '[]') as string[];
    expect(notes).toEqual([
      'Slide 2: the bar chart is a picture of the rendered figure - PowerPoint draws the bars of two value axes over each other.',
    ]);
    const entries = unzip(native.body);
    expect(slideCharts(entries, 2)).toHaveLength(0);
    const charts = slideCharts(entries, 1);
    expect(charts).toHaveLength(2);
    for (const xml of charts) {
      // Bars and a line in one chart, the line on a second value axis on the
      // right, crossing a hidden category axis at its far end.
      expect(xml).toContain('<c:barChart>');
      expect(xml).toContain('<c:lineChart>');
      expect(xml.match(/<c:valAx>/g)).toHaveLength(2);
      expect(xml).toContain('<c:axPos val="r"/>');
      expect(xml).toContain('<c:crosses val="max"/>');
      expect(xml).toContain('<c:delete val="1"/>');
    }
    // Each right-hand axis writes its own unit: the price compact, the ratio with its x.
    const logos = charts.find((xml) => xml.includes('<c:v>ASP</c:v>'));
    const burn = charts.find((xml) => xml.includes('<c:v>Burn multiple</c:v>'));
    expect(logos).toContain('<c:v>New logos</c:v>');
    expect(logos).toMatch(/formatCode="[^"]*&quot;k&quot;/);
    expect(burn).toMatch(/formatCode="[^"]*&quot;x&quot;"/);
    // Each line carries its values as PowerPoint labels: the burn
    // multiple to the hundredth, the prices under their points as on the slide.
    expect(burn).toContain('formatCode="#,##0.00&quot;x&quot;"');
    expect(logos).toContain('<c:dLblPos val="b"/>');
  });
});
