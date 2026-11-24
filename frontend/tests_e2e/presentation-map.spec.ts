import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { slideChartBoxes, slidePictureBoxes, unzip } from './pptx-archive';

// ---------------------------------------------------------------------------
// Presentations - the map chart.
//
// A figure by country - customers, headcount, incidents - is a world
// choropleth: the categories are ISO 3166-1 alpha-2 codes, one series colours
// each country on a ramp of the template's chart colour, the countries
// without a value stay in the muted surface, and a code that is not one is
// refused with its index named. Asserted here through the real import route,
// the real viewer - on a content slide and in a dashboard panel - and the real
// export: the editable PPTX, where the map is a picture placed where the viewer
// draws it, its title and caption written as text.
//
// One deck imported through the API from a bare spec (no LLM call), owned by
// the admin and deleted afterwards, with fictitious figures; the file runs
// SERIALLY for one login (see `presentation-charts.spec.ts`).
// ---------------------------------------------------------------------------

const map = (extra: Record<string, unknown> = {}) => ({
  type: 'chart',
  kind: 'map',
  categories: ['FR', 'US', 'AU', 'GB'],
  series: [{ name: 'Customers', values: [84, 36, 14, 9] }],
  show_values: true,
  ...extra,
});

const FIXTURE_SPEC = {
  title: 'E2E map',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 'm-title', layout: 'title', title: 'Where our customers are' },
    {
      id: 'm-world',
      layout: 'content',
      title: 'Customers by country',
      blocks: [map({ title: 'Customers by country', caption: 'End of quarter' })],
    },
    {
      id: 'm-panel',
      layout: 'dashboard',
      title: 'At a glance',
      tiles: [
        { value: '143', label: 'Customers' },
        { value: '4', label: 'Countries' },
      ],
      panels: [
        { title: 'Customers by country', span: 2, block: map({ label_size: 'sm' }) },
        {
          title: 'By quarter',
          block: { type: 'chart', kind: 'bar', categories: ['Q1', 'Q2', 'Q3'], series: [{ name: 'Customers', values: [100, 120, 143] }] },
        },
      ],
    },
    {
      // The stepped legend and no markers: the issue's block as written.
      id: 'm-classes',
      layout: 'content',
      title: 'Employees by country',
      blocks: [
        {
          type: 'chart',
          kind: 'map',
          categories: ['FR', 'US', 'AU', 'GB', 'SA', 'SG', 'BE'],
          series: [{ name: 'Employees', values: [120, 40, 12, 12, 7, 6, 1] }],
          show_values: false,
          legend_style: 'classes',
          markers: false,
        },
      ],
    },
  ],
};

const SLIDE = { world: 2, panel: 3, classes: 4 } as const;
/** The issue's classes, lowest first: natural breaks on 1, 6, 7, 12, 40, 120. */
const CLASS_LABELS = ['1', '6 - 7', '12', '40', '120 +'];

async function importDeck(apiCtx: APIRequestContext, token: string, spec: object, name: string) {
  return apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: { name, mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(spec), 'utf8') },
    },
  });
}

async function deleteDeck(deckId: string): Promise<void> {
  if (!deckId) return;
  const { apiCtx, token } = await createAdminApiContext();
  try {
    await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
  } finally {
    await apiCtx.dispose();
  }
}

async function exportDeck(deckId: string, format: 'pptx_native') {
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

/** Opens the stage on one slide (reveal reads its 0-based index from the hash). */
async function openStage(page: Page, deckId: string, slide: number): Promise<void> {
  await page.goto(`/presentations/${deckId}#/${slide - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${slide} of \\d+\\.`) })).toBeVisible();
}

/** The map on the slide on screen, once every drawing there has settled. */
async function settledMap(page: Page): Promise<Locator> {
  const slide = page.locator('.reveal .slides > section.present');
  await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
  const chart = slide.locator('.deck-chart[data-kind="map"]');
  await expect(chart.locator('svg.deck-chart-map')).toBeVisible();
  return chart;
}

/** The computed fill of a country's shape. */
async function fillOf(chart: Locator, code: string): Promise<string> {
  return chart.locator(`path.deck-chart-country[data-code="${code}"]`).evaluate((path) => getComputedStyle(path).fill);
}

/** Moves the pointer onto a country: the centre of its value when it writes one, else of its box. */
async function hoverCountry(page: Page, chart: Locator, code: string, category?: number): Promise<void> {
  const target =
    category === undefined
      ? chart.locator(`path.deck-chart-country[data-code="${code}"]`)
      : chart.locator(`text.deck-chart-value[data-category="${category}"]`);
  const box = await target.boundingBox();
  if (!box) throw new Error(`${code} is not drawn`);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
}

test.describe('Presentations - map chart', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await importDeck(apiCtx, token, FIXTURE_SPEC, 'e2e-map.json');
      if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
      deckId = ((await resp.json()) as { id: string }).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    await deleteDeck(deckId);
  });

  test('should refuse a code ISO 3166-1 does not assign, naming its index', async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const wrong = map({ categories: ['FR', 'UK', 'US', 'AU'] });
      const spec = { ...FIXTURE_SPEC, slides: [{ id: 'x', layout: 'content', title: 'Wrong', blocks: [wrong] }] };
      const resp = await importDeck(apiCtx, token, spec, 'e2e-map-wrong.json');
      expect(resp.status()).toBe(422);
      const { detail } = (await resp.json()) as { detail: string };
      expect(detail).toContain('categories.1 "UK" is not an ISO 3166-1 alpha-2 country code');
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should draw a world choropleth on a content slide, the values on the large countries and every one on hover', async ({
    page,
  }) => {
    await openStage(page, deckId, SLIDE.world);
    const chart = await settledMap(page);

    // Every country the block names is drawn on the ramp, each value at its own
    // place on it (the ramp is continuous); the rest in the empty fill.
    const [france, unitedStates, australia, britain, germany, spain] = await Promise.all(
      ['FR', 'US', 'AU', 'GB', 'DE', 'ES'].map((code) => fillOf(chart, code)),
    );
    expect(germany).toBe(spain);
    for (const fill of [france, unitedStates, australia, britain]) expect(fill).not.toBe(germany);
    expect(new Set([france, unitedStates, australia, britain]).size).toBe(4);

    // The United States and Australia hold their value; the ramp is the legend.
    await expect(chart.locator('text.deck-chart-value[data-category="1"]')).toHaveText('36');
    await expect(chart.locator('text.deck-chart-value[data-category="2"]')).toHaveText('14');
    await expect(chart.locator('.deck-chart-ramp')).toContainText('Customers');
    await expect(chart.locator('.deck-chart-ramp-value')).toHaveText(['9', '84']);
    await expect(chart.locator('.deck-chart-head .deck-chart-title')).toHaveText('Customers by country');
    await expect(chart.locator('figcaption')).toHaveText('End of quarter');

    // The hover names the country and its value, or says it has none.
    const tooltip = chart.locator('.deck-chart-tooltip');
    await hoverCountry(page, chart, 'US', 1);
    await expect(tooltip.locator('.deck-chart-tooltip-title')).toHaveText('United States');
    await expect(tooltip.locator('.deck-chart-tooltip-value')).toHaveText('36');
    await hoverCountry(page, chart, 'DE');
    await expect(tooltip.locator('.deck-chart-tooltip-title')).toHaveText('Germany');
    await expect(tooltip.locator('.deck-chart-tooltip-value')).toHaveText('No data');
  });

  test('should draw the map in a dashboard panel, its ramp on the panel title\'s row', async ({ page }) => {
    await openStage(page, deckId, SLIDE.panel);
    await settledMap(page);
    // `has` is resolved inside each panel, so the map's locator is relative to it.
    const panel = page
      .locator('.reveal .slides > section.present article.deck-panel')
      .filter({ has: page.locator('.deck-chart[data-kind="map"]') });
    await expect(panel.locator('.deck-panel-title')).toHaveText('Customers by country');
    const [title, ramp, canvas, svg] = await Promise.all(
      ['.deck-panel-title', '.deck-chart-ramp', '.deck-chart-canvas', 'svg.deck-chart-map'].map((selector) =>
        panel.locator(selector).boundingBox(),
      ),
    );
    if (!title || !ramp || !canvas || !svg) throw new Error('The panel did not lay its map out');
    // The ramp on the title's row, the map under both, as wide as the panel's canvas.
    expect(Math.abs(ramp.y + ramp.height / 2 - (title.y + title.height / 2))).toBeLessThan(title.height);
    expect(canvas.y).toBeGreaterThan(title.y + title.height - 1);
    expect(Math.abs(svg.width - canvas.width)).toBeLessThan(2);
  });

  test('should draw a classed map flat, with no marker and no value, its classes in a column beside the plot', async ({
    page,
  }) => {
    await openStage(page, deckId, SLIDE.classes);
    const chart = await settledMap(page);
    await expect(chart.locator('circle.deck-chart-marker')).toHaveCount(0);
    await expect(chart.locator('text.deck-chart-value')).toHaveCount(0);
    await expect(chart.locator('.deck-chart-ramp')).toHaveCount(0);

    // One row per class, a dot and its range, the highest on top, right of the plot.
    const legend = chart.locator('.deck-chart-legend.deck-chart-classes');
    await expect(legend.locator('li')).toHaveText(CLASS_LABELS);
    const [canvas, top, bottom] = await Promise.all([
      chart.locator('.deck-chart-canvas').boundingBox(),
      legend.locator('li').last().boundingBox(),
      legend.locator('li').first().boundingBox(),
    ]);
    if (!canvas || !top || !bottom) throw new Error('The classed map did not lay its legend out');
    expect(top.x).toBeGreaterThan(canvas.x + canvas.width - 1);
    expect(top.y).toBeLessThan(bottom.y);

    // Each class is one flat colour: tied countries alike, every class apart, the rest empty.
    const [france, unitedStates, australia, britain, saudi, belgium, germany] = await Promise.all(
      ['FR', 'US', 'AU', 'GB', 'SA', 'BE', 'DE'].map((code) => fillOf(chart, code)),
    );
    expect(australia).toBe(britain);
    expect(new Set([france, unitedStates, australia, saudi, belgium, germany]).size).toBe(6);
    const dots = await legend
      .locator('.deck-chart-swatch')
      .evaluateAll((swatches) => swatches.map((swatch) => getComputedStyle(swatch).backgroundColor));
    expect(dots).toEqual([belgium, saudi, australia, unitedStates, france]);
  });

  test('should export the map to a PPTX as a picture where the viewer draws it, its text editable', async ({
    page,
  }) => {
    await openStage(page, deckId, SLIDE.world);
    const chart = await settledMap(page);
    const canvas = await chart.evaluate((figure) => {
      const frame = (figure.closest('section') as HTMLElement).getBoundingClientRect();
      const rect = (figure.querySelector('.deck-chart-canvas') as HTMLElement).getBoundingClientRect();
      return {
        left: (rect.left - frame.left) / frame.width,
        top: (rect.top - frame.top) / frame.height,
        width: rect.width / frame.width,
        height: rect.height / frame.height,
      };
    });

    const native = await exportDeck(deckId, 'pptx_native');
    const notes = JSON.parse(native.notes ?? '[]') as string[];
    expect(notes).toContain(
      'Slide 2: the map chart is a picture of the rendered figure - PowerPoint has no map chart that opens outside Office; its title, caption and legend are text.',
    );
    const entries = unzip(native.body);
    // No PowerPoint chart: the map is the picture of its canvas, where the viewer draws it.
    expect(slideChartBoxes(entries, SLIDE.world)).toHaveLength(0);
    const near = slidePictureBoxes(entries, SLIDE.world).filter((picture) =>
      (['left', 'top', 'width', 'height'] as const).every((side) => Math.abs(picture[side] - canvas[side]) < 0.01),
    );
    expect(near).toHaveLength(1);
    const xml = entries.get(`ppt/slides/slide${SLIDE.world}.xml`)?.toString('utf8') ?? '';
    for (const text of ['Customers by country', 'End of quarter', 'Customers', '84']) expect(xml).toContain(`<a:t>${text}</a:t>`);

    // The classed map: the same picture, its class rows written as text beside it.
    expect(slideChartBoxes(entries, SLIDE.classes)).toHaveLength(0);
    expect(slidePictureBoxes(entries, SLIDE.classes).length).toBeGreaterThanOrEqual(1);
    const classes = entries.get(`ppt/slides/slide${SLIDE.classes}.xml`)?.toString('utf8') ?? '';
    for (const label of CLASS_LABELS) expect(classes).toContain(`<a:t>${label}</a:t>`);
  });
});
