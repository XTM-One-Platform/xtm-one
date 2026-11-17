import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { SPREAD_ROUNDING_SLACK_PX, SPREAD_ROW_MAX_EXTRA_PX } from '../src/components/presentation/spread-math';

// ---------------------------------------------------------------------------
// Presentations - a table's `size` and `fill`.
//
// A 6 x 6 budget table on a content slide left a band of empty height above
// its footer callout, and nothing in the spec could enlarge its text or
// spread its rows. `size` (sm / md / lg) steps the header, cell and footnote
// type along the deck scale and scales the padding; `fill` hands the block
// the run's free height and the stretched table grows its BODY rows - the
// header keeps its height - each by at most `SPREAD_ROW_MAX_EXTRA_PX`
// (`spread.ts` with the table's own measurer), the rest of the room
// staying below the footnote for the layout audit to measure.
//
// Everything is opt-in: a table that sets neither field renders exactly as
// before. The geometry is read off the viewer the way the audit and the
// editable PPTX read it (`getBoundingClientRect`, computed font sizes), so
// what is asserted here is what the export writes. The deck is imported
// through the API from a bare spec (no LLM call) and deleted afterwards; the
// layout check renders it once in the platform's headless browser.
// ---------------------------------------------------------------------------

const HEADERS = ['Metric', 'Q1', 'Q2', 'Q3', 'Q4', 'Year'];
const FOOTNOTE = 'Figures in EUR M, unaudited.';

/** A 6 x 6 budget table, `rows` body rows of one line each. */
const budgetTable = (rows: number, extra: Record<string, unknown> = {}) => ({
  type: 'table',
  headers: HEADERS,
  rows: Array.from({ length: rows }, (_, row) => [
    `Line ${row + 1}`,
    ...Array.from({ length: 5 }, (_, cell) => `${(row + 1) * (cell + 2) * 1.1}`.slice(0, 5)),
  ]),
  highlight_first_column: true,
  ...extra,
});

/** The table over a one-line footer callout, which leaves the table ~120px of room. */
const FOOTER = { type: 'callout', tone: 'accent', content: 'Q4 carries the renewal cohort.' };

const slide = (id: string, title: string, table: Record<string, unknown>) => ({
  id,
  layout: 'content',
  title,
  blocks: [table],
  footer_block: FOOTER,
});

const FIXTURE_SPEC = {
  title: 'E2E table size and fill',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Tables', subtitle: 'Size and fill' },
    // The type scale, with a footnote to read it on.
    slide('s-plain', 'Budget - as before', budgetTable(6, { footnote: FOOTNOTE })),
    slide('s-md-false', 'Budget - md, fill false', budgetTable(6, { size: 'md', fill: false, footnote: FOOTNOTE })),
    slide('s-sm', 'Budget - sm', budgetTable(6, { size: 'sm', footnote: FOOTNOTE })),
    slide('s-lg', 'Budget - lg', budgetTable(6, { size: 'lg', footnote: FOOTNOTE })),
    // The spread, on the bare table of the issue.
    slide('s-packed', 'Budget - packed', budgetTable(6)),
    slide('s-fill', 'Budget - fill alone', budgetTable(6, { fill: true })),
    slide('s-lg-fill', 'Budget - lg, fill', budgetTable(6, { size: 'lg', fill: true })),
    // Fourteen rows at lg do not fit a 16:9 body: the body shrinks to fit, as for any over-full slide.
    slide('s-lg-overflow', 'Budget - lg, too many rows', budgetTable(14, { size: 'lg' })),
    // Four small rows end early: blocks are sized to their content, the audit never names `fill`.
    slide('s-sm-sparse', 'Budget - sm, four rows', budgetTable(4, { size: 'sm' })),
  ],
};

const position = (id: string): number => FIXTURE_SPEC.slides.findIndex((s) => s.id === id) + 1;

async function importDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'e2e-table-size-fill.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8'),
      },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id } = (await resp.json()) as { id: string };
  return id;
}

async function openStage(page: Page, deckId: string, at: number): Promise<void> {
  await page.goto(`/presentations/${deckId}#/${at - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${at} of \\d+\\.`) })).toBeVisible();
}

type Box = { top: number; bottom: number; height: number };

interface Geometry {
  size: string | null;
  fill: string | null;
  spreadCap: number | null;
  fit: string | null;
  /** Browser px per canvas px: the boxes below are browser px, the computed styles canvas px. */
  scale: number;
  host: Box;
  block: Box;
  wrap: Box;
  footnote: Box | null;
  /** The header row, then the body rows. */
  rows: Box[];
  /** Computed font sizes, canvas px (a computed style is a layout value the stage transform does not touch). */
  fonts: { header: number; cell: number; footnote: number | null };
  cellPadding: { top: number; left: number };
  /** The bottom of the rows actually painted: a row cut by the wrap paints past its edge. */
  clipped: boolean;
}

async function readGeometry(page: Page): Promise<Geometry> {
  const section = page.locator('.reveal .slides > section.present');
  await expect(section.locator('.deck-fit-content > .deck-blocks-slide > .deck-table-block')).toBeVisible();
  return section.evaluate((el) => {
    const box = (node: Element): Box => {
      const r = node.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, height: r.height };
    };
    const frame = el.querySelector<HTMLElement>('.deck-slide');
    const fitHost = el.querySelector<HTMLElement>('.deck-fit');
    const host = el.querySelector<HTMLElement>('.deck-fit-content > .deck-blocks-slide');
    const block = host?.querySelector<HTMLElement>(':scope > .deck-table-block');
    const wrap = block?.querySelector<HTMLElement>(':scope > .deck-table-wrap');
    const table = wrap?.querySelector<HTMLTableElement>(':scope > .deck-table');
    const footnote = block?.querySelector<HTMLElement>(':scope > .deck-table-footnote') ?? null;
    if (!frame || !fitHost || !host || !block || !wrap || !table) throw new Error('No table in the slide body');
    const th = table.querySelector('th') as HTMLElement;
    const td = table.querySelector('td') as HTMLElement;
    const cap = Number.parseFloat(block.dataset.spreadCap ?? '');
    const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
    const rows = Array.from(table.rows).map(box);
    const fontPx = (node: HTMLElement) => Number.parseFloat(getComputedStyle(node).fontSize);
    const wrapBox = box(wrap);
    return {
      size: block.getAttribute('data-size'),
      fill: block.getAttribute('data-fill'),
      spreadCap: Number.isFinite(cap) ? cap : null,
      fit: fitHost.getAttribute('data-fit'),
      scale,
      host: box(host),
      block: box(block),
      wrap: wrapBox,
      footnote: footnote ? box(footnote) : null,
      rows,
      fonts: { header: fontPx(th), cell: fontPx(td), footnote: footnote ? fontPx(footnote) : null },
      cellPadding: { top: Number.parseFloat(getComputedStyle(td).paddingTop), left: Number.parseFloat(getComputedStyle(td).paddingLeft) },
      clipped: rows[rows.length - 1].bottom > wrapBox.bottom + 1,
    };
  });
}

/** Browser px on the scaled stage: a hair of rounding, never a layout difference. */
const TOLERANCE = 2;
const close = (a: number, b: number, tolerance = TOLERANCE): boolean => Math.abs(a - b) <= tolerance;

test.describe('Presentations - a table takes a size and a bounded fill', () => {
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

  test('a table that sets neither field, and one that spells the defaults, draw the same slide', async ({ page }) => {
    await openStage(page, deckId, position('s-plain'));
    const plain = await readGeometry(page);
    await openStage(page, deckId, position('s-md-false'));
    const spelt = await readGeometry(page);

    expect(plain.size).toBe('md');
    expect(plain.fill).toBeNull();
    expect(plain.spreadCap).toBeNull();
    expect(plain.fit).toBeNull();
    // The look a table always had: the small body size, 18 x 28 px of padding, fine-print footnote.
    expect(plain.fonts).toEqual({ header: 24, cell: 24, footnote: 20 });
    expect(plain.cellPadding).toEqual({ top: 18, left: 28 });
    // Packed under the title: the empty band above the footer block.
    expect(plain.host.bottom - plain.block.bottom).toBeGreaterThan(40);
    expect(plain.clipped).toBe(false);

    expect(spelt.fill).toBeNull();
    expect(spelt.spreadCap).toBeNull();
    expect(spelt.fonts).toEqual(plain.fonts);
    for (const [index, row] of spelt.rows.entries()) {
      expect(close(row.top, plain.rows[index].top, 0.5)).toBe(true);
      expect(close(row.bottom, plain.rows[index].bottom, 0.5)).toBe(true);
    }
    expect(close((spelt.footnote as Box).bottom, (plain.footnote as Box).bottom, 0.5)).toBe(true);
  });

  test('size steps the header, cell and footnote type and scales the padding; md stays md', async ({ page }) => {
    await openStage(page, deckId, position('s-plain'));
    const md = await readGeometry(page);
    await openStage(page, deckId, position('s-sm'));
    const sm = await readGeometry(page);
    await openStage(page, deckId, position('s-lg'));
    const lg = await readGeometry(page);

    expect(sm.size).toBe('sm');
    expect(sm.fonts).toEqual({ header: 20, cell: 20, footnote: 20 });
    expect(sm.cellPadding).toEqual({ top: 14.4, left: 22.4 });
    expect(sm.rows[1].height).toBeLessThan(md.rows[1].height);

    expect(lg.size).toBe('lg');
    expect(lg.fonts).toEqual({ header: 28, cell: 28, footnote: 24 });
    expect(lg.cellPadding).toEqual({ top: 22.5, left: 35 });
    // A larger row, at the ratio the body may have been fitted at (lg with a footnote is tight on 16:9).
    expect(lg.rows[0].height / Number.parseFloat(lg.fit ?? '1')).toBeGreaterThan(md.rows[0].height + 8 * md.scale);
  });

  test('fill spreads the body rows over the room up to the readable extra, the header and footnote staying put', async ({
    page,
  }) => {
    await openStage(page, deckId, position('s-packed'));
    const packed = await readGeometry(page);
    await openStage(page, deckId, position('s-fill'));
    const filled = await readGeometry(page);

    expect(packed.fill).toBeNull();
    expect(packed.spreadCap).toBeNull();
    expect(filled.fill).toBe('true');
    expect(filled.fit).toBeNull();
    expect(filled.rows.length).toBe(packed.rows.length);
    expect(filled.clipped).toBe(false);
    // The run hands the block the free height: it starts where the packed one did...
    expect(close(filled.block.top, packed.block.top)).toBe(true);
    expect(close(filled.host.height, packed.host.height)).toBe(true);
    // ...the header row keeps its height, every body row grew, by the same amount...
    expect(close(filled.rows[0].height, packed.rows[0].height)).toBe(true);
    const growth = filled.rows.slice(1).map((row, index) => row.height - packed.rows[index + 1].height);
    for (const extra of growth) {
      expect(extra).toBeGreaterThan(5);
      expect(close(extra, growth[0], 1.5)).toBe(true);
      expect(extra).toBeLessThanOrEqual(SPREAD_ROW_MAX_EXTRA_PX * filled.scale + TOLERANCE);
    }
    // ...and the table's frame ends where the block does (the bare table of the issue carries no footnote).
    expect(close(filled.wrap.bottom, filled.block.bottom)).toBe(true);

    // The bound: the cap `spread.ts` published is the packed block plus one readable extra per body row.
    const n = packed.rows.length;
    const cap = packed.block.height + ((n - 1) * SPREAD_ROW_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX) * packed.scale;
    const tolerance = TOLERANCE + n * packed.scale;
    expect(filled.spreadCap).not.toBeNull();
    expect(close((filled.spreadCap as number) * filled.scale, cap, tolerance)).toBe(true);
    if (cap < filled.host.height - tolerance) {
      // The room is taller than the cap: the block stopped there and the rest stays below it.
      expect(close(filled.block.height, cap, tolerance)).toBe(true);
      expect(filled.host.bottom - filled.block.bottom).toBeGreaterThan(tolerance);
    } else {
      // The cap never binds in a room shorter than it: the block reaches the body's floor.
      expect(close(filled.block.bottom, filled.host.bottom, tolerance)).toBe(true);
    }
  });

  test('lg with fill on the 6 x 6 table: larger cells, no band above the footer block, nothing clipped', async ({
    page,
  }) => {
    await openStage(page, deckId, position('s-packed'));
    const packed = await readGeometry(page);
    await openStage(page, deckId, position('s-lg-fill'));
    const g = await readGeometry(page);
    expect(g.fill).toBe('true');
    expect(g.size).toBe('lg');
    expect(g.fonts.cell).toBe(28);
    expect(g.clipped).toBe(false);
    expect(g.fit).toBeNull();
    expect(g.rows[0].height).toBeGreaterThan(packed.rows[0].height + 8 * packed.scale);
    const n = g.rows.length;
    const tolerance = TOLERANCE + n * g.scale;
    // Either the rows took the room to the floor, or they stopped at the readable cap.
    const atFloor = close(g.block.bottom, g.host.bottom, tolerance);
    const atCap = g.spreadCap !== null && close(g.block.height, g.spreadCap * g.scale, tolerance);
    expect(atFloor || atCap).toBe(true);
  });

  test('the layout check reports the table that does not fit at lg, and no empty band on the filled one', async () => {
    test.setTimeout(180_000);
    const { apiCtx, token } = await createAdminApiContext();
    let report: { skipped: string | null; findings: { slide_id: string | null; kind: string; message: string }[] };
    try {
      const response = await apiCtx.post(`/api/v1/presentations/${deckId}/layout-check`, {
        headers: { Authorization: `Bearer ${token}` },
        timeout: 150_000,
      });
      expect(response.ok(), `layout check failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      report = (await response.json()) as typeof report;
    } finally {
      await apiCtx.dispose();
    }
    const dump = JSON.stringify(report, null, 1);
    expect(report.skipped, dump).toBeNull();
    const on = (slideId: string) => report.findings.filter((finding) => finding.slide_id === slideId);
    // Fourteen lg rows: the body was shrunk to fit (and clipped at the floor, if even that is not enough).
    expect(on('s-lg-overflow').map((finding) => finding.kind), dump).toEqual(
      expect.arrayContaining(['fit']),
    );
    // Blocks are sized to their content: a table that ends early is at most told to
    // take more content, a footer_block takeaway or a merge - never to fill or distribute -
    // and the lg + fill slide of the issue has no finding at all.
    const sparse = on('s-sm-sparse').filter((finding) => finding.kind === 'sparse');
    expect(sparse.length, dump).toBeLessThanOrEqual(1);
    for (const finding of sparse) expect(finding.message, dump).toContain('Add content to the body');
    for (const id of ['s-sm-sparse', 's-lg-fill', 's-fill']) {
      for (const finding of on(id)) {
        expect(finding.message.toLowerCase(), dump).not.toContain('set fill');
        expect(finding.message, dump).not.toContain('distribute');
      }
    }
    expect(on('s-lg-fill'), dump).toEqual([]);
  });
});
