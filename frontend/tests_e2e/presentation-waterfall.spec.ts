import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - the waterfall chart.
//
// A board revenue slide shows the ARR bridge: opening ARR, + new logos,
// + expansion, - churn, = ending ARR. The one check a board member makes is
// that the bars add up, so the platform makes it on save: a bridge whose
// ending is not the sum of its steps is refused with the gap named, unless
// `allow_gap` draws the miss as an "Unexplained" step. Asserted here through
// the real import route and the real viewer: the figure the audience reads
// (floating steps in their tones, signed values, connectors, the tooltip's
// running level) and the refusal the author reads.
//
// One deck imported through the API from a bare spec (no LLM call), owned by
// the admin and deleted afterwards; the file runs SERIALLY for one login (see
// `presentation-charts.spec.ts`). Reads of the figure are auto-retrying
// assertions taken once the chart has dropped `data-deck-pending`.
// ---------------------------------------------------------------------------

const ARR_STEPS = [16_500_000, 1_800_000, 1_300_000, -400_000];
const CATEGORIES = ['Opening ARR', 'New logos', 'Expansion', 'Churn', 'Closing ARR'];

const bridge = (ending: number, extra: Record<string, unknown> = {}) => ({
  type: 'chart',
  kind: 'waterfall',
  fill: true,
  categories: CATEGORIES,
  series: [{ name: 'ARR', values: [...ARR_STEPS, ending] }],
  totals: [0, 4],
  format: 'currency',
  ...extra,
});

const FIXTURE_SPEC = {
  title: 'E2E waterfall',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 'w-title', layout: 'title', title: 'Q3 2026 Quarterly Review' },
    { id: 'w-bridge', layout: 'content', title: 'ARR bridge', blocks: [bridge(19_200_000)] },
    { id: 'w-gap', layout: 'content', title: 'ARR bridge with a gap', blocks: [bridge(19_140_000, { allow_gap: true })] },
  ],
};

const SLIDE = { bridge: 2, gap: 3 } as const;

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

/** The waterfall on the slide on screen, once every drawing there has settled. */
async function settledWaterfall(page: Page): Promise<Locator> {
  const slide = page.locator('.reveal .slides > section.present');
  await expect(slide.locator('[data-deck-pending]')).toHaveCount(0);
  const chart = slide.locator('.deck-chart[data-kind="waterfall"]');
  await expect(chart).toBeVisible();
  return chart;
}

/** The computed `fill` of every bar, read in the evaluation that finds them. */
async function barFills(chart: Locator): Promise<string[]> {
  return chart
    .locator('.recharts-bar-rectangle path')
    .evaluateAll((elements) => elements.map((element) => getComputedStyle(element).fill));
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

test.describe('Presentations - waterfall chart', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await importDeck(apiCtx, token, FIXTURE_SPEC, 'e2e-waterfall.json');
      if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
      deckId = ((await resp.json()) as { id: string }).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    await deleteDeck(deckId);
  });

  test('should refuse a bridge that does not add up, naming the total, both figures and the gap', async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const short = { ...FIXTURE_SPEC, slides: [{ id: 'x', layout: 'content', title: 'Short', blocks: [bridge(19_140_000)] }] };
      const resp = await importDeck(apiCtx, token, short, 'e2e-waterfall-short.json');
      expect(resp.status()).toBe(422);
      const { detail } = (await resp.json()) as { detail: string };
      expect(detail).toContain('"Closing ARR" (categories.4) €19.14M vs running sum €19.2M, gap €0.06M');
      expect(detail).toContain('allow_gap');
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should draw the quarter-over-quarter ARR bridge as one running sum', async ({ page }) => {
    await openStage(page, deckId, SLIDE.bridge);
    const chart = await settledWaterfall(page);

    // Five bars, each labelled with what it says: the totals' levels, the
    // changes with their sign.
    await expect(chart.locator('.recharts-bar-rectangle')).toHaveCount(5);
    await expect(chart.locator('.deck-chart-value')).toHaveText(['€16.5M', '+€1.8M', '+€1.3M', '-€400k', '€19.2M']);
    await expect(chart.locator('.recharts-cartesian-axis-tick-value')).toContainText(CATEGORIES);
    // A dashed connector carries the level from each bar to the next.
    await expect(chart.locator('.deck-chart-connector')).toHaveCount(4);
    // One series of changes needs no legend.
    await expect(chart.locator('.deck-chart-legend')).toHaveCount(0);

    // The totals in the accent, the rises in success, the fall in error.
    const [accent, success, error] = await Promise.all(
      ['--deck-accent', '--deck-success', '--deck-error'].map((token) => resolvedToken(page, token)),
    );
    await expect.poll(() => barFills(chart)).toEqual([accent, success, success, error, accent]);

    // The hover reads the change and the level it brings the bridge to.
    await chart.locator('.recharts-bar-rectangle').nth(1).hover();
    const tooltip = chart.locator('.deck-chart-tooltip');
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText('New logos');
    await expect(tooltip.getByRole('listitem')).toHaveText([/^ARR\s*\+€1\.8M$/, /^Running total\s*€18\.3M$/]);
  });

  test('should draw the miss of a bridge saved with allow_gap as an Unexplained step', async ({ page }) => {
    await openStage(page, deckId, SLIDE.gap);
    const chart = await settledWaterfall(page);
    await expect(chart.locator('.recharts-bar-rectangle')).toHaveCount(6);
    await expect(chart.locator('.deck-chart-value')).toHaveText([
      '€16.5M',
      '+€1.8M',
      '+€1.3M',
      '-€400k',
      '-€60k',
      '€19.1M',
    ]);
    await expect(chart.locator('.recharts-cartesian-axis-tick-value')).toContainText(['Unexplained']);
    const warning = await resolvedToken(page, '--deck-warning');
    await expect.poll(() => barFills(chart).then((fills) => fills[4])).toBe(warning);
  });
});
