import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext, createTwoSchemeTemplate, deleteTemplate, importDeckOnTemplate } from './helpers';
import { slideChartBoxes, slidePictureBoxes, unzip } from './pptx-archive';

// ---------------------------------------------------------------------------
// Presentations - native charts and the dashboard layout.
//
// The `chart` block is drawn by Recharts inside the deck's fixed canvas, in the
// template's palette and fonts, and the `dashboard` layout composes KPI tiles
// and chart panels on one slide. Everything asserted here is what the AUDIENCE
// sees in the viewer: the figure's elements, the formatted values written on
// it, the legend, the tooltip under the pointer, the colours resolved from the
// deck's tokens, the motion on slide entry - and the stills where motion has
// no place (a preview, reduced motion). The pure formatting rules behind the
// strings are pinned in `tests_unit/chart-format.test.ts`; the export and the layout
// check are server-side and have their own tests.
//
// The deck is a persisted presentation imported through the API from a bare
// spec (no LLM call), one slide per chart kind, owned by the admin and deleted
// afterwards. The file runs SERIALLY: `beforeAll` runs once per worker, so a
// fully parallel run would log in and import the decks once per test, and a
// burst of logins is what the auth rate limit is there to refuse. Every test
// only READS the shared deck; the theme switch, which writes, gets a deck of
// its own, on a template of its own that dresses both schemes (the install's
// default may dress one only - the XTM One Brand Deck is dark only).
//
// A chart carries `data-deck-pending` until its figure is complete - drawn,
// and grown in when it animates, since Recharts writes the values only then -
// and a slide entry remounts the drawing, so a read of the figure is always an
// auto-retrying assertion, never a one-shot `allTextContents()`, and a computed
// style is read in the evaluation that finds its element (`computedFill`).
// ---------------------------------------------------------------------------

/** One content slide holding one chart, filling the body. */
const chartSlide = (id: string, title: string, chart: Record<string, unknown>) => ({
  id,
  layout: 'content',
  title,
  blocks: [{ type: 'chart', fill: true, ...chart }],
});

/**
 * Five verticals' share of each region, every stack a whole: on its
 * side, each `100%` total is written past a bar at the top of the axis, where
 * the canvas edge used to cut its `%`.
 */
const SHARE_STACK = {
  type: 'chart',
  kind: 'stacked_bar',
  horizontal: true,
  format: 'percent',
  series: [
    { name: 'Finance', values: [40, 35, 30, 25] },
    { name: 'Public', values: [25, 25, 25, 25] },
    { name: 'Energy', values: [15, 20, 20, 20] },
    { name: 'Health', values: [12, 12, 15, 18] },
    { name: 'Retail', values: [8, 8, 10, 12] },
  ],
  categories: ['EMEA', 'Americas', 'APAC', 'LATAM'],
};

/** Three versions over four quarters: a legend too long for a one-column panel's title row. */
const VERSION_STACK = {
  categories: ['Q1', 'Q2', 'Q3', 'Q4'],
  series: [
    { name: 'Version 6', values: [60, 40, 30, 20] },
    { name: 'Version 5', values: [90, 150, 170, 100] },
    { name: 'Version 3', values: [0, 0, 0, 180] },
  ],
};

const FIXTURE_SPEC = {
  title: 'E2E chart kinds',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Native charts', subtitle: 'Every kind, one slide each' },
    chartSlide('s-bar', 'Bar', {
      kind: 'bar',
      format: 'currency',
      unit: 'EUR',
      target: 30_000_000,
      goal_label: 'Plan',
      series: [
        { name: 'Bookings', values: [11_200_000, 22_400_000, 26_800_000, 16_500_000] },
        { name: 'Renewals', values: [8_100_000, 9_400_000, 10_200_000, 12_700_000], tone: 'muted' },
      ],
      categories: ['Q1', 'Q2', 'Q3', 'Q4'],
    }),
    chartSlide('s-stacked', 'Stacked bar', {
      kind: 'stacked_bar',
      unit: 'users',
      series: [
        { name: '6.4', values: [120, 180, 260, 340, 410, 470] },
        { name: '6.5', values: [0, 40, 130, 260, 380, 450] },
        { name: '6.6', values: [0, 0, 0, 30, 120, 284] },
      ],
      categories: ['Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep'],
    }),
    chartSlide('s-line', 'Line', {
      kind: 'line',
      format: 'percent',
      series: [
        { name: 'NRR', values: [71, 74, 76, 79, 81, 83, 84, 86] },
        { name: 'GRR', values: [88, 89, 89, 90, 91, 91, 92, 93] },
      ],
      categories: ['Q1 24', 'Q2 24', 'Q3 24', 'Q4 24', 'Q1 25', 'Q2 25', 'Q3 25', 'Q4 25'],
    }),
    chartSlide('s-area', 'Area', {
      kind: 'area',
      stacked: true,
      series: [
        { name: 'Community', values: [4200, 4800, 5300, 6100, 6900] },
        { name: 'Enterprise', values: [600, 900, 1300, 1800, 2400] },
      ],
      categories: ['2021', '2022', '2023', '2024', '2025'],
    }),
    chartSlide('s-pie', 'Pie', {
      kind: 'pie',
      format: 'percent',
      series: [{ name: 'Share', values: [46, 28, 16, 10] }],
      categories: ['EMEA', 'Americas', 'APAC', 'Other'],
    }),
    chartSlide('s-donut', 'Donut', {
      kind: 'donut',
      format: 'currency',
      unit: 'USD',
      series: [{ name: 'ARR', values: [12_400_000, 8_300_000, 5_100_000, 2_700_000, 1_500_000] }],
      categories: ['Finance', 'Public', 'Energy', 'Health', 'Retail'],
    }),
    chartSlide('s-gauge', 'Gauge', {
      kind: 'gauge',
      format: 'percent',
      series: [{ name: 'NRR', values: [86] }],
      categories: ['NRR'],
    }),
    chartSlide('s-combo', 'Combo', {
      kind: 'combo',
      format: 'currency',
      unit: 'EUR',
      target: 550_000,
      series: [
        { name: 'Pipeline', kind: 'bar', values: [320_000, 410_000, 470_000, 527_100] },
        { name: 'Closed', kind: 'line', values: [140_000, 190_000, 260_000, 310_000] },
      ],
      categories: ['Q1', 'Q2', 'Q3', 'Q4'],
    }),
    chartSlide('s-horizontal', 'Horizontal bar', {
      kind: 'bar',
      horizontal: true,
      // Drawn straight on the slide, every other chart on its card.
      framed: false,
      unit: 'h',
      target: 40,
      goal_label: 'SLA',
      series: [{ name: 'Time to first response', values: [12, 26, 31, 48] }],
      categories: ['P1', 'P2', 'P3', 'P4'],
    }),
    {
      id: 's-dashboard',
      layout: 'dashboard',
      title: 'OpenCTI Adoption & Interest',
      subtitle: 'Q3 2026 - Board of Directors',
      tiles: [
        { label: 'Active instances', value: '12,480', delta: '+9% QoQ', sparkline: [8900, 9600, 10_300, 11_100, 11_700, 12_480] },
        { label: 'EE customers', value: '214', delta: '+18', tone: 'success' },
        { label: 'EE trials', value: '63', sublabel: 'this quarter', tone: 'info' },
        { label: 'GitHub stars', value: '9.1k', delta: '+420', sparkline: [7200, 7600, 8000, 8400, 8800, 9100] },
        { label: 'Filigran Academy', value: '3,340', sublabel: 'certified', tone: 'warning' },
      ],
      panels: [
        {
          title: 'Active instances per quarter',
          block: {
            type: 'chart',
            kind: 'line',
            series: [{ name: 'Instances', values: [6100, 6900, 7800, 8900, 9600, 10_300, 11_100, 12_480] }],
            categories: ['Q4 24', 'Q1 25', 'Q2 25', 'Q3 25', 'Q4 25', 'Q1 26', 'Q2 26', 'Q3 26'],
          },
        },
        {
          title: 'Instances by version',
          block: {
            type: 'chart',
            kind: 'stacked_bar',
            series: [
              { name: '6.4', values: [4200, 3100, 1900] },
              { name: '6.5', values: [3800, 5200, 4600] },
              { name: '6.6', values: [0, 2400, 5980] },
            ],
            categories: ['Q1 26', 'Q2 26', 'Q3 26'],
          },
        },
        {
          title: 'EE trials per quarter',
          block: {
            type: 'chart',
            kind: 'bar',
            series: [{ name: 'Trials', values: [31, 44, 52, 63] }],
            categories: ['Q4 25', 'Q1 26', 'Q2 26', 'Q3 26'],
          },
        },
      ],
    },
    {
      // Where the legend sits: the same one-column stacked panel with
      // its legend under the plot and, as the baseline, on the title's row -
      // where three entries of this length wrap - and a donut with its legend
      // beside the ring.
      id: 's-legends',
      layout: 'dashboard',
      title: 'Legend position',
      subtitle: 'Under the plot, beside it, and the default',
      tiles: [
        { label: 'Installs', value: '1,330' },
        { label: 'Regions', value: '4' },
        { label: 'Versions', value: '3' },
      ],
      panels: [
        {
          title: 'Installs by version',
          block: { type: 'chart', kind: 'stacked_bar', legend_position: 'bottom', ...VERSION_STACK },
        },
        {
          title: 'Share by region',
          block: {
            type: 'chart',
            kind: 'donut',
            format: 'percent',
            legend_position: 'right',
            series: [{ name: 'Share', values: [46, 28, 16, 10] }],
            categories: ['EMEA', 'Americas', 'APAC', 'Other'],
          },
        },
        {
          title: 'Installs by version',
          block: { type: 'chart', kind: 'stacked_bar', ...VERSION_STACK },
        },
      ],
    },
    // A horizontal 100% stack filling a content slide, then the same
    // chart in dashboard panels of span 3 and span 1, at every label size. No
    // subtitle: the second row's panels keep the height a one-column panel of
    // a two-row dashboard has (about 200px of canvas), where even the large
    // text writes its four totals.
    chartSlide('s-share', 'Share by vertical', SHARE_STACK),
    {
      id: 's-share-dashboard',
      layout: 'dashboard',
      title: 'Share by vertical',
      tiles: [
        { label: 'Regions', value: '4' },
        { label: 'Verticals', value: '5' },
      ],
      panels: [
        { title: 'Span 3', span: 3, block: SHARE_STACK },
        { title: 'Span 1', span: 1, block: SHARE_STACK },
        { title: 'Span 1, small text', span: 1, block: { ...SHARE_STACK, label_size: 'sm' } },
        { title: 'Span 1, large text', span: 1, block: { ...SHARE_STACK, label_size: 'lg' } },
      ],
    },
  ],
};

/** 1-based position of each fixture slide on the stage, by its id. */
const SLIDE = {
  title: 1,
  bar: 2,
  stacked: 3,
  line: 4,
  area: 5,
  pie: 6,
  donut: 7,
  gauge: 8,
  combo: 9,
  horizontal: 10,
  dashboard: 11,
  legends: 12,
  share: 13,
  shareDashboard: 14,
} as const;
/** How many slides the fixture deck has: what the stage's counter reads `of`. */
const SLIDE_COUNT = FIXTURE_SPEC.slides.length;

/** A deck of its own for the test that WRITES (the theme switch). */
const THEME_SPEC = {
  title: 'E2E chart theme switch',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 't-title', layout: 'title', title: 'Theme switch' },
    chartSlide('t-bar', 'Bar', {
      kind: 'bar',
      series: [{ name: 'Trials', values: [31, 44, 52, 63] }],
      categories: ['Q4 25', 'Q1 26', 'Q2 26', 'Q3 26'],
    }),
    // The horizontal 100% stack, read in the light scheme once the deck switches.
    chartSlide('t-share', 'Share by vertical', SHARE_STACK),
  ],
};

/** The regional ARR dashboard, once with small tiles and once at the default size. */
const regionTiles = 'ABCDEF'.split('').map((letter) => ({
  value: 'EUR 12.3M',
  label: `Region ${letter} ARR`,
  sublabel: 'Q3 25 EUR 4.5M \u00b7 Q2 EUR 6.7M',
  delta: '+34.5% YoY',
  trend: 'up',
}));
const regionPanels = ['ARR by quarter', 'Net new ARR', 'Pipeline'].map((title) => ({
  title,
  block: { type: 'chart', kind: 'bar', series: [{ name: 'ARR', values: [4.5, 5.2, 6.7, 12.3] }], categories: ['Q4 25', 'Q1 26', 'Q2 26', 'Q3 26'] },
}));
const TILE_SIZE_SPEC = {
  title: 'E2E dashboard tile size',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 'z-small', layout: 'dashboard', title: 'Regional ARR, Q3 2026', tile_size: 'sm', tiles: regionTiles, panels: regionPanels },
    { id: 'z-default', layout: 'dashboard', title: 'Regional ARR, Q3 2026', tiles: regionTiles, panels: regionPanels },
  ],
};

/** The lines each tile's note runs over: the distinct tops of its text's client rects. */
async function noteLines(row: Locator, note: string): Promise<number[]> {
  return row.locator(`.deck-metric ${note}`).evaluateAll((notes) =>
    notes.map((element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      const tops = Array.from(range.getClientRects(), (rect) => rect.top).sort((a, b) => a - b);
      return tops.filter((top, index) => index === 0 || top - tops[index - 1] > 2).length;
    }),
  );
}

/**
 * Two panels half and half: grouped bars over eight quarters with a
 * value below zero, beside a 3-column table of four rows, under six tiles.
 */
const HALF_AND_HALF_TILES = ['ARR', 'Net new', 'NRR', 'Logos', 'Pipeline', 'Win rate'].map((label, index) => ({
  label,
  value: ['€19.2M', '€2.9M', '109%', '37', '€48M', '27%'][index],
}));
const HALF_AND_HALF_PANELS = [
  {
    title: 'Net new ARR by quarter',
    block: {
      type: 'chart',
      kind: 'bar',
      format: 'currency',
      unit: 'EUR',
      categories: ['Q1 25', 'Q2 25', 'Q3 25', 'Q4 25', 'Q1 26', 'Q2 26', 'Q3 26', 'Q4 26'],
      series: [
        { name: 'New logos', values: [1_500_000, 1_800_000, 1_700_000, 2_400_000, 2_700_000, 2_500_000, 3_200_000, 3_400_000] },
        { name: 'Expansion', values: [800_000, 900_000, -300_000, 1_100_000, 1_300_000, 1_200_000, 1_600_000, 1_800_000] },
      ],
    },
  },
  {
    title: 'Top accounts',
    block: {
      type: 'table',
      headers: ['Account', 'Region', 'ARR'],
      rows: [
        ['Acme', 'EMEA', '€2.4M'],
        ['Globex', 'Americas', '€1.9M'],
        ['Initech', 'APAC', '€1.2M'],
        ['Umbrella', 'EMEA', '€0.9M'],
      ],
    },
  },
];
/** The same slide on two equal columns, then on the default three. */
const PANEL_COLUMNS_SPEC = {
  title: 'E2E dashboard panel columns',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 'c-two', layout: 'dashboard', title: 'Net new ARR, FY 2026', panel_columns: 2, tiles: HALF_AND_HALF_TILES, panels: HALF_AND_HALF_PANELS },
    { id: 'c-three', layout: 'dashboard', title: 'Net new ARR, FY 2026', tiles: HALF_AND_HALF_TILES, panels: HALF_AND_HALF_PANELS },
  ],
};

/** A gauge of `value` against `target`, as a dashboard panel holds it. */
const gaugeBlock = (fields: Record<string, unknown> = {}) => ({
  type: 'chart',
  kind: 'gauge',
  target: 10,
  goal_label: 'Target',
  series: [{ name: 'Coverage', values: [8] }],
  categories: ['Coverage'],
  ...fields,
});

/**
 * Gauges in a third of the panel grid: two dials beside a stacked bar, then
 * the three sizes side by side. Its own deck, so the export reads one slide.
 */
const GAUGE_SPEC = {
  title: 'E2E gauge box',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    {
      id: 'g-panels',
      layout: 'dashboard',
      title: 'Exposure coverage',
      tiles: [
        { label: 'Assets', value: '1,204' },
        { label: 'Coverage', value: '80%' },
        { label: 'Spend', value: '€27.5' },
      ],
      panels: [
        { title: 'Coverage', block: gaugeBlock() },
        {
          title: 'Spend',
          block: gaugeBlock({ format: 'currency', target: 40, goal_label: 'Scale', series: [{ name: 'Spend', values: [27.5] }], categories: ['Spend'] }),
        },
        { title: 'Installs by version', block: { type: 'chart', kind: 'stacked_bar', ...VERSION_STACK } },
      ],
    },
    {
      id: 'g-sizes',
      layout: 'dashboard',
      title: 'Gauge sizes',
      tiles: [
        { label: 'Assets', value: '1,204' },
        { label: 'Coverage', value: '80%' },
      ],
      panels: (['sm', 'md', 'lg'] as const).map((size) => ({ title: size, block: gaugeBlock({ size }) })),
    },
  ],
};

/**
 * Each gauge of `slide`, read in one evaluation (page px): the room between
 * its canvas' edges and the box it draws - the dial's half disc and the
 * reading written on its baseline - and the dial's diameter.
 */
const gaugeBoxes = (slide: Locator) =>
  slide.locator('.deck-chart[data-kind="gauge"]').evaluateAll((figures) =>
    figures.map((figure) => {
      const canvas = (figure.querySelector('.deck-chart-canvas') as Element).getBoundingClientRect();
      const rects = (selector: string) =>
        Array.from(figure.querySelectorAll(selector), (element) => element.getBoundingClientRect()).filter((rect) => rect.width > 0);
      const dial = rects('.recharts-pie-sector path');
      const box = [...dial, ...rects('.deck-chart-gauge-value, .deck-chart-gauge-max')];
      return {
        canvasHeight: canvas.height,
        above: Math.min(...box.map((rect) => rect.top)) - canvas.top,
        below: canvas.bottom - Math.max(...box.map((rect) => rect.bottom)),
        diameter: Math.max(...dial.map((rect) => rect.right)) - Math.min(...dial.map((rect) => rect.left)),
      };
    }),
  );

/** A finding of the server-side layout check (`POST /presentations/{id}/layout-check`). */
interface LayoutFinding {
  slide_id: string | null;
  kind: string;
}

/** The panel grid of `slide` and its panels, in page px, read in one evaluation. */
const panelGrid = (slide: Locator) =>
  slide.locator('.deck-dashboard-panels').evaluate((grid) => {
    const box = (element: Element) => {
      const rect = element.getBoundingClientRect();
      return { left: rect.left, right: rect.right, top: rect.top, width: rect.width };
    };
    return { grid: box(grid), panels: Array.from(grid.querySelectorAll('article.deck-panel'), box) };
  });

async function importDeck(apiCtx: APIRequestContext, token: string, spec: object, name: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: { name, mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(spec), 'utf8') },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id } = (await resp.json()) as { id: string };
  return id;
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

/** The viewer's counter button reads `3 / 11` and is named "Slide 3 of 11. Go to slide". */
async function expectStageSlide(toolbar: Locator, current: number): Promise<void> {
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${current} of \\d+\\.`) })).toBeVisible();
}

/**
 * Opens the stage on one slide (the viewer reads its 0-based index from the
 * URL hash, like every reveal deck) and waits for the deck to be ready.
 */
async function openStage(page: Page, deckId: string, slide: number): Promise<Locator> {
  await page.goto(`/presentations/${deckId}#/${slide - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expectStageSlide(toolbar, slide);
  return toolbar;
}

/** The slide on screen. Reveal marks it `present`; there is exactly one. */
function presentSlide(page: Page): Locator {
  return page.locator('.reveal .slides > section.present');
}

/**
 * The chart of the slide on screen, once every drawing on that slide has
 * settled: a chart carries `data-deck-pending` from mount until its figure is
 * complete (drawn, and grown in when it animates), which is also the flag the
 * export waits on before it prints.
 */
async function settledChart(page: Page, kind: string): Promise<Locator> {
  const slide = presentSlide(page);
  await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
  const chart = slide.locator(`.deck-chart[data-kind="${kind}"]`);
  await expect(chart).toBeVisible();
  return chart;
}

/** The values written on the figure (`LabelList` glyphs), in DOM order. */
const valueLabels = (chart: Locator): Locator => chart.locator('.deck-chart-value');

/**
 * The axis ticks, both axes together. Ticks of ONE axis keep their order; the
 * two axes are asserted apart, since which axis Recharts writes first is its
 * own business.
 */
const axisTicks = (chart: Locator): Locator => chart.locator('.recharts-cartesian-axis-tick-value');

/** The legend's entries in order, the target line's name included. */
const legendNames = (chart: Locator): Locator => chart.locator('.deck-chart-legend-name');

/**
 * Every chart of `slide`: the width of its canvas, the stack totals it wrote
 * and how far the right edge of the furthest one runs past the canvas' (page
 * px; negative is inside), read in one evaluation so no box is a round trip
 * stale. The canvas is what clips the drawing, so a total past it is
 * a total the audience sees cut.
 */
const totalsAgainstCanvas = (slide: Locator): Promise<{ width: number; totals: string[]; overrun: number }[]> =>
  slide.locator('.deck-chart').evaluateAll((figures) =>
    figures.map((figure) => {
      const canvas = figure.querySelector('.deck-chart-canvas') as Element;
      const edge = canvas.getBoundingClientRect();
      const totals = Array.from(figure.querySelectorAll('.deck-chart-value[data-role="total"]'));
      return {
        width: edge.width,
        totals: totals.map((el) => el.textContent ?? ''),
        overrun: Math.max(...totals.map((el) => el.getBoundingClientRect().right - edge.right)),
      };
    }),
  );

/** A tooltip row reads the series name and its value, two spans side by side. */
const tooltipRow = (name: string, value: string): RegExp =>
  new RegExp(`^${name}\\s*${value.replace(/[.$]/g, '\\$&')}$`);

/**
 * The colour a `--deck-*` token resolves to on this deck, as the browser
 * writes a computed colour (`rgb(r, g, b)`), so it compares to a computed
 * `fill` - the token itself is stored as whatever the template wrote (`#42caff`).
 */
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

/**
 * The computed `fill` of the first element a locator matches, read in the
 * evaluation that finds it: Recharts keys a bar on its position and replaces it
 * on every frame the bar moves, so an element resolved one round trip earlier
 * may be detached by now - and the browser computes no style at all for a
 * detached element (`""`).
 */
async function computedFill(target: Locator): Promise<string> {
  return target.evaluateAll((elements) => {
    if (elements.length === 0) throw new Error('The locator matched no element');
    return getComputedStyle(elements[0]).fill;
  });
}

/** The `animation-name` of every element a locator matches, read in the evaluation that finds them. */
async function animationNames(target: Locator): Promise<string[]> {
  return target.evaluateAll((elements) => elements.map((element) => getComputedStyle(element).animationName));
}

/** What the commit that dropped a chart canvas' `data-deck-pending` looked like. */
interface SettleRecord {
  /** Value labels the figure had written. */
  labels: number;
  /** Bars the same commit added or removed: Recharts replaces the bars it redraws. */
  bars: number;
}

/**
 * Installed before the page loads: appends a `SettleRecord` for a chart canvas
 * every time its `data-deck-pending` goes. One mutation batch holds one
 * commit's DOM changes, so the bars counted are the ones the commit that
 * dropped the marker replaced.
 */
function recordSettles(): void {
  const records = new Map<Element, SettleRecord[]>();
  (window as unknown as { __deckSettles: Map<Element, SettleRecord[]> }).__deckSettles = records;
  const bars = (node: Node): number =>
    node instanceof Element
      ? Number(node.matches('.recharts-bar-rectangle')) + node.querySelectorAll('.recharts-bar-rectangle').length
      : 0;
  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      const canvas = mutation.target;
      if (mutation.type !== 'attributes' || !(canvas instanceof Element)) continue;
      if (!canvas.matches('.deck-chart-canvas') || canvas.hasAttribute('data-deck-pending')) continue;
      let replaced = 0;
      for (const change of mutations) {
        if (change.type !== 'childList' || !canvas.contains(change.target)) continue;
        change.addedNodes.forEach((node) => (replaced += bars(node)));
        change.removedNodes.forEach((node) => (replaced += bars(node)));
      }
      const record = { labels: canvas.querySelectorAll('.deck-chart-value').length, bars: replaced };
      records.set(canvas, [...(records.get(canvas) ?? []), record]);
    }
  }).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-deck-pending'] });
}

/** What a chart canvas and its figure painted at one DOM change under the canvas. */
interface SurfaceRecord {
  /** The canvas held a drawing (Recharts' wrapper). */
  drawn: boolean;
  /** The canvas still held its ready signal: the build had not settled. */
  pending: boolean;
  canvas: string;
  figure: string;
}

/**
 * Installed before the page loads: appends a `SurfaceRecord` for a chart
 * canvas at every change under it - its ready signal set or dropped, the
 * drawing mounted or replaced - so a surface that follows the build is caught
 * on the change that flipped it.
 */
function recordSurfaces(): void {
  const records = new Map<Element, SurfaceRecord[]>();
  (window as unknown as { __deckSurfaces: Map<Element, SurfaceRecord[]> }).__deckSurfaces = records;
  new MutationObserver((mutations) => {
    const canvases = new Set<Element>();
    for (const mutation of mutations) {
      const target = mutation.target instanceof Element ? mutation.target : mutation.target.parentElement;
      const canvas = target?.closest('.deck-chart-canvas');
      if (canvas) canvases.add(canvas);
    }
    for (const canvas of canvases) {
      const figure = canvas.closest('.deck-chart');
      const record = {
        drawn: canvas.querySelector(':scope > .recharts-wrapper') !== null,
        pending: canvas.hasAttribute('data-deck-pending'),
        canvas: getComputedStyle(canvas).backgroundColor,
        figure: figure ? getComputedStyle(figure).backgroundColor : '',
      };
      records.set(canvas, [...(records.get(canvas) ?? []), record]);
    }
  }).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-deck-pending'] });
}

/** The `SurfaceRecord`s `recordSurfaces` kept for a chart canvas, oldest first. */
async function surfaceRecords(canvas: Locator): Promise<SurfaceRecord[]> {
  return canvas.evaluate(
    (element) =>
      (window as unknown as { __deckSurfaces: Map<Element, SurfaceRecord[]> }).__deckSurfaces.get(element) ?? [],
  );
}

/** The `SettleRecord`s `recordSettles` kept for a chart canvas, oldest first. */
async function settleRecords(canvas: Locator): Promise<SettleRecord[]> {
  return canvas.evaluate(
    (element) =>
      (window as unknown as { __deckSettles: Map<Element, SettleRecord[]> }).__deckSettles.get(element) ?? [],
  );
}

test.describe('Presentations - native charts', () => {
  // One worker, one login, one import of each deck (see the header).
  test.describe.configure({ mode: 'serial' });

  let deckId = '';
  let themeDeckId = '';
  let themeTemplateId = '';
  let tileSizeDeckId = '';
  let panelColumnsDeckId = '';
  let gaugeDeckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      deckId = await importDeck(apiCtx, token, FIXTURE_SPEC, 'e2e-chart-kinds.json');
      tileSizeDeckId = await importDeck(apiCtx, token, TILE_SIZE_SPEC, 'e2e-dashboard-tile-size.json');
      panelColumnsDeckId = await importDeck(apiCtx, token, PANEL_COLUMNS_SPEC, 'e2e-dashboard-panel-columns.json');
      gaugeDeckId = await importDeck(apiCtx, token, GAUGE_SPEC, 'e2e-gauge-box.json');
      const templateName = `E2E chart theme template ${Date.now()}`;
      themeTemplateId = await createTwoSchemeTemplate(apiCtx, token, templateName);
      themeDeckId = await importDeckOnTemplate(apiCtx, token, THEME_SPEC, templateName, 'e2e-chart-theme.json');
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    await deleteDeck(deckId);
    await deleteDeck(tileSizeDeckId);
    await deleteDeck(panelColumnsDeckId);
    await deleteDeck(gaugeDeckId);
    await deleteDeck(themeDeckId);
    await deleteTemplate(themeTemplateId);
  });

  test('should draw a bar chart in the palette with formatted values, a named target line and a legend', async ({
    page,
  }) => {
    await openStage(page, deckId, SLIDE.bar);
    const chart = await settledChart(page, 'bar');

    // Two series over four quarters: eight bars, each labelled with its amount
    // written compact in the currency's sign (`€16.5M`, never `16500000`).
    await expect(chart.locator('.recharts-bar-rectangle')).toHaveCount(8);
    await expect(valueLabels(chart)).toHaveText([
      '€11.2M',
      '€22.4M',
      '€26.8M',
      '€16.5M',
      '€8.1M',
      '€9.4M',
      '€10.2M',
      '€12.7M',
    ]);

    // The value axis reaches the target (30M) and writes its ticks compact too.
    await expect(axisTicks(chart)).toContainText(['Q1', 'Q2', 'Q3', 'Q4']);
    await expect(axisTicks(chart)).toContainText(['€0', '€30M']);

    // The legend names both series and the target line by its label; the
    // target is a reading aid, not a series, so it has no toggle.
    await expect(legendNames(chart)).toHaveText(['Bookings', 'Renewals', 'Plan']);
    await expect(chart.locator('.deck-chart-legend-target')).toHaveCount(1);
    await expect(chart.locator('.deck-chart-legend-item[aria-pressed]')).toHaveCount(2);

    // The target is drawn as a horizontal line on a vertical bar chart.
    await expect(chart.locator('.deck-chart-target[data-axis="y"]')).toHaveCount(1);

    // Colours come from the deck's tokens: the first untoned series takes the
    // template's first chart colour, a `muted` series its semantic colour.
    const bars = chart.locator('.recharts-bar-rectangle path, .recharts-bar-rectangle rect');
    expect(await computedFill(bars.nth(0))).toBe(await resolvedToken(page, '--deck-chart-1'));
    expect(await computedFill(bars.nth(4))).toBe(await resolvedToken(page, '--deck-muted'));
  });

  test('should raise a tooltip under the pointer and let a legend click hide a series, never the last one', async ({
    page,
  }) => {
    await openStage(page, deckId, SLIDE.bar);
    const chart = await settledChart(page, 'bar');
    const bars = chart.locator('.recharts-bar-rectangle');
    const canvas = chart.locator('.deck-chart-canvas');

    // Hovering a bar raises the tooltip for its category: the series names and
    // the values written the way the labels are.
    await bars.nth(0).hover();
    const tooltip = chart.locator('.deck-chart-tooltip');
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText('Q1');
    await expect(tooltip.getByRole('listitem')).toHaveText([
      tooltipRow('Bookings', '€11.2M'),
      tooltipRow('Renewals', '€8.1M'),
    ]);

    // The tooltip sits inside the figure, next to the pointer - not off the
    // slide, whatever the CSS scale the stage draws the canvas at.
    const [tooltipBox, canvasBox] = await Promise.all([tooltip.boundingBox(), canvas.boundingBox()]);
    expect(tooltipBox).not.toBeNull();
    expect(canvasBox).not.toBeNull();
    expect(tooltipBox!.x).toBeGreaterThanOrEqual(canvasBox!.x - 1);
    expect(tooltipBox!.x + tooltipBox!.width).toBeLessThanOrEqual(canvasBox!.x + canvasBox!.width + 1);
    expect(tooltipBox!.y).toBeGreaterThanOrEqual(canvasBox!.y - 1);
    expect(tooltipBox!.y + tooltipBox!.height).toBeLessThanOrEqual(canvasBox!.y + canvasBox!.height + 1);

    // A legend click hides its series: the bars go, the entry is pressed off
    // and greyed, the tooltip no longer lists it.
    const renewals = chart.getByRole('button', { name: 'Renewals' });
    const bookings = chart.getByRole('button', { name: 'Bookings' });
    await renewals.click();
    await expect(renewals).toHaveAttribute('aria-pressed', 'false');
    await expect(renewals).toHaveAttribute('data-hidden', 'true');
    await expect(bars).toHaveCount(4);
    await bars.nth(1).hover();
    await expect(tooltip.getByRole('listitem')).toHaveText([tooltipRow('Bookings', '€22.4M')]);

    // The last visible series cannot be hidden: an empty chart says nothing.
    await bookings.click();
    await expect(bookings).toHaveAttribute('aria-pressed', 'true');
    await expect(bars).toHaveCount(4);

    // Clicking the hidden entry again brings its series back.
    await renewals.click();
    await expect(renewals).toHaveAttribute('aria-pressed', 'true');
    await expect(bars).toHaveCount(8);
  });

  test('should write one total per stack, a category whose top series is zero included, and the segments that fit', async ({
    page,
  }) => {
    await openStage(page, deckId, SLIDE.stacked);
    const chart = await settledChart(page, 'stacked_bar');

    // Three versions over six months; a zero value draws no bar (four of them).
    await expect(chart.locator('.recharts-bar-rectangle')).toHaveCount(14);
    // One total per stack, with the unit - and the first three months, whose
    // top series (6.6) is still at zero, are labelled all the same.
    await expect(chart.locator('.deck-chart-value[data-role="total"]')).toHaveText([
      '120 users',
      '220 users',
      '390 users',
      '630 users',
      '910 users',
      '1,204 users',
    ]);
    // A segment tall enough for its value writes it inside; April's
    // lone segment IS its total and is written once.
    await expect(chart.locator('.deck-chart-value[data-role="segment"]').filter({ hasText: /^470 users$/ })).toHaveCount(1);
    await expect(chart.locator('.deck-chart-value[data-role="segment"][data-category="0"]')).toHaveCount(0);
    // Whatever is not written is counted on the facts the layout check reads.
    const facts = chart.locator('.deck-chart-facts');
    const asked = Number(await facts.getAttribute('data-values-asked'));
    const written = Number(await facts.getAttribute('data-values-written'));
    expect(written).toBe(await valueLabels(chart).count());
    expect(asked).toBeGreaterThanOrEqual(written);
    // Axis ticks drop the unit: the label carries it once. (The figure is
    // complete once its labels are, so the ticks can be read in one go.)
    await expect(axisTicks(chart)).toContainText(['Apr', 'Sep']);
    const ticks = await axisTicks(chart).allTextContents();
    expect(ticks.some((tick) => tick.includes('users'))).toBe(false);
    await expect(legendNames(chart)).toHaveText(['6.4', '6.5', '6.6']);
  });

  test('should draw line, area, pie and donut charts with their values and legends', async ({ page }) => {
    await openStage(page, deckId, SLIDE.line);
    let chart = await settledChart(page, 'line');
    await expect(chart.locator('.recharts-line-curve')).toHaveCount(2);
    // Every point of both lines is labelled: eight quarters, two series.
    await expect(valueLabels(chart)).toHaveCount(16);
    await expect(valueLabels(chart)).toContainText(['71%', '86%', '88%', '93%']);
    await expect(legendNames(chart)).toHaveText(['NRR', 'GRR']);

    await page.keyboard.press('ArrowRight');
    chart = await settledChart(page, 'area');
    await expect(chart.locator('.recharts-area-area')).toHaveCount(2);
    // A stacked area writes the stack totals with the locale's thousands separator.
    await expect(valueLabels(chart)).toHaveText(['4,800', '5,700', '6,600', '7,900', '9,300']);

    await page.keyboard.press('ArrowRight');
    chart = await settledChart(page, 'pie');
    await expect(chart.locator('.recharts-pie-sector')).toHaveCount(4);
    await expect(valueLabels(chart)).toHaveText(['46%', '28%', '16%', '10%']);
    // A pie names its slices in the legend, one entry per category.
    await expect(legendNames(chart)).toHaveText(['EMEA', 'Americas', 'APAC', 'Other']);

    await page.keyboard.press('ArrowRight');
    chart = await settledChart(page, 'donut');
    await expect(chart.locator('.recharts-pie-sector')).toHaveCount(5);
    await expect(valueLabels(chart)).toHaveText(['$12.4M', '$8.3M', '$5.1M', '$2.7M', '$1.5M']);
    await expect(legendNames(chart)).toHaveText(['Finance', 'Public', 'Energy', 'Health', 'Retail']);
    // A currency donut writes the whole in its hole.
    await expect(chart.locator('.deck-chart-center-value')).toHaveText('$30M');
  });

  test('should read a gauge against its maximum and draw a combo with bars, a line and a target', async ({
    page,
  }) => {
    await openStage(page, deckId, SLIDE.gauge);
    let chart = await settledChart(page, 'gauge');
    // A percent dial with no target reads out of 100; the gauge has no legend.
    await expect(chart.locator('.deck-chart-gauge-value')).toHaveText('86%');
    await expect(chart.locator('.deck-chart-gauge-max')).toHaveText('/ 100%');
    await expect(chart.locator('.deck-chart-legend')).toHaveCount(0);

    await page.keyboard.press('ArrowRight');
    chart = await settledChart(page, 'combo');
    await expect(chart.locator('.recharts-bar-rectangle')).toHaveCount(4);
    await expect(chart.locator('.recharts-line-curve')).toHaveCount(1);
    // The bars and the line both write their values; the line runs
    // well under the bars' tops, so each sits over its point.
    await expect(chart.locator('.deck-chart-value[data-series="0"]')).toHaveText(['€320k', '€410k', '€470k', '€527.1k']);
    await expect(chart.locator('.deck-chart-value[data-series="1"]')).toHaveText(['€140k', '€190k', '€260k', '€310k']);
    await expect(chart.locator('.deck-chart-value[data-placement]')).toHaveCount(0);
    // A target with no label is named in the legend by its value.
    await expect(legendNames(chart)).toHaveText(['Pipeline', 'Closed', '€550k']);
    await expect(chart.locator('.deck-chart-target[data-axis="y"]')).toHaveCount(1);
  });

  test('should turn a horizontal bar chart on its side, target line included', async ({ page }) => {
    await openStage(page, deckId, SLIDE.horizontal);
    const chart = await settledChart(page, 'bar');
    await expect(chart.locator('.recharts-bar-rectangle')).toHaveCount(4);
    // A one-character unit is glued to the number.
    await expect(valueLabels(chart)).toHaveText(['12h', '26h', '31h', '48h']);
    // The categories sit on the vertical axis, the values along the bottom.
    await expect(axisTicks(chart)).toContainText(['P1', 'P4']);
    await expect(axisTicks(chart)).toContainText(['0', '60']);
    // The target is a VERTICAL line across the bars, named by its label.
    await expect(chart.locator('.deck-chart-target[data-axis="x"]')).toHaveCount(1);
    await expect(legendNames(chart)).toHaveText(['Time to first response', 'SLA']);
  });

  test('should keep every total of a horizontal 100% stack inside its canvas, on a content slide and in panels of span 3 and span 1', async ({
    page,
  }) => {
    // A stack reaching the top of its axis writes its total past the plot's
    // right edge, in the room the layout keeps for it: the whole
    // `100%`, its `%` included, stands inside the canvas that clips the figure.
    // Every figure of the slide is read once it has written its four totals -
    // the entry build writes them last - and measured in that same state.
    const whole = ['100%', '100%', '100%', '100%'];
    const totalsWritten = async (slide: Locator, figures: number) => {
      await expect.poll(async () => (await totalsAgainstCanvas(slide)).map((figure) => figure.totals)).toEqual(
        Array.from({ length: figures }, () => whole),
      );
      return totalsAgainstCanvas(slide);
    };
    const toolbar = await openStage(page, deckId, SLIDE.share);
    await settledChart(page, 'stacked_bar');
    const [content] = await totalsWritten(presentSlide(page), 1);
    expect(content.totals).toEqual(whole);
    expect(content.overrun, 'content slide').toBeLessThan(0);

    // The same stack in a dashboard: across the grid (span 3) and in one
    // column (span 1) at every label size, each panel's canvas its own edge.
    await page.keyboard.press('ArrowRight');
    await expectStageSlide(toolbar, SLIDE.shareDashboard);
    const slide = presentSlide(page);
    await expect(slide.locator('article.deck-panel')).toHaveCount(4);
    const panels = await totalsWritten(slide, 4);
    const widths = panels.map((panel) => Math.round(panel.width));
    expect(widths).toEqual([widths[0], widths[1], widths[1], widths[1]]);
    expect(widths[0]).toBeGreaterThan(widths[1] * 2);
    for (const [index, panel] of panels.entries()) {
      expect(panel.totals, `panel ${index}`).toEqual(whole);
      expect(panel.overrun, `panel ${index}`).toBeLessThan(0);
    }
  });

  test('should lay the board dashboard out on one slide without shrinking it', async ({ page }) => {
    const toolbar = await openStage(page, deckId, SLIDE.dashboard);
    // The dashboard is ONE slide of the deck - its panels are not read by
    // reveal as a vertical stack of slides.
    await expectStageSlide(toolbar, SLIDE.dashboard);
    await expect(
      toolbar.getByRole('button', { name: new RegExp(`^Slide ${SLIDE.dashboard} of ${SLIDE_COUNT}\\.`) }),
    ).toBeVisible();
    const slide = presentSlide(page);
    await expect(slide).not.toHaveClass(/stack/);
    await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);

    // Five KPI tiles on top, two of them with a sparkline; three panels below.
    const dashboard = slide.locator('.deck-dashboard');
    await expect(dashboard.locator('.deck-dashboard-tiles .deck-metric')).toHaveCount(5);
    await expect(dashboard.locator('svg.deck-sparkline')).toHaveCount(2);
    const panels = dashboard.locator('article.deck-panel');
    await expect(panels).toHaveCount(3);
    await expect(panels.locator('.deck-panel-title')).toHaveText([
      'Active instances per quarter',
      'Instances by version',
      'EE trials per quarter',
    ]);

    // Five tiles and three panels fit 16:9 without the fit-to-height shrink:
    // the body host publishes `data-fit` ONLY when it had to scale down.
    await expect(slide.locator('.deck-fit')).toHaveCount(1);
    await expect(slide.locator('.deck-fit')).not.toHaveAttribute('data-fit', /.*/);

    // Each panel draws its chart and reads the room it has: the narrow line
    // panel thins its eight quarters to the ticks that fit and writes each
    // point's value in the smaller sizes of its text scale, the
    // stacked panel writes one total per stack (the first quarter, whose 6.6
    // count is zero, included) - compact, as its axis is - the bar
    // panel writes each count.
    const stacked = panels.nth(1).locator('.deck-chart[data-kind="stacked_bar"]');
    await expect(stacked.locator('.deck-chart-value[data-role="total"]')).toHaveText(['8k', '10.7k', '12.5k']);

    const bar = panels.nth(2).locator('.deck-chart[data-kind="bar"]');
    await expect(valueLabels(bar)).toHaveText(['31', '44', '52', '63']);

    // Every drawing on the slide is complete (the labels above are written
    // only then), so the line panel's ticks can be read in one go.
    const line = panels.nth(0).locator('.deck-chart[data-kind="line"]');
    await expect(line.locator('.recharts-line-curve')).toHaveCount(1);
    await expect(valueLabels(line)).toHaveText(['6.1k', '6.9k', '7.8k', '8.9k', '9.6k', '10.3k', '11.1k', '12.5k']);
    const lineTicks = (await axisTicks(line).allTextContents()).filter((tick) => /^Q\d \d\d$/.test(tick));
    expect(lineTicks.length).toBeGreaterThanOrEqual(2);
    expect(lineTicks.length).toBeLessThan(8);
    expect(lineTicks[0]).toBe('Q4 24');
    expect(lineTicks[lineTicks.length - 1]).toBe('Q3 26');
  });

  test('should keep a comparison sublabel on one line in a row of six small tiles', async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    const toolbar = await openStage(page, tileSizeDeckId, 1);
    let slide = presentSlide(page);
    await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
    let row = slide.locator('.deck-dashboard-tiles');
    await expect(row).toHaveAttribute('data-size', 'sm');
    await expect(row.locator('.deck-metric')).toHaveCount(6);
    // Every note of the small row holds on one line, and the body is not shrunk.
    expect(await noteLines(row, '.deck-metric-sublabel')).toEqual([1, 1, 1, 1, 1, 1]);
    expect(await noteLines(row, '.deck-metric-label')).toEqual([1, 1, 1, 1, 1, 1]);
    expect(await noteLines(row, '.deck-metric-delta .deck-icon-text-label')).toEqual([1, 1, 1, 1, 1, 1]);
    await expect(slide.locator('.deck-fit')).not.toHaveAttribute('data-fit', /.*/);
    const smallRow = await row.evaluate((element) => (element as HTMLElement).offsetHeight);

    // The same row at the default size: the sublabel wraps, which is why the field exists.
    await page.keyboard.press('ArrowRight');
    await expectStageSlide(toolbar, 2);
    slide = presentSlide(page);
    row = slide.locator('.deck-dashboard-tiles');
    await expect(row).toHaveAttribute('data-size', 'md');
    expect(await noteLines(row, '.deck-metric-sublabel')).toEqual([2, 2, 2, 2, 2, 2]);
    expect(await row.evaluate((element) => (element as HTMLElement).offsetHeight)).toBeGreaterThan(smallRow);

    // The server's own render agrees: the layout check finds nothing on the small row's slide.
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const response = await apiCtx.post(`/api/v1/presentations/${tileSizeDeckId}/layout-check`, {
        headers: { Authorization: `Bearer ${token}` },
        timeout: 150_000,
      });
      expect(response.ok(), `layout check failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      const report = (await response.json()) as {
        skipped: string | null;
        findings: { slide_index: number; kind: string; message: string }[];
      };
      expect(report.skipped).toBeNull();
      const onSmall = report.findings.filter((finding) => finding.slide_index === 0);
      expect(onSmall, JSON.stringify(onSmall)).toEqual([]);
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should draw two panels half and half on panel_columns 2, and a third of the grid each without it', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    const toolbar = await openStage(page, panelColumnsDeckId, 1);
    let slide = presentSlide(page);
    await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
    await expect(slide.locator('.deck-dashboard-tiles .deck-metric')).toHaveCount(6);
    await expect(slide.locator('.deck-dashboard-panels')).toHaveAttribute('data-columns', '2');
    await expect(slide.locator('.deck-panel table tbody tr')).toHaveCount(4);
    await expect(slide.locator('.deck-chart[data-kind="bar"] .recharts-bar-rectangle')).toHaveCount(16);

    // Side by side on one row, at one width, the two together spanning the grid.
    const two = await panelGrid(slide);
    expect(two.panels).toHaveLength(2);
    const [chart, table] = two.panels;
    expect(Math.abs(chart.width - table.width)).toBeLessThan(1);
    expect(Math.abs(chart.top - table.top)).toBeLessThan(1);
    expect(Math.abs(chart.left - two.grid.left)).toBeLessThan(1);
    expect(Math.abs(table.right - two.grid.right)).toBeLessThan(1);
    // Six tiles and the two panels fit without the fit-to-height shrink.
    await expect(slide.locator('.deck-fit')).not.toHaveAttribute('data-fit', /.*/);

    // The same panels without the field: the three columns of every dashboard,
    // a third of the grid each and the last column left empty.
    await page.keyboard.press('ArrowRight');
    await expectStageSlide(toolbar, 2);
    slide = presentSlide(page);
    await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
    await expect(slide.locator('.deck-dashboard-panels')).toHaveAttribute('data-columns', '3');
    const three = await panelGrid(slide);
    expect(Math.abs(three.panels[0].width - three.panels[1].width)).toBeLessThan(1);
    expect(three.panels[0].width / chart.width).toBeGreaterThan(0.6);
    expect(three.panels[0].width / chart.width).toBeLessThan(0.7);
    expect(three.grid.right - three.panels[1].right).toBeGreaterThan(three.panels[0].width);
  });

  test('should report no overflow on the half-and-half dashboard in the layout check, on either grid', async () => {
    // A server-side render of every slide at 1920 x 1080 through the platform's headless browser.
    test.setTimeout(180_000);
    const { apiCtx, token } = await createAdminApiContext();
    let findings: LayoutFinding[];
    let skipped: string | null;
    try {
      const response = await apiCtx.post(`/api/v1/presentations/${panelColumnsDeckId}/layout-check`, {
        headers: { Authorization: `Bearer ${token}` },
        timeout: 150_000,
      });
      expect(response.ok(), `layout check failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      ({ findings, skipped } = (await response.json()) as { findings: LayoutFinding[]; skipped: string | null });
    } finally {
      await apiCtx.dispose();
    }
    const dump = JSON.stringify(findings, null, 1);
    expect(skipped, dump).toBeNull();
    for (const slideId of ['c-two', 'c-three']) {
      const kinds = findings.filter((finding) => finding.slide_id === slideId).map((finding) => finding.kind);
      for (const kind of ['overflow', 'clipped', 'fit', 'shrunk']) expect(kinds, `${slideId} ${kind}: ${dump}`).not.toContain(kind);
    }
  });

  test('should export the chart at the width each grid gives it in the editable PPTX', async () => {
    test.setTimeout(180_000);
    const { apiCtx, token } = await createAdminApiContext();
    let archive: Buffer;
    try {
      const response = await apiCtx.post(`/api/v1/presentations/${panelColumnsDeckId}/export`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { format: 'pptx_native' },
        timeout: 150_000,
      });
      expect(response.ok(), `export failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      archive = await response.body();
    } finally {
      await apiCtx.dispose();
    }
    const entries = unzip(archive);
    const [half] = slideChartBoxes(entries, 1);
    const [third] = slideChartBoxes(entries, 2);
    expect(half).toBeDefined();
    expect(third).toBeDefined();
    // The export measures the rendered DOM: the half-width panel's chart is
    // about 1.5 times the one-third panel's, and stays in the left half.
    expect(half.width / third.width).toBeGreaterThan(1.3);
    expect(half.width / third.width).toBeLessThan(1.8);
    expect(half.left + half.width).toBeLessThan(0.5);
  });

  test('should stand a gauge in the middle of its panel, sm, md and lg as three diameters', async ({ page }) => {
    const toolbar = await openStage(page, gaugeDeckId, 1);
    let slide = presentSlide(page);
    await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
    await expect(slide.locator('.deck-chart-gauge-value')).toHaveText(['8', '€27.5']);
    await expect(slide.locator('.deck-chart-gauge-max')).toHaveText(['Target 10', 'Scale €40']);
    // As much room above the dial and its reading as below them, in a panel
    // much taller than a half disc is.
    const dials = await gaugeBoxes(slide);
    expect(dials).toHaveLength(2);
    for (const [index, dial] of dials.entries()) {
      expect(dial.above, `gauge ${index}`).toBeGreaterThan(dial.canvasHeight / 5);
      expect(Math.abs(dial.above - dial.below), `gauge ${index}`).toBeLessThanOrEqual(3);
    }

    await page.keyboard.press('ArrowRight');
    await expectStageSlide(toolbar, 2);
    slide = presentSlide(page);
    await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
    const sizes = await gaugeBoxes(slide);
    expect(sizes).toHaveLength(3);
    const [sm, md, lg] = sizes.map((dial) => dial.diameter);
    expect(md / sm).toBeGreaterThan(1.3);
    expect(lg / md).toBeGreaterThan(1.15);
    for (const [index, dial] of sizes.entries()) {
      expect(Math.abs(dial.above - dial.below), `size ${index}`).toBeLessThanOrEqual(3);
    }
  });

  test('should export each gauge as a picture of the canvas it stands centred in, where the slide shows it', async ({
    page,
  }) => {
    test.setTimeout(180_000);
    await openStage(page, gaugeDeckId, 1);
    const slide = presentSlide(page);
    await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
    // The canvases as shares of the slide, the way the PPTX places a picture.
    const canvases = await slide.evaluate((section) => {
      const frame = section.getBoundingClientRect();
      return Array.from(section.querySelectorAll('.deck-chart[data-kind="gauge"] .deck-chart-canvas'), (canvas) => {
        const rect = canvas.getBoundingClientRect();
        return {
          left: (rect.left - frame.left) / frame.width,
          top: (rect.top - frame.top) / frame.height,
          width: rect.width / frame.width,
          height: rect.height / frame.height,
        };
      });
    });
    expect(canvases).toHaveLength(2);
    const { apiCtx, token } = await createAdminApiContext();
    let archive: Buffer;
    try {
      const response = await apiCtx.post(`/api/v1/presentations/${gaugeDeckId}/export`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { format: 'pptx_native' },
        timeout: 150_000,
      });
      expect(response.ok(), `export failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      archive = await response.body();
    } finally {
      await apiCtx.dispose();
    }
    // PowerPoint has no gauge: each is pasted as a picture of its whole canvas,
    // so the dial stands in the picture where it stands in the panel.
    const pictures = slidePictureBoxes(unzip(archive), 1);
    for (const [index, canvas] of canvases.entries()) {
      const near = pictures.filter((picture) =>
        (['left', 'top', 'width', 'height'] as const).every((side) => Math.abs(picture[side] - canvas[side]) < 0.01),
      );
      expect(near, `gauge ${index}`).toHaveLength(1);
    }
  });

  test('should put a legend under the plot on one centred line, or beside it as a column, where the block says so', async ({
    page,
  }) => {
    await openStage(page, deckId, SLIDE.legends);
    const slide = presentSlide(page);
    await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
    const panels = slide.locator('article.deck-panel');
    await expect(panels).toHaveCount(3);

    /** The boxes of a panel's parts, in page px, with the legend entries' own. */
    const measure = (panel: Locator) =>
      panel.evaluate((article) => {
        const box = (selector: string) => {
          const rect = (article.querySelector(selector) as Element).getBoundingClientRect();
          return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, height: rect.height };
        };
        return {
          title: box('.deck-panel-title'),
          canvas: box('.deck-chart-canvas'),
          entries: Array.from(article.querySelectorAll('.deck-chart-legend-item')).map((entry) => {
            const rect = entry.getBoundingClientRect();
            return { left: rect.left, right: rect.right, top: Math.round(rect.top) };
          }),
        };
      });

    // Under the plot: the title alone on its row, the legend on ONE
    // line under the x-axis labels, centred under the plot...
    const bottom = panels.nth(0);
    await expect(bottom.locator('.deck-chart')).toHaveAttribute('data-legend-position', 'bottom');
    await expect(legendNames(bottom)).toHaveText(['Version 6', 'Version 5', 'Version 3']);
    const under = await measure(bottom);
    expect(under.title.bottom).toBeLessThanOrEqual(under.canvas.top + 0.5);
    expect(new Set(under.entries.map((entry) => entry.top)).size).toBe(1);
    expect(Math.min(...under.entries.map((entry) => entry.top))).toBeGreaterThanOrEqual(under.canvas.bottom - 0.5);
    const entriesCentre = (Math.min(...under.entries.map((e) => e.left)) + Math.max(...under.entries.map((e) => e.right))) / 2;
    expect(Math.abs(entriesCentre - (under.canvas.left + under.canvas.right) / 2)).toBeLessThan(6);
    // ...and the plot taller than the same panel with the default, whose three
    // entries wrap on the title's row beside the title.
    const baseline = panels.nth(2);
    await expect(baseline.locator('.deck-chart')).not.toHaveAttribute('data-legend-position', /.*/);
    const beside = await measure(baseline);
    expect(new Set(beside.entries.map((entry) => entry.top)).size).toBeGreaterThan(1);
    expect(Math.max(...beside.entries.map((entry) => entry.top))).toBeLessThan(beside.canvas.top);
    expect(under.canvas.height).toBeGreaterThan(beside.canvas.height);

    // Beside the plot: a column of entries to the right of the ring, centred
    // on its height, under a title that takes the panel's width.
    const right = panels.nth(1);
    await expect(right.locator('.deck-chart')).toHaveAttribute('data-legend-position', 'right');
    await expect(legendNames(right)).toHaveText(['EMEA', 'Americas', 'APAC', 'Other']);
    const column = await measure(right);
    expect(column.title.bottom).toBeLessThanOrEqual(column.canvas.top + 0.5);
    expect(Math.min(...column.entries.map((entry) => entry.left))).toBeGreaterThanOrEqual(column.canvas.right - 0.5);
    expect(new Set(column.entries.map((entry) => entry.left)).size).toBe(1);
    expect(new Set(column.entries.map((entry) => entry.top)).size).toBe(4);
    const columnTop = Math.min(...column.entries.map((entry) => entry.top));
    const columnBottom = await right.locator('.deck-chart-legend').evaluate((el) => el.getBoundingClientRect().bottom);
    const canvasMiddle = (column.canvas.top + column.canvas.bottom) / 2;
    expect(Math.abs((columnTop + columnBottom) / 2 - canvasMiddle)).toBeLessThan(12);
    // The legend still toggles: a slice hidden, the entry pressed off.
    const other = right.locator('.deck-chart-legend-item').filter({ hasText: 'Other' });
    await other.click();
    await expect(other).toHaveAttribute('aria-pressed', 'false');
    await expect(right.locator('.recharts-pie-sector')).toHaveCount(3);
  });

  test('should build the figure when its slide enters and hold a still on a slide that is laid out but not shown', async ({
    page,
  }) => {
    await openStage(page, deckId, SLIDE.title);
    // Reveal lays the neighbouring slides out ahead of time, so the bar chart
    // is drawn while the title slide is on screen - as a still, its entry not
    // yet counted, or the audience would arrive on a figure already built.
    const barSlide = page.locator('.reveal .slides > section').nth(SLIDE.bar - 1);
    const canvas = barSlide.locator('.deck-chart-canvas');
    await expect(canvas).toHaveCount(1);
    await expect(canvas).not.toHaveAttribute('data-deck-animate', /.*/);

    await page.keyboard.press('ArrowRight');
    await expect(barSlide).toHaveClass(/present/);
    // Entering the slide starts the build: the canvas says so while it runs,
    // and the target line gets its enter animation from the deck stylesheet.
    await expect(canvas).toHaveAttribute('data-deck-animate', 'true');
    // The attribute lands before Recharts has laid the plot out: the target
    // line may not be drawn yet.
    await expect.poll(() => animationNames(barSlide.locator('.deck-chart-target'))).toEqual(['deck-chart-target-in']);
    // ...and the figure settles once the animation has run.
    await expect(barSlide.locator('[data-deck-pending]')).toHaveCount(0);
    await expect(barSlide.locator('.recharts-bar-rectangle')).toHaveCount(8);
  });

  test('should drop data-deck-pending over a figure that has grown in, in a commit that redraws nothing', async ({
    page,
  }) => {
    await page.addInitScript(recordSettles);
    await openStage(page, deckId, SLIDE.title);
    const barSlide = page.locator('.reveal .slides > section').nth(SLIDE.bar - 1);
    const canvas = barSlide.locator('.deck-chart-canvas');
    await page.keyboard.press('ArrowRight');
    // The entry remounts the drawing: wait for that build, not for the still
    // drawn while the slide was laid out off screen.
    await expect(canvas).toHaveAttribute('data-deck-animate', 'true');
    await expect(barSlide.locator('[data-deck-pending]')).toHaveCount(0);
    // When the marker went, every value label was written (Recharts writes them
    // once the series have grown in), and the commit that dropped it replaced no
    // bar: a redraw grows the series in again, the labels hidden meanwhile.
    expect((await settleRecords(canvas)).at(-1)).toEqual({ labels: 8, bars: 0 });
  });

  test("should keep the chart's surface while it builds on entry: a card, or bare when framed is off", async ({
    page,
  }) => {
    await page.addInitScript(recordSurfaces);
    const toolbar = await openStage(page, deckId, SLIDE.title);
    const barSlide = page.locator('.reveal .slides > section').nth(SLIDE.bar - 1);
    const canvas = barSlide.locator('.deck-chart-canvas');
    await page.keyboard.press('ArrowRight');
    await expect(canvas).toHaveAttribute('data-deck-animate', 'true');
    await expect(barSlide.locator('[data-deck-pending]')).toHaveCount(0);

    // The figure stands on the deck's card surface, lit like the other cards.
    const card = await resolvedToken(page, '--deck-card-bg');
    const figure = barSlide.locator('.deck-chart');
    await expect(figure).toHaveAttribute('data-framed', 'true');
    const look = await figure.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        background: style.backgroundColor,
        border: style.borderTopWidth,
        isolation: style.isolation,
        light: getComputedStyle(element, '::after').content,
      };
    });
    expect(look).toEqual({ background: card, border: '1px', isolation: 'isolate', light: '""' });

    // The entry grew the series in with the drawing on screen and its ready
    // signal held - and no change in that build moved the surface: the canvas
    // painted no placeholder under the drawing, the figure kept its card.
    const records = await surfaceRecords(canvas);
    const settled = records.at(-1);
    expect(settled).toMatchObject({ drawn: true, pending: false, figure: card });
    expect(records.some((record) => record.drawn && record.pending)).toBe(true);
    for (const record of records.filter((entry) => entry.drawn)) {
      expect(record).toMatchObject({ canvas: settled?.canvas, figure: card });
    }

    // `framed: false` draws the chart straight on the slide.
    await page.evaluate((index) => {
      window.location.hash = `#/${index}`;
    }, SLIDE.horizontal - 1);
    await expectStageSlide(toolbar, SLIDE.horizontal);
    const bare = await settledChart(page, 'bar');
    await expect(bare).not.toHaveAttribute('data-framed', /.*/);
    expect(
      await bare.evaluate((element) => {
        const style = getComputedStyle(element);
        return { background: style.backgroundColor, border: style.borderTopWidth };
      }),
    ).toEqual({ background: 'rgba(0, 0, 0, 0)', border: '0px' });

    // In a dashboard panel, the panel is the chart's card: its figure draws no
    // box and does not light the panel a second time.
    await page.evaluate((index) => {
      window.location.hash = `#/${index}`;
    }, SLIDE.dashboard - 1);
    await expectStageSlide(toolbar, SLIDE.dashboard);
    const panelFigures = presentSlide(page).locator('.deck-panel .deck-chart');
    await expect(panelFigures).toHaveCount(3);
    expect(
      await panelFigures.first().evaluate((element) => ({
        display: getComputedStyle(element).display,
        light: getComputedStyle(element, '::after').content,
      })),
    ).toEqual({ display: 'contents', light: 'none' });
  });

  test('should hold data-deck-pending while a legend click redraws the figure', async ({ page }) => {
    await page.addInitScript(recordSettles);
    await openStage(page, deckId, SLIDE.bar);
    const chart = await settledChart(page, 'bar');
    const canvas = chart.locator('.deck-chart-canvas');
    await expect.poll(async () => (await settleRecords(canvas)).length).toBeGreaterThan(0);
    const before = (await settleRecords(canvas)).length;

    // The click redraws the figure in place: Recharts grows the series again,
    // so it is a new build, pending until it has grown in - never a figure
    // reported complete while its values are not written yet.
    await chart.getByRole('button', { name: 'Renewals' }).click();
    await expect.poll(async () => (await settleRecords(canvas)).length).toBeGreaterThan(before);
    await expect(canvas).not.toHaveAttribute('data-deck-pending', /.*/);
    // When the marker went again, the remaining series' four values were
    // written, and the commit that dropped it replaced no bar.
    expect((await settleRecords(canvas)).at(-1)).toEqual({ labels: 4, bars: 0 });
  });

  test('should draw a still in the speaker view previews, never an animation', async ({ page }) => {
    await page.goto(`/presentations/${deckId}/speaker`);
    await expect(page.getByTestId('presentation.speaker')).toBeVisible();
    // The speaker window previews the current and the next slide through
    // `SlidePreview` (`data-deck-preview`), where a chart is a picture: the
    // next slide of the title is the bar chart.
    const previews = page.locator('.deck-root[data-deck-preview="true"]');
    const charts = previews.locator('.deck-chart');
    await expect(charts.first()).toBeVisible();
    await expect(previews.locator('[data-deck-pending]')).toHaveCount(0);
    await expect(previews.locator('[data-deck-animate]')).toHaveCount(0);
    await expect(charts.first().locator('.recharts-bar-rectangle')).toHaveCount(8);
  });

  test.describe('with reduced motion', () => {
    // A browser-context option, not a test option: it reaches the page as the
    // `prefers-reduced-motion: reduce` media query.
    test.use({ contextOptions: { reducedMotion: 'reduce' } });

    test('should draw the figure at once, with no entry animation', async ({ page }) => {
      await openStage(page, deckId, SLIDE.title);
      await page.keyboard.press('ArrowRight');
      const chart = await settledChart(page, 'bar');
      await expect(chart.locator('.deck-chart-canvas')).not.toHaveAttribute('data-deck-animate', /.*/);
      await expect(chart.locator('.recharts-bar-rectangle')).toHaveCount(8);
      // `evaluateAll` does not wait for the target line to be drawn.
      await expect.poll(() => animationNames(chart.locator('.deck-chart-target'))).toEqual(['none']);
    });
  });

  test('should give up on a canvas that gets no room, and build it once its slide is on screen', async ({ page }) => {
    // reveal keeps a slide far from the current one out of the layout
    // (`display: none`), so the dashboard's canvases get no room while the
    // stage opens on the title: nothing draws them, and after STALL_MS of
    // visible time they stop holding the ready signal, marked unfinished.
    const toolbar = await openStage(page, deckId, SLIDE.title);
    const dashboard = page.locator('.reveal .slides > section').nth(SLIDE.dashboard - 1);
    const canvases = dashboard.locator('.deck-chart-canvas');
    await expect(canvases).toHaveCount(3);
    await expect(dashboard.locator('.deck-chart-canvas[data-deck-unfinished="true"]')).toHaveCount(3);
    await expect(dashboard.locator('.deck-chart-canvas[data-deck-pending]')).toHaveCount(0);
    await expect(canvases.locator('.recharts-surface')).toHaveCount(0);

    // Once the slide is on screen the canvases get room: a new build, pending
    // again until it has grown in, and the figure is finished.
    await page.evaluate((index) => {
      window.location.hash = `#/${index}`;
    }, SLIDE.dashboard - 1);
    await expectStageSlide(toolbar, SLIDE.dashboard);
    await expect(dashboard.locator('.deck-chart-canvas[data-deck-unfinished]')).toHaveCount(0);
    await expect(dashboard.locator('[data-deck-pending]')).toHaveCount(0);
    await expect(canvases.locator('.recharts-surface')).toHaveCount(3);
  });

  test("should show the chart's error state, and keep the deck, when the drawing cannot load", async ({ page }) => {
    // A lazy chunk that fails (a network error, a build replaced under an open
    // tab) used to reach the route's error boundary, which replaced the deck.
    await page.route(
      (url) => /\/ChartCanvas[-.]/.test(url.pathname),
      (route) => route.abort('failed'),
    );
    const toolbar = await openStage(page, deckId, SLIDE.bar);
    const chart = presentSlide(page).locator('.deck-chart[data-kind="bar"]');
    await expect(chart.locator('.deck-chart-error')).toHaveText('This chart could not be drawn.');
    const canvas = chart.locator('.deck-chart-canvas');
    await expect(canvas).toHaveAttribute('data-deck-unfinished', 'true');
    await expect(canvas).not.toHaveAttribute('data-deck-pending', /.*/);
    await expect(presentSlide(page).locator('[data-deck-pending]')).toHaveCount(0);
    // The rest of the figure and the deck around it are still there.
    await expect(legendNames(chart)).toHaveText(['Bookings', 'Renewals', 'Plan']);
    await expect(toolbar).toBeVisible();
  });

  test('should recolour the charts from the template palette when the deck switches theme', async ({ page }) => {
    const toolbar = await openStage(page, themeDeckId, 2);
    let chart = await settledChart(page, 'bar');
    const barShape = '.recharts-bar-rectangle path, .recharts-bar-rectangle rect';
    const darkPrimary = await resolvedToken(page, '--deck-chart-1');
    expect(await computedFill(chart.locator(barShape))).toBe(darkPrimary);

    // The theme is a deck setting; the template's palette for the other scheme
    // is served with the deck, so the same series takes the light palette's
    // first colour - no image to regenerate, no colour written in the spec.
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.patch(`/api/v1/presentations/${themeDeckId}`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { theme: 'light' },
      });
      expect(resp.ok(), await resp.text()).toBe(true);
    } finally {
      await apiCtx.dispose();
    }

    await page.reload();
    await expect(page.locator('.reveal.ready')).toBeVisible();
    chart = await settledChart(page, 'bar');
    await expect(page.locator('.deck-root').first()).toHaveAttribute('data-deck-theme', 'light');
    const lightPrimary = await resolvedToken(page, '--deck-chart-1');
    expect(lightPrimary).not.toBe(darkPrimary);
    expect(await computedFill(chart.locator(barShape))).toBe(lightPrimary);

    // The room a horizontal stack keeps for its totals is geometry, not
    // colour: in the light scheme every `100%` stands inside the canvas too.
    await page.evaluate(() => {
      window.location.hash = '#/2';
    });
    await expectStageSlide(toolbar, 3);
    await settledChart(page, 'stacked_bar');
    const whole = ['100%', '100%', '100%', '100%'];
    await expect.poll(async () => (await totalsAgainstCanvas(presentSlide(page))).map((figure) => figure.totals)).toEqual([whole]);
    const [share] = await totalsAgainstCanvas(presentSlide(page));
    expect(share.totals).toEqual(whole);
    expect(share.overrun).toBeLessThan(0);
  });
});
