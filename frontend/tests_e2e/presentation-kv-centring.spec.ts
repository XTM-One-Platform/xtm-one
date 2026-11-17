import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - a `fill` key / value block centres every pair between its
// rules (#10239).
//
// Each pair carries its own bottom rule, so a spread that grew the GAPS put
// the free height after every rule: each pair sat on the rule below it, far
// under the one above, and the last one was pushed onto the frame's edge. The
// spread now grows the ROWS, each pair centred in its row. For a framed,
// `fill` block at every density (normal, compact, tight), on its own in the
// body and under a gauge in an equal-height column:
//
// 1. for each row, |(text centre - rule above) - (rule below - text centre)|
//    is at most 2 canvas px, the frame's inner edge counting as the rule of
//    the first and last rows;
// 2. every icon's box lies fully inside the frame and inside its row.
//
// On its own in the body the block is stretched (the room is taller than its
// cap), which is asserted so the centring is never measured on packed rows.
// `fill: false` keeps the packed rows.
//
// Geometry is read off the live viewer in canvas px (the stage scale divided
// out). The deck is imported through the API from a bare spec (no LLM call)
// and deleted afterwards; the file runs SERIALLY. Neutral fixture text only.
// ---------------------------------------------------------------------------

/** Canvas px a pair's centre may sit off its row's centre. */
const CENTRE_TOLERANCE = 2;
/** Canvas px: a hair of rounding on the scaled stage. */
const TOLERANCE = 1;

const DENSITIES = ['normal', 'compact', 'tight'] as const;
type Density = (typeof DENSITIES)[number];

const kv = (density: Density, fill: boolean) => ({
  type: 'key_value',
  framed: true,
  fill,
  density,
  items: [
    { label: 'Objective', value: 'First value', icon: 'target' },
    { label: 'Milestone', value: 'Second value', icon: 'flag' },
    { label: 'Trend', value: 'Third value', icon: 'trending-up' },
  ],
});

const gauge = {
  type: 'chart',
  kind: 'gauge',
  series: [{ name: 'Score', values: [72] }],
  categories: ['Score'],
  target: 100,
};

const longList = {
  type: 'list',
  items: Array.from({ length: 10 }, (_, index) => `Point ${index + 1} of the other column`),
};

const FIXTURE_SPEC = {
  title: 'E2E key value centring',
  theme: 'light',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Key value rows', subtitle: 'Centred between their rules' },
    ...DENSITIES.map((density) => ({
      id: `fill-${density}`,
      layout: 'content',
      title: `Key value, fill, ${density}`,
      blocks: [kv(density, true)],
    })),
    ...DENSITIES.map((density) => ({
      id: `column-${density}`,
      layout: 'content',
      title: `Key value under a gauge, ${density}`,
      equal_height: true,
      columns: [[gauge, kv(density, true)], [longList]],
    })),
    { id: 'packed-tight', layout: 'content', title: 'Key value, packed', blocks: [kv('tight', false)] },
  ],
};

const position = (id: string): number => FIXTURE_SPEC.slides.findIndex((s) => s.id === id) + 1;

async function importDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'e2e-kv-centring.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8'),
      },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id } = (await resp.json()) as { id: string };
  return id;
}

async function openStage(page: Page, deckId: string, slide: number): Promise<void> {
  await page.goto(`/presentations/${deckId}#/${slide - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${slide} of \\d+\\.`) })).toBeVisible();
  const present = page.locator('.reveal .slides > section.present');
  await expect(present.locator('[data-deck-pending]')).toHaveCount(0);
  await expect(present.locator('.deck-kv')).toBeVisible();
}

type Box = { top: number; bottom: number; left: number; right: number; height: number };

interface RowGeometry {
  /** The row's band between its rules: the rule above (or the frame's inner top) to the rule below (or the frame's inner bottom). */
  above: number;
  below: number;
  /** The pair's text: the union of the label's and the value's boxes. */
  text: Box;
  icon: Box | null;
  /** The row's padding box height without its rule, and its content height. */
  height: number;
  content: number;
}

interface KvGeometry {
  frame: Box;
  rows: RowGeometry[];
}

/** The `.deck-kv` of the slide on screen, in canvas px (the stage scale divided out). */
async function readKv(page: Page): Promise<KvGeometry> {
  const present = page.locator('.reveal .slides > section.present');
  return present.evaluate((section) => {
    const slide = section.querySelector<HTMLElement>('.deck-slide');
    const root = section.querySelector<HTMLElement>('.deck-kv');
    if (!slide || !root) throw new Error('No key value block on the slide');
    const origin = slide.getBoundingClientRect();
    const scale = origin.width / slide.offsetWidth;
    const box = (r: DOMRect): Box => ({
      top: (r.top - origin.top) / scale,
      bottom: (r.bottom - origin.top) / scale,
      left: (r.left - origin.left) / scale,
      right: (r.right - origin.left) / scale,
      height: r.height / scale,
    });
    const rootCs = getComputedStyle(root);
    const outer = box(root.getBoundingClientRect());
    const frame: Box = {
      ...outer,
      top: outer.top + Number.parseFloat(rootCs.borderTopWidth),
      bottom: outer.bottom - Number.parseFloat(rootCs.borderBottomWidth),
    };
    const rows = Array.from(root.querySelectorAll<HTMLElement>(':scope > .deck-kv-row')).map((row) => {
      const cs = getComputedStyle(row);
      const r = box(row.getBoundingClientRect());
      const rule = Number.parseFloat(cs.borderBottomWidth) || 0;
      const label = row.querySelector('.deck-kv-label .deck-icon-text-label') ?? row.querySelector('.deck-kv-label');
      const value = row.querySelector('.deck-kv-value');
      if (!label || !value) throw new Error('A key value row without its label or value');
      const a = box(label.getBoundingClientRect());
      const b = box(value.getBoundingClientRect());
      const svg = row.querySelector('.deck-kv-label svg');
      const padding = Number.parseFloat(cs.paddingTop) + Number.parseFloat(cs.paddingBottom);
      return {
        above: r.top,
        below: r.bottom - rule,
        text: {
          top: Math.min(a.top, b.top),
          bottom: Math.max(a.bottom, b.bottom),
          left: Math.min(a.left, b.left),
          right: Math.max(a.right, b.right),
          height: Math.max(a.bottom, b.bottom) - Math.min(a.top, b.top),
        },
        icon: svg ? box(svg.getBoundingClientRect()) : null,
        height: r.height - rule,
        content: r.height - rule - padding,
      };
    });
    return { frame, rows };
  });
}

function expectCentred(geometry: KvGeometry, what: string): void {
  const { frame, rows } = geometry;
  expect(rows, what).toHaveLength(3);
  // The frame's inner edge is the first row's rule above and the last row's rule below.
  expect(Math.abs(rows[0].above - frame.top), `${what}: first row against the frame top`).toBeLessThanOrEqual(TOLERANCE);
  expect(Math.abs(frame.bottom - rows[2].below), `${what}: last row against the frame bottom`).toBeLessThanOrEqual(TOLERANCE);
  for (const [index, row] of rows.entries()) {
    const centre = (row.text.top + row.text.bottom) / 2;
    const offset = Math.abs(centre - row.above - (row.below - centre));
    expect(offset, `${what}: row ${index + 1} centred between its rules`).toBeLessThanOrEqual(CENTRE_TOLERANCE);
    expect(row.icon, `${what}: row ${index + 1} draws its icon`).not.toBeNull();
    const icon = row.icon as Box;
    // Fully drawn: inside the frame and inside its own row band.
    expect(icon.top, `${what}: row ${index + 1} icon top`).toBeGreaterThanOrEqual(Math.max(frame.top, row.above) - TOLERANCE);
    expect(icon.bottom, `${what}: row ${index + 1} icon bottom`).toBeLessThanOrEqual(Math.min(frame.bottom, row.below) + TOLERANCE);
    expect(icon.left, `${what}: row ${index + 1} icon left`).toBeGreaterThanOrEqual(frame.left - TOLERANCE);
    expect(icon.right, `${what}: row ${index + 1} icon right`).toBeLessThanOrEqual(frame.right + TOLERANCE);
  }
  // The frame's spacing is symmetric: the first pair as far from the top as the last from the bottom.
  const top = rows[0].text.top - frame.top;
  const bottom = frame.bottom - rows[2].text.bottom;
  expect(Math.abs(top - bottom), `${what}: symmetric inner spacing`).toBeLessThanOrEqual(CENTRE_TOLERANCE * 2);
}

test.describe('Presentations - a fill key value centres each pair between its rules', () => {
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

  for (const density of DENSITIES) {
    test(`${density}: on its own in the body the rows grow and every pair is centred`, async ({ page }) => {
      await openStage(page, deckId, position(`fill-${density}`));
      const geometry = await readKv(page);
      // The rows really grew past their text and padding.
      for (const row of geometry.rows) expect(row.height).toBeGreaterThan(row.text.height + 20);
      // ...and evenly.
      for (const row of geometry.rows) expect(Math.abs(row.height - geometry.rows[0].height)).toBeLessThanOrEqual(TOLERANCE * 2);
      expectCentred(geometry, `fill ${density}`);
    });

    test(`${density}: under a gauge in an equal-height column every pair is centred`, async ({ page }) => {
      await openStage(page, deckId, position(`column-${density}`));
      expectCentred(await readKv(page), `column ${density}`);
    });
  }

  test('fill: false keeps the rows packed', async ({ page }) => {
    await openStage(page, deckId, position('packed-tight'));
    const packed = await readKv(page);
    await openStage(page, deckId, position('fill-tight'));
    const filled = await readKv(page);
    expect(packed.rows).toHaveLength(3);
    for (const [index, row] of packed.rows.entries()) {
      expect(row.height).toBeLessThan(filled.rows[index].height - 10);
    }
  });
});
