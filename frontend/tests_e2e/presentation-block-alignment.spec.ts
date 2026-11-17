import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - the alignment of five blocks on the 1920 x 1080 canvas.
//
// 1. A vertical `process_flow` is ONE grid of icon / title / description
//    columns every step subgrids into: the descriptions start at one x, the
//    title column is the widest title's up to 40% of the flow, and a title
//    that fits never wraps while the description column still can.
// 2. A `phases` timeline shares its rows: a title wrapping to two lines on
//    one step holds the title row for all, and the descriptions start on one
//    line.
// 3. A `pull` quote aligned `center` drops the side bar for a short rule
//    centred over the text.
// 4. An inline-headed callout's icon sits on the title's first line, for a
//    one-line and a two-line title alike.
// 5. A metric icon's INK - not its box - shares the value's left edge.
// 6. A horizontal `process_flow` is laid on the equal-columns grid:
//    every step the same width, the last included, each on the edges of the
//    column of a `columns` block of the same count above it, the arrows
//    centred in the gutters; a loop keeps one trailing gutter for its return
//    arrow.
//
// Geometry is read off the live viewer in canvas px (the stage scale divided
// out), the way the layout audit and the PPTX extractor read it. The deck is
// imported through the API from a bare spec (no LLM call) and deleted
// afterwards; the file runs SERIALLY.
// ---------------------------------------------------------------------------

/** Canvas px: a hair of rounding on the scaled stage, never a layout difference. */
const TOLERANCE = 1;
/** Canvas px the title column may take at most: 40% of the flow. */
const TITLE_COLUMN_SHARE = 0.4;

const step = (icon: string | undefined, title: string, description: string) => ({ icon, title, description });

/** Short and long titles; the first and third descriptions run to two lines in any column the flow leaves them. */
const FLOW_STEPS = [
  step(
    'search',
    'Detect',
    'Continuous discovery of exposures across the whole estate - assets, identities, cloud accounts and third parties - correlated with the live threat landscape every day.',
  ),
  step('target', 'Prioritize what matters', 'Rank by exploitability and business impact.'),
  step(
    'shield',
    'Validate',
    'Attack simulation confirms which exposures are actually reachable from the outside and which of the deployed controls hold up under pressure.',
  ),
  step('rocket', 'Mobilize the response', 'Tickets, owners and deadlines.'),
  step('activity', 'Measure', 'Trend the exposure window and the time to remediate, quarter over quarter.'),
];

const flow = (count: number, icons = true) => ({
  type: 'process_flow',
  direction: 'vertical',
  steps: FLOW_STEPS.slice(0, count).map((s) => (icons ? s : { ...s, icon: undefined })),
});

/** Short titles and one-line descriptions: nothing widens a step, the grid alone sizes them. */
const ROW_STEPS = [
  step('search', 'Detect', 'Find the exposures.'),
  step('target', 'Prioritize', 'Rank them.'),
  step('shield', 'Validate', 'Prove them.'),
  step('rocket', 'Mobilize', 'Fix them.'),
];

const rowFlow = (count: number, loop = false) => ({
  type: 'process_flow',
  direction: 'horizontal',
  loop: loop || undefined,
  steps: ROW_STEPS.slice(0, count),
});

/** A `columns` block of `count` equal columns at the default (`md`) gap: the grid a flow lines up under. */
const columnsBlock = (count: number) => ({
  type: 'columns',
  columns: Array.from({ length: count }, (_, index) => [{ type: 'text', content: `Column ${index + 1}` }]),
});

const phase = (label: string, title: string, description: string) => ({ label, title, description });

const PHASES_ONE_LINE = [
  phase('Q1', 'Discover', 'Inventory every asset and its owner.'),
  phase('Q2', 'Assess', 'Score the exposures.'),
  phase('Q3', 'Validate', 'Simulate the attack paths that matter.'),
  phase('Q4', 'Mobilize', 'Fix, verify, report.'),
];

const PHASES_TWO_LINES = [
  phase('Q1', 'Discover', 'Inventory every asset and its owner.'),
  // Long enough to wrap in a quarter of the body: the title row grows for every step.
  phase('Q2', 'Assess and prioritize every exposure by impact', 'Score the exposures.'),
  phase('Q3', 'Validate', 'Simulate the attack paths that matter.'),
  phase('Q4', 'Mobilize', 'Fix, verify, report.'),
];

const QUOTE = 'If I took OpenCTI away from my team, they would walk out.';
const CALLOUT_ONE_LINE = 'One line of title';
/** Two lines in a half-width column at the title size (about 38 characters a line). */
const CALLOUT_TWO_LINES = 'A callout title that wraps onto a second line of its own';

const content = (id: string, title: string, ...blocks: object[]) => ({ id, layout: 'content', title, blocks });

const FIXTURE_SPEC = {
  title: 'E2E block alignment',
  theme: 'light',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Alignment' },
    content('s-flow-3', 'Three steps', flow(3)),
    // Four steps, the third without an icon: its title must not slide into the icon column.
    content('s-flow-4', 'Four steps, one without an icon', {
      ...flow(4),
      steps: flow(4).steps.map((s, index) => (index === 2 ? { ...s, icon: undefined } : s)),
    }),
    // Five steps with no icon at all: two columns, the titles flush with the card's padding.
    content('s-flow-5', 'Five steps, no icons', flow(5, false)),
    // A horizontal flow under a columns block of the same count: three, four, and a loop of three.
    content('s-flow-row-3', 'Three steps under three columns', columnsBlock(3), rowFlow(3)),
    content('s-flow-row-4', 'Four steps under four columns', columnsBlock(4), rowFlow(4)),
    content('s-flow-row-loop', 'A loop of three under three columns', columnsBlock(3), rowFlow(3, true)),
    content('s-timeline-1', 'Phases, one-line titles', { type: 'timeline', style: 'phases', items: PHASES_ONE_LINE }),
    content('s-timeline-2', 'Phases, a two-line title', { type: 'timeline', style: 'phases', items: PHASES_TWO_LINES }),
    content('s-quote-start', 'Pull quote, start', { type: 'quote', variant: 'pull', content: QUOTE, author: 'Dana Whitlock' }),
    content('s-quote-center', 'Pull quote, centred', {
      type: 'quote',
      variant: 'pull',
      align: 'center',
      content: QUOTE,
      author: 'Dana Whitlock',
    }),
    content('s-callout-1', 'Inline callout, one line', {
      type: 'callout',
      header: 'inline',
      icon: 'lightbulb',
      title: CALLOUT_ONE_LINE,
      content: 'The body under the head.',
    }),
    {
      id: 's-callout-2',
      layout: 'content',
      title: 'Inline callout, two lines',
      columns: [
        [{ type: 'callout', header: 'inline', icon: 'lightbulb', title: CALLOUT_TWO_LINES, content: 'The body under the head.' }],
        [{ type: 'text', content: 'The other column.' }],
      ],
    },
    // `lg` scales the tile's icon to 50 px, so the inset is a visible 2.3 px.
    content('s-metrics-grid', 'A grid of metrics with icons', {
      type: 'metrics_grid',
      size: 'lg',
      items: [
        { value: '42%', label: 'Fewer alerts', icon: 'globe' },
        { value: '11 min', label: 'Time to triage', icon: 'clock' },
        { value: '3x', label: 'Coverage', icon: 'target' },
      ],
    }),
    content(
      's-metrics-lone',
      'Lone metrics with icons',
      { type: 'metric', value: '80K', label: 'Employees', icon: 'globe' },
      { type: 'metric', value: '80K', label: 'Centred', icon: 'globe', align: 'center' },
    ),
  ],
};

const position = (id: string): number => FIXTURE_SPEC.slides.findIndex((s) => s.id === id) + 1;

async function importDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'e2e-block-alignment.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8'),
      },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id } = (await resp.json()) as { id: string };
  return id;
}

async function openStage(page: Page, deckId: string, slide: number): Promise<Locator> {
  await page.goto(`/presentations/${deckId}#/${slide - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${slide} of \\d+\\.`) })).toBeVisible();
  const present = page.locator('.reveal .slides > section.present');
  // The icons' lazy chunk has landed, and nothing was shrunk to fit: the measures are canvas px as drawn.
  await expect(present.locator('[data-deck-pending]')).toHaveCount(0);
  await expect(present.locator('.deck-fit[data-fit]')).toHaveCount(0);
  return present;
}

type Box = { left: number; top: number; right: number; bottom: number; width: number; height: number; cx: number; cy: number };

/**
 * Measures under `root`, in canvas px from the slide frame's corner (the
 * frame is the root's ancestor when the root is a block, its descendant when
 * it is the slide). One page-side function, three readings:
 * - `box`: the border box of every element matching `selector` (`root`'s own without one);
 * - `ink`: the left edge of every icon's INK - its drawn geometry (`getBBox`,
 *   in its 24-unit grid) widened by half the stroke, where the eye reads the
 *   icon as starting - never its box;
 * - `first-line`: the box of the first line of text of the first match (a
 *   Range over its contents, the top-most fragment).
 */
async function measure(root: Locator, kind: 'box' | 'ink' | 'first-line', selector?: string): Promise<Box[]> {
  return root.evaluate(
    (el, { kind: what, query }) => {
      const frame =
        el.closest<HTMLElement>('.deck-slide') ??
        el.querySelector<HTMLElement>('.deck-slide') ??
        document.querySelector<HTMLElement>('.reveal .slides > section.present .deck-slide');
      if (!frame) throw new Error('No slide frame');
      const origin = frame.getBoundingClientRect();
      const scale = origin.width / frame.offsetWidth;
      const canvas = (r: DOMRect) => {
        const left = (r.left - origin.left) / scale;
        const top = (r.top - origin.top) / scale;
        const width = r.width / scale;
        const height = r.height / scale;
        return { left, top, right: left + width, bottom: top + height, width, height, cx: left + width / 2, cy: top + height / 2 };
      };
      const targets = query ? Array.from(el.querySelectorAll(query)) : [el];
      if (what === 'box') return targets.map((target) => canvas(target.getBoundingClientRect()));
      if (what === 'ink') {
        return targets.map((target) => {
          const svg = target as SVGSVGElement;
          const box = canvas(svg.getBoundingClientRect());
          const grid = svg.viewBox.baseVal.width || 24;
          const stroke = Number.parseFloat(svg.getAttribute('stroke-width') ?? '2');
          const inkLeft = box.left + ((svg.getBBox().x - stroke / 2) * box.width) / grid;
          return { ...box, left: inkLeft, width: box.right - inkLeft, cx: (inkLeft + box.right) / 2 };
        });
      }
      const [target] = targets;
      if (!target) throw new Error(`No ${query} in the slide`);
      const range = document.createRange();
      range.selectNodeContents(target);
      const rects = Array.from(range.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
      if (!rects.length) throw new Error(`No text in ${query}`);
      return [canvas(rects.reduce((top, r) => (r.top < top.top ? r : top)))];
    },
    { kind, query: selector },
  );
}

const boxes = (root: Locator, selector?: string) => measure(root, 'box', selector);
const near = (a: number, b: number, tolerance = TOLERANCE) => Math.abs(a - b) <= tolerance;
const allNear = (values: number[], tolerance = TOLERANCE) => values.every((value) => near(value, values[0], tolerance));
const list = (values: number[]) => values.map((value) => value.toFixed(1)).join(' / ');

/** The canvas px of a computed length on `locator`. */
async function computedPx(locator: Locator, property: string): Promise<number> {
  return locator.evaluate((el, name) => Number.parseFloat(getComputedStyle(el).getPropertyValue(name)), property);
}

test.describe('Presentations - block alignment on the 1920 x 1080 canvas', () => {
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

  for (const [id, count, icons] of [
    ['s-flow-3', 3, true],
    ['s-flow-4', 4, true],
    ['s-flow-5', 5, false],
  ] as const) {
    test(`a vertical flow of ${count} steps lines its descriptions up on one x and keeps its titles on one line`, async ({
      page,
    }) => {
      const present = await openStage(page, deckId, position(id));
      const flowBlock = present.locator('.deck-flow[data-direction="vertical"]');
      if (icons) await expect(flowBlock).toHaveAttribute('data-icons', 'true');
      else await expect(flowBlock).not.toHaveAttribute('data-icons');
      const [box] = await boxes(flowBlock);
      const cards = await boxes(flowBlock, '.deck-flow-card');
      const titles = await boxes(flowBlock, '.deck-flow-title');
      const descs = await boxes(flowBlock, '.deck-flow-desc');
      expect(cards).toHaveLength(count);
      expect(titles).toHaveLength(count);
      expect(descs).toHaveLength(count);
      // One x for every description and every title, whatever the title beside it.
      expect(allNear(descs.map((d) => d.left)), `description x: ${list(descs.map((d) => d.left))}`).toBe(true);
      expect(allNear(titles.map((t) => t.left)), `title x: ${list(titles.map((t) => t.left))}`).toBe(true);
      // The title column is the widest title's - a fifth of this flow - never the 40% it may take at most.
      const titleColumn = Math.max(...titles.map((t) => t.width));
      expect(titleColumn).toBeLessThanOrEqual(box.width * TITLE_COLUMN_SHARE + TOLERANCE);
      expect(titleColumn).toBeLessThan(box.width * 0.3);
      // No title wraps (one line is 35 canvas px: 28px at 1.25), while the longest description does:
      // the description column is the one that gives.
      const titleLine = await computedPx(flowBlock.locator('.deck-flow-title').first(), 'line-height');
      for (const title of titles) expect(title.height, `a title of ${title.height}px`).toBeLessThan(titleLine * 1.5);
      const descLine = await computedPx(flowBlock.locator('.deck-flow-desc').first(), 'line-height');
      expect(descs[0].height, 'the first description wraps').toBeGreaterThan(descLine * 1.5);
      // Every description starts past the title column and ends inside the flow.
      const titleRight = Math.max(...titles.map((t) => t.right));
      for (const desc of descs) {
        expect(desc.left).toBeGreaterThanOrEqual(titleRight - TOLERANCE);
        expect(desc.right).toBeLessThanOrEqual(box.right + TOLERANCE);
      }
      if (icons) {
        const iconBoxes = await boxes(flowBlock, '.deck-flow-icon');
        // The step without an icon (the third of four) still keeps its title on the title column.
        expect(iconBoxes).toHaveLength(id === 's-flow-4' ? count - 1 : count);
        expect(allNear(iconBoxes.map((i) => i.left)), `icon x: ${list(iconBoxes.map((i) => i.left))}`).toBe(true);
        for (const icon of iconBoxes) expect(icon.right).toBeLessThanOrEqual(titles[0].left);
      } else {
        // Two columns: the titles sit at the card's own left edge, its border and padding in.
        const card = flowBlock.locator('.deck-flow-card').first();
        const inset = (await computedPx(card, 'border-left-width')) + (await computedPx(card, 'padding-left'));
        for (const [index, title] of titles.entries()) {
          expect(near(title.left, cards[index].left + inset), `step ${index + 1}: title at ${title.left}, card at ${cards[index].left}`).toBe(true);
        }
      }
    });
  }

  for (const [id, count, loop] of [
    ['s-flow-row-3', 3, false],
    ['s-flow-row-4', 4, false],
    ['s-flow-row-loop', 3, true],
  ] as const) {
    test(`a horizontal ${loop ? 'loop ' : ''}flow of ${count} steps sizes every step equally and lines up under ${count} columns`, async ({
      page,
    }) => {
      const present = await openStage(page, deckId, position(id));
      const flowBlock = present.locator('.deck-flow[data-direction="horizontal"]');
      await expect(flowBlock).toHaveCount(1);
      // Nothing widened a step: the titles kept their size and no word overflowed.
      await expect(flowBlock).not.toHaveAttribute('data-title-fit');
      const [box] = await boxes(flowBlock);
      const cards = await boxes(flowBlock, '.deck-flow-card');
      const arrows = await boxes(flowBlock, '.deck-flow-arrow');
      const icons = await boxes(flowBlock, '.deck-flow-arrow .deck-icon');
      const columns = await boxes(present.locator('.deck-blocks > .deck-columns'), '.deck-column');
      expect(cards).toHaveLength(count);
      expect(columns).toHaveLength(count);
      expect(arrows).toHaveLength(loop ? count : count - 1);
      // The gutter is the columns block's gap.
      const gap = await computedPx(flowBlock, 'column-gap');
      expect(near(gap, await computedPx(present.locator('.deck-blocks > .deck-columns'), 'column-gap'))).toBe(true);
      // Every step the same width, the last one included.
      expect(allNear(cards.map((c) => c.width)), `card widths: ${list(cards.map((c) => c.width))}`).toBe(true);
      // Edge to edge: the first card on the block's left edge, the last on its right (less the loop's trailing gutter).
      expect(near(cards[0].left, box.left)).toBe(true);
      expect(near(cards[count - 1].right, loop ? box.right - gap : box.right)).toBe(true);
      if (loop) {
        // The return arrow fills the trailing gutter; each card is a gutter's share narrower than its column.
        expect(near(arrows[count - 1].right, box.right)).toBe(true);
        expect(near(cards[0].width, columns[0].width - gap / count)).toBe(true);
      } else {
        // Each card on the edges of the column above it.
        for (const [index, card] of cards.entries()) {
          expect(near(card.left, columns[index].left), `card ${index + 1} left ${card.left} vs column ${columns[index].left}`).toBe(true);
          expect(near(card.right, columns[index].right), `card ${index + 1} right ${card.right} vs column ${columns[index].right}`).toBe(true);
        }
      }
      // Every arrow fills the gutter after its card - as wide as the gap, as tall as the card - with its icon centred in it.
      for (const [index, arrow] of arrows.entries()) {
        const card = cards[index];
        expect(near(arrow.left, card.right), `arrow ${index + 1} starts at ${arrow.left}, card ends at ${card.right}`).toBe(true);
        expect(near(arrow.width, gap)).toBe(true);
        expect(near(arrow.top, card.top) && near(arrow.bottom, card.bottom), `arrow ${index + 1} off its card's height`).toBe(true);
        expect(near(icons[index].cx, card.right + gap / 2), `icon ${index + 1} centre ${icons[index].cx} vs gutter centre ${card.right + gap / 2}`).toBe(true);
        if (index + 1 < count) expect(near(arrow.right, cards[index + 1].left)).toBe(true);
      }
    });
  }

  for (const [id, wrapped] of [
    ['s-timeline-1', false],
    ['s-timeline-2', true],
  ] as const) {
    test(`phases with ${wrapped ? 'a two-line' : 'one-line'} titles start their descriptions on one line`, async ({ page }) => {
      const present = await openStage(page, deckId, position(id));
      const timeline = present.locator('.deck-timeline[data-style="phases"]');
      const nodes = await boxes(timeline, '.deck-timeline-node');
      const labels = await boxes(timeline, '.deck-timeline-label');
      const titles = await boxes(timeline, '.deck-timeline-title');
      const descs = await boxes(timeline, '.deck-timeline-desc');
      expect(nodes).toHaveLength(4);
      expect(labels).toHaveLength(4);
      expect(titles).toHaveLength(4);
      expect(descs).toHaveLength(4);
      // Shared rows: one y for the labels, one for the titles, one for the descriptions.
      expect(allNear(nodes.map((n) => n.top)), `node y: ${list(nodes.map((n) => n.top))}`).toBe(true);
      expect(allNear(labels.map((l) => l.top)), `label y: ${list(labels.map((l) => l.top))}`).toBe(true);
      expect(allNear(titles.map((t) => t.top)), `title y: ${list(titles.map((t) => t.top))}`).toBe(true);
      expect(allNear(descs.map((d) => d.top)), `description y: ${list(descs.map((d) => d.top))}`).toBe(true);
      // Every description starts under the LOWEST title: the title row is the tallest title's.
      const lowestTitle = Math.max(...titles.map((t) => t.bottom));
      for (const desc of descs) expect(desc.top).toBeGreaterThanOrEqual(lowestTitle - TOLERANCE);
      // Each piece stays centred on its own node.
      for (const [index, title] of titles.entries()) expect(near(title.cx, nodes[index].cx, 2), `title ${index + 1} off its node`).toBe(true);
      for (const [index, desc] of descs.entries()) expect(near(desc.cx, nodes[index].cx, 2), `description ${index + 1} off its node`).toBe(true);
      const titleLine = await computedPx(timeline.locator('.deck-timeline-title').first(), 'line-height');
      const twoLines = titles.filter((t) => t.height > titleLine * 1.5);
      if (wrapped) {
        // The second title wraps; the others, one line, still hold the same row.
        expect(twoLines).toHaveLength(1);
        expect(titles[1].height).toBeGreaterThan(titleLine * 1.5);
      } else {
        expect(twoLines).toHaveLength(0);
      }
    });
  }

  test('a pull quote keeps its side bar at the start and trades it for a centred rule when centred', async ({ page }) => {
    const start = await openStage(page, deckId, position('s-quote-start'));
    const startQuote = start.locator('.deck-quote[data-variant="pull"]');
    await expect(startQuote).not.toHaveAttribute('data-align');
    await expect(startQuote.locator('.deck-quote-rule')).toHaveCount(0);
    expect(await computedPx(startQuote, 'border-left-width')).toBe(12);

    const centred = await openStage(page, deckId, position('s-quote-center'));
    const quote = centred.locator('.deck-quote[data-variant="pull"][data-align="center"]');
    await expect(quote).toHaveCount(1);
    // No side bar, no inset for one.
    expect(await computedPx(quote, 'border-left-width')).toBe(0);
    expect(await computedPx(quote, 'padding-left')).toBe(0);
    const rule = quote.locator('.deck-quote-rule');
    await expect(rule).toHaveCount(1);
    await expect(rule).toHaveAttribute('aria-hidden', 'true');
    const [ruleBox] = await boxes(quote, '.deck-quote-rule');
    const [text] = await boxes(quote, 'blockquote');
    const [figure] = await boxes(quote);
    // A short rule - 96 x 8 canvas px - centred over the text, above it.
    expect(near(ruleBox.width, 96) && near(ruleBox.height, 8), `${ruleBox.width} x ${ruleBox.height}`).toBe(true);
    expect(near(ruleBox.cx, text.cx), `rule ${ruleBox.cx} vs text ${text.cx}`).toBe(true);
    expect(near(ruleBox.cx, figure.cx)).toBe(true);
    expect(ruleBox.bottom).toBeLessThan(text.top);
    // Painted in the accent, so the PPTX extractor carries it as a shape.
    const painted = await rule.evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(painted).not.toBe('rgba(0, 0, 0, 0)');
  });

  for (const [id, lines] of [
    ['s-callout-1', 1],
    ['s-callout-2', 2],
  ] as const) {
    test(`an inline callout's icon sits on the first line of a ${lines}-line title`, async ({ page }) => {
      const present = await openStage(page, deckId, position(id));
      const callout = present.locator('.deck-callout[data-header="inline"]');
      await expect(callout).toHaveCount(1);
      const [title] = await boxes(callout, '.deck-callout-title');
      const [icon] = await boxes(callout, '.deck-callout-icon .deck-icon');
      const [first] = await measure(callout, 'first-line', '.deck-callout-title');
      const titleLine = await computedPx(callout.locator('.deck-callout-title'), 'line-height');
      expect(near(title.height, titleLine * lines, 2), `a ${lines}-line title is ${title.height}px`).toBe(true);
      // Centred on the first line's box, not on the title's.
      expect(near(icon.cy, first.cy, 2), `icon ${icon.cy} vs first line ${first.cy}`).toBe(true);
      if (lines === 2) expect(icon.cy).toBeLessThan(title.cy - titleLine / 4);
      else expect(near(icon.cy, title.cy, 2)).toBe(true);
      // Beside the title, inside the head.
      expect(icon.right).toBeLessThanOrEqual(title.left);
    });
  }

  /** Every start-aligned tile on the slide: the icon's ink, the value and the label share one left edge. */
  async function expectInkOnTheEdge(present: Locator, count: number): Promise<void> {
    const tiles = present.locator('.deck-metric:not([data-align="center"])');
    await expect(tiles).toHaveCount(count);
    for (let index = 0; index < count; index += 1) {
      const tile = tiles.nth(index);
      const [value] = await boxes(tile, '.deck-metric-value');
      const [label] = await boxes(tile, '.deck-metric-label');
      const [iconBox] = await boxes(tile, '.deck-metric-icon .deck-icon');
      const [ink] = await measure(tile, 'ink', '.deck-metric-icon .deck-icon');
      expect(near(label.left, value.left), `tile ${index + 1}: label ${label.left} vs value ${value.left}`).toBe(true);
      // The ink, not the box: the box is pulled back by the icon's inset.
      expect(near(ink.left, value.left), `tile ${index + 1}: ink ${ink.left.toFixed(2)} vs value ${value.left.toFixed(2)}`).toBe(true);
      expect(iconBox.left).toBeLessThan(value.left - 1);
    }
  }

  test('a metric icon starts its ink on the edge the value and the label share, in a grid and alone', async ({ page }) => {
    const grid = await openStage(page, deckId, position('s-metrics-grid'));
    await expect(grid.locator('.deck-metrics')).toHaveAttribute('data-size', 'lg');
    await expectInkOnTheEdge(grid, 3);

    const lone = await openStage(page, deckId, position('s-metrics-lone'));
    await expectInkOnTheEdge(lone, 1);
    // A centred tile keeps its icon's box centred with the value.
    const centred = lone.locator('.deck-metric[data-align="center"]');
    const [centredValue] = await boxes(centred, '.deck-metric-value');
    const [centredIcon] = await boxes(centred, '.deck-metric-icon .deck-icon');
    expect(near(centredIcon.cx, centredValue.cx)).toBe(true);
  });
});
