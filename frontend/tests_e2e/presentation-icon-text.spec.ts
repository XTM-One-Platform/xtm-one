import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - an icon beside a label, on the 1920 x 1080 canvas.
//
// Every block that draws an icon next to a text label draws it on ONE shared
// row (`IconText`, `.deck-icon-text` in presentation.css): a table's header
// and body cells, a check list's items, badges, an inline callout head, a
// comparison side's label, a key / value label, a feature's tile (gap only:
// the tile centres on the whole text block, presentation-feature-centre), a
// metric's trend arrow and delta, an events timeline's node and body, a
// vertical process flow's icon and title (on the flow's shared columns), a
// profile line's plate and fact. For each, with a one-line and a two-line
// label where the block lets a label wrap (badges and profile lines are one
// line by design):
//
// 1. the gap between the icon's box and the text's box is at least 0.4em of
//    the label's type;
// 2. the icon's centre sits on the centre of the label's FIRST line, within
//    2 px - not on the centre of the whole label.
//
// And a horizontal process flow's step titles never break inside a word: a
// word that fits no step at the titles' size steps every title down one size
// when that fits it, and widens its step when it does not (`flow-fit.ts`).
//
// Geometry is read off the live viewer in canvas px (the stage scale divided
// out), the way the layout audit and the PPTX extractor read it. The deck is
// imported through the API from a bare spec (no LLM call) and deleted
// afterwards; the file runs SERIALLY. Neutral fixture text only.
// ---------------------------------------------------------------------------

/** Canvas px the icon's centre may sit off the first line's. */
const CENTRE_TOLERANCE = 2;
/** The least gap between the icon's box and the text's, in ems of the label's type. */
const MIN_GAP_EM = 0.4;
/** Canvas px: a hair of rounding on the scaled stage, never a layout difference. */
const TOLERANCE = 1;

const ONE_LINE = 'One line label';
/** Long enough to wrap once - never twice - in the column each block below gives it. */
const TWO_LINES = {
  tableColumnA: 'A heading long enough to wrap onto a second line of its own column',
  tableColumnB: 'Another heading long enough to wrap onto a second line of its column',
  halfColumn: 'A label long enough to wrap onto a second line in a half-width column of the slide',
  calloutTitle: 'A callout title that wraps onto a second line of its own',
  compareLabel: 'A side label long enough to wrap onto a second line',
  kvLabel: 'A key label long enough to wrap onto a second line in its column',
  featureTitle: 'A feature title long enough to wrap onto a second line',
  delta: 'a change written out in enough words to wrap',
  eventTitle: 'An event title long enough to wrap onto a second line of its own',
  flowTitle: 'A step title long enough to wrap onto a second line in its column',
};

const text = (content: string) => ({ type: 'text', content });
const content = (id: string, title: string, ...blocks: object[]) => ({ id, layout: 'content', title, blocks });
/** The block in the left column of a two-column slide: half the width, where a long label wraps. */
const halved = (id: string, title: string, block: object) => ({
  id,
  layout: 'content',
  title,
  columns: [[block], [text('The other column.')]],
});

const FLOW_WORDS = ['Plan', 'Review', 'Build', 'Verify', 'Ship', 'Learn'];
const flowOf = (titles: string[]) => ({
  type: 'process_flow',
  steps: titles.map((title) => ({ title, description: 'A short description.' })),
});

const FIXTURE_SPEC = {
  title: 'E2E icon beside a label',
  theme: 'light',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Icon beside a label' },
    // Two long columns, so neither takes the width the auto table layout would hand a lone one; one-word cells
    // elsewhere, which that layout cannot squeeze below their word.
    content('s-table', 'Table', {
      type: 'table',
      headers: [
        { text: 'Overview', icon: 'star' },
        { text: TWO_LINES.tableColumnA, icon: 'flag' },
        { text: TWO_LINES.tableColumnB, icon: 'tag' },
        { text: 'Centred', icon: 'check', align: 'center' },
      ],
      rows: [
        [{ text: 'First', icon: 'check' }, { text: TWO_LINES.tableColumnA, icon: 'info' }, { text: 'Short', icon: 'minus' }, { icon: 'check' }],
        [{ text: 'Second', icon: 'x' }, { text: 'Short' }, { text: TWO_LINES.tableColumnB, icon: 'info' }, { icon: 'x' }],
        [{ text: 'Third', icon: 'check' }, { text: 'Short' }, { text: 'Short' }, { icon: 'check' }],
      ],
    }),
    content('s-list', 'List', {
      type: 'list',
      style: 'check',
      columns: 2,
      items: [{ text: ONE_LINE }, { text: TWO_LINES.halfColumn }, { text: 'A third line', icon: 'star' }],
    }),
    content('s-badges', 'Badges', {
      type: 'badges',
      items: [
        { text: 'Badge one', icon: 'star' },
        { text: 'Badge two', icon: 'check', tone: 'success' },
      ],
    }),
    content('s-callout-1', 'Inline callout, one line', {
      type: 'callout',
      header: 'inline',
      icon: 'lightbulb',
      title: ONE_LINE,
      content: 'The body under the head.',
    }),
    halved('s-callout-2', 'Inline callout, two lines', {
      type: 'callout',
      header: 'inline',
      icon: 'lightbulb',
      title: TWO_LINES.calloutTitle,
      content: 'The body under the head.',
    }),
    content('s-compare', 'Comparison', {
      type: 'comparison',
      left: { label: ONE_LINE, icon: 'x', blocks: [text('Left side.')] },
      right: { label: TWO_LINES.compareLabel, icon: 'check', blocks: [text('Right side.')] },
    }),
    content('s-kv', 'Key / value', {
      type: 'key_value',
      items: [
        { label: ONE_LINE, value: 'Value one', icon: 'star' },
        { label: TWO_LINES.kvLabel, value: 'Value two', icon: 'flag' },
      ],
    }),
    content('s-features', 'Feature list', {
      type: 'feature_list',
      columns: 2,
      items: [
        { icon: 'star', title: ONE_LINE, description: 'A short description.' },
        { icon: 'flag', title: TWO_LINES.featureTitle, description: 'A short description.' },
      ],
    }),
    content('s-metrics', 'Metrics', {
      type: 'metrics_grid',
      columns: 4,
      items: [
        { value: '42', label: 'Label one', delta: '+12%', trend: 'up' },
        { value: '17', label: 'Label two', delta: TWO_LINES.delta, trend: 'down' },
        { value: '8', label: 'Label three', delta: '-3%', trend: 'flat' },
        { value: '5', label: 'Label four' },
      ],
    }),
    halved('s-timeline', 'Events timeline', {
      type: 'timeline',
      style: 'events',
      items: [
        { label: 'Step', title: ONE_LINE, description: 'A short description.', icon: 'star' },
        { title: TWO_LINES.eventTitle, description: 'A short description.', icon: 'flag' },
        { title: 'A third event', description: 'A short description.' },
      ],
    }),
    content('s-flow-vertical', 'Vertical flow', {
      type: 'process_flow',
      direction: 'vertical',
      steps: [
        { icon: 'star', title: ONE_LINE, description: 'A short description.' },
        { icon: 'flag', title: TWO_LINES.flowTitle, description: 'A short description.' },
        { title: 'A step without an icon', description: 'A short description.' },
      ],
    }),
    content('s-profiles', 'Profiles', {
      type: 'profiles',
      items: [
        {
          name: 'Person One',
          title: 'A role',
          lines: [
            { text: ONE_LINE, icon: 'star' },
            { text: 'A second fact', icon: 'flag' },
            { text: 'A third fact' },
          ],
        },
      ],
    }),
    // Six steps across the slide, a gutter apart: a step is about
    // 247 px and its card's text about 181 px, where a 14-letter word (197 px
    // at the titles' 28 px) does not fit but fits one size down (169 px at
    // 24 px), and a 20-letter word fits at neither size. Both margins are over
    // 10 px either way.
    content('s-flow-words-step', 'Flow, a word that fits one size down', flowOf(['Plan', 'Accountability', ...FLOW_WORDS.slice(2)])),
    content('s-flow-words-widen', 'Flow, a word that widens its step', flowOf(['Plan', 'Internationalisation', ...FLOW_WORDS.slice(2)])),
    content('s-flow-words-none', 'Flow, short words', flowOf(FLOW_WORDS)),
  ],
};

const position = (id: string): number => FIXTURE_SPEC.slides.findIndex((s) => s.id === id) + 1;

async function importDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'e2e-icon-text.json',
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

/** One icon + text pair as drawn, in canvas px. */
interface Pair {
  /** The slot's box (the icon's, or the anchor's: a tile, a node, a plate). */
  slot: Box;
  /** The drawn icon's box: the Lucide svg in the slot, else the slot itself (a numbered node). */
  icon: Box;
  /** The label's box, and its first line of text. */
  label: Box;
  firstLine: Box;
  /** How many lines the label runs to, off its height and line height. */
  lines: number;
  /** The row's type in canvas px: the em the gap is read against. */
  fontSize: number;
  text: string;
}

/**
 * Every pair under `root`: the rows matching `row`, each with its slot and
 * label (the shared classes, or the ones a grid block puts on its cells),
 * measured from the slide frame's corner with the stage scale divided out.
 */
async function pairs(
  root: Locator,
  row: string,
  slot = '.deck-icon-text-icon',
  label = '.deck-icon-text-label',
): Promise<Pair[]> {
  return root.evaluate(
    (el, q) => {
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
      const out: Pair[] = [];
      for (const rowEl of Array.from(el.querySelectorAll<HTMLElement>(q.row))) {
        const slotEl = rowEl.querySelector<HTMLElement>(q.slot);
        const labelEl = rowEl.querySelector<HTMLElement>(q.label);
        if (!slotEl || !labelEl) continue;
        const iconEl = slotEl.querySelector<SVGElement>('.deck-icon') ?? slotEl;
        // The text's own fragments: a range over an element that holds a block
        // (a title over a description) reports the block's whole box too.
        const rects: DOMRect[] = [];
        const walker = document.createTreeWalker(labelEl, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          if (!node.textContent?.trim()) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          rects.push(...Array.from(range.getClientRects()).filter((r) => r.width > 0 && r.height > 0));
        }
        if (!rects.length) throw new Error(`No text in ${q.label} of ${q.row}`);
        const first = rects.reduce((top, r) => (r.top < top.top ? r : top));
        // Computed lengths are canvas px already: the stage transform scales rects, not styles.
        const lineHeight = Number.parseFloat(getComputedStyle(labelEl).lineHeight);
        const firstChild = labelEl.firstElementChild as HTMLElement | null;
        const measured = firstChild && firstChild.getClientRects().length ? firstChild : labelEl;
        const labelBox = canvas(measured.getBoundingClientRect());
        out.push({
          slot: canvas(slotEl.getBoundingClientRect()),
          icon: canvas(iconEl.getBoundingClientRect()),
          label: labelBox,
          firstLine: canvas(first),
          lines: Math.round(labelBox.height / lineHeight),
          fontSize: Number.parseFloat(getComputedStyle(rowEl).fontSize),
          text: (labelEl.textContent ?? '').trim(),
        });
      }
      return out;
    },
    { row, slot, label },
  );
}

/** The words of every element matching `selector` under `root` that are drawn across two lines. */
async function wordsBrokenInside(root: Locator, selector: string): Promise<string[]> {
  return root.evaluate((el, query) => {
    const broken: string[] = [];
    for (const title of Array.from(el.querySelectorAll(query))) {
      const walker = document.createTreeWalker(title, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const content = node.textContent ?? '';
        for (const match of content.matchAll(/\S+/g)) {
          const start = match.index ?? 0;
          const range = document.createRange();
          range.setStart(node, start);
          range.setEnd(node, start + match[0].length);
          const tops = new Set(
            Array.from(range.getClientRects())
              .filter((r) => r.width > 0 && r.height > 0)
              .map((r) => Math.round(r.top)),
          );
          if (tops.size > 1) broken.push(match[0]);
        }
      }
    }
    return broken;
  }, selector);
}

const near = (a: number, b: number, tolerance = TOLERANCE) => Math.abs(a - b) <= tolerance;
const fixed = (value: number) => value.toFixed(1);

/** The two invariants of a pair, with the lines its label was meant to run to. */
function expectPair(pair: Pair, lines: number, what: string): void {
  expect(pair.lines, `${what}: "${pair.text}" runs to ${pair.lines} line(s)`).toBe(lines);
  const gap = pair.firstLine.left - pair.slot.right;
  expect(gap, `${what}: gap ${fixed(gap)} px for ${fixed(pair.fontSize)} px type`).toBeGreaterThanOrEqual(
    MIN_GAP_EM * pair.fontSize - TOLERANCE,
  );
  expect(
    near(pair.icon.cy, pair.firstLine.cy, CENTRE_TOLERANCE),
    `${what}: icon centre ${fixed(pair.icon.cy)} vs first line centre ${fixed(pair.firstLine.cy)}`,
  ).toBe(true);
  if (lines > 1) {
    // On the first line, not on the box: the whole label's centre is at least half a line lower.
    expect(pair.icon.cy, `${what}: the icon is centred on the whole label`).toBeLessThan(pair.label.cy - pair.firstLine.height / 4);
  }
}

test.describe('Presentations - an icon beside a label on the 1920 x 1080 canvas', () => {
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

  test('a table centres a header and a body cell icon on the first line of its text', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-table'));
    const heads = await pairs(present, '.deck-table th');
    expect(heads).toHaveLength(4);
    expectPair(heads[0], 1, 'header 1');
    expectPair(heads[1], 2, 'header 2');
    expectPair(heads[2], 2, 'header 3');
    expectPair(heads[3], 1, 'centred header');
    const cells = await pairs(present, '.deck-table td');
    expect(cells).toHaveLength(6);
    expectPair(cells[0], 1, 'row 1, cell 1');
    expectPair(cells[1], 2, 'row 1, cell 2');
    expectPair(cells[2], 1, 'row 1, cell 3');
    expectPair(cells[3], 1, 'row 2, cell 1');
    expectPair(cells[4], 2, 'row 2, cell 3');
    expectPair(cells[5], 1, 'row 3, cell 1');
    // A centred header with an icon reads centred: its row - icon and text together - sits on the cell's centre.
    const centring = await present
      .locator('.deck-table th')
      .nth(3)
      .evaluate((th) => {
        const frame = th.closest<HTMLElement>('.deck-slide') as HTMLElement;
        const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
        const row = th.querySelector('.deck-icon-text') as HTMLElement;
        const cell = th.getBoundingClientRect();
        const pair = row.getBoundingClientRect();
        return (pair.left + pair.width / 2 - (cell.left + cell.width / 2)) / scale;
      });
    expect(Math.abs(centring), `the centred header's row is ${fixed(centring)} px off the cell's centre`).toBeLessThanOrEqual(TOLERANCE);
    // An icon-only cell draws no label box, so nothing spaces its icon off centre.
    const iconOnly = present.locator('.deck-table td .deck-icon-text:not(:has(.deck-icon-text-label))');
    await expect(iconOnly).toHaveCount(3);
    // A row of one-line cells, icons or not, is one line tall: the icon's row adds nothing under the text.
    const oneLineRow = await present
      .locator('.deck-table tbody tr')
      .nth(2)
      .locator('td')
      .first()
      .evaluate((td) => {
        const frame = td.closest<HTMLElement>('.deck-slide') as HTMLElement;
        const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
        const style = getComputedStyle(td);
        const inner = td.getBoundingClientRect().height / scale - Number.parseFloat(style.paddingTop) - Number.parseFloat(style.paddingBottom);
        return { inner, line: Number.parseFloat(style.lineHeight) };
      });
    expect(near(oneLineRow.inner, oneLineRow.line, 2), `a one-line row is ${fixed(oneLineRow.inner)} px for a ${fixed(oneLineRow.line)} px line`).toBe(true);
  });

  test('a check list centres its marker on the first line of a one- and a two-line item', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-list'));
    const items = await pairs(present, '.deck-list li');
    expect(items).toHaveLength(3);
    expectPair(items[0], 1, 'item 1');
    expectPair(items[1], 2, 'item 2');
    expectPair(items[2], 1, 'item with its own icon');
  });

  test('a badge centres its icon on its one line', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-badges'));
    const badges = await pairs(present, '.deck-badge');
    expect(badges).toHaveLength(2);
    for (const [index, badge] of badges.entries()) expectPair(badge, 1, `badge ${index + 1}`);
  });

  for (const [id, lines] of [
    ['s-callout-1', 1],
    ['s-callout-2', 2],
  ] as const) {
    test(`an inline callout centres its icon on the first line of a ${lines}-line title`, async ({ page }) => {
      const present = await openStage(page, deckId, position(id));
      const heads = await pairs(present, '.deck-callout[data-header="inline"] .deck-callout-head');
      expect(heads).toHaveLength(1);
      expectPair(heads[0], lines, 'callout head');
      // The icon keeps the 28 px it had beside the 34 px default title (0.8 of the title's type).
      expect(near(heads[0].icon.height, 0.8 * heads[0].fontSize)).toBe(true);
    });
  }

  test('a comparison centres a side label icon on the first line, one line or two', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-compare'));
    const labels = await pairs(present, '.deck-compare-label');
    expect(labels).toHaveLength(2);
    expectPair(labels[0], 1, 'left label');
    expectPair(labels[1], 2, 'right label');
  });

  test('a key / value label centres its icon on the first line, and the value still shares the baseline', async ({
    page,
  }) => {
    const present = await openStage(page, deckId, position('s-kv'));
    const labels = await pairs(present, '.deck-kv-label');
    expect(labels).toHaveLength(2);
    expectPair(labels[0], 1, 'label 1');
    expectPair(labels[1], 2, 'label 2');
    // The pair aligns on its baselines: the value's first line sits on the label's first line, not under the icon.
    const values = await pairs(present, '.deck-kv-row', '.deck-kv-label', '.deck-kv-value');
    expect(values).toHaveLength(2);
    for (const [index, value] of values.entries()) {
      expect(
        near(value.firstLine.bottom, labels[index].firstLine.bottom, 4),
        `row ${index + 1}: value baseline ${fixed(value.firstLine.bottom)} vs label ${fixed(labels[index].firstLine.bottom)}`,
      ).toBe(true);
    }
  });

  test('a feature keeps the shared gap beside its tile; the tile is centred on the whole text block', async ({ page }) => {
    // The exception to the first-line rule: a feature's tile centres on the
    // title AND its description (`presentation-feature-centre.spec.ts` measures
    // the centring); here only the gap and the tile's size are the shared row's.
    const present = await openStage(page, deckId, position('s-features'));
    const features = await pairs(present, '.deck-feature');
    expect(features).toHaveLength(2);
    for (const feature of features) {
      expect(near(feature.slot.height, 68)).toBe(true);
      const gap = feature.firstLine.left - feature.slot.right;
      expect(gap).toBeGreaterThanOrEqual(MIN_GAP_EM * feature.fontSize - TOLERANCE);
    }
  });

  test('a metric centres its trend arrow on the first line of a one- and a two-line delta', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-metrics'));
    const deltas = await pairs(present, '.deck-metric-delta');
    expect(deltas).toHaveLength(3);
    expectPair(deltas[0], 1, 'delta 1');
    expectPair(deltas[1], 2, 'delta 2');
    expectPair(deltas[2], 1, 'delta 3');
  });

  test('an events timeline centres its node on the first line of the body - the label, else the title', async ({
    page,
  }) => {
    const present = await openStage(page, deckId, position('s-timeline'));
    const events = await pairs(present, '.deck-timeline[data-style="events"] .deck-timeline-item');
    expect(events).toHaveLength(3);
    // The first body opens with its eyebrow label (one line), the second with a two-line title, the third with a numbered node.
    expectPair(events[0], 1, 'event with a label');
    expectPair(events[1], 2, 'event with a two-line title');
    expectPair(events[2], 1, 'numbered event');
    for (const event of events) expect(near(event.slot.height, 72)).toBe(true);
  });

  test('a vertical flow centres its icon on the first line of the title, the description starting on that line', async ({
    page,
  }) => {
    const present = await openStage(page, deckId, position('s-flow-vertical'));
    const flow = present.locator('.deck-flow[data-direction="vertical"]');
    await expect(flow).toHaveAttribute('data-icons', 'true');
    const steps = await pairs(flow, '.deck-flow-card', '.deck-flow-icon', '.deck-flow-title');
    expect(steps).toHaveLength(2);
    expectPair(steps[0], 1, 'step 1');
    expectPair(steps[1], 2, 'step 2');
    // The description's first line centres on the same icon, and a step without an icon keeps its title on that line too.
    const descs = await pairs(flow, '.deck-flow-card', '.deck-flow-icon', '.deck-flow-desc');
    for (const [index, desc] of descs.entries()) {
      expect(near(desc.firstLine.cy, steps[index].firstLine.cy, CENTRE_TOLERANCE), `step ${index + 1}: description`).toBe(true);
    }
    const titles = await flow.locator('.deck-flow-title').evaluateAll((els) =>
      els.map((el) => {
        const card = el.closest('.deck-flow-card') as HTMLElement;
        return el.getBoundingClientRect().top - card.getBoundingClientRect().top;
      }),
    );
    expect(titles).toHaveLength(3);
    expect(near(titles[2], titles[0]), 'the step without an icon starts its title where the others do').toBe(true);
  });

  test('a profile line centres its plate on its one line', async ({ page }) => {
    const present = await openStage(page, deckId, position('s-profiles'));
    const lines = await pairs(present, '.deck-profile-line');
    expect(lines).toHaveLength(3);
    for (const [index, line] of lines.entries()) {
      expectPair(line, 1, `line ${index + 1}`);
      expect(near(line.slot.width, 32)).toBe(true);
    }
  });

  test('a horizontal flow never breaks a step title inside a word: one size down when that fits it, a wider step when not', async ({
    page,
  }) => {
    // The steps' widths (a step is its card; its arrow is drawn in the gutter after it), in screen px.
    const stepWidths = (flow: Locator) => flow.locator('.deck-flow-step').evaluateAll((els) => els.map((el) => el.getBoundingClientRect().width));
    const titleSizes = (flow: Locator) => flow.locator('.deck-flow-title').evaluateAll((els) => els.map((el) => Number.parseFloat(getComputedStyle(el).fontSize)));

    // Short words: the titles stay at their size and the steps share the flow equally.
    const plain = await openStage(page, deckId, position('s-flow-words-none'));
    const plainFlow = plain.locator('.deck-flow:not([data-direction="vertical"])');
    await expect(plainFlow).not.toHaveAttribute('data-title-fit');
    expect(await wordsBrokenInside(plainFlow, '.deck-flow-title')).toEqual([]);
    const plainSteps = await stepWidths(plainFlow);
    for (const width of plainSteps) expect(near(width, plainSteps[0]), `equal steps: ${plainSteps.map(fixed).join(' / ')}`).toBe(true);
    const [plainSize] = await titleSizes(plainFlow);

    // A 14-letter word: one size down fits it, and every title steps down together, the steps still equal.
    const stepped = await openStage(page, deckId, position('s-flow-words-step'));
    const steppedFlow = stepped.locator('.deck-flow:not([data-direction="vertical"])');
    await expect(steppedFlow).toHaveAttribute('data-title-fit', 'tight');
    expect(await wordsBrokenInside(steppedFlow, '.deck-flow-title')).toEqual([]);
    for (const size of await titleSizes(steppedFlow)) expect(size).toBeLessThan(plainSize);
    const steppedSteps = await stepWidths(steppedFlow);
    for (const width of steppedSteps) expect(near(width, steppedSteps[0]), `equal steps: ${steppedSteps.map(fixed).join(' / ')}`).toBe(true);

    // A 20-letter word fits at neither size: the titles keep their size and that step alone widens.
    const widened = await openStage(page, deckId, position('s-flow-words-widen'));
    const widenedFlow = widened.locator('.deck-flow:not([data-direction="vertical"])');
    await expect(widenedFlow).not.toHaveAttribute('data-title-fit');
    expect(await wordsBrokenInside(widenedFlow, '.deck-flow-title')).toEqual([]);
    for (const size of await titleSizes(widenedFlow)) expect(size).toBe(plainSize);
    const widenedSteps = await stepWidths(widenedFlow);
    expect(widenedSteps[1], `steps: ${widenedSteps.map(fixed).join(' / ')}`).toBeGreaterThan(widenedSteps[0] + 10);
    for (const width of widenedSteps.filter((_, index) => index !== 1)) expect(near(width, widenedSteps[0])).toBe(true);
    // The long word is on one line of a title that is no wider than its card.
    const overflow = await widenedFlow
      .locator('.deck-flow-title')
      .nth(1)
      .evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
});
