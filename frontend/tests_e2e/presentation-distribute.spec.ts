import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { SPREAD_BLOCK_MAX_EXTRA_PX, SPREAD_ROUNDING_SLACK_PX } from '../src/components/presentation/spread-math';

// ---------------------------------------------------------------------------
// Presentations - opt-in `distribute` on content slides.
//
// `fill` is one block spreading its rows; `distribute` is the SLIDE spreading
// its blocks: the free height lands BETWEEN them - the first under the header
// - the way the agenda spreads its rows. Off by default, so a deck
// that never sets it packs its run to the top exactly as before.
//
// The spread is BOUNDED (`spread.ts`): every gap between blocks grows
// by at most `SPREAD_BLOCK_MAX_EXTRA_PX` canvas px over the run's own gap, so
// two paragraphs never sit at opposite ends of a tall body - the run stops at
// its cap (published as `data-spread-cap`) and the rest of the room stays
// below it, where the layout audit measures it. A run whose cap is taller
// than the room still spreads to the floor exactly as before. This spec pins
// the packed geometry and both spreads on a live deck, in the single-flow
// form and in the columns form, plus the rule the schema states: when a slide
// sets `vertical_align` AND `distribute`, `distribute` wins for the run.
//
// The geometry is CSS, read off the viewer the way the layout audit and the
// editable PPTX export read it (`getBoundingClientRect`), so what is asserted
// here is also what the export writes. The deck is imported through the API
// from a bare spec (no LLM call), owned by the admin and deleted afterwards;
// the file runs SERIALLY so the import and the login happen once.
// ---------------------------------------------------------------------------

const text = (content: string) => ({ type: 'text', content });

const TWO_BLOCKS = [text('The first thing to say about the quarter.'), text('And the second, which closes the argument.')];

const FIVE_BLOCKS = [
  { type: 'heading', content: 'Where the time went', level: 3 },
  text('Detection lagged because half the fleet reported nothing for weeks.'),
  { type: 'list', items: ['Stale rules', 'No feedback loop', 'Alerts nobody owned'] },
  { type: 'callout', tone: 'accent', title: 'Decision', content: 'Fund the feedback loop first.' },
  text('Everything else follows from that one choice.'),
];

const COLUMNS = [
  [text('Left, first block.'), text('Left, second block.')],
  [text('Right, the only block.')],
];

type SlideFixture = { id: string; layout: 'title' | 'content'; title: string } & Record<string, unknown>;

const content = (id: string, title: string, extra: Record<string, unknown>): SlideFixture => ({
  id,
  layout: 'content',
  title,
  ...extra,
});

const FIXTURE_SPEC = {
  title: 'E2E content distribute',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Content slides and distribute', subtitle: 'Packed, then spread' } as SlideFixture,
    content('packed-two', 'Two blocks - packed', { blocks: TWO_BLOCKS }),
    content('spread-two', 'Two blocks - distribute', { blocks: TWO_BLOCKS, distribute: true }),
    content('packed-five', 'Five blocks - packed', { blocks: FIVE_BLOCKS }),
    content('spread-five', 'Five blocks - distribute', { blocks: FIVE_BLOCKS, distribute: true }),
    // The opt-out spelt out: `distribute: false` is the same slide as no field at all.
    content('false-two', 'Two blocks - distribute: false', { blocks: TWO_BLOCKS, distribute: false }),
    // `vertical_align` alone moves the run; with `distribute` the run is spread instead.
    content('center-two', 'Two blocks - centred', { blocks: TWO_BLOCKS, vertical_align: 'center' }),
    content('center-spread-two', 'Two blocks - centred and distribute', {
      blocks: TWO_BLOCKS,
      vertical_align: 'center',
      distribute: true,
    }),
    content('columns-packed', 'Columns - packed', { columns: COLUMNS }),
    content('columns-spread', 'Columns - distribute', { columns: COLUMNS, distribute: true }),
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

/** Opens the viewer on one slide (0-based index in the hash) and waits for the deck. */
async function openStage(page: Page, deckId: string, slide: number): Promise<Locator> {
  await page.goto(`/presentations/${deckId}#/${slide - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${slide} of \\d+\\.`) })).toBeVisible();
  return toolbar;
}

type Box = { top: number; bottom: number; height: number };

/**
 * A run and the ROOM it spreads in, read apart: the room is the
 * `.deck-fit` host - the space between the header and the footer block, the
 * box the layout audit measures `sparse` against - and the run is the element
 * that carries `distribute` and, with it, the cap. Before the bound the two
 * had the same height (the run stretched to the room); with the cap binding,
 * the run stops at its cap and the room keeps its height, so an assertion
 * about "the same room on both slides" must read the room, never the run.
 * `children` are the run's direct children (the blocks), `spreadCap` the cap
 * the run published (canvas px, `data-spread-cap`) and `scale` the stage
 * scale (browser px per canvas px) that converts it.
 */
type Run = {
  distribute: string | null;
  spreadCap: number | null;
  scale: number;
  room: Box;
  run: Box;
  children: Box[];
};

const HOST = '.deck-fit-content > .deck-blocks-slide, .deck-fit-content > .deck-columns-slide';

async function readRun(page: Page): Promise<Run> {
  const slide = page.locator('.reveal .slides > section.present');
  await expect(slide.locator(HOST)).toBeVisible();
  return slide.evaluate((section, hostSelector) => {
    const box = (el: Element): Box => {
      const r = el.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, height: r.height };
    };
    const frame = section.querySelector<HTMLElement>('.deck-slide');
    const host = section.querySelector<HTMLElement>(hostSelector);
    const room = host?.closest<HTMLElement>('.deck-fit');
    if (!frame || !host || !room) throw new Error('No body host in the slide');
    const cap = Number.parseFloat(host.dataset.spreadCap ?? '');
    return {
      distribute: host.getAttribute('data-distribute'),
      spreadCap: Number.isFinite(cap) ? cap : null,
      // The frame is 1920 layout px wide however the stage is scaled.
      scale: frame.getBoundingClientRect().width / frame.offsetWidth,
      room: box(room),
      run: box(host),
      children: Array.from(host.children).map(box),
    };
  }, HOST);
}

/**
 * On a columns slide: each column's own run, the way `readRun` reads the
 * slide's. The column is the room its run spreads in (under `distribute` the
 * columns stretch to the body).
 */
async function readColumns(page: Page): Promise<Run[]> {
  const slide = page.locator('.reveal .slides > section.present');
  await expect(slide.locator('.deck-fit-content > .deck-columns-slide')).toBeVisible();
  return slide.evaluate((section) => {
    const box = (el: Element): Box => {
      const r = el.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, height: r.height };
    };
    const frame = section.querySelector<HTMLElement>('.deck-slide');
    if (!frame) throw new Error('No slide frame');
    const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
    return Array.from(section.querySelectorAll('.deck-fit-content > .deck-columns-slide > .deck-column')).map((column) => {
      const run = column.querySelector<HTMLElement>(':scope > .deck-blocks');
      if (!run) throw new Error('A column without a run');
      const cap = Number.parseFloat(run.dataset.spreadCap ?? '');
      return {
        distribute: run.getAttribute('data-distribute'),
        spreadCap: Number.isFinite(cap) ? cap : null,
        scale,
        room: box(column),
        run: box(run),
        children: Array.from(run.children).map(box),
      };
    });
  });
}

/** Vertical gaps between consecutive children. */
const gaps = (children: Box[]): number[] => children.slice(1).map((child, index) => child.top - children[index].bottom);

/** Browser px on the scaled stage: a hair of rounding, never a layout difference. */
const TOLERANCE = 2;

const close = (a: number, b: number, tolerance = TOLERANCE): boolean => Math.abs(a - b) <= tolerance;

/** The band a packed run leaves at the bottom must be large to mean anything; a block's own margin is not. */
const BAND = 40;

/**
 * The cap `spread.ts` computes, in browser px, from the PACKED run: the blocks
 * at their content height, the run's own gap between them, one
 * `SPREAD_BLOCK_MAX_EXTRA_PX` per gap and the rounding slack. `offsetHeight`
 * rounds every block to a whole canvas px, so the published cap may differ
 * from this by up to one canvas px per block.
 */
function expectedCap(packed: Run): { cap: number; tolerance: number } {
  const n = packed.children.length;
  const blocksHeight = packed.children.reduce((sum, child) => sum + child.height, 0);
  const baseGaps = gaps(packed.children).reduce((sum, gap) => sum + gap, 0);
  const cap = blocksHeight + baseGaps + ((n - 1) * SPREAD_BLOCK_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX) * packed.scale;
  return { cap, tolerance: TOLERANCE + n * packed.scale };
}

/**
 * The spread run against its packed twin: same ROOM, same blocks, same start
 * under the header, the free height landing BETWEEN the blocks evenly - and
 * bounded: every gap grew by at most the readable extra. When the cap
 * is shorter than the room the run stopped there (its box is the cap, not the
 * room) and the floor is bare; when it is taller the run spans the room and
 * reaches the floor as it always did.
 */
function expectSpreadOver(spread: Run, packed: Run): void {
  // The room does not move: header, footer block and fit host are the same on
  // both slides. The packed run stretched to it - that is the box the cap
  // now bounds.
  expect(close(spread.room.height, packed.room.height)).toBe(true);
  expect(close(packed.run.height, packed.room.height)).toBe(true);
  expect(spread.children.length).toBe(packed.children.length);
  for (const [index, child] of spread.children.entries()) expect(close(child.height, packed.children[index].height)).toBe(true);
  expect(close(spread.children[0].top, packed.children[0].top)).toBe(true);
  const packedGaps = gaps(packed.children);
  const spreadGaps = gaps(spread.children);
  for (const [index, gap] of spreadGaps.entries()) {
    expect(gap).toBeGreaterThan(packedGaps[index] + 10);
    expect(close(gap, spreadGaps[0])).toBe(true);
  }
  const { cap, tolerance } = expectedCap(packed);
  expect(packed.spreadCap).toBeNull();
  expect(spread.spreadCap).not.toBeNull();
  expect(close((spread.spreadCap as number) * spread.scale, cap, tolerance)).toBe(true);
  const extra = SPREAD_BLOCK_MAX_EXTRA_PX * spread.scale;
  for (const [index, gap] of spreadGaps.entries()) expect(gap).toBeLessThanOrEqual(packedGaps[index] + extra + tolerance);
  const last = spread.children[spread.children.length - 1];
  if (cap < spread.room.height - tolerance) {
    // The cap binds: the run's box IS the cap, every gap grew by exactly the
    // readable extra, and the rest of the room stays bare below the run.
    expect(close(spread.run.height, cap, tolerance)).toBe(true);
    for (const [index, gap] of spreadGaps.entries()) expect(close(gap, packedGaps[index] + extra, tolerance)).toBe(true);
    expect(close(last.bottom - spread.children[0].top, cap, tolerance)).toBe(true);
    expect(spread.room.bottom - last.bottom).toBeGreaterThan(tolerance);
  } else {
    // A cap taller than the room never binds: the run spans the room and the
    // last block sits on its floor, as before the bound.
    expect(close(spread.run.height, spread.room.height, tolerance)).toBe(true);
    expect(spread.room.bottom - last.bottom).toBeLessThan(BAND);
  }
}

test.describe('Presentations - opt-in distribute on content slides', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      deckId = await importDeck(apiCtx, token, FIXTURE_SPEC, 'e2e-content-distribute.json');
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    await deleteDeck(deckId);
  });

  for (const [count, packedId, spreadId] of [
    ['two', 'packed-two', 'spread-two'],
    ['five', 'packed-five', 'spread-five'],
  ] as const) {
    test(`${count} blocks: without distribute the run packs to the top, with it the blocks spread over the body`, async ({
      page,
    }) => {
      await openStage(page, deckId, position(packedId));
      const packed = await readRun(page);
      await openStage(page, deckId, position(spreadId));
      const spread = await readRun(page);

      // Before: no attribute, no cap, the blocks follow each other at the
      // deck's gap and the last one stops well short of the room's floor -
      // the empty band the audit reports as `sparse`.
      expect(packed.distribute).toBeNull();
      expect(packed.spreadCap).toBeNull();
      expect(packed.children.length).toBeGreaterThan(1);
      const packedGaps = gaps(packed.children);
      for (const gap of packedGaps) expect(close(gap, packedGaps[0])).toBe(true);
      expect(packed.room.bottom - packed.children[packed.children.length - 1].bottom).toBeGreaterThan(BAND);

      // After: `justify-content: space-between` on the run, bounded by its cap.
      expect(spread.distribute).toBe('true');
      expectSpreadOver(spread, packed);
    });
  }

  test('two short paragraphs never sit at opposite ends of the body', async ({ page }) => {
    // The reported look: a body of two one-line blocks spread to the
    // floor put them a screen apart. The cap of two short blocks is far
    // shorter than the room, so it BINDS: the gap grew by exactly the
    // readable extra and the floor is bare - which is what the audit now sees.
    await openStage(page, deckId, position('packed-two'));
    const packed = await readRun(page);
    await openStage(page, deckId, position('spread-two'));
    const spread = await readRun(page);
    const { cap, tolerance } = expectedCap(packed);
    expect(cap).toBeLessThan(spread.room.height - BAND);
    const [gap] = gaps(spread.children);
    expect(close(gap, gaps(packed.children)[0] + SPREAD_BLOCK_MAX_EXTRA_PX * spread.scale, tolerance)).toBe(true);
    // The run's own box is the cap - not the room the packed run stretched to.
    expect(close(spread.run.height, cap, tolerance)).toBe(true);
    expect(spread.run.height).toBeLessThan(packed.run.height - BAND);
    expect(spread.room.bottom - spread.children[1].bottom).toBeGreaterThan(BAND);
  });

  test('distribute: false is the very same slide as no distribute at all', async ({ page }) => {
    await openStage(page, deckId, position('packed-two'));
    const absent = await readRun(page);
    await openStage(page, deckId, position('false-two'));
    const spelt = await readRun(page);

    expect(spelt.distribute).toBeNull();
    expect(spelt.spreadCap).toBeNull();
    expect(close(spelt.run.height, absent.run.height, 0.5)).toBe(true);
    expect(spelt.children.length).toBe(absent.children.length);
    for (const [index, child] of spelt.children.entries()) {
      expect(close(child.top, absent.children[index].top, 0.5)).toBe(true);
      expect(close(child.bottom, absent.children[index].bottom, 0.5)).toBe(true);
    }
  });

  test('distribute wins over vertical_align for the run', async ({ page }) => {
    await openStage(page, deckId, position('packed-two'));
    const packed = await readRun(page);
    await openStage(page, deckId, position('center-two'));
    const centred = await readRun(page);
    await openStage(page, deckId, position('center-spread-two'));
    const both = await readRun(page);

    // `vertical_align: center` alone still moves the whole run down, as before.
    expect(centred.distribute).toBeNull();
    expect(centred.children[0].top - packed.children[0].top).toBeGreaterThan(BAND);
    expect(close(gaps(centred.children)[0], gaps(packed.children)[0])).toBe(true);
    // With `distribute` set too, the run is spread from the top instead.
    expect(both.distribute).toBe('true');
    expectSpreadOver(both, packed);
  });

  test('on a columns slide every column takes the body height and spreads its own blocks', async ({ page }) => {
    await openStage(page, deckId, position('columns-packed'));
    const packedHost = await readRun(page);
    const packedColumns = await readColumns(page);
    await openStage(page, deckId, position('columns-spread'));
    const spreadHost = await readRun(page);
    const spreadColumns = await readColumns(page);

    // Before: the columns are as tall as their content and stop short of the
    // room's floor (the columns grid itself spans the room).
    expect(packedHost.distribute).toBeNull();
    expect(packedColumns.length).toBe(2);
    expect(close(packedHost.run.height, packedHost.room.height)).toBe(true);
    for (const column of packedColumns) expect(packedHost.room.bottom - column.room.bottom).toBeGreaterThan(BAND);

    // After: the columns stretch to the room, and a column holding two blocks
    // puts the first under the header and spreads the second away from it -
    // by the readable extra, no further: each column's run is a
    // distribute run with a cap of its own, so two short paragraphs are not
    // sent to opposite ends of the column. A column holding one block has
    // nothing to spread and keeps it at the top.
    expect(spreadHost.distribute).toBe('true');
    expect(close(spreadHost.room.height, packedHost.room.height)).toBe(true);
    for (const column of spreadColumns) expect(close(column.room.height, spreadHost.room.height)).toBe(true);
    const [left, right] = spreadColumns;
    expect(left.children.length).toBe(2);
    expect(left.distribute).toBe('true');
    expect(close(left.children[0].top, packedColumns[0].children[0].top)).toBe(true);
    const packedGap = gaps(packedColumns[0].children)[0];
    const leftGap = gaps(left.children)[0];
    expect(leftGap).toBeGreaterThan(packedGap + 10);
    const { cap, tolerance } = expectedCap(packedColumns[0]);
    expect(left.spreadCap).not.toBeNull();
    expect(close((left.spreadCap as number) * left.scale, cap, tolerance)).toBe(true);
    if (cap < left.room.height - tolerance) {
      // The cap binds: the column's run stops at it and the column's floor is bare.
      expect(close(left.run.height, cap, tolerance)).toBe(true);
      expect(close(leftGap, packedGap + SPREAD_BLOCK_MAX_EXTRA_PX * left.scale, tolerance)).toBe(true);
      expect(left.room.bottom - left.children[1].bottom).toBeGreaterThan(tolerance);
    } else {
      expect(close(left.run.height, left.room.height, tolerance)).toBe(true);
      expect(left.room.bottom - left.children[1].bottom).toBeLessThan(BAND);
    }
    expect(right.children.length).toBe(1);
    expect(close(right.children[0].top, packedColumns[1].children[0].top)).toBe(true);
  });
});
