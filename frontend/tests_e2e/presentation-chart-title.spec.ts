import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { inflateRawSync } from 'node:zlib';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - a chart titled inside its own frame.
//
// A chart on a content slide had no title of its own: a caption sat under the
// plot, a heading stood outside the card, and a callout wrapped around the
// chart put a title on top but kept the plot at its readable minimum over an
// empty band (the callout handed its height to a logo grid only), so an
// eight-point line chart hid its values. The `chart` block now takes a
// `title`, drawn at the top inside its frame the way a dashboard panel draws
// its own - the legend on the same row, on the right - and a `fill` callout
// hands its height to a `fill` chart too.
//
// This spec pins both on a live deck, the way the layout audit and the PPTX
// export read it (`getBoundingClientRect` of the real DOM, brought back to
// CANVAS px by the stage scale - the `core` project runs at Desktop Chrome's
// 1280 x 720, where the 1920 x 1080 canvas is drawn at about 0.62, while a
// computed padding stays in canvas px):
//
// - the 2 x 2 slide of the brief: each chart's title top-left inside its card,
//   the legend on its row, the plot reaching the card's bottom padding with no
//   empty band, every value written, and the layout check reporting neither a
//   short chart nor hidden values;
// - the same four charts built the old way, a `fill` callout around each: the
//   plot now takes the card's remaining height. What the card leaves an
//   eight-point line - its own padding and head taken - is about the 220
//   canvas px the values need, so whether they are written there is the
//   planner's call (the layout check says so when they are not): the card
//   form is read for the hand-off alone, the title form for the values;
// - the editable PPTX: the title an editable text run on the slide, the chart
//   a native chart part beside it.
//
// The deck is imported through the API from a bare spec (no LLM call), owned
// by the admin and deleted afterwards; the file runs SERIALLY so the import
// and the login happen once (see `presentation-fill.spec.ts`).
// ---------------------------------------------------------------------------

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug'];
const SIGNUPS = [120, 135, 150, 149, 170, 182, 190, 205];
// Well under the signups at every point, so no two values compete for one spot.
const PLAN = [60, 70, 80, 90, 100, 110, 120, 130];

/** The chart of the brief; `withPlan` adds a second series so a legend shows. */
const chart = (title: string, withPlan: boolean) => ({
  type: 'chart',
  kind: 'line',
  title,
  fill: true,
  categories: MONTHS,
  series: [{ name: 'Signups', values: SIGNUPS }, ...(withPlan ? [{ name: 'Plan', values: PLAN }] : [])],
});

/** One cell as it was built before the `chart` block took a `title`: a `fill` card callout titling a `fill` chart (one series: the hand-off is what is read here). */
const card = (title: string) => {
  const { title: _dropped, ...untitled } = chart(title, false);
  return { type: 'callout', variant: 'card', header: 'inline', title, fill: true, children: [untitled] };
};

const TITLES = ['Self-serve signups', 'Sales-led signups', 'EU signups', 'US signups'];

const FIXTURE_SPEC = {
  title: 'E2E chart title',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Charts titled in their frame', subtitle: 'Two by two' },
    {
      id: 's-titled',
      layout: 'content',
      title: 'Monthly signups by segment',
      blocks: [
        { type: 'columns', columns: [[chart(TITLES[0], true)], [chart(TITLES[1], false)]] },
        { type: 'columns', columns: [[chart(TITLES[2], true)], [chart(TITLES[3], false)]] },
      ],
    },
    {
      id: 's-callouts',
      layout: 'content',
      title: 'Monthly signups by segment, in cards',
      blocks: [
        { type: 'columns', columns: [[card(TITLES[0])], [card(TITLES[1])]] },
        { type: 'columns', columns: [[card(TITLES[2])], [card(TITLES[3])]] },
      ],
    },
  ],
};

/** 1-based stage position of a fixture slide, by id. */
const position = (id: string): number => FIXTURE_SPEC.slides.findIndex((slide) => slide.id === id) + 1;

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

/** Opens the viewer on one slide (0-based index in the hash) and waits for its charts to settle. */
async function openStage(page: Page, deckId: string, slide: number): Promise<Locator> {
  await page.goto(`/presentations/${deckId}#/${slide - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${slide} of \\d+\\.`) })).toBeVisible();
  const present = page.locator('.reveal .slides > section.present');
  await expect(present.locator('.deck-chart')).toHaveCount(4);
  await expect(present.locator('[data-deck-pending]')).toHaveCount(0);
  return present;
}

type Box = { top: number; bottom: number; left: number; right: number; height: number; width: number };

interface ChartGeometry {
  title: string | null;
  titleBox: Box | null;
  legendBox: Box | null;
  figure: Box;
  figurePaddingBottom: number;
  canvas: Box;
  values: number;
  /** The `fill` callout around the chart, when there is one, and its bottom padding. */
  card: Box | null;
  cardPaddingBottom: number;
}

interface SlideGeometry {
  /** Browser px per canvas px: the stage scales the 1920 x 1080 canvas to the viewer. */
  scale: number;
  room: Box;
  rows: Box[];
  charts: ChartGeometry[];
}

/**
 * Every box in CANVAS px (the 1920 x 1080 layout the stylesheet is written
 * in), so a computed padding compares with it directly whatever the stage
 * scale is on this viewport.
 */
async function readGeometry(slide: Locator): Promise<SlideGeometry> {
  return slide.evaluate((section) => {
    const frame = section.querySelector<HTMLElement>('.deck-slide');
    if (!frame) throw new Error('No slide frame on the slide');
    // The frame is 1920 layout px wide however the stage is scaled.
    const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
    const box = (el: Element): Box => {
      const r = el.getBoundingClientRect();
      return {
        top: r.top / scale,
        bottom: r.bottom / scale,
        left: r.left / scale,
        right: r.right / scale,
        height: r.height / scale,
        width: r.width / scale,
      };
    };
    const room = section.querySelector('.deck-fit');
    const host = section.querySelector('.deck-fit-content > .deck-blocks-slide');
    if (!room || !host) throw new Error('No content body on the slide');
    const rows = Array.from(host.querySelectorAll(':scope > .deck-columns')).map(box);
    const charts = Array.from(section.querySelectorAll<HTMLElement>('.deck-chart')).map((figure) => {
      const title = figure.querySelector('.deck-chart-head > .deck-chart-title');
      const legend = figure.querySelector('.deck-chart-legend');
      const canvas = figure.querySelector('.deck-chart-canvas');
      if (!canvas) throw new Error('A chart without a canvas');
      const card = figure.closest<HTMLElement>('.deck-callout');
      return {
        title: title ? title.textContent : null,
        titleBox: title ? box(title) : null,
        legendBox: legend ? box(legend) : null,
        figure: box(figure),
        figurePaddingBottom: Number.parseFloat(getComputedStyle(figure).paddingBottom),
        canvas: box(canvas),
        values: figure.querySelectorAll('.deck-chart-value').length,
        card: card ? box(card) : null,
        cardPaddingBottom: card ? Number.parseFloat(getComputedStyle(card).paddingBottom) : 0,
      };
    });
    return { scale, room: box(room), rows, charts };
  });
}

/** Canvas px: a hair of rounding, never a layout difference. */
const TOLERANCE = 2;
const close = (a: number, b: number, tolerance = TOLERANCE): boolean => Math.abs(a - b) <= tolerance;

interface Finding {
  slide_id: string | null;
  kind: string;
  severity: string;
  message: string;
}

interface Report {
  slide_count: number;
  skipped: string | null;
  findings: Finding[];
}

/** The entries of a zip (a PPTX is one), inflated: name -> bytes. Enough for python-pptx's output. */
function zipEntries(archive: Buffer): Map<string, Buffer> {
  const eocd = archive.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  if (eocd < 0) throw new Error('Not a zip archive');
  const count = archive.readUInt16LE(eocd + 10);
  let offset = archive.readUInt32LE(eocd + 16);
  const entries = new Map<string, Buffer>();
  for (let index = 0; index < count; index += 1) {
    const method = archive.readUInt16LE(offset + 10);
    const compressed = archive.readUInt32LE(offset + 20);
    const nameLength = archive.readUInt16LE(offset + 28);
    const extraLength = archive.readUInt16LE(offset + 30);
    const commentLength = archive.readUInt16LE(offset + 32);
    const localOffset = archive.readUInt32LE(offset + 42);
    const name = archive.toString('utf8', offset + 46, offset + 46 + nameLength);
    const start = localOffset + 30 + archive.readUInt16LE(localOffset + 26) + archive.readUInt16LE(localOffset + 28);
    const data = archive.subarray(start, start + compressed);
    entries.set(name, method === 8 ? inflateRawSync(data) : Buffer.from(data));
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

test.describe('Presentations - a chart titled inside its frame', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      deckId = await importDeck(apiCtx, token, FIXTURE_SPEC, 'e2e-chart-title.json');
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    await deleteDeck(deckId);
  });

  test('the 2 x 2: each title top-left in its card, the legend on its row, the plot to the bottom, every value written', async ({
    page,
  }) => {
    const slide = await openStage(page, deckId, position('s-titled'));
    const { scale, room, rows, charts } = await readGeometry(slide);
    expect(scale).toBeGreaterThan(0);

    expect(charts.map((entry) => entry.title)).toEqual(TITLES);
    // The two rows share the body: the second ends on the body's floor.
    expect(rows).toHaveLength(2);
    expect(close(rows[1].bottom, room.bottom, 4)).toBe(true);

    for (const entry of charts) {
      const { titleBox, legendBox, figure, canvas } = entry;
      expect(titleBox).not.toBeNull();
      // Top-left inside the frame: under the card's top edge, at its left padding.
      expect(titleBox!.top).toBeGreaterThan(figure.top);
      expect(titleBox!.top - figure.top).toBeLessThan(40);
      expect(titleBox!.left).toBeGreaterThan(figure.left);
      expect(titleBox!.right).toBeLessThan(figure.left + figure.width * 0.6 + TOLERANCE);
      // The canvas starts under the head and reaches the card's bottom padding:
      // no empty band under the plot.
      expect(canvas.top).toBeGreaterThanOrEqual(titleBox!.bottom - TOLERANCE);
      expect(close(canvas.bottom, figure.bottom - entry.figurePaddingBottom, 4)).toBe(true);
      // Well past the readable minimum (160 canvas px) a callout used to leave it at.
      expect(canvas.height).toBeGreaterThan(190);
      if (legendBox) {
        // The legend on the title's row, on its right.
        expect(legendBox.left).toBeGreaterThan(titleBox!.right);
        expect(legendBox.top).toBeLessThan(titleBox!.bottom);
        expect(legendBox.bottom).toBeGreaterThan(titleBox!.top);
      }
      // Every point is written: eight per series (the layout check below
      // confirms none was hidden for want of room).
      expect(entry.values).toBeGreaterThanOrEqual(8 * (entry.legendBox ? 2 : 1));
    }
    // The two-series charts carry a legend; the lone-series ones none.
    expect(charts.map((entry) => entry.legendBox !== null)).toEqual([true, false, true, false]);
  });

  test('a fill callout around a fill chart hands it the height the card takes', async ({ page }) => {
    const slide = await openStage(page, deckId, position('s-callouts'));
    const { room, rows, charts } = await readGeometry(slide);

    expect(rows).toHaveLength(2);
    expect(close(rows[1].bottom, room.bottom, 4)).toBe(true);
    for (const entry of charts) {
      expect(entry.title).toBeNull();
      expect(entry.card).not.toBeNull();
      // The figure ends at the card's bottom padding, and its plot at the figure's.
      expect(close(entry.figure.bottom, entry.card!.bottom - entry.cardPaddingBottom, 4)).toBe(true);
      expect(close(entry.canvas.bottom, entry.figure.bottom - entry.figurePaddingBottom, 4)).toBe(true);
      // Past the readable minimum (160 canvas px) the card used to leave it at.
      // (Whether the values fit in what the card leaves is the planner's call,
      // see the header; the titled form above is where they are asserted.)
      expect(entry.canvas.height).toBeGreaterThan(190);
    }
  });

  test('the layout check reports no hidden values on the titled slide and no short chart on either', async () => {
    // A server-side render of every slide through the platform's headless browser.
    test.setTimeout(180_000);
    const { apiCtx, token } = await createAdminApiContext();
    let report: Report;
    try {
      const response = await apiCtx.post(`/api/v1/presentations/${deckId}/layout-check`, {
        headers: { Authorization: `Bearer ${token}` },
        timeout: 150_000,
      });
      expect(response.ok(), `layout check failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      report = (await response.json()) as Report;
    } finally {
      await apiCtx.dispose();
    }
    const dump = JSON.stringify(report, null, 1);
    expect(report.skipped, dump).toBeNull();
    expect(report.slide_count, dump).toBe(3);
    const kinds = (slideId: string) =>
      report.findings.filter((finding) => finding.slide_id === slideId).map((finding) => finding.kind);
    // Titled in their frame, the four plots have the room and write every value.
    expect(kinds('s-titled'), dump).not.toContain('chart');
    expect(kinds('s-titled'), dump).not.toContain('values');
    // In cards the plots are handed the room too - never too short - while
    // the values are the planner's call in what a card's head and padding leave.
    expect(kinds('s-callouts'), dump).not.toContain('chart');
  });

  test('the editable PPTX carries the title as a text run on the slide beside a native chart', async () => {
    test.setTimeout(180_000);
    const { apiCtx, token } = await createAdminApiContext();
    let archive: Buffer;
    try {
      // `pptx_native` is the editable deck (shapes measured off the render);
      // plain `pptx` is one picture per slide (`EXPORT_FORMATS`).
      const response = await apiCtx.post(`/api/v1/presentations/${deckId}/export`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { format: 'pptx_native' },
        timeout: 150_000,
      });
      expect(response.ok(), `export failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      archive = await response.body();
    } finally {
      await apiCtx.dispose();
    }
    const entries = zipEntries(archive);
    // The second slide of the deck: the titled 2 x 2.
    const slideXml = entries.get('ppt/slides/slide2.xml')?.toString('utf8') ?? '';
    expect(slideXml.length).toBeGreaterThan(0);
    for (const title of TITLES) expect(slideXml).toContain(`>${title}<`);
    // Four native chart parts on the slide: the figures, not pictures of them.
    const chartParts = slideXml.match(/<a:graphicData uri="http:\/\/schemas\.openxmlformats\.org\/drawingml\/2006\/chart"/g) ?? [];
    expect(chartParts).toHaveLength(4);
  });
});
