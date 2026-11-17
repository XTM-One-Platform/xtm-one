import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - a feature's tile centred on its whole text block.
//
// A `feature_list` item is a round icon tile beside a title and an optional
// description. The tile's centre is the centre of the TEXT BLOCK - the title
// alone, the title and a one-line description, a description wrapped to three
// lines - within 2 px, at every size and in one or two columns. An agenda
// `list` of icon items draws the same round tile, centred on its text, and a
// `distribute` agenda of icon rows spreads them no wider than 1.5x the
// feature list's `lg` row gap. Geometry is read off the live viewer in canvas
// px (the stage scale divided out), the way the layout audit and the editable
// PPTX export read it. The deck is imported through the API from a bare spec
// (neutral fixture text, no LLM call), owned by the admin and deleted
// afterwards; the file runs serially.
// ---------------------------------------------------------------------------

const ONE_LINE = 'One line on what it does';
// About 120 characters fill two lines of a two-column `md` feature; this one
// is long enough to wrap onto three lines or more in that column.
const WRAPPED =
  'A description long enough to wrap onto three lines or more in its column, so the text block is much taller than the tile beside it, and the tile has to sit on the centre of the whole block rather than on the title line';

const featureItems = (count: number, description?: string) =>
  Array.from({ length: count }, (_, index) => ({
    icon: 'shield-check',
    title: `Capability ${index + 1}`,
    ...(description ? { description } : {}),
  }));

const content = (id: string, title: string, block: object) => ({ id, layout: 'content', title, blocks: [block] });

const AGENDA_ICONS = ['shield', 'radar', 'target', 'map', 'gavel', 'flag', 'star', 'layers'];

const FIXTURE_SPEC = {
  title: 'E2E feature centring',
  theme: 'light',
  aspect_ratio: '16:9',
  transition: 'none',
  slides: [
    { id: 's-title', layout: 'title', title: 'Feature centring' },
    content('lg-two-columns', 'Eight features, lg, two columns', {
      type: 'feature_list',
      columns: 2,
      size: 'lg',
      items: featureItems(8, ONE_LINE),
    }),
    content('mixed', 'Title only, one line, three lines', {
      type: 'feature_list',
      columns: 2,
      size: 'md',
      items: [
        { icon: 'star', title: 'A title alone' },
        { icon: 'flag', title: 'A title and one line', description: 'One short line.' },
        { icon: 'layers', title: 'A title and a wrapped description', description: WRAPPED },
      ],
    }),
    content('sm-one-column', 'Small, one column', {
      type: 'feature_list',
      size: 'sm',
      items: featureItems(3, ONE_LINE),
    }),
    content('lg-gap', 'Feature list lg row gap', {
      type: 'feature_list',
      columns: 2,
      size: 'lg',
      items: featureItems(4, ONE_LINE),
    }),
    {
      id: 'agenda-icons',
      layout: 'agenda',
      title: 'Agenda with icons',
      columns: 2,
      style: 'list',
      distribute: true,
      items: AGENDA_ICONS.map((icon, index) => ({ icon, title: `Topic ${index + 1}`, description: 'One line on the topic.' })),
    },
  ],
};

const position = (id: string): number => FIXTURE_SPEC.slides.findIndex((slide) => slide.id === id) + 1;

/** Canvas px: a hair of rounding, never a misplaced tile (the acceptance: within 2 px). */
const TOLERANCE = 2;

async function importDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'e2e-feature-centre.json',
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
}

type Centring = { lines: number; tile: number; text: number; tileHeight: number };

/** Per feature: the tile's centre and the centre of its title + description, in canvas px. */
async function readFeatures(page: Page): Promise<Centring[]> {
  return page.locator('.reveal .slides > section.present').evaluate((section) => {
    const frame = section.querySelector<HTMLElement>('.deck-slide');
    if (!frame) throw new Error('No slide frame');
    const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
    return Array.from(section.querySelectorAll<HTMLElement>('.deck-feature')).map((row) => {
      const tile = row.querySelector('.deck-icon-tile');
      const title = row.querySelector('.deck-feature-title');
      if (!tile || !title) throw new Error('A feature without its tile or title');
      const desc = row.querySelector('.deck-feature-desc');
      const t = tile.getBoundingClientRect();
      const top = title.getBoundingClientRect().top;
      const bottom = (desc ?? title).getBoundingClientRect().bottom;
      const lineHeight = desc ? Number.parseFloat(getComputedStyle(desc).lineHeight) : 0;
      const descLines = desc && lineHeight ? Math.round(desc.getBoundingClientRect().height / lineHeight) : 0;
      return {
        lines: 1 + descLines,
        tile: (t.top + t.bottom) / 2 / scale,
        text: (top + bottom) / 2 / scale,
        tileHeight: t.height / scale,
      };
    });
  });
}

test.describe('Presentations - a feature tile centred on its whole text block', () => {
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

  test('eight lg features in two columns: every tile on the centre of its title and description', async ({ page }) => {
    await openStage(page, deckId, position('lg-two-columns'));
    const features = await readFeatures(page);
    expect(features).toHaveLength(8);
    for (const feature of features) {
      expect(feature.lines).toBe(2);
      expect(Math.abs(feature.tile - feature.text)).toBeLessThanOrEqual(TOLERANCE);
    }
  });

  test('a title alone, a one-line and a three-line description: each tile on its own text block', async ({ page }) => {
    await openStage(page, deckId, position('mixed'));
    const features = await readFeatures(page);
    expect(features).toHaveLength(3);
    expect(features.map((feature) => feature.lines)).toEqual([1, 2, expect.any(Number)]);
    expect(features[2].lines).toBeGreaterThanOrEqual(3);
    for (const feature of features) expect(Math.abs(feature.tile - feature.text)).toBeLessThanOrEqual(TOLERANCE);
  });

  test('a small one-column list centres its tiles the same way', async ({ page }) => {
    await openStage(page, deckId, position('sm-one-column'));
    const features = await readFeatures(page);
    expect(features).toHaveLength(3);
    for (const feature of features) expect(Math.abs(feature.tile - feature.text)).toBeLessThanOrEqual(TOLERANCE);
  });

  test('an agenda of icon items: round tiles centred on their text, rows no wider apart than 1.5x the feature gap', async ({
    page,
  }) => {
    await openStage(page, deckId, position('lg-gap'));
    const featureGap = await page
      .locator('.reveal .slides > section.present .deck-features')
      .evaluate((el) => Number.parseFloat(getComputedStyle(el).rowGap));
    expect(featureGap).toBeGreaterThan(0);

    await openStage(page, deckId, position('agenda-icons'));
    const agenda = await page.locator('.reveal .slides > section.present').evaluate((section) => {
      const frame = section.querySelector<HTMLElement>('.deck-slide');
      if (!frame) throw new Error('No slide frame');
      const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
      const cells = Array.from(section.querySelectorAll<HTMLElement>('.deck-agenda-item')).map((cell) => {
        const marker = cell.querySelector<HTMLElement>('.deck-agenda-marker');
        const title = cell.querySelector('.deck-agenda-title');
        if (!marker || !title) throw new Error('An agenda cell without its marker or title');
        const desc = cell.querySelector('.deck-agenda-desc:not([data-empty="true"])');
        const m = marker.getBoundingClientRect();
        const cs = getComputedStyle(marker);
        return {
          marker: (m.top + m.bottom) / 2 / scale,
          text: (title.getBoundingClientRect().top + (desc ?? title).getBoundingClientRect().bottom) / 2 / scale,
          round: Number.parseFloat(cs.borderTopLeftRadius) >= (m.width / scale) * 0.45,
          ring: Number.parseFloat(cs.borderTopWidth) > 0,
          hasIcon: !!marker.querySelector('.deck-icon'),
        };
      });
      const rows = Array.from(section.querySelectorAll<HTMLElement>('.deck-agenda-row')).map((row) => {
        const r = row.getBoundingClientRect();
        return { top: r.top / scale, bottom: r.bottom / scale };
      });
      const gaps = rows.slice(1).map((row, index) => row.top - rows[index].bottom);
      return { cells, gaps };
    });
    expect(agenda.cells).toHaveLength(8);
    for (const cell of agenda.cells) {
      expect(cell.hasIcon).toBe(true);
      expect(cell.round).toBe(true);
      expect(cell.ring).toBe(true);
      expect(Math.abs(cell.marker - cell.text)).toBeLessThanOrEqual(TOLERANCE);
    }
    expect(agenda.gaps.length).toBe(3);
    for (const gap of agenda.gaps) expect(gap).toBeLessThanOrEqual(featureGap * 1.5 + TOLERANCE);
  });
});
