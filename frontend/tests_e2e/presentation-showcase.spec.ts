import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - the two keynote compositions.
//
// 1. The showcase image: `image.frame` ("browser" / "device") draws a window
//    chrome over a glow in the brand gradient around a product screenshot.
//    The window takes the PICTURE's shape - never a letterboxed box - and
//    with `fill` in the wide column of a `wide_left` slide it is the largest
//    window of the picture's ratio the body holds: the column stretches to
//    the body (a `fill` image is a filler, like a `fill` chart), the room is
//    measured, and the window is centred in what it did not use.
// 2. The `challenge_answer` block: two stacked cards joined by an arrow, the
//    challenge in the error tone over the answer in the success tone, of one
//    width and one left edge; `fill` shares the height between them.
//
// Both are read off the viewer at 1920 x 1080 the way the layout audit and the
// editable PPTX read them (`getBoundingClientRect`, computed styles). The deck
// is imported through the API as a package carrying the screenshot as an
// embedded asset (the import copies it as a file of the importer, so no
// template and no AI-gated upload is needed), and deleted afterwards.
// ---------------------------------------------------------------------------

/** A 320 x 200 PNG (ratio 1.6): a mock product screen, enough to load and measure. */
const SCREENSHOT = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAUAAAADICAIAAAAWZq/8AAAB9klEQVR42u3coQ2AMBBAUUqYowrRIdBlBBzMBo4RQDMGw7ACoYrmPX/mkp+raogpN8A/tVYAAgYEDAgYBAwIGBAwIGAQMCBgQMAgYEDAgIABAYOAAQEDAgYEDHXoSob77bBBKHfPowsMntCAgAEBAwIGAQMCBgQMCBgEDAgYEDAIGBAwIGBAwCBgQMCAgEHAgIABAQMCBgEDAgYEDAgYBAwIGBAwCBgQMCBgQMAgYEDAgIABAYOAAQEDX4SYsi2ACwwIGBAwCBgQMCBgQMAgYEDAgIBBwICAAQEDAgYBAwIGBAwIGCrQlQwPg+94Kndd57fBaTls7719HV1g8IQGBAwIGBAwCBgQMCBgQMAgYEDAgIBBwICAAQEDAgYBAwIGBAwCBgQMCBgQMAgYEDAgYEDAIGBAwICAQcCAgAEBAwIGAQMCBgQMCBgEDAgYEDAIGBAwIGBAwCBgQMCAgAEBg4ABAQMCBgEDAgYEDAgYBAwIGBAwCBgQMCBgQMAgYEDAgIABAYOAAQEDAgYBAwIGBAwIGAQMCBgQMCBgEDAgYEDAIGBAwICAAQGDgAEBAwIGAQMCBgQMCBgEDAgYEDAgYBAwIGBAwCBg4H9CTNkWwAUGBAwIGAQMCBgQMCBgEDAgYEDAIGBAwICAAQGDgAEBAwIGBAwVeACfYAw2MehNvAAAAABJRU5ErkJggg==',
  'base64',
);
const RATIO = 320 / 200;

/** The browser frame's chrome, canvas px (`presentation.css`): the bar, and the hairline on every side. */
const BROWSER_BAR_PX = 36;
const HAIRLINE_PX = 1;
/** The `lg` size bucket: the picture's target height, canvas px. */
const LG_BUCKET_PX = 720;
/** The gap between the two cards of a `challenge_answer`, canvas px: the connector's height (`blocks/challenge.tsx`). */
const CHALLENGE_GAP_PX = 66;

const READING = [
  { type: 'heading', content: 'What you are looking at', level: 3 },
  { type: 'list', style: 'arrow', items: ['The **timeline** of the case', 'The graph of what the indicators touch'] },
];

const showcaseSlide = (id: string, title: string, picture: Record<string, unknown>) => ({
  id,
  layout: 'content',
  title,
  columns: [[picture], READING],
  ratio: 'wide_left',
  vertical_align: 'center',
});

const CHALLENGE = {
  type: 'challenge_answer',
  icon: 'layers',
  challenge: 'Five consoles and five queues - every incident is correlated **by hand**.',
  answer: 'One case model - every source lands in the same investigation.',
};

/** The screenshot's id inside the package: the import re-points every reference at the file it copies. */
const SCREENSHOT_ID = 'SCREENSHOT_FILE_ID';

function buildSpec(fileId: string) {
  const image = (extra: Record<string, unknown>) => ({ type: 'image', file_id: fileId, alt: 'The workspace', ...extra });
  return {
    title: 'E2E showcase and challenge',
    theme: 'dark',
    aspect_ratio: '16:9',
    transition: 'none',
    slides: [
      { id: 's-title', layout: 'title', title: 'Keynote', subtitle: 'Showcase and challenge' },
      showcaseSlide('s-browser', 'Every alert lands in one investigation', image({ frame: 'browser', fill: true })),
      showcaseSlide('s-device', 'On every screen', image({ frame: 'device', fill: true })),
      // The generic half: a bare `fill` image in the wide column takes the body height too.
      showcaseSlide('s-bare', 'The same screen, no frame', image({ fill: true })),
      // A framed picture that does not fill: the size bucket is its height, the window its ratio wider.
      { id: 's-bucket', layout: 'content', title: 'The workspace', blocks: [image({ frame: 'browser', size: 'lg' })] },
      {
        id: 's-challenge',
        layout: 'content',
        title: 'Correlation was the job nobody had time for',
        blocks: [{ ...CHALLENGE, fill: true }],
        footer_block: { type: 'callout', tone: 'accent', content: 'Median triage fell from **20 to 11 minutes**.' },
      },
      // xtm-one #10257: photo columns beside text cards under a metrics row.
      // The fill photo takes the height the row leaves (cropped), the small one
      // shares its column, and neither shrinks the body. The text column must
      // fit the body on its own (no spacer, two items per card): with an md
      // spacer and three items per card the CARDS alone overflowed the 16:9
      // body (fitted at 0.898, the photos already at the row height), which
      // is the text's shrink, not the photos'.
      {
        id: 's-photo-columns',
        layout: 'content',
        title: 'Photo columns beside text cards',
        blocks: [
          {
            type: 'metrics_grid',
            size: 'sm',
            items: [
              { value: '4', label: 'Products' },
              { value: '7', label: 'Regions' },
              { value: '12', label: 'Teams' },
              { value: '30', label: 'Partners' },
            ],
          },
          {
            type: 'columns',
            columns: [
              [image({ fit: 'cover', fill: true })],
              [
                { type: 'callout', variant: 'card', title: 'Ship', children: [CARD_LIST] },
                { type: 'callout', variant: 'card', title: 'Learn', children: [CARD_LIST] },
              ],
              [image({ fit: 'cover', size: 'sm' }), { type: 'quote', variant: 'card', content: 'One thread, every team.' }],
            ],
          },
        ],
      },
      { id: 's-plain-list', layout: 'content', title: 'A plain list', columns: [[PHOTO_LIST], [PHOTO_LIST]] },
      {
        id: 's-challenge-column',
        layout: 'content',
        title: 'The problem, and the number',
        columns: [[CHALLENGE], [{ type: 'metric', value: '11 min', label: 'Median triage', trend: 'down', delta_tone: 'success' }]],
        ratio: 'wide_left',
      },
    ],
  };
}

const PHOTO_LIST = { type: 'list', size: 'md', items: ['One roadmap', 'One release train', 'One customer view'] };
const CARD_LIST = { ...PHOTO_LIST, items: PHOTO_LIST.items.slice(0, 2) };

const SPEC = buildSpec(SCREENSHOT_ID);
const position = (id: string): number => SPEC.slides.findIndex((s) => s.id === id) + 1;

/** Imports the deck as a package whose one embedded asset is the screenshot; the import copies it and re-points the spec. */
async function importDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const envelope = {
    export_type: 'presentation',
    version: 1,
    presentation: {
      spec: SPEC,
      assets: [
        {
          id: SCREENSHOT_ID,
          filename: 'workspace.png',
          content_type: 'image/png',
          asset_role: 'screenshot',
          description: 'The investigation workspace',
          data_base64: SCREENSHOT.toString('base64'),
        },
      ],
    },
  };
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: { name: 'e2e-showcase.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(envelope), 'utf8') },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id, import_report: report } = (await resp.json()) as { id: string; import_report?: { assets_copied?: number } };
  if (report?.assets_copied !== 1) throw new Error(`The screenshot was not copied in: ${JSON.stringify(report)}`);
  return id;
}

async function openStage(page: Page, deckId: string, at: number): Promise<void> {
  await page.goto(`/presentations/${deckId}#/${at - 1}`);
  const toolbar = page.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${at} of \\d+\\.`) })).toBeVisible();
  // The picture's bytes and the icon catalog have landed: nothing is pending on the present slide.
  await expect(page.locator('.reveal .slides > section.present [data-deck-pending]')).toHaveCount(0);
}

type Box = { left: number; right: number; top: number; bottom: number; width: number; height: number };

interface ShowcaseGeometry {
  frame: string | null;
  /** Browser px per canvas px. */
  scale: number;
  /** The body room (`.deck-fit`) and the two columns. */
  host: Box;
  wide: Box;
  narrow: Box;
  figure: Box;
  room: Box;
  window: Box;
  image: Box;
  bar: Box;
  dots: number;
  camera: number;
  glow: { ariaHidden: string | null; backdrop: string | null; background: string } | null;
  vars: { ratio: string; roomW: string; roomH: string };
  windowBackground: string;
}

async function readShowcase(page: Page): Promise<ShowcaseGeometry> {
  const section = page.locator('.reveal .slides > section.present');
  await expect(section.locator('.deck-figure img.deck-image')).toBeVisible();
  return section.evaluate((el) => {
    const box = (node: Element): Box => {
      const r = node.getBoundingClientRect();
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height };
    };
    const frame = el.querySelector<HTMLElement>('.deck-slide');
    const host = el.querySelector<HTMLElement>('.deck-fit');
    const columns = Array.from(el.querySelectorAll<HTMLElement>('.deck-columns-slide > .deck-column'));
    const figure = el.querySelector<HTMLElement>('.deck-figure');
    const image = figure?.querySelector<HTMLImageElement>('img.deck-image');
    if (!frame || !host || columns.length !== 2 || !figure || !image) throw new Error('No showcase slide on the stage');
    const room = figure.querySelector<HTMLElement>('.deck-showcase-room');
    const showcase = figure.querySelector<HTMLElement>('.deck-showcase');
    const windowEl = figure.querySelector<HTMLElement>('.deck-showcase-window');
    const bar = figure.querySelector<HTMLElement>('.deck-showcase-bar');
    const glow = figure.querySelector<HTMLElement>('.deck-showcase-glow');
    const scale = frame.getBoundingClientRect().width / frame.offsetWidth;
    return {
      frame: figure.getAttribute('data-frame'),
      scale,
      host: box(host),
      wide: box(columns[0]),
      narrow: box(columns[1]),
      figure: box(figure),
      room: box(room ?? figure),
      window: box(windowEl ?? image),
      image: box(image),
      bar: bar ? box(bar) : { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 },
      dots: figure.querySelectorAll('.deck-showcase-dot').length,
      camera: figure.querySelectorAll('.deck-showcase-camera').length,
      glow: glow
        ? { ariaHidden: glow.getAttribute('aria-hidden'), backdrop: glow.getAttribute('data-export-backdrop'), background: getComputedStyle(glow).backgroundImage }
        : null,
      vars: showcase
        ? {
            ratio: showcase.style.getPropertyValue('--deck-image-ratio'),
            roomW: showcase.style.getPropertyValue('--deck-room-w'),
            roomH: showcase.style.getPropertyValue('--deck-room-h'),
          }
        : { ratio: '', roomW: '', roomH: '' },
      windowBackground: windowEl ? getComputedStyle(windowEl).backgroundColor : '',
    };
  });
}

interface ChallengeGeometry {
  scale: number;
  host: Box;
  block: Box;
  fill: string | null;
  cards: { role: string | null; box: Box; tone: string; borderLeft: string; label: string }[];
  arrow: { box: Box; ariaHidden: string | null } | null;
  errorTone: string;
  successTone: string;
}

async function readChallenge(page: Page): Promise<ChallengeGeometry> {
  const section = page.locator('.reveal .slides > section.present');
  await expect(section.locator('.deck-challenge')).toBeVisible();
  return section.evaluate((el) => {
    const box = (node: Element): Box => {
      const r = node.getBoundingClientRect();
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height };
    };
    const frame = el.querySelector<HTMLElement>('.deck-slide');
    const host = el.querySelector<HTMLElement>('.deck-fit');
    const block = el.querySelector<HTMLElement>('.deck-challenge');
    if (!frame || !host || !block) throw new Error('No challenge_answer block on the stage');
    const arrow = block.querySelector<HTMLElement>(':scope > .deck-challenge-arrow');
    const cards = Array.from(block.querySelectorAll<HTMLElement>(':scope > .deck-challenge-card')).map((card) => ({
      role: card.getAttribute('data-role'),
      box: box(card),
      tone: getComputedStyle(card).getPropertyValue('--tone').trim(),
      borderLeft: getComputedStyle(card).borderLeftColor,
      label: (card.querySelector('.deck-challenge-label')?.textContent ?? '').trim(),
    }));
    const tokens = getComputedStyle(block);
    return {
      scale: frame.getBoundingClientRect().width / frame.offsetWidth,
      host: box(host),
      block: box(block),
      fill: block.getAttribute('data-fill'),
      cards,
      arrow: arrow ? { box: box(arrow), ariaHidden: arrow.getAttribute('aria-hidden') } : null,
      errorTone: tokens.getPropertyValue('--deck-error').trim(),
      successTone: tokens.getPropertyValue('--deck-success').trim(),
    };
  });
}

/** Browser px on the scaled stage: a hair of rounding, never a layout difference. */
const TOLERANCE = 2;
const close = (a: number, b: number, tolerance = TOLERANCE): boolean => Math.abs(a - b) <= tolerance;

test.describe('Presentations - the showcase image and the challenge_answer block', () => {
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
      // The copied screenshot is a file of the deck's owner; deleting the deck is what the library does.
      await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('a browser-framed fill image takes the body height of the wide column, the window hugging the picture', async ({
    page,
  }) => {
    await openStage(page, deckId, position('s-browser'));
    const g = await readShowcase(page);
    expect(g.frame).toBe('browser');

    // The column of the picture stretches to the body; the reading beside it hugs its text.
    expect(close(g.wide.height, g.host.height, 3)).toBe(true);
    expect(g.narrow.height).toBeLessThan(g.host.height * 0.8);
    // The figure takes the column, the room takes the figure.
    expect(close(g.figure.height, g.wide.height, 3)).toBe(true);
    expect(close(g.room.height, g.figure.height, 3)).toBe(true);

    // The room was measured in canvas px and the picture's ratio published on the box.
    expect(Number.parseFloat(g.vars.ratio)).toBeCloseTo(RATIO, 3);
    expect(Number.parseFloat(g.vars.roomW) * g.scale).toBeCloseTo(g.room.width, 0);
    expect(Number.parseFloat(g.vars.roomH) * g.scale).toBeCloseTo(g.room.height, 0);

    // The window is the largest box of the picture's ratio the room holds: here
    // the column's width binds (a 1.6 picture under a 16:9 body), so the window
    // spans the column and sits centred in the height it did not use.
    expect(close(g.window.width, g.room.width)).toBe(true);
    expect(g.window.height).toBeLessThan(g.room.height - TOLERANCE);
    const above = g.window.top - g.room.top;
    const below = g.room.bottom - g.window.bottom;
    expect(above).toBeGreaterThan(TOLERANCE);
    expect(close(above, below)).toBe(true);

    // No letterboxing: the picture's box IS the picture - the window's inner
    // width, at the picture's own ratio - under a bar of the frame's height.
    const hairline = HAIRLINE_PX * g.scale;
    expect(close(g.image.width, g.window.width - 2 * hairline)).toBe(true);
    expect(close(g.image.height, g.image.width / RATIO)).toBe(true);
    expect(close(g.bar.height, BROWSER_BAR_PX * g.scale)).toBe(true);
    expect(close(g.image.top, g.bar.bottom)).toBe(true);
    expect(close(g.window.height, g.bar.height + g.image.height + 2 * hairline)).toBe(true);
    expect(g.dots).toBe(3);
    expect(g.camera).toBe(0);

    // The glow: decoration, captured alone by the export, painted as a gradient.
    expect(g.glow).not.toBeNull();
    expect(g.glow?.ariaHidden).toBe('true');
    expect(g.glow?.backdrop).toBe('true');
    expect(g.glow?.background).toContain('radial-gradient');
  });

  test('the device frame draws a bezel with a camera dot and keeps the window inside the room', async ({ page }) => {
    await openStage(page, deckId, position('s-device'));
    const g = await readShowcase(page);
    expect(g.frame).toBe('device');
    expect(g.camera).toBe(1);
    expect(g.dots).toBe(0);
    expect(g.windowBackground).not.toBe('rgba(0, 0, 0, 0)');
    expect(close(g.wide.height, g.host.height, 3)).toBe(true);
    expect(g.window.width).toBeLessThanOrEqual(g.room.width + TOLERANCE);
    expect(g.window.height).toBeLessThanOrEqual(g.room.height + TOLERANCE);
    // The picture keeps its ratio inside the bezel.
    expect(close(g.image.height, g.image.width / RATIO)).toBe(true);
    expect(g.image.left).toBeGreaterThan(g.window.left + 8 * g.scale);
  });

  test('a bare fill image in the wide column takes the body height too, as before it could not', async ({ page }) => {
    await openStage(page, deckId, position('s-bare'));
    const g = await readShowcase(page);
    expect(g.frame).toBeNull();
    expect(g.glow).toBeNull();
    // No showcase box: the picture itself is handed the column, as a fill chart is.
    expect(g.vars.ratio).toBe('');
    expect(close(g.wide.height, g.host.height, 3)).toBe(true);
    expect(close(g.image.height, g.figure.height, 3)).toBe(true);
    expect(g.image.height).toBeGreaterThan(LG_BUCKET_PX * g.scale * 0.9);
  });

  test('a framed image that does not fill takes its size bucket: the picture is the bucket tall, the window its ratio wider', async ({
    page,
  }) => {
    await openStage(page, deckId, position('s-bucket'));
    // A single-flow body has no columns: read the figure alone.
    const section = page.locator('.reveal .slides > section.present');
    await expect(section.locator('.deck-figure img.deck-image')).toBeVisible();
    const geometry = await section.evaluate((el) => {
      const frame = el.querySelector<HTMLElement>('.deck-slide') as HTMLElement;
      const figure = el.querySelector<HTMLElement>('.deck-figure') as HTMLElement;
      const windowEl = figure.querySelector<HTMLElement>('.deck-showcase-window') as HTMLElement;
      const image = figure.querySelector<HTMLImageElement>('img.deck-image') as HTMLImageElement;
      const showcase = figure.querySelector<HTMLElement>('.deck-showcase') as HTMLElement;
      const r = (node: Element) => node.getBoundingClientRect();
      return {
        scale: r(frame).width / frame.offsetWidth,
        fill: figure.getAttribute('data-fill'),
        size: figure.getAttribute('data-size'),
        roomW: showcase.style.getPropertyValue('--deck-room-w'),
        window: { width: r(windowEl).width, height: r(windowEl).height },
        image: { width: r(image).width, height: r(image).height },
        figureWidth: r(figure).width,
      };
    });
    expect(geometry.fill).toBeNull();
    expect(geometry.size).toBe('lg');
    // No room is measured for a picture that does not fill.
    expect(geometry.roomW).toBe('');
    const expectedImageWidth = Math.min(geometry.figureWidth - 2 * HAIRLINE_PX * geometry.scale, LG_BUCKET_PX * geometry.scale * RATIO);
    expect(close(geometry.image.width, expectedImageWidth)).toBe(true);
    expect(close(geometry.image.height, geometry.image.width / RATIO)).toBe(true);
    expect(close(geometry.window.width, geometry.image.width + 2 * HAIRLINE_PX * geometry.scale)).toBe(true);
  });

  test('challenge_answer stacks two cards of one width and one left edge, error over success, joined by an arrow', async ({
    page,
  }) => {
    await openStage(page, deckId, position('s-challenge'));
    const g = await readChallenge(page);
    expect(g.fill).toBe('true');
    expect(g.cards.map((card) => card.role)).toEqual(['challenge', 'answer']);
    const [challenge, answer] = g.cards;

    // One width, one left edge - whatever the two sentences measure.
    expect(close(challenge.box.width, answer.box.width, 1)).toBe(true);
    expect(close(challenge.box.left, answer.box.left, 1)).toBe(true);
    expect(close(challenge.box.width, g.block.width, 1)).toBe(true);

    // Error over success: the eyebrow, the left bar and the tone variable are the card's.
    expect(challenge.label).toBe('Challenge');
    expect(answer.label).toBe('Answer');
    expect(challenge.tone).toBe(g.errorTone);
    expect(answer.tone).toBe(g.successTone);
    expect(challenge.borderLeft).not.toBe(answer.borderLeft);

    // The arrow fills the gap between them, edge to edge, centred, and is decoration.
    expect(g.arrow).not.toBeNull();
    const arrow = g.arrow as NonNullable<typeof g.arrow>;
    expect(arrow.ariaHidden).toBe('true');
    expect(close(arrow.box.top, challenge.box.bottom, 1)).toBe(true);
    expect(close(arrow.box.bottom, answer.box.top, 1)).toBe(true);
    expect(close(answer.box.top - challenge.box.bottom, CHALLENGE_GAP_PX * g.scale, 1)).toBe(true);
    const centre = (g.block.left + g.block.right) / 2;
    expect(close((arrow.box.left + arrow.box.right) / 2, centre)).toBe(true);

    // `fill`: the two cards share the room above the footer, equally; the arrow keeps its height.
    expect(close(challenge.box.height, answer.box.height)).toBe(true);
    expect(close(challenge.box.top, g.host.top, 3)).toBe(true);
    expect(close(answer.box.bottom, g.host.bottom, 3)).toBe(true);
    expect(challenge.box.height).toBeGreaterThan(arrow.box.height * 2);
  });

  test('in a column without fill, the block hugs its words and the cards still share one width', async ({ page }) => {
    await openStage(page, deckId, position('s-challenge-column'));
    const g = await readChallenge(page);
    expect(g.fill).toBeNull();
    const [challenge, answer] = g.cards;
    expect(close(challenge.box.width, answer.box.width, 1)).toBe(true);
    expect(close(challenge.box.left, answer.box.left, 1)).toBe(true);
    // Not stretched: the block ends well above the body's floor.
    expect(g.host.bottom - g.block.bottom).toBeGreaterThan(40);
    // The cards hug their sentences: a one-line answer is a shorter card than the two-line challenge, or as tall.
    expect(answer.box.height).toBeLessThanOrEqual(challenge.box.height + TOLERANCE);
  });

  test('photo columns beside text cards: no shrink, the photos cropped to their cells, the list at its plain size', async ({
    page,
  }) => {
    const readPhotoSlide = async (at: number) => {
      await openStage(page, deckId, at);
      const section = page.locator('.reveal .slides > section.present');
      return section.evaluate((el) => {
        const host = el.querySelector<HTMLElement>('.deck-fit');
        const item = el.querySelector<HTMLElement>('.deck-list li');
        const figures = Array.from(el.querySelectorAll<HTMLElement>('.deck-figure'));
        return {
          fitted: host?.hasAttribute('data-fit') ?? false,
          fontSize: item ? Number.parseFloat(getComputedStyle(item).fontSize) : 0,
          itemHeight: item?.getBoundingClientRect().height ?? 0,
          figures: figures.map((figure) => {
            const img = figure.querySelector<HTMLElement>('img.deck-image') as HTMLElement;
            const f = figure.getBoundingClientRect();
            const i = img.getBoundingClientRect();
            return { figureH: f.height, imageH: i.height, imageW: i.width, figureW: f.width, fit: getComputedStyle(img).objectFit };
          }),
        };
      });
    };
    const photo = await readPhotoSlide(position('s-photo-columns'));
    const plain = await readPhotoSlide(position('s-plain-list'));
    // The photos never shrink the body: it carries no fit ratio at all.
    expect(photo.fitted).toBe(false);
    // The list in a callout card is set at the size of a list md on a plain two-column slide.
    expect(plain.fontSize).toBeGreaterThan(0);
    expect(Math.abs(photo.fontSize - plain.fontSize)).toBeLessThanOrEqual(1);
    expect(Math.abs(photo.itemHeight - plain.itemHeight)).toBeLessThanOrEqual(1);
    // Both photos are cropped to fill their cell (cover), never letterboxed in it.
    expect(photo.figures).toHaveLength(2);
    for (const figure of photo.figures) {
      expect(figure.fit).toBe('cover');
      expect(close(figure.imageW, figure.figureW, 1)).toBe(true);
      expect(close(figure.imageH, figure.figureH, 1)).toBe(true);
    }
  });

  test('the layout check finds nothing cut, shrunk or overflowing on either composition', async () => {
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
    const excess = new Set(['fit', 'clipped', 'nowrap', 'overflow', 'size']);
    for (const id of ['s-browser', 's-device', 's-bare', 's-challenge', 's-challenge-column', 's-photo-columns', 's-plain-list']) {
      const bad = report.findings.filter((finding) => finding.slide_id === id && excess.has(finding.kind));
      expect(bad, dump).toEqual([]);
    }
  });
});
