import { test, expect } from '@playwright/test';
import { createAdminApiContext, createTwoSchemeTemplate, DECK_POLL_MS, deleteTemplate, importDeckOnTemplate } from './helpers';
import { logoItems as items, logoSvg, openStage, uploadLogos, type Logo } from './presentation-logo-fixtures';

// ---------------------------------------------------------------------------
// Presentations - logo walls and callouts that look designed.
//
// A logo is sized by its MARK: the renderer draws it at the block's
// `logo_height` (sm 28 / md 40 / lg 56 / xl 72 canvas px), crops away the
// transparent margin the file carries (the trim box stored on upload, served
// as the deck's `asset_meta`) and, with `optical_balance`, lets a compact seal
// stand a little taller than a wordmark so both carry the same weight. A
// `logo_wall` puts grouped logos in equal cards that fill the body, each row
// spread over the card's full width. A callout with a `header` puts its icon
// on the title line and gives its children the full width.
//
// The logos are synthetic SVGs uploaded as template assets
// (`presentation-logo-fixtures.ts`: wordmarks at exactly the target height,
// seals, badges and combos around them, each padded with transparent margin
// so the trim has work to do). The geometry is read off the live viewer the way the
// layout audit and the editable PPTX export read it: `getBoundingClientRect`,
// converted to canvas px by the stage scale.
//
// The deck is imported through the API (no LLM call) on a private two-scheme
// template, then its slides are written with one PATCH; everything is deleted
// afterwards. The file runs SERIALLY so the uploads happen once.
// ---------------------------------------------------------------------------

function wallBlock(logos: Logo[]) {
  const groups = [
    { title: 'Government', icon: 'landmark' },
    { title: 'Finance', icon: 'banknote' },
    { title: 'Energy', icon: 'zap' },
    { title: 'Industry', icon: 'factory' },
  ];
  return {
    type: 'logo_wall',
    columns: 2,
    per_row: 4,
    logo_height: 'md',
    tone: 'original',
    groups: groups.map((group, index) => ({ ...group, logos: items(logos.slice(index * 8, index * 8 + 8)) })),
  };
}

const LIST = { type: 'list', items: ['First point of the card', 'Second point, a little longer'] };

function buildSlides(logos: Logo[], gone: Logo) {
  const content = (id: string, title: string, block: object, extra: object = {}) => ({
    id,
    layout: 'content',
    title,
    blocks: [block],
    ...extra,
  });
  const grid = (size: string) => ({ type: 'logo_grid', columns: 4, size, items: items(logos.slice(0, 8)) });
  return [
    { id: 's-title', layout: 'title', title: 'Logo walls and callouts', subtitle: 'Designed, not boxed-in' },
    content('s-wall-light', 'Trusted across sectors', wallBlock(logos)),
    content('s-wall-dark', 'Trusted across sectors', wallBlock(logos), { theme: 'dark' }),
    content('s-grid-sm', 'Size sm', grid('sm')),
    content('s-grid-md', 'Size md', grid('md')),
    content('s-grid-lg', 'Size lg', grid('lg')),
    content('s-callout-inline', 'Inline header', {
      type: 'callout',
      variant: 'card',
      header: 'inline',
      icon: 'shield-check',
      title: 'A card callout',
      children: [LIST],
    }),
    content('s-callout-legacy', 'Historical callout', {
      type: 'callout',
      icon: 'shield-check',
      title: 'A tinted callout',
      children: [LIST],
    }),
    content('s-missing-logo', 'A logo whose file is gone', { type: 'logo_grid', columns: 2, items: items([logos[0], gone]) }),
  ];
}

const SLIDE_IDS = [
  's-title',
  's-wall-light',
  's-wall-dark',
  's-grid-sm',
  's-grid-md',
  's-grid-lg',
  's-callout-inline',
  's-callout-legacy',
  's-missing-logo',
];

/** 1-based stage position of a fixture slide, by id. */
const position = (id: string): number => SLIDE_IDS.indexOf(id) + 1;

type Box = { left: number; top: number; right: number; bottom: number; width: number; height: number };

/** Browser px on the scaled stage: a hair of rounding, never a layout difference. */
const TOLERANCE = 2;
const close = (a: number, b: number, tolerance = TOLERANCE): boolean => Math.abs(a - b) <= tolerance;

test.describe('Presentations - logo walls and callouts', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';
  let templateId = '';
  let logos: Logo[] = [];

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const templateName = `E2E logo walls ${Date.now()}`;
      templateId = await createTwoSchemeTemplate(apiCtx, token, templateName);
      logos = await uploadLogos(apiCtx, token, templateId, 32);
      // Placed on a slide, then deleted: the deck keeps naming a file nobody can fetch.
      const [gone] = await uploadLogos(apiCtx, token, templateId, 1);
      const shell = { title: 'E2E logo walls', theme: 'light', aspect_ratio: '16:9', transition: 'none', slides: [] };
      deckId = await importDeckOnTemplate(
        apiCtx,
        token,
        { ...shell, slides: [{ id: 's-title', layout: 'title', title: 'Logo walls' }] },
        templateName,
        'e2e-logo-walls.json',
      );
      const deck = await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
      const { revision } = (await deck.json()) as { revision: number };
      const patched = await apiCtx.patch(`/api/v1/presentations/${deckId}`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { slides: buildSlides(logos, gone), expected_revision: revision },
      });
      if (!patched.ok()) throw new Error(`Writing the fixture slides failed: ${patched.status()} ${await patched.text()}`);
      const removed = await apiCtx.delete(`/api/v1/template-containers/${templateId}/assets/${gone.file_id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!removed.ok()) throw new Error(`Deleting the gone logo failed: ${removed.status()} ${await removed.text()}`);
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

  for (const [slideId, scheme] of [
    ['s-wall-light', 'light'],
    ['s-wall-dark', 'dark'],
  ] as const) {
    test(`a 2 x 2 wall of 4 x 8 logos fills the ${scheme} slide with readable, balanced logos`, async ({ page }) => {
      const present = await openStage(page, deckId, position(slideId));
      await expect(present).toHaveAttribute('data-scheme', scheme);
      const kinds = Object.fromEntries(logos.map((logo) => [logo.label, logo.kind]));
      const geometry = await present.evaluate((section) => {
        const box = (el: Element): Box => {
          const r = el.getBoundingClientRect();
          return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
        };
        const frame = section.querySelector<HTMLElement>('.deck-slide');
        const fit = section.querySelector('.deck-fit');
        const host = section.querySelector('.deck-fit-content > .deck-blocks-slide');
        const wall = host?.querySelector(':scope > .deck-logo-wall');
        if (!frame || !fit || !host || !wall) throw new Error('No logo wall in the slide body');
        const cards = Array.from(wall.querySelectorAll('.deck-logo-group')).map((card) => {
          const field = card.querySelector('.deck-logo-field');
          if (!field) throw new Error('A card without logos');
          return {
            box: box(card),
            field: box(field),
            rows: Array.from(field.querySelectorAll(':scope > .deck-logo-row')).map((row) => ({
              short: row.hasAttribute('data-short'),
              marks: Array.from(row.querySelectorAll('.deck-logo-mark')).map((mark) => ({
                box: box(mark),
                label: mark.querySelector('img')?.getAttribute('alt') ?? '',
              })),
            })),
          };
        });
        return {
          scale: frame.getBoundingClientRect().width / frame.offsetWidth,
          fit: fit.getAttribute('data-fit'),
          host: box(host),
          wall: box(wall),
          cards,
        };
      });

      // No shrink: the body fits its room as drawn.
      expect(geometry.fit).toBeNull();

      // The cards fill the body: one block, top to bottom, four equal cards on two rows.
      expect(close(geometry.wall.top, geometry.host.top)).toBe(true);
      expect(close(geometry.wall.bottom, geometry.host.bottom)).toBe(true);
      expect(geometry.cards).toHaveLength(4);
      const [first, ...others] = geometry.cards;
      for (const card of others) {
        expect(close(card.box.width, first.box.width)).toBe(true);
        expect(close(card.box.height, first.box.height)).toBe(true);
      }
      expect(new Set(geometry.cards.map((card) => Math.round(card.box.top))).size).toBe(2);

      const canvas = (px: number) => px / geometry.scale;
      for (const card of geometry.cards) {
        expect(card.rows).toHaveLength(2);
        for (const row of card.rows) {
          expect(row.marks).toHaveLength(4);
          expect(row.short).toBe(false);
          // The logos use the card's full width: the row runs edge to edge.
          expect(close(row.marks[0].box.left, card.field.left)).toBe(true);
          expect(close(row.marks[row.marks.length - 1].box.right, card.field.right)).toBe(true);
          for (const mark of row.marks) {
            const height = canvas(mark.box.height);
            // Readable: no logo under the md height.
            expect(height).toBeGreaterThanOrEqual(39.5);
            const kind = kinds[mark.label];
            if (kind === 'wordmark') {
              // A wordmark sits at exactly the target height...
              expect(close(height, 40, 0.5)).toBe(true);
            } else if (kind === 'seal' || kind === 'badge') {
              // ...and a compact mark a little taller, never more than 1.5x.
              expect(height).toBeGreaterThan(44);
              expect(height).toBeLessThanOrEqual(60.5);
            }
          }
        }
      }
    });
  }

  test('logo_grid size sm / md / lg draws three visibly different logo heights', async ({ page }) => {
    const heights: number[] = [];
    for (const [slideId, expected] of [
      ['s-grid-sm', 28],
      ['s-grid-md', 40],
      ['s-grid-lg', 56],
    ] as const) {
      const present = await openStage(page, deckId, position(slideId));
      const { scale, smallest } = await present.evaluate((section) => {
        const frame = section.querySelector<HTMLElement>('.deck-slide');
        if (!frame) throw new Error('No slide frame');
        const marks = Array.from(section.querySelectorAll('.deck-logo-grid .deck-logo-mark'));
        return {
          scale: frame.getBoundingClientRect().width / frame.offsetWidth,
          smallest: Math.min(...marks.map((mark) => mark.getBoundingClientRect().height)),
        };
      });
      // The smallest mark is a wordmark, drawn at exactly the size's height.
      expect(close(smallest / scale, expected, 0.5)).toBe(true);
      heights.push(smallest / scale);
    }
    expect(heights[0]).toBeLessThan(heights[1]);
    expect(heights[1]).toBeLessThan(heights[2]);
  });

  test('an inline-header callout gives its children the full width; the historical one keeps its icon column', async ({
    page,
  }) => {
    const read = async (slideId: string) => {
      const present = await openStage(page, deckId, position(slideId));
      return present.evaluate((section) => {
        const frame = section.querySelector<HTMLElement>('.deck-slide');
        const callout = section.querySelector<HTMLElement>('.deck-callout');
        const icon = callout?.querySelector('.deck-callout-icon');
        const child = callout?.querySelector('.deck-callout-body .deck-blocks > *');
        if (!frame || !callout || !icon || !child) throw new Error('No callout with an icon and a child block');
        // Computed lengths are canvas px; the rects are on the scaled stage.
        const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
        const style = getComputedStyle(callout);
        const inset = (Number.parseFloat(style.borderLeftWidth) + Number.parseFloat(style.paddingLeft)) * scale;
        return {
          contentLeft: callout.getBoundingClientRect().left + inset,
          iconLeft: icon.getBoundingClientRect().left,
          iconRight: icon.getBoundingClientRect().right,
          childLeft: child.getBoundingClientRect().left,
          header: callout.getAttribute('data-header'),
          variant: callout.getAttribute('data-variant'),
        };
      });
    };

    const inline = await read('s-callout-inline');
    expect(inline.header).toBe('inline');
    expect(inline.variant).toBe('card');
    // The child starts at the callout's left padding, under the icon, not beside it.
    expect(close(inline.childLeft, inline.contentLeft)).toBe(true);
    expect(close(inline.iconLeft, inline.childLeft)).toBe(true);

    const legacy = await read('s-callout-legacy');
    expect(legacy.header).toBeNull();
    // The historical look writes no look attribute: its DOM is the one from before looks.
    expect(legacy.variant).toBeNull();
    // No header: the historical layout, the children in the column right of the icon.
    expect(legacy.childLeft).toBeGreaterThan(legacy.iconRight);
  });

  test("a logo whose file is gone never holds the deck's ready signal", async ({ page }) => {
    // `openStage` waits for every `[data-deck-pending]` to clear - the viewer's
    // print path (`deckSettled`) waits on the same marker, and the export page
    // with no time bound of its own.
    const present = await openStage(page, deckId, position('s-missing-logo'));
    const grid = present.locator('.deck-logo-grid');
    await expect(grid.locator('.deck-logo-mark')).toHaveCount(2);
    // The logo that exists is drawn; the deleted one leaves an empty mark, no broken image.
    await expect(grid.locator('img.deck-logo')).toHaveCount(1);
  });

  test('a logo still on its way holds the ready signal until its bytes arrive', async ({ page }) => {
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    // The URL is versioned by the file's revision (`?v=`), hence the pattern.
    await page.route(new RegExp(`/api/v1/chat/files/${logos[0].file_id}/download(\\?.*)?$`), async (route) => {
      await held;
      await route.continue();
    });
    const slide = position('s-grid-md');
    await page.goto(`/presentations/${deckId}#/${slide - 1}`);
    const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
    await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${slide} of \\d+\\.`) })).toBeVisible();
    const present = page.locator('.reveal .slides > section.present');
    // The other seven logos arrive; the held one keeps the slide unsettled...
    await expect(present.locator('.deck-logo-grid img.deck-logo')).toHaveCount(7);
    await expect(present.locator('[data-deck-pending]')).toHaveCount(1);
    // ...until its bytes are let through.
    release();
    await expect(present.locator('[data-deck-pending]')).toHaveCount(0);
    await expect(present.locator('.deck-logo-grid img.deck-logo')).toHaveCount(8);
  });

  // Last: it replaces a logo's bytes, which every slide showing it would see.
  test('a logo replaced in place is fetched again, so its image and its trim box stay one file', async ({ page }) => {
    test.setTimeout(90_000);
    const target = logos[7];
    const versions: (string | null)[] = [];
    page.on('request', (request) => {
      if (request.url().includes(`/api/v1/chat/files/${target.file_id}/download`)) {
        versions.push(new URL(request.url()).searchParams.get('v'));
      }
    });
    await page.clock.install();
    const present = await openStage(page, deckId, position('s-grid-md'));
    const mark = present.locator('.deck-logo-mark').nth(7);
    const first = versions[0];
    // The download is versioned by the bytes the geometry was read from.
    expect(first).toBeTruthy();
    const staleSrc = await mark.locator('img').getAttribute('src');
    const ratio = async () => {
      const box = await mark.boundingBox();
      return box ? box.width / box.height : 0;
    };
    expect(await ratio()).toBeGreaterThan(2); // a wordmark

    const { apiCtx, token } = await createAdminApiContext();
    try {
      // Same asset id, new bytes: a seal where the wordmark was.
      const replaced = await apiCtx.put(`/api/v1/template-containers/${templateId}/assets/${target.file_id}`, {
        headers: { Authorization: `Bearer ${token}` },
        multipart: { file: { name: 'seal.svg', mimeType: 'image/svg+xml', buffer: Buffer.from(logoSvg('seal', 3), 'utf8') } },
      });
      if (!replaced.ok()) throw new Error(`Replacing the logo failed: ${replaced.status()} ${await replaced.text()}`);
    } finally {
      await apiCtx.dispose();
    }

    // The deck's poll brings the new trim box with a new revision: the image is
    // fetched again under it, never the cached one cropped with the new box.
    // The poll is brought forward on the page clock rather than waited out.
    await page.clock.fastForward(DECK_POLL_MS);
    await expect.poll(() => versions.some((v) => v !== null && v !== first), { timeout: 45_000 }).toBe(true);
    await expect.poll(async () => mark.locator('img').getAttribute('src'), { timeout: 15_000 }).not.toBe(staleSrc);
    await expect.poll(ratio, { timeout: 15_000 }).toBeLessThan(1.3);
  });
});
