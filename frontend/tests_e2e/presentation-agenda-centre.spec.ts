import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - the agenda's chip and number centred on the text block.
//
// In the `list` style the chip (an icon or a two-digit number) sits beside the
// title and the description: its centre is the centre of the TEXT BLOCK, the
// title plus its description when the item has one, the title alone when it
// has none - never the title line. In the `grid` style the `N.` number is
// centred on the title beside it. The geometry is CSS, so it is read off the
// viewer with `getBoundingClientRect`, the way the layout audit and the
// editable PPTX export read it, in canvas px (the stage scale divided out).
//
// The fixture is the STORED shape: `distribute: true`, as the backend stores
// every agenda, imported through the API from a bare spec,
// owned by the admin and deleted afterwards; the file runs serially.
// ---------------------------------------------------------------------------

const ICON_ITEMS = [
  { title: 'Threat landscape', description: 'What changed this quarter across the sectors we cover.', icon: 'shield' },
  { title: 'Detection coverage', icon: 'radar' },
  { title: 'Exposure', description: 'Open findings by business unit.', icon: 'target' },
  { title: 'Roadmap', icon: 'map' },
  { title: 'Decisions', description: 'The two calls this board makes today.', icon: 'gavel' },
];

const NUMBER_ITEMS = [
  { title: 'Context', description: 'Where the programme stands.' },
  { title: 'Results' },
  { title: 'Risks', description: 'What could still slip.' },
  { title: 'Next steps' },
];

const FIXTURE_SPEC = {
  title: 'E2E agenda centring',
  theme: 'light',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Agenda centring' },
    { id: 'icons-list', layout: 'agenda', title: 'Icons, one column', items: ICON_ITEMS, columns: 1, style: 'list', distribute: true },
    { id: 'icons-list-packed', layout: 'agenda', title: 'Icons, packed', items: ICON_ITEMS, columns: 1, style: 'list', distribute: false },
    { id: 'numbers-list', layout: 'agenda', title: 'Numbers, two columns', items: NUMBER_ITEMS, columns: 2, style: 'list', distribute: true },
    {
      id: 'titles-only',
      layout: 'agenda',
      title: 'Titles only',
      items: NUMBER_ITEMS.map(({ title }) => ({ title })),
      columns: 2,
      style: 'list',
      distribute: true,
    },
    { id: 'numbers-grid', layout: 'agenda', title: 'Grid, three columns', items: [...NUMBER_ITEMS, ...NUMBER_ITEMS.slice(0, 2)], columns: 3, style: 'grid', distribute: true },
  ],
};

const position = (id: string): number => FIXTURE_SPEC.slides.findIndex((slide) => slide.id === id) + 1;

/** Canvas px: a hair of rounding, never a misplaced chip (Sam's acceptance: within 2 px). */
const TOLERANCE = 2;

async function importDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: { name: 'e2e-agenda-centre.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8') },
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
  await expect(page.locator('.reveal .slides > section.present .deck-agenda')).toBeVisible();
}

type Centring = { lines: 1 | 2; marker: number; text: number };

/**
 * Per agenda cell: the centre of the chip (or number) and the centre of the
 * text block it must sit on, in canvas px. `lines` is 2 when the item draws a
 * description under its title.
 */
async function readCentring(page: Page): Promise<Centring[]> {
  return page.locator('.reveal .slides > section.present').evaluate((section) => {
    const frame = section.querySelector<HTMLElement>('.deck-slide');
    if (!frame) throw new Error('No slide frame');
    const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
    const centre = (top: number, bottom: number) => (top + bottom) / 2 / scale;
    return Array.from(section.querySelectorAll<HTMLElement>('.deck-agenda-item')).map((cell) => {
      const marker = cell.querySelector('.deck-agenda-marker, .deck-agenda-number');
      const title = cell.querySelector('.deck-agenda-title');
      if (!marker || !title) throw new Error('An agenda cell without its marker or title');
      const m = marker.getBoundingClientRect();
      const t = title.getBoundingClientRect();
      const desc = cell.querySelector('.deck-agenda-desc:not([data-empty="true"])');
      const grid = cell.closest('.deck-agenda')?.getAttribute('data-style') === 'grid';
      // The grid's number is centred on its title; the list's chip on the block.
      const bottom = desc && !grid ? desc.getBoundingClientRect().bottom : t.bottom;
      return { lines: desc ? 2 : 1, marker: centre(m.top, m.bottom), text: centre(t.top, bottom) } as const;
    });
  });
}

test.describe('Presentations - agenda chip centred on the text block', () => {
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

  for (const id of ['icons-list', 'icons-list-packed', 'numbers-list', 'titles-only', 'numbers-grid']) {
    test(`${id}: every chip or number is centred on its text block`, async ({ page }) => {
      await openStage(page, deckId, position(id));
      const cells = await readCentring(page);
      expect(cells.length).toBeGreaterThan(0);
      for (const cell of cells) expect(Math.abs(cell.marker - cell.text)).toBeLessThanOrEqual(TOLERANCE);
      if (id === 'icons-list' || id === 'numbers-list') {
        // Both shapes are on the slide: a one-line item and a two-line one.
        expect(cells.some((cell) => cell.lines === 1)).toBe(true);
        expect(cells.some((cell) => cell.lines === 2)).toBe(true);
      }
    });
  }

  test('the titles of a two-column row still start on one line', async ({ page }) => {
    // The reserved description line: a cell without a description keeps its
    // title level with its neighbour's, its chip held inside the title track.
    await openStage(page, deckId, position('numbers-list'));
    const tops = await page.locator('.reveal .slides > section.present').evaluate((section) =>
      Array.from(section.querySelectorAll('.deck-agenda-row')).map((row) =>
        Array.from(row.querySelectorAll('.deck-agenda-title')).map((title) => title.getBoundingClientRect().top),
      ),
    );
    for (const row of tops) for (const top of row) expect(Math.abs(top - row[0])).toBeLessThanOrEqual(1);
  });
});
