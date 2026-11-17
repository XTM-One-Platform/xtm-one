import { test, expect, type Locator } from '@playwright/test';
import {
  createAdminApiContext,
  createTwoSchemeTemplate,
  deleteTemplate,
  expectEditorSaved,
  importDeckOnTemplate,
} from './helpers';
import { slidePictureBoxes, unzip } from './pptx-archive';
import { logoItems, openStage, uploadLogos, type Logo } from './presentation-logo-fixtures';

// ---------------------------------------------------------------------------
// Presentations - even logo cells, four columns side by side and logos on
// timeline steps.
//
// Columns may line their blocks up as rows of equal height (`equal_rows`),
// and a block can be added by hand from the "+" at the end of a run.
//
// A `logo_grid` with `columns` shares its width in equal cells and centres
// each logo in its own (`align: even`, the default whenever `columns` is set),
// so a grid under a timeline of as many steps lines up with them; `start`
// under `columns` keeps the same equal cells, packed from the start.
// A `columns` block of four keeps them side by side unless the widest
// column's content cannot fit a quarter of the block; only then does it wrap
// 2 x 2, and the layout audit says so. A `timeline` step carries its own
// logo, drawn under its description and centred on its node, the logos of all
// steps on one row.
//
// `tile_height` gives every cell of a grid one height in canvas px,
// whatever it holds, so sibling grids with different logo mixes line up; in
// such a cell a seal grows to the inner height a wordmark's width leaves it,
// instead of stopping at 1.5x `logo_height`.
//
// The logos are synthetic wordmarks (`presentation-logo-fixtures.ts`), which
// every size draws at exactly its height, so "the same visual height" is the
// same height - the tile slide adds the fixture seals. Geometry is read off
// the live viewer in canvas px (the stage scale divided out) and, for the
// PPTX, off the exported slide's picture boxes. The deck is 16:9 and light,
// imported through the API on a private template and deleted afterwards; the
// file runs SERIALLY.
// ---------------------------------------------------------------------------

/** Canvas px: "within 4 px" is the acceptance bar. */
const CENTRE_TOLERANCE = 4;
const HEIGHT_TOLERANCE = 0.5;
/** Canvas px of every tile on the sibling-grids slide. */
const TILE_HEIGHT = 96;
/** `--logo-tile-pad-y` of a fixed tile, whatever `logo_height`: `--deck-space-1`, 8 canvas px. */
const TILE_PAD_Y = 8;
/** Canvas px of the labelled certification row's tiles. */
const LABELLED_TILE_HEIGHT = 120;

const STEPS = [
  { label: 'Sep 2021', title: 'Seed', description: '$4.5M - a round whose description runs to two lines on its own' },
  { label: 'Jun 2022', title: 'Series A', description: '$10M' },
  { label: 'Apr 2024', title: 'Series B', description: '$35M' },
  { label: 'Oct 2025', title: 'Series C', description: '$60M' },
];

const ALIGNS = ['start', 'center', 'even', 'stretch'] as const;

const content = (id: string, title: string, ...blocks: object[]) => ({ id, layout: 'content', title, blocks });

/**
 * Four sibling grids of `columns: 4`, every cell
 * `TILE_HEIGHT` tall and packed from the start, each mixing the wordmarks
 * with the fixture's seal and badge differently (4, 3, 2 and 4 logos).
 */
function siblingGrids(wordmarks: Logo[], seal: Logo, badge: Logo) {
  const grid = (items: Logo[]) => ({
    type: 'logo_grid',
    columns: 4,
    align: 'start',
    logo_height: 'md',
    tile_height: TILE_HEIGHT,
    items: logoItems(items),
  });
  return [
    grid(wordmarks),
    grid([seal, wordmarks[0], wordmarks[1]]),
    grid([badge, wordmarks[2]]),
    grid([wordmarks[3], seal, badge, wordmarks[0]]),
  ];
}

function buildSlides(logos: Logo[], seal: Logo, badge: Logo) {
  const rounds = {
    type: 'timeline',
    style: 'phases',
    items: STEPS.map((step, index) => ({ ...step, logo_file_id: logos[index].file_id })),
  };
  // The sibling the framed step tiles must meet edge to edge.
  const metrics = {
    type: 'metrics_grid',
    columns: 4,
    items: [
      { value: '42%', label: 'Fewer alerts' },
      { value: '3x', label: 'Faster triage' },
      { value: '120', label: 'Customers' },
      { value: '24/7', label: 'Coverage' },
    ],
  };
  // One labelled row of fixed tiles mixing marks of every shape.
  const labelled = {
    type: 'logo_grid',
    columns: 4,
    align: 'even',
    logo_height: 'lg',
    tile_height: LABELLED_TILE_HEIGHT,
    show_labels: true,
    items: logoItems([logos[0], seal, logos[1], badge]),
  };
  const steps = { type: 'timeline', style: 'phases', items: STEPS };
  const grid = (fields: object) => ({ type: 'logo_grid', columns: 4, items: logoItems(logos.slice(0, 4)), ...fields });
  const column = (fields: object) => [{ type: 'logo_grid', ...fields }];
  const small = column({ items: logoItems(logos.slice(0, 1)), logo_height: 'md' });
  const wide = column({ columns: 6, logo_height: 'xl', items: logoItems([...logos, ...logos].slice(0, 6)) });
  const card = (title: string, items: string[]) => ({ type: 'phase_card', icon: 'shield-check', title, items });
  return [
    content('s-rounds', 'Funding rounds', rounds, metrics),
    content('s-grid-under-steps', 'Investors', steps, grid({ align: 'even' }), grid({})),
    content('s-aligns', 'Every alignment', ...ALIGNS.map((align) => grid({ align, logo_height: 'sm' }))),
    content('s-tiles', 'Sibling grids, one tile', ...siblingGrids(logos, seal, badge)),
    content('s-tile-labels', 'Certifications', labelled),
    content('s-columns-4', 'Four columns', { type: 'columns', columns: [small, small, small, small] }),
    content('s-columns-wrap', 'Too wide for four', { type: 'columns', columns: [wide, wide, wide, wide] }),
    {
      id: 's-equal-rows',
      layout: 'content',
      title: 'Rows of equal cards',
      equal_rows: true,
      columns: [
        [card('Pentesting', ['Attack chaining', 'Scenarios', 'Scoring']), card('Table-top', ['Crisis simulation'])],
        [card('Simulation', ['Continuous testing']), card('Validation', ['CTI-driven', 'AI validation', 'Orchestration'])],
      ],
    },
    content('s-add-block', 'Add a block by hand', { type: 'text', content: 'The only block, for now.' }),
  ];
}

const SLIDE_IDS = ['s-rounds', 's-grid-under-steps', 's-aligns', 's-tiles', 's-tile-labels', 's-columns-4', 's-columns-wrap', 's-equal-rows', 's-add-block'];
const position = (id: string): number => SLIDE_IDS.indexOf(id) + 1;

type Box = { left: number; top: number; width: number; height: number; cx: number; cy: number };

/**
 * In canvas px from the slide frame's corner: the boxes under `root` matching
 * `selector`, or `root`'s own box without one.
 */
async function canvasBoxes(root: Locator, selector?: string): Promise<Box[]> {
  return root.evaluate((el, query) => {
    // An element of the edit layer sits beside the deck: measured against the slide on stage.
    const frame =
      (el.closest('section') ?? el).querySelector<HTMLElement>('.deck-slide') ??
      document.querySelector<HTMLElement>('.reveal .slides > section.present .deck-slide');
    if (!frame) throw new Error('No slide frame');
    const origin = frame.getBoundingClientRect();
    const scale = origin.width / frame.offsetWidth;
    return (query ? Array.from(el.querySelectorAll(query)) : [el]).map((target) => {
      const r = target.getBoundingClientRect();
      const left = (r.left - origin.left) / scale;
      const top = (r.top - origin.top) / scale;
      const width = r.width / scale;
      const height = r.height / scale;
      return { left, top, width, height, cx: left + width / 2, cy: top + height / 2 };
    });
  }, selector);
}

const near = (a: number, b: number, tolerance: number) => Math.abs(a - b) <= tolerance;

test.describe('Presentations - even logo cells, four columns and step logos', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';
  let templateId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const templateName = `E2E logo layout ${Date.now()}`;
      templateId = await createTwoSchemeTemplate(apiCtx, token, templateName);
      // The mixed set's wordmarks, each drawn at exactly the size asked for,
      // and its seal and badge for the tile slide.
      const uploaded = await uploadLogos(apiCtx, token, templateId, 8);
      const logos = uploaded.filter((logo) => logo.kind === 'wordmark');
      expect(logos).toHaveLength(4);
      const seal = uploaded.find((logo) => logo.kind === 'seal');
      const badge = uploaded.find((logo) => logo.kind === 'badge');
      if (!seal || !badge) throw new Error('The mixed logo set carries one seal and one badge');
      const shell = { title: 'E2E logo layout', theme: 'light', aspect_ratio: '16:9', transition: 'none', slides: [] };
      deckId = await importDeckOnTemplate(
        apiCtx,
        token,
        { ...shell, slides: [{ id: 's-title', layout: 'title', title: 'Logo layout' }] },
        templateName,
        'e2e-logo-layout.json',
      );
      const deck = await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
      const { revision } = (await deck.json()) as { revision: number };
      const patched = await apiCtx.patch(`/api/v1/presentations/${deckId}`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { slides: buildSlides(logos, seal, badge), expected_revision: revision },
      });
      if (!patched.ok()) throw new Error(`Writing the fixture slides failed: ${patched.status()} ${await patched.text()}`);
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      if (deckId) await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
    } finally {
      await apiCtx.dispose();
    }
    await deleteTemplate(templateId);
  });

  test('each step logo is centred on its node, on one row, at one height', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-rounds'));
    await expect(present).toHaveAttribute('data-scheme', 'light');
    await expect(present.locator('.deck-fit[data-fit]')).toHaveCount(0);
    await expect(present.locator('.deck-timeline')).toHaveAttribute('data-logos', 'true');
    const nodes = await canvasBoxes(present, '.deck-timeline-node');
    const marks = await canvasBoxes(present, '.deck-timeline-logo .deck-logo-mark');
    expect(nodes).toHaveLength(4);
    expect(marks).toHaveLength(4);
    marks.forEach((mark, index) => {
      expect(near(mark.cx, nodes[index].cx, CENTRE_TOLERANCE), `step ${index + 1}: logo ${mark.cx} vs node ${nodes[index].cx}`).toBe(true);
      // One row, whatever the description above it: the first one runs longer.
      expect(near(mark.cy, marks[0].cy, 1), `step ${index + 1}: row ${mark.cy} vs ${marks[0].cy}`).toBe(true);
      // `logo_height: sm` (the default), 28 canvas px for a wordmark.
      expect(near(mark.height, 28, HEIGHT_TOLERANCE), `step ${index + 1}: ${mark.height}px high`).toBe(true);
      // Under the description: below every step's body.
      expect(mark.top).toBeGreaterThan(nodes[index].top + nodes[index].height);
    });
    const bodies = await canvasBoxes(present, '.deck-timeline-body');
    const lowestBody = Math.max(...bodies.map((body) => body.top + body.height));
    for (const mark of marks) expect(mark.top).toBeGreaterThanOrEqual(lowestBody);

    // A framed step tile is its step's whole column: the row of tiles
    // runs edge to edge, the outer tiles on the sibling metrics_grid's edges.
    const tiles = await canvasBoxes(present, '.deck-timeline-logo[data-framed="true"]');
    expect(tiles).toHaveLength(4);
    const [grid] = await canvasBoxes(present.locator('.deck-metrics').first());
    const first = tiles[0];
    const last = tiles[tiles.length - 1];
    expect(near(first.left, grid.left, CENTRE_TOLERANCE), `first tile at ${first.left} vs the grid at ${grid.left}`).toBe(true);
    expect(
      near(last.left + last.width, grid.left + grid.width, CENTRE_TOLERANCE),
      `last tile ends at ${last.left + last.width} vs the grid at ${grid.left + grid.width}`,
    ).toBe(true);
    for (const tile of tiles) expect(near(tile.width, first.width, 1), `tile ${tile.width} vs ${first.width}`).toBe(true);
  });

  test('an even grid of four under four steps centres each logo under its step', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-grid-under-steps'));
    const grids = present.locator('.deck-logo-grid');
    await expect(grids).toHaveCount(2);
    // The one that says `even`, and the one that says nothing and sets columns.
    await expect(grids.nth(0)).toHaveAttribute('data-align', 'even');
    await expect(grids.nth(1)).toHaveAttribute('data-align', 'even');
    const [timeline] = await canvasBoxes(present, '.deck-timeline');
    const nodes = await canvasBoxes(present, '.deck-timeline-node');
    expect(nodes).toHaveLength(4);
    for (const [index, grid] of [0, 1].entries()) {
      const [box] = await canvasBoxes(grids.nth(grid));
      // The same width as the timeline above it: the body's.
      expect(near(box.left, timeline.left, 1) && near(box.width, timeline.width, 1), `grid ${index + 1} spans the timeline`).toBe(true);
      const marks = await canvasBoxes(grids.nth(grid), '.deck-logo-mark');
      expect(marks).toHaveLength(4);
      marks.forEach((mark, step) => {
        expect(near(mark.cx, nodes[step].cx, CENTRE_TOLERANCE), `grid ${index + 1}, logo ${step + 1}: ${mark.cx} vs ${nodes[step].cx}`).toBe(true);
        expect(near(mark.cy, marks[0].cy, 1)).toBe(true);
      });
    }
  });

  test('four logos under columns 4 stay on one row whatever the alignment, each alignment its own', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-aligns'));
    const grids = present.locator('.deck-logo-grid');
    await expect(grids).toHaveCount(ALIGNS.length);
    for (const [index, align] of ALIGNS.entries()) {
      const grid = grids.nth(index);
      await expect(grid).toHaveAttribute('data-align', align);
      const [box] = await canvasBoxes(grid);
      const cells = await canvasBoxes(grid, '.deck-logo-cell');
      expect(cells).toHaveLength(4);
      expect(new Set(cells.map((cell) => Math.round(cell.cy))).size, `align ${align}: one row`).toBe(1);
      const first = cells[0];
      const last = cells[cells.length - 1];
      if (align === 'even' || align === 'start') {
        // Equal cells over the full width: the grid's four columns (`start`
        // under `columns` keeps them too, packed from the start).
        await expect(grid).toHaveAttribute('data-equal-cells', 'true');
        for (const cell of cells) expect(near(cell.width, first.width, 1), `${align}: ${cell.width} vs ${first.width}`).toBe(true);
        expect(near(first.left, box.left, 1) && near(last.left + last.width, box.left + box.width, 1)).toBe(true);
      } else if (align === 'stretch') {
        // Spread edge to edge: the first flush left, the last flush right.
        await expect(grid).not.toHaveAttribute('data-equal-cells');
        expect(near(first.left, box.left, 1) && near(last.left + last.width, box.left + box.width, 1)).toBe(true);
      } else {
        // Packed around the centre: the four cells take less than the width.
        await expect(grid).not.toHaveAttribute('data-equal-cells');
        const span = last.left + last.width - first.left;
        expect(span).toBeLessThan(box.width - 100);
        expect(near(first.left + span / 2, box.cx, 1)).toBe(true);
      }
    }
  });

  test('sibling grids at one tile_height share one tile, a seal fills it, start keeps four equal cells', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-tiles'));
    // Four tiles of 96 px fit the body as they are: the measures below are canvas px, unscaled.
    await expect(present.locator('.deck-fit[data-fit]')).toHaveCount(0);
    const grids = present.locator('.deck-logo-grid');
    await expect(grids).toHaveCount(4);
    const gap = 24; // `--logo-gap` at the default `gap: md`: `--deck-space-3`.
    const cellWidths: number[] = [];
    for (const index of [0, 1, 2, 3]) {
      const grid = grids.nth(index);
      await expect(grid).toHaveAttribute('data-tile', 'true');
      await expect(grid).toHaveAttribute('data-equal-cells', 'true');
      await expect(grid).toHaveAttribute('data-align', 'start');
      const [box] = await canvasBoxes(grid);
      const cells = await canvasBoxes(grid, '.deck-logo-cell');
      expect(cells.length, `grid ${index + 1}`).toBe([4, 3, 2, 4][index]);
      // (1) Every tile is the height asked for, whatever the grid holds.
      for (const cell of cells) expect(near(cell.height, TILE_HEIGHT, 1), `grid ${index + 1}: tile ${cell.height}px`).toBe(true);
      // (3) `start` with `columns: 4`: a quarter of the row each, packed from
      // the start - a short row's cells keep the full row's width.
      const quarter = (box.width - 3 * gap) / 4;
      for (const [cellIndex, cell] of cells.entries()) {
        expect(near(cell.width, quarter, 1), `grid ${index + 1}, cell ${cellIndex + 1}: ${cell.width} vs ${quarter}`).toBe(true);
        expect(near(cell.left, box.left + cellIndex * (quarter + gap), 1), `grid ${index + 1}, cell ${cellIndex + 1} at ${cell.left}`).toBe(true);
      }
      cellWidths.push(cells[0].width);
    }
    // The four grids line up column for column.
    for (const width of cellWidths) expect(near(width, cellWidths[0], 1)).toBe(true);

    // (2) In the fixed tile the seal grows past the 1.5x of `logo_height` a
    // hugging cell allows, up to the inner height (the tile less the framed
    // tile's 8 px frame), while the wordmarks sit at `logo_height: md`, 40 px.
    const inner = TILE_HEIGHT - 2 * TILE_PAD_Y;
    // The fixture labels name each logo's kind (`seal 4`, `wordmark 1`), as the alt text.
    const alts = await present.locator('.deck-logo-mark').evaluateAll((nodes) => nodes.map((node) => node.querySelector('img')?.alt ?? ''));
    const boxes = await canvasBoxes(present, '.deck-logo-mark');
    expect(boxes).toHaveLength(alts.length);
    const seals = boxes.filter((_, index) => alts[index].startsWith('seal'));
    const wordmarks = boxes.filter((_, index) => alts[index].startsWith('wordmark'));
    expect(seals.length).toBeGreaterThanOrEqual(2);
    expect(wordmarks.length).toBeGreaterThanOrEqual(8);
    for (const seal of seals) {
      expect(seal.height, `seal ${seal.height}px vs the inner ${inner}px`).toBeLessThanOrEqual(inner + 1);
      expect(seal.height).toBeGreaterThan(40 * 1.5);
    }
    for (const wordmark of wordmarks) expect(near(wordmark.height, 40, HEIGHT_TOLERANCE), `wordmark ${wordmark.height}px`).toBe(true);
    // Every mark stays inside its tile.
    const cells = await canvasBoxes(present, '.deck-logo-cell');
    for (const mark of boxes) {
      const cell = cells.find((c) => mark.cx > c.left && mark.cx < c.left + c.width && mark.cy > c.top && mark.cy < c.top + c.height);
      expect(cell, `a cell holds the mark at ${mark.cx},${mark.cy}`).toBeDefined();
      expect(mark.top).toBeGreaterThanOrEqual(cell!.top + TILE_PAD_Y - 1);
      expect(mark.top + mark.height).toBeLessThanOrEqual(cell!.top + cell!.height - TILE_PAD_Y + 1);
    }
  });

  test('a labelled row of fixed tiles keeps one label baseline, whatever the shape of each mark', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-tile-labels'));
    const grid = present.locator('.deck-logo-grid');
    await expect(grid).toHaveAttribute('data-tile', 'true');
    await expect(grid).toHaveAttribute('data-labels', 'true');
    const cells = await canvasBoxes(grid, '.deck-logo-cell');
    const labels = await canvasBoxes(grid, '.deck-logo-label');
    expect(cells).toHaveLength(4);
    expect(labels).toHaveLength(4);
    // One row: every label top the same within 1 px, measured from its tile.
    const offsets = labels.map((label, index) => label.top - cells[index].top);
    for (const offset of offsets) expect(near(offset, offsets[0], 1), `label offsets ${offsets.join(', ')}`).toBe(true);
    // On the bottom frame, inside the tile.
    labels.forEach((label, index) => {
      const bottom = cells[index].top + cells[index].height;
      expect(label.top + label.height).toBeLessThanOrEqual(bottom - TILE_PAD_Y + 1);
      expect(label.top + label.height).toBeGreaterThanOrEqual(bottom - TILE_PAD_Y - 2);
    });
    // The marks stay centred in the space above the label (the cell's 8 px
    // gap and 1 px border taken into the tolerance).
    const marks = await canvasBoxes(grid, '.deck-logo-mark');
    marks.forEach((mark, index) => {
      const room = (cells[index].top + TILE_PAD_Y + labels[index].top) / 2;
      expect(near(mark.cy, room, 6), `mark ${index + 1} centred at ${mark.cy} vs ${room}`).toBe(true);
    });
  });

  test('a columns block of four stays four on 16:9', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-columns-4'));
    const block = present.locator('.deck-columns');
    await expect(block).toHaveAttribute('data-count', '4');
    await expect(block).not.toHaveAttribute('data-wrapped');
    const [box] = await canvasBoxes(block);
    const columns = await canvasBoxes(block, ':scope > .deck-column');
    expect(columns).toHaveLength(4);
    for (const column of columns) {
      expect(near(column.top, columns[0].top, 1)).toBe(true);
      expect(near(column.width, columns[0].width, 1)).toBe(true);
    }
    expect(near(columns[0].left, box.left, 1) && near(columns[3].left + columns[3].width, box.left + box.width, 1)).toBe(true);
    // Each small grid keeps its logo at its size: nothing was squeezed to fit.
    for (const mark of await canvasBoxes(block, '.deck-logo-mark')) expect(near(mark.height, 40, HEIGHT_TOLERANCE)).toBe(true);
  });

  test('four columns too wide for a quarter each wrap 2 x 2, and the audit says so', async ({ page }) => {
    test.setTimeout(180_000);
    const present = await openStage(page, deckId, position('s-columns-wrap'));
    const block = present.locator('.deck-columns');
    await expect(block).toHaveAttribute('data-wrapped', '2');
    const need = Number(await block.getAttribute('data-column-need'));
    const room = Number(await block.getAttribute('data-column-room'));
    expect(need).toBeGreaterThan(room);
    const columns = await canvasBoxes(block, ':scope > .deck-column');
    expect(new Set(columns.map((column) => Math.round(column.top))).size).toBe(2);

    const { apiCtx, token } = await createAdminApiContext();
    try {
      const response = await apiCtx.post(`/api/v1/presentations/${deckId}/layout-check`, {
        headers: { Authorization: `Bearer ${token}` },
        timeout: 150_000,
      });
      expect(response.ok(), `layout check failed: ${response.status()} ${await response.text()}`).toBeTruthy();
      const report = (await response.json()) as {
        skipped: string | null;
        findings: { slide_index: number; kind: string; severity: string; message: string; amount: number | null }[];
      };
      expect(report.skipped).toBeNull();
      const wrapped = report.findings.filter((finding) => finding.kind === 'columns');
      expect(wrapped.map((finding) => finding.slide_index)).toEqual([position('s-columns-wrap') - 1]);
      expect(wrapped[0].severity).toBe('dense');
      expect(wrapped[0].message).toContain('cannot keep its 4 columns side by side');
      // The width the widest column needs, measured by the server's own render.
      expect(wrapped[0].amount ?? 0).toBeGreaterThan(room);
    } finally {
      await apiCtx.dispose();
    }
  });

  test('equal row heights give the cards of a row one height, whatever each holds', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-equal-rows'));
    await expect(present.locator('.deck-columns-slide')).toHaveAttribute('data-equal-rows', 'true');
    // DOM order: the first column's two cards, then the second's.
    const [first, second, third, fourth] = await canvasBoxes(present, '.deck-column .deck-phase');
    for (const [left, right, row] of [
      [first, third, 1],
      [second, fourth, 2],
    ] as const) {
      expect(near(left.top, right.top, 1), `row ${row}: tops ${left.top} / ${right.top}`).toBe(true);
      expect(near(left.height, right.height, 1), `row ${row}: heights ${left.height} / ${right.height}`).toBe(true);
    }
    // Three bullets against one: the rows are the tallest card's, not a shared stretch.
    expect(second.top).toBeGreaterThan(first.top + first.height);
  });

  test('a block is added by hand from the "+" at the end of the body', async ({ page }) => {
    const slide = position('s-add-block');
    await page.goto(`/presentations/${deckId}?panel=editor#/${slide - 1}`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    await expect(page.getByTestId('presentation.editorPanel')).toBeVisible();
    await expect(page.locator('[data-deck-editing="true"]')).toBeVisible();
    const present = page.locator('.reveal .slides > section.present');
    await expect(present.locator('[data-edit-block]')).toHaveCount(1);
    // One "+" for the slide's one run, under its last block.
    const add = page.locator('[data-testid="presentation.runAdd"][data-run="blocks"]');
    await expect(add).toHaveCount(1);
    const [only] = await canvasBoxes(present, '[data-edit-block="blocks.0"]');
    const [plus] = await canvasBoxes(add);
    expect(plus.top).toBeGreaterThanOrEqual(only.top + only.height);
    await add.getByRole('button', { name: 'Add a block' }).click();
    await page.getByTestId('overflow-menu-item-insert-text').click();
    // The new block lands after the last one, selected, and saves itself.
    const added = present.locator('[data-edit-block="blocks.1"]');
    await expect(added).toBeVisible();
    await expect(added).toHaveAttribute('data-edit-selected', 'true');
    await expectEditorSaved(page);
    // The deck is shared by the other tests: the block goes again, saved too.
    await page.keyboard.press('Delete');
    await expect(present.locator('[data-edit-block]')).toHaveCount(1);
    await expectEditorSaved(page);
  });

  test('the editable PPTX places each step logo as a picture centred on its node', async ({ page }) => {
    test.setTimeout(180_000);
    const present = await openStage(page, deckId, position('s-rounds'));
    const nodes = await canvasBoxes(present, '.deck-timeline-node');
    const marks = await canvasBoxes(present, '.deck-timeline-logo .deck-logo-mark');
    const { apiCtx, token } = await createAdminApiContext();
    let archive: Buffer;
    try {
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
    // The canvas is 1920 x 1080; the PPTX boxes are fractions of the slide.
    const pictures = slidePictureBoxes(unzip(archive), position('s-rounds')).map((box) => ({
      cx: (box.left + box.width / 2) * 1920,
      cy: (box.top + box.height / 2) * 1080,
    }));
    const row = pictures.filter((picture) => near(picture.cy, marks[0].cy, 30));
    expect(row, JSON.stringify(pictures)).toHaveLength(4);
    row.sort((a, b) => a.cx - b.cx);
    row.forEach((picture, index) => {
      expect(near(picture.cx, nodes[index].cx, CENTRE_TOLERANCE), `step ${index + 1}: picture ${picture.cx} vs node ${nodes[index].cx}`).toBe(true);
    });
  });
});
