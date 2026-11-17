import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import {
  SPREAD_CARD_MAX_EXTRA_PX,
  SPREAD_ROUNDING_SLACK_PX,
  SPREAD_ROW_MAX_EXTRA_PX,
} from '../src/components/presentation/spread-math';

// ---------------------------------------------------------------------------
// Presentations - content slides that leave the bottom empty.
//
// A `content` slide holding one grid block (a five-item `feature_list`) packed
// the rows under the title and left the bottom third of the slide bare. The
// grid blocks - list, metrics_grid, key_value, feature_list, numbered_cards,
// personas - take an opt-in `fill`: the run hands the block the free height
// and the block spreads its rows over it. Everything is opt-in: a block that
// does not set the field renders exactly as before.
//
// The spread is BOUNDED (`spread.ts`): every gap between rows grows by
// at most `SPREAD_ROW_MAX_EXTRA_PX` canvas px over the block's own row gap.
// Three bullets therefore never fill a slide - the block stops at its cap
// (published as `data-spread-cap`) and the rest of the room stays below it,
// where the layout audit measures it. A block whose cap is taller than the
// room still spreads to the floor exactly as before. This spec pins all three
// on a live deck: the packed geometry, the bounded spread, the unbounded one.
//
// The numbered cards - a `numbered_cards` block with `fill` and the takeaways
// slide body - grow their CARDS instead of their gaps, each row by at most
// `SPREAD_CARD_MAX_EXTRA_PX`: one row of three takeaways no longer
// stretches into three near-empty pillars, and on the takeaways slide the
// height the cap leaves is shared above and below the row.
//
// The rendered geometry is CSS, so it is read off the viewer the way the
// layout audit and the editable PPTX export read it: `getBoundingClientRect`
// of the body host, the block root and the block's rows. What is asserted
// here is therefore also what the export writes (`test_presentation_export.py`
// pins that the writer places every row where the render measured it).
//
// The deck is imported through the API from a bare spec (no LLM call), owned
// by the admin and deleted afterwards; the file runs SERIALLY so the import
// and the login happen once (see `presentation-charts.spec.ts`).
// ---------------------------------------------------------------------------

type FillBlockType = 'feature_list' | 'list' | 'key_value' | 'metrics_grid' | 'numbered_cards' | 'personas';

/** Every fill-capable grid block, with the root class its renderer emits `data-fill` on. */
const GRID_BLOCKS: Record<FillBlockType, { root: string; block: Record<string, unknown>; spreads: 'rows' | 'stretch' | 'grow' }> = {
  feature_list: {
    root: '.deck-features',
    spreads: 'rows',
    block: {
      type: 'feature_list',
      // Five, not six: a feature's title centres on its tile, which
      // makes a row taller, and six packed rows left no room under them.
      items: Array.from({ length: 5 }, (_, index) => ({
        icon: 'shield-check',
        title: `Capability ${index + 1}`,
        description: 'One line on what it does for the team',
      })),
    },
  },
  list: {
    root: '.deck-list',
    spreads: 'rows',
    block: { type: 'list', style: 'check', items: Array.from({ length: 6 }, (_, index) => `Point ${index + 1} of the argument`) },
  },
  key_value: {
    root: '.deck-kv',
    // Each row carries its own rule: the ROWS grow, each pair centred between
    // its rules, one readable extra per row at most (#10239).
    spreads: 'grow',
    block: {
      type: 'key_value',
      items: Array.from({ length: 6 }, (_, index) => ({ label: `Term ${index + 1}`, value: `Value ${index + 1}` })),
    },
  },
  metrics_grid: {
    root: '.deck-metrics',
    spreads: 'rows',
    block: {
      type: 'metrics_grid',
      columns: 3,
      items: Array.from({ length: 6 }, (_, index) => ({ value: `${(index + 1) * 12}%`, label: `Metric ${index + 1}` })),
    },
  },
  numbered_cards: {
    root: '.deck-cards',
    // Reuses the takeaways slide's `NumberedCards` fill: rows share the height,
    // each up to a readable extra.
    spreads: 'stretch',
    block: {
      type: 'numbered_cards',
      columns: 3,
      items: Array.from({ length: 6 }, (_, index) => ({ title: `Principle ${index + 1}`, description: 'Why it matters' })),
    },
  },
  personas: {
    root: '.deck-personas',
    spreads: 'rows',
    block: {
      type: 'personas',
      columns: 2,
      items: ['CISO', 'SOC analyst', 'Threat hunter', 'CTI lead'].map((name) => ({
        name,
        tagline: 'What they own and are measured on',
      })),
    },
  },
};

const BLOCK_TYPES = Object.keys(GRID_BLOCKS) as FillBlockType[];

const contentSlide = (id: string, title: string, block: Record<string, unknown>) => ({
  id,
  layout: 'content',
  title,
  blocks: [block],
});

// Two-line reasons: two rows of these cards, one readable extra each, reach
// well past the body, so the two-row case never sits on the edge of its cap.
const takeawaysSlide = (id: string, count: number) => ({
  id,
  layout: 'takeaways',
  title: `${count} takeaways`,
  items: Array.from({ length: count }, (_, index) => ({
    icon: 'shield-check',
    title: `Takeaway ${index + 1}`,
    description: 'Two lines on why it matters to the room, and on what the team does about it next',
  })),
});

/** Per block: the slide a deck saved before the field shows, and the one `fill: true` shows. */
const FIXTURE_SPEC = {
  title: 'E2E grid fill',
  theme: 'dark',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Grid blocks and fill', subtitle: 'Packed, then spread' },
    ...BLOCK_TYPES.flatMap((type) => [
      contentSlide(`plain-${type}`, `${type} - packed`, GRID_BLOCKS[type].block),
      contentSlide(`fill-${type}`, `${type} - fill`, { ...GRID_BLOCKS[type].block, fill: true }),
    ]),
    // The opt-out spelt out: `fill: false` is the same slide as no field at all.
    contentSlide('false-feature_list', 'feature_list - fill: false', { ...GRID_BLOCKS.feature_list.block, fill: false }),
    // The takeaways body always fills: one row of cards, then two.
    takeawaysSlide('takeaways-one-row', 3),
    takeawaysSlide('takeaways-two-rows', 6),
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
 * The body host, the block root and the block's ROWS (items sharing a top
 * edge) on the slide on screen, plus the cap the block published (canvas px)
 * and the stage scale (browser px per canvas px) that converts it.
 */
type Geometry = { fill: string | null; spreadCap: number | null; scale: number; host: Box; block: Box; rows: Box[] };

async function readGeometry(page: Page, root: string): Promise<Geometry> {
  const slide = page.locator('.reveal .slides > section.present');
  await expect(slide.locator(`.deck-fit-content > .deck-blocks-slide > ${root}`)).toBeVisible();
  return slide.evaluate((section, rootSelector) => {
    const box = (el: Element): Box => {
      const r = el.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, height: r.height };
    };
    const frame = section.querySelector<HTMLElement>('.deck-slide');
    const host = section.querySelector('.deck-fit-content > .deck-blocks-slide');
    const block = host?.querySelector<HTMLElement>(`:scope > ${rootSelector}`);
    if (!frame || !host || !block) throw new Error(`No ${rootSelector} in the slide body`);
    const rows: Box[] = [];
    const children = Array.from(block.children)
      .map(box)
      .filter((r) => r.height > 0)
      .sort((a, b) => a.top - b.top);
    for (const child of children) {
      const row = rows.find((r) => Math.abs(r.top - child.top) < 1);
      if (row) {
        row.bottom = Math.max(row.bottom, child.bottom);
        row.height = row.bottom - row.top;
      } else {
        rows.push({ ...child });
      }
    }
    const cap = Number.parseFloat(block.dataset.spreadCap ?? '');
    return {
      fill: block.getAttribute('data-fill'),
      spreadCap: Number.isFinite(cap) ? cap : null,
      // The frame is 1920 layout px wide however the stage is scaled.
      scale: frame.getBoundingClientRect().width / frame.offsetWidth,
      host: box(host),
      block: box(block),
      rows,
    };
  }, root);
}

/** Vertical gaps between consecutive rows. */
const gaps = (rows: Box[]): number[] => rows.slice(1).map((row, index) => row.top - rows[index].bottom);

/**
 * The `fill` cards grid of the slide on screen at its NATURAL height (browser
 * px) and its row count: the grid put at its content height for one
 * synchronous layout, then restored - what `measureCardRows` reads, since a
 * `fill` card carries a taller numeral than a packed one and the packed slide
 * cannot stand in for it.
 */
async function readNaturalCards(page: Page): Promise<{ height: number; rows: number }> {
  const slide = page.locator('.reveal .slides > section.present');
  return slide.evaluate((section) => {
    const grid = section.querySelector<HTMLElement>('.deck-fit-content .deck-cards[data-fill="true"]');
    if (!grid) throw new Error('No fill cards in the slide body');
    const { flex, maxHeight } = grid.style;
    grid.style.flex = 'none';
    grid.style.maxHeight = 'none';
    const height = grid.getBoundingClientRect().height;
    const tops: number[] = [];
    for (const card of Array.from(grid.children)) {
      const top = card.getBoundingClientRect().top;
      if (!tops.some((seen) => Math.abs(seen - top) < 1)) tops.push(top);
    }
    grid.style.flex = flex;
    grid.style.maxHeight = maxHeight;
    return { height, rows: tops.length };
  });
}

/** The cap `spread.ts` writes on a `fill` cards grid, in browser px: one readable extra per ROW of cards. */
function expectedCardCap(natural: { height: number; rows: number }, scale: number): { cap: number; tolerance: number } {
  const cap = natural.height + (natural.rows * SPREAD_CARD_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX) * scale;
  return { cap, tolerance: TOLERANCE + natural.rows * scale };
}

/** The takeaways slide on screen: the room, the cards grid that is its body, its cards and its cap. */
type TakeawaysGeometry = { spreadCap: number | null; scale: number; room: Box; grid: Box; cards: Box[] };

async function readTakeaways(page: Page): Promise<TakeawaysGeometry> {
  const slide = page.locator('.reveal .slides > section.present');
  await expect(slide.locator('.deck-fit-content > .deck-cards[data-fill="true"]')).toBeVisible();
  return slide.evaluate((section) => {
    const box = (el: Element): Box => {
      const r = el.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, height: r.height };
    };
    const frame = section.querySelector<HTMLElement>('.deck-slide');
    const room = section.querySelector('.deck-fit');
    const grid = section.querySelector<HTMLElement>('.deck-fit-content > .deck-cards');
    if (!frame || !room || !grid) throw new Error('No takeaways body on the slide');
    const cap = Number.parseFloat(grid.dataset.spreadCap ?? '');
    return {
      spreadCap: Number.isFinite(cap) ? cap : null,
      scale: frame.getBoundingClientRect().width / frame.offsetWidth,
      room: box(room),
      grid: box(grid),
      cards: Array.from(grid.children).map(box),
    };
  });
}

/** Browser px on the scaled stage: a hair of rounding, never a layout difference. */
const TOLERANCE = 2;

const close = (a: number, b: number, tolerance = TOLERANCE): boolean => Math.abs(a - b) <= tolerance;

/**
 * The cap `spread.ts` computes, in browser px, from the PACKED geometry: the
 * rows at their content height, the block's own gap between them, one
 * `SPREAD_ROW_MAX_EXTRA_PX` per gap and the rounding slack. `offsetHeight`
 * rounds every row to a whole canvas px, so the published cap may differ from
 * this by up to one canvas px per row.
 */
function expectedCap(packed: Geometry): { cap: number; tolerance: number } {
  const n = packed.rows.length;
  const rowsHeight = packed.rows.reduce((sum, row) => sum + row.height, 0);
  const baseGaps = gaps(packed.rows).reduce((sum, gap) => sum + gap, 0);
  const cap = rowsHeight + baseGaps + ((n - 1) * SPREAD_ROW_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX) * packed.scale;
  return { cap, tolerance: TOLERANCE + n * packed.scale };
}

test.describe('Presentations - opt-in fill on the grid blocks', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      deckId = await importDeck(apiCtx, token, FIXTURE_SPEC, 'e2e-grid-fill.json');
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    await deleteDeck(deckId);
  });

  for (const type of BLOCK_TYPES) {
    const { root, spreads } = GRID_BLOCKS[type];

    test(`${type}: without fill the rows pack under the title, with fill they relax up to the readable gap`, async ({
      page,
    }) => {
      await openStage(page, deckId, position(`plain-${type}`));
      const packed = await readGeometry(page, root);
      await openStage(page, deckId, position(`fill-${type}`));
      const filled = await readGeometry(page, root);

      // Before: no attribute, no cap, the block keeps its content height and
      // stops well short of the body's bottom edge - the empty band the audit
      // reports as `sparse`.
      expect(packed.fill).toBeNull();
      expect(packed.spreadCap).toBeNull();
      expect(packed.rows.length).toBeGreaterThan(1);
      expect(packed.host.bottom - packed.block.bottom).toBeGreaterThan(40);

      // After: the run hands the block the free height (the `.deck-blocks >
      // [data-fill]` hook) and its rows run from its top edge to its bottom edge.
      expect(filled.fill).toBe('true');
      expect(filled.rows.length).toBe(packed.rows.length);
      expect(close(filled.block.top, filled.host.top)).toBe(true);
      expect(close(filled.rows[0].top, filled.block.top)).toBe(true);
      expect(close(filled.rows[filled.rows.length - 1].bottom, filled.block.bottom)).toBe(true);
      // The room is the same on both slides: only the block grew into it.
      expect(close(filled.host.height, packed.host.height)).toBe(true);

      const packedGaps = gaps(packed.rows);
      const filledGaps = gaps(filled.rows);
      if (spreads === 'rows') {
        // `align-content: space-between`: rows keep their height and the free
        // height lands BETWEEN them, evenly.
        for (const [index, row] of filled.rows.entries()) expect(close(row.height, packed.rows[index].height)).toBe(true);
        for (const gap of filledGaps) {
          expect(gap).toBeGreaterThan(packedGaps[0] + 10);
          expect(close(gap, filledGaps[0])).toBe(true);
        }
        // The bound: the block published the cap `spread.ts` measured
        // - the packed rows and gaps plus one readable extra per gap...
        const { cap, tolerance } = expectedCap(packed);
        expect(filled.spreadCap).not.toBeNull();
        expect(close((filled.spreadCap as number) * filled.scale, cap, tolerance)).toBe(true);
        const maxGap = packedGaps[0] + SPREAD_ROW_MAX_EXTRA_PX * filled.scale + tolerance;
        for (const gap of filledGaps) expect(gap).toBeLessThanOrEqual(maxGap);
        if (cap < filled.host.height - tolerance) {
          // ...and, the room being taller than the cap, it stopped THERE: the
          // rows relaxed by exactly the readable extra and the rest of the
          // room stays bare below the block, for the audit to measure.
          expect(close(filled.block.height, cap, tolerance)).toBe(true);
          for (const gap of filledGaps) expect(close(gap, packedGaps[0] + SPREAD_ROW_MAX_EXTRA_PX * filled.scale, tolerance)).toBe(true);
          expect(filled.host.bottom - filled.block.bottom).toBeGreaterThan(tolerance);
        } else {
          // A cap taller than the room never binds: the block spans the body
          // and reaches its floor, as before the bound (a cap within the
          // rounding tolerance of the room is read the same way).
          expect(close(filled.block.bottom, filled.host.bottom, tolerance)).toBe(true);
        }
      } else if (spreads === 'grow') {
        // The rows grow evenly and the gaps stay the block's own...
        for (const [index, gap] of filledGaps.entries()) expect(close(gap, packedGaps[index])).toBe(true);
        for (const [index, row] of filled.rows.entries()) {
          expect(row.height).toBeGreaterThan(packed.rows[index].height + 10);
          expect(close(row.height, filled.rows[0].height)).toBe(true);
        }
        // ...each by one readable extra at most: the packed rows stand in for
        // the natural ones (the same pairs, unstretched).
        const n = packed.rows.length;
        const natural = packed.block.height;
        const cap = natural + (n * SPREAD_ROW_MAX_EXTRA_PX + SPREAD_ROUNDING_SLACK_PX) * filled.scale;
        const tolerance = TOLERANCE + n * filled.scale;
        expect(filled.spreadCap).not.toBeNull();
        expect(close((filled.spreadCap as number) * filled.scale, cap, tolerance)).toBe(true);
        if (cap < filled.host.height - tolerance) {
          expect(close(filled.block.height, cap, tolerance)).toBe(true);
          expect(filled.host.bottom - filled.block.bottom).toBeGreaterThan(tolerance);
        } else {
          expect(close(filled.block.bottom, filled.host.bottom, tolerance)).toBe(true);
        }
      } else {
        // The takeaways rule: rows share the height and the gap stays the
        // deck's - the cards, not the gaps, grow...
        for (const [index, gap] of filledGaps.entries()) expect(close(gap, packedGaps[index])).toBe(true);
        for (const [index, row] of filled.rows.entries()) {
          expect(row.height).toBeGreaterThan(packed.rows[index].height + 10);
          expect(close(row.height, filled.rows[0].height)).toBe(true);
        }
        // ...each row by one readable extra at most: the cap is the
        // cards at their natural height plus one extra per ROW.
        const { cap, tolerance } = expectedCardCap(await readNaturalCards(page), filled.scale);
        expect(filled.spreadCap).not.toBeNull();
        expect(close((filled.spreadCap as number) * filled.scale, cap, tolerance)).toBe(true);
        if (cap < filled.host.height - tolerance) {
          // A content slide keeps its run at the top: the rest of the room
          // stays below the cards, where the audit measures it.
          expect(close(filled.block.height, cap, tolerance)).toBe(true);
          expect(filled.host.bottom - filled.block.bottom).toBeGreaterThan(tolerance);
        } else {
          expect(close(filled.block.bottom, filled.host.bottom, tolerance)).toBe(true);
        }
      }
    });
  }

  test('fill: false is the very same slide as no fill at all', async ({ page }) => {
    await openStage(page, deckId, position('plain-feature_list'));
    const absent = await readGeometry(page, GRID_BLOCKS.feature_list.root);
    await openStage(page, deckId, position('false-feature_list'));
    const spelt = await readGeometry(page, GRID_BLOCKS.feature_list.root);

    expect(spelt.fill).toBeNull();
    expect(spelt.spreadCap).toBeNull();
    expect(close(spelt.block.top, absent.block.top, 0.5)).toBe(true);
    expect(close(spelt.block.bottom, absent.block.bottom, 0.5)).toBe(true);
    expect(spelt.rows.length).toBe(absent.rows.length);
    for (const [index, row] of spelt.rows.entries()) {
      expect(close(row.top, absent.rows[index].top, 0.5)).toBe(true);
      expect(close(row.bottom, absent.rows[index].bottom, 0.5)).toBe(true);
    }
  });

  test('takeaways: one row of cards stops at a readable height, centred in the body', async ({ page }) => {
    await openStage(page, deckId, position('takeaways-one-row'));
    const one = await readTakeaways(page);
    const { cap, tolerance } = expectedCardCap(await readNaturalCards(page), one.scale);

    // Three cards are one row, whose cap is far shorter than the body: the
    // cards stop there instead of becoming three pillars of the body's height.
    expect(new Set(one.cards.map((card) => Math.round(card.top))).size).toBe(1);
    expect(one.spreadCap).not.toBeNull();
    expect(close((one.spreadCap as number) * one.scale, cap, tolerance)).toBe(true);
    expect(cap).toBeLessThan(one.room.height * 0.75);
    expect(close(one.grid.height, cap, tolerance)).toBe(true);
    for (const card of one.cards) expect(close(card.height, one.grid.height, tolerance)).toBe(true);
    // What the cap leaves is shared above and below the row, not left under it.
    const above = one.grid.top - one.room.top;
    const below = one.room.bottom - one.grid.bottom;
    expect(above).toBeGreaterThan(tolerance);
    expect(close(above, below, tolerance)).toBe(true);
  });

  test('takeaways: two rows of cards still share the whole body', async ({ page }) => {
    await openStage(page, deckId, position('takeaways-two-rows'));
    const two = await readTakeaways(page);
    const { cap, tolerance } = expectedCardCap(await readNaturalCards(page), two.scale);

    // Six cards are two rows of three: one readable extra per row reaches
    // past the body, so the cap never binds and the cards span the room as
    // they always did - nothing is pushed off the top or the bottom.
    expect(new Set(two.cards.map((card) => Math.round(card.top))).size).toBe(2);
    expect(two.spreadCap).not.toBeNull();
    expect(cap).toBeGreaterThan(two.room.height);
    expect(close(two.grid.top, two.room.top, tolerance)).toBe(true);
    expect(close(two.grid.bottom, two.room.bottom, tolerance)).toBe(true);
  });
});
