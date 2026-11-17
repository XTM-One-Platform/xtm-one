import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { slideChartBoxes, unzip } from './pptx-archive';

// ---------------------------------------------------------------------------
// Presentations - the butterfly chart.
//
// A people slide compares two populations rung by rung: individual
// contributors against managers per seniority level, the first drawn left of
// a central spine and the second right of it, both read as plain sizes. The
// acceptance block of the issue is imported through the real route on a
// content slide and in a dashboard panel, read in the viewer (mirrored bars,
// every value unsigned, no bar and no value for a zero, the rungs on
// the spine, the legend over the two sides, the hover), exported to PDF and
// to the editable PPTX (a clustered bar chart whose left series is stored
// negative under a sign-hiding number format that labels a zero with
// nothing, the export notes saying where its categories went), and a third
// series or a negative value is refused with the slide and the field named.
//
// One deck imported through the API from a bare spec (no LLM call), owned by
// the admin and deleted afterwards; the file runs SERIALLY for one login (see
// `presentation-charts.spec.ts`). Reads of the figure are auto-retrying
// assertions taken once the chart has dropped `data-deck-pending`.
// ---------------------------------------------------------------------------

const CATEGORIES = ['Executive', 'Director', 'Manager', 'Lead', 'Senior', 'Mid', 'Entry'];
const CONTRIBUTORS = [0, 2, 10, 40, 80, 30, 15];
const MANAGERS = [5, 20, 18, 6, 0, 0, 0];
/** What a side writes: every value but a zero, a rung that side lacks. */
const written = (values: number[]) => values.filter((value) => value !== 0).map(String);
const WRITTEN_COUNT = written(CONTRIBUTORS).length + written(MANAGERS).length;

/** The issue's acceptance block, word for word. */
const PYRAMID = {
  type: 'chart',
  kind: 'butterfly',
  categories: CATEGORIES,
  series: [
    { name: 'Contributors', values: CONTRIBUTORS },
    { name: 'Managers', values: MANAGERS },
  ],
  show_values: true,
};

const FIXTURE_SPEC = {
  title: 'E2E butterfly',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 'b-content', layout: 'content', title: 'Seniority mix', blocks: [PYRAMID] },
    {
      id: 'b-dashboard',
      layout: 'dashboard',
      title: 'People',
      tiles: [
        { value: '177', label: 'Contributors' },
        { value: '49', label: 'Managers' },
      ],
      panels: [
        { title: 'Seniority mix', block: { ...PYRAMID, show_totals: true } },
        { title: 'Hires', span: 2, block: { type: 'chart', kind: 'bar', categories: ['Q1', 'Q2'], series: [{ name: 'Hires', values: [12, 18] }] } },
      ],
    },
  ],
};

const SLIDE = { content: 1, dashboard: 2 } as const;

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

/** Opens the stage on one slide (reveal reads its 0-based index from the hash). */
async function openStage(page: Page, deckId: string, slide: number): Promise<void> {
  await page.goto(`/presentations/${deckId}#/${slide - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${slide} of \\d+\\.`) })).toBeVisible();
}

/** The butterfly on the slide on screen, once every drawing there has settled. */
async function settledButterfly(page: Page): Promise<Locator> {
  const slide = page.locator('.reveal .slides > section.present');
  await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
  const chart = slide.locator('.deck-chart[data-kind="butterfly"]');
  await expect(chart).toBeVisible();
  return chart;
}

/** Each visible bar's two ends along the value axis and its row, read in the evaluation that finds them. */
async function barEnds(chart: Locator): Promise<{ from: number; to: number; row: number }[]> {
  return chart.locator('.recharts-bar-rectangle path').evaluateAll((paths) =>
    paths
      .filter((path) => path.getAttribute('fill') !== 'transparent')
      .map((path) => {
        const x = Number(path.getAttribute('x'));
        return { from: x, to: x + Number(path.getAttribute('width')), row: Number(path.getAttribute('y')) };
      }),
  );
}

/** One side's values as the figure writes them, top to bottom. */
async function sideValues(chart: Locator, series: number): Promise<string[]> {
  return chart.locator(`.deck-chart-value[data-series="${series}"]`).evaluateAll((labels) =>
    labels
      .sort((a, b) => Number((a as HTMLElement).dataset.category) - Number((b as HTMLElement).dataset.category))
      .map((label) => (label.textContent ?? '').trim()),
  );
}

async function exportDeck(deckId: string, format: 'pdf' | 'pptx_native') {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    const response = await apiCtx.post(`/api/v1/presentations/${deckId}/export`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { format },
      timeout: 150_000,
    });
    expect(response.ok(), `export failed: ${response.status()} ${await response.text()}`).toBeTruthy();
    return { body: await response.body(), notes: response.headers()['x-export-notes'] ?? '' };
  } finally {
    await apiCtx.dispose();
  }
}

test.describe('Presentations - butterfly chart', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await importDeck(apiCtx, token, FIXTURE_SPEC, 'e2e-butterfly.json');
      if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
      deckId = ((await resp.json()) as { id: string }).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    await deleteDeck(deckId);
  });

  test('should refuse a third series or a negative value, naming the slide and the field', async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const deck = (block: object) => ({ ...FIXTURE_SPEC, slides: [{ id: 'x', layout: 'content', title: 'Mix', blocks: [block] }] });
      const third = { ...PYRAMID, series: [...PYRAMID.series, { name: 'Executives', values: [1, 0, 0, 0, 0, 0, 0] }] };
      let resp = await importDeck(apiCtx, token, deck(third), 'e2e-butterfly-third.json');
      expect(resp.status()).toBe(422);
      let { detail } = (await resp.json()) as { detail: string };
      expect(detail).toContain('Slide 1 (content): blocks.0 in chart');
      expect(detail).toContain('series has 3 entries but a butterfly plots exactly 2');

      const signed = { ...PYRAMID, series: [PYRAMID.series[0], { name: 'Managers', values: [5, 20, -18, 6, 0, 0, 0] }] };
      resp = await importDeck(apiCtx, token, deck(signed), 'e2e-butterfly-signed.json');
      expect(resp.status()).toBe(422);
      ({ detail } = (await resp.json()) as { detail: string });
      expect(detail).toContain('Slide 1 (content): blocks.0 in chart');
      expect(detail).toContain('series.1.values.2 is -18');
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should draw the pyramid mirrored on a content slide, every value a plain size', async ({ page }) => {
    await openStage(page, deckId, SLIDE.content);
    const chart = await settledButterfly(page);

    // Six contributor bars run leftwards, four manager bars rightwards (a
    // zero draws no bar), each side from its own edge of the spine's column.
    const bars = await barEnds(chart);
    const left = bars.filter((bar) => bar.to < bar.from);
    const right = bars.filter((bar) => bar.to > bar.from);
    expect(left).toHaveLength(6);
    expect(right).toHaveLength(4);
    const spineLeft = Math.max(...left.map((bar) => bar.from));
    const spineRight = Math.min(...right.map((bar) => bar.from));
    expect(spineRight).toBeGreaterThan(spineLeft);

    // Every value but a zero: no "0" on Executive's left nor on Senior's, Mid's and Entry's right.
    await expect(chart.locator('.deck-chart-value')).toHaveCount(WRITTEN_COUNT);
    expect(await sideValues(chart, 0)).toEqual(written(CONTRIBUTORS));
    expect(await sideValues(chart, 1)).toEqual(written(MANAGERS));
    await expect(chart.locator('.deck-chart-value[data-series="0"][data-category="0"]')).toHaveCount(0);
    for (const category of [4, 5, 6]) {
      await expect(chart.locator(`.deck-chart-value[data-series="1"][data-category="${category}"]`)).toHaveCount(0);
    }
    await expect(chart.locator('text.deck-chart-tick-category')).toHaveText(CATEGORIES);
    // No sign anywhere: the values, and the ticks of both sides.
    const ticks = await chart.locator('.deck-chart-tick-primary').allTextContents();
    expect(ticks.filter((tick) => tick.trim() === '80')).toHaveLength(2);
    expect([...ticks, ...(await chart.locator('.deck-chart-value').allTextContents())].some((text) => text.includes('-'))).toBe(false);

    // The legend over the two sides: the first series at the left end, the second at the right.
    const names = chart.locator('.deck-chart-legend[data-split="true"] .deck-chart-legend-name');
    await expect(names).toHaveText(['Contributors', 'Managers']);
    const [first, second] = await names.evaluateAll((spans) => spans.map((span) => span.getBoundingClientRect().left));
    expect(second).toBeGreaterThan(first);

    // The hover reads both sides' sizes for the rung, a zero included:
    // Senior's bar is the fourth contributor bar drawn (Executive's zero draws none).
    await chart.locator('.recharts-bar-rectangle path:not([fill="transparent"])').nth(3).hover();
    const tooltip = chart.locator('.deck-chart-tooltip');
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText('Senior');
    await expect(tooltip.getByRole('listitem')).toHaveText([/^Contributors\s*80$/, /^Managers\s*0$/]);
  });

  test('should draw it in a dashboard panel, with its side totals under the legend', async ({ page }) => {
    await openStage(page, deckId, SLIDE.dashboard);
    const chart = await settledButterfly(page);
    await expect(chart.locator('.deck-chart-value')).toHaveCount(WRITTEN_COUNT);
    const values = (await chart.locator('.deck-chart-value').allTextContents()).map((text) => text.trim());
    expect(values.some((text) => text === '0' || text.includes('-'))).toBe(false);
    await expect(chart.locator('text.deck-chart-tick-category')).toHaveText(CATEGORIES);
    await expect(page.locator('.reveal .slides > section.present .deck-chart-legend-total')).toHaveText(['Total 177', 'Total 49']);
  });

  test('should export to PDF and to an editable PPTX whose left side is stored negative and written unsigned', async () => {
    test.setTimeout(300_000);
    const pdf = await exportDeck(deckId, 'pdf');
    expect(pdf.body.subarray(0, 5).toString('latin1')).toBe('%PDF-');

    const pptx = await exportDeck(deckId, 'pptx_native');
    const entries = unzip(pptx.body);
    expect(slideChartBoxes(entries, SLIDE.content)).toHaveLength(1);
    const charts = [...entries.entries()]
      .filter(([name]) => /^ppt\/charts\/chart\d+\.xml$/.test(name))
      .map(([, xml]) => xml.toString('utf8'))
      .filter((xml) => xml.includes('Contributors'));
    expect(charts).toHaveLength(2);
    for (const xml of charts) {
      expect(xml).toContain('<c:barDir val="bar"/>');
      expect(xml).toContain('<c:grouping val="clustered"/>');
      expect(xml).toContain('<c:overlap val="100"/>');
      // The labels write no sign and nothing for a zero; the axis keeps its 0 tick.
      expect(xml).toContain('formatCode="#,##0;#,##0;"');
      expect(xml).toContain('formatCode="#,##0;#,##0"');
      // The first series' cache holds its sizes negative, the second's positive.
      const caches = [...xml.matchAll(/<c:val>[\s\S]*?<\/c:val>/g)].map(([cache]) =>
        [...cache.matchAll(/<c:v>([^<]*)<\/c:v>/g)].map(([, value]) => Number(value)),
      );
      expect(caches[0]).toEqual(CONTRIBUTORS.map((value) => (value ? -value : 0)));
      expect(caches[1]).toEqual(MANAGERS);
    }
    // The notes say where PowerPoint writes the rungs the viewer wrote on the spine.
    expect(pptx.notes).toContain("the butterfly chart's categories are on its left axis");
  });
});
