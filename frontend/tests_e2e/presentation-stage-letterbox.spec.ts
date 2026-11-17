import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import {
  createAdminApiContext,
  createTwoSchemeTemplate,
  deleteTemplate,
  expectEditorSaved,
  importDeckOnTemplate,
} from './helpers';

// ---------------------------------------------------------------------------
// Presentations - the stage letterboxes a deck in the app background.
//
// The case of the report: a LIGHT deck whose cover carries its own dark theme.
// In the viewer and the studio the dark cover sat between two white
// bands - the deck root filled the whole stage surface and painted the DECK's
// paper around the slide wherever the surface's ratio was not the deck's.
// The stage now letterboxes the deck at rest as it does while presenting:
// the deck root is the largest 16:9 box of the surface, centred, and
// what shows around it is the surface, painted with the app's own background -
// whatever the deck theme, whatever the scheme of the slide on screen, in the
// dark and in the light app modes, beside the studio rail and with the editor
// on the stage too.
//
// The decks are imported through the API (no LLM call) on a template of their
// own that dresses both schemes: the install's default depends on the platform
// mode (the dark-only XTM One Brand Deck in CI), and the editor snaps a light
// deck under a dark-only template to dark the moment it opens. Decks and
// template are owned by the admin and deleted afterwards.
// ---------------------------------------------------------------------------

const FIXTURE_SPEC = {
  title: 'E2E stage letterbox deck',
  theme: 'light',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'title', title: 'Uncover Threats.\nTake Action.', subtitle: 'A dark cover on a light deck', theme: 'dark' },
    {
      layout: 'content',
      title: 'The body stays light',
      blocks: [
        { type: 'heading', content: 'A heading to select', level: 3 },
        { type: 'text', content: 'A paragraph that follows the deck theme.' },
      ],
    },
    // From the cover, the last two slides lie beyond reveal's view distance.
    { layout: 'content', title: 'A third slide', blocks: [{ type: 'text', content: 'Still light.' }] },
    { layout: 'content', title: 'A fourth slide', blocks: [{ type: 'text', content: 'Far from the cover.' }] },
    { layout: 'closing', title: 'Thank you' },
  ],
};

// The reverse: a DARK deck whose cover carries its own light theme.
const REVERSE_SPEC = {
  title: 'E2E stage letterbox dark deck',
  theme: 'dark',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'title', title: 'A light cover', subtitle: 'On a dark deck', theme: 'light' },
    { layout: 'closing', title: 'Thank you' },
  ],
};

// A fading deck: its papers cross-fade under the fading slides.
const FADE_SPEC = {
  title: 'E2E stage letterbox fading deck',
  theme: 'light',
  aspect_ratio: '16:9',
  transition: 'fade',
  slides: [
    { layout: 'title', title: 'A dark cover', subtitle: 'That fades', theme: 'dark' },
    { layout: 'content', title: 'A light slide', blocks: [{ type: 'text', content: 'Faded in.' }] },
  ],
};

// A light deck whose LAST slide carries its own dark theme: jumping
// back from it across the deck, reveal hides it at once.
const JUMP_BACK_SPEC = {
  title: 'E2E stage letterbox jump back deck',
  theme: 'light',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'content', title: 'First', blocks: [{ type: 'text', content: 'Light.' }] },
    { layout: 'content', title: 'Second', blocks: [{ type: 'text', content: 'Light.' }] },
    { layout: 'content', title: 'Third', blocks: [{ type: 'text', content: 'Light.' }] },
    { layout: 'content', title: 'Fourth', blocks: [{ type: 'text', content: 'Light.' }] },
    { layout: 'closing', title: 'Thank you', theme: 'dark' },
  ],
};

// A light deck on a template whose light paper is a gradient.
const GRADIENT_PAPER = 'linear-gradient(90deg, #ff8a00, #00a3ff)';
const GRADIENT_SPEC = {
  title: 'E2E stage letterbox gradient deck',
  theme: 'light',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'content', title: 'On a gradient paper', blocks: [{ type: 'text', content: 'The template paper.' }] },
    { layout: 'closing', title: 'Thank you' },
  ],
};

/** The two-scheme template the fixture decks wear, created once per worker. */
let templateName = '';
let templateId = '';

/** Runs `action` with an admin API context, disposed afterwards. */
async function asAdmin<T>(action: (apiCtx: APIRequestContext, token: string) => Promise<T>): Promise<T> {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    return await action(apiCtx, token);
  } finally {
    await apiCtx.dispose();
  }
}

async function deleteFixtureDeck(id: string): Promise<void> {
  if (!id) return;
  const { apiCtx, token } = await createAdminApiContext();
  try {
    await apiCtx.delete(`/api/v1/presentations/${id}`, { headers: { Authorization: `Bearer ${token}` } });
  } finally {
    await apiCtx.dispose();
  }
}

type Box = { x: number; y: number; width: number; height: number };
type Rgb = [number, number, number];
type Point = [number, number];

async function boxOf(locator: Locator): Promise<Box> {
  const box = await locator.boundingBox();
  if (!box) throw new Error('element has no box');
  return box;
}

/** The largest per-channel difference between two colours. */
function distance(a: Rgb, b: Rgb): number {
  return Math.max(...a.map((channel, i) => Math.abs(channel - b[i])));
}

/** A CSS colour value resolved by the browser inside `scope`, as [r, g, b]. */
async function resolvedColor(scope: Locator, value: string): Promise<Rgb> {
  return scope.evaluate((el, cssValue) => {
    const probe = document.createElement('span');
    probe.style.backgroundColor = cssValue;
    el.appendChild(probe);
    const color = getComputedStyle(probe).backgroundColor;
    probe.remove();
    const [r, g, b] = (color.match(/[\d.]+/g) ?? []).map((n) => Math.round(Number(n)));
    return [r, g, b] as [number, number, number];
  }, value);
}

/** The app's own background as it stands now (mode, theme and branding included):
 *  the design-system surface the page body and the resting stage both paint. */
function appBackground(page: Page): Promise<Rgb> {
  return resolvedColor(page.locator('body'), 'var(--bg-elevation-default)');
}

/** The colour of one screen pixel, read off a fresh screenshot in the page. */
async function pixelAt(page: Page, x: number, y: number): Promise<Rgb> {
  const png = await page.screenshot({ clip: { x, y, width: 1, height: 1 } });
  return page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const canvas = document.createElement('canvas');
    canvas.width = 1;
    canvas.height = 1;
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(img, 0, 0);
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
    return [r, g, b] as [number, number, number];
  }, png.toString('base64'));
}

/** A point of a box, as fractions of its width and height. */
function pointIn(box: Box, fx: number, fy: number): Point {
  return [Math.round(box.x + box.width * fx), Math.round(box.y + box.height * fy)];
}

/**
 * Starts a slide change and freezes it `at` ms in: every running animation -
 * the papers and the slides alike - is paused and seeked, so the pixels read
 * next are those of that frame. Fails when the papers had already settled.
 */
async function freezeSlideChange(page: Page, start: () => Promise<void>, at: number): Promise<void> {
  await start();
  const papers = await page.evaluate((ms) => {
    document.body.getBoundingClientRect();
    const animations = document.getAnimations();
    for (const animation of animations) {
      animation.pause();
      animation.currentTime = ms;
    }
    return animations.filter((animation) => {
      const target = animation.effect instanceof KeyframeEffect ? animation.effect.target : null;
      return target instanceof Element && target.classList.contains('slide-background');
    }).length;
  }, at);
  expect(papers, 'the papers were still changing when frozen').toBeGreaterThan(0);
}

/** Waits until no paper is changing any more. */
async function waitForSettledPapers(page: Page): Promise<void> {
  await page.waitForFunction(() =>
    document.getAnimations().every((animation) => {
      const target = animation.effect instanceof KeyframeEffect ? animation.effect.target : null;
      const paper = target instanceof Element && target.classList.contains('slide-background');
      return !paper || animation.playState === 'finished';
    }),
  );
}

/** The opacity of every slide's paper, in slide order. */
function readPaperOpacities(page: Page): Promise<number[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll('.reveal .backgrounds > .slide-background')).map((background) =>
      Number(getComputedStyle(background).opacity),
    ),
  );
}

/** The class of the element the pointer would hit at a point. */
async function classAt(page: Page, x: number, y: number): Promise<string> {
  return page.evaluate(([px, py]) => {
    const el = document.elementFromPoint(px, py) as HTMLElement | null;
    return el ? String(el.className) : '(none)';
  }, [x, y] as const);
}

/** The reveal section of a slide - the SLIDE ROOT that carries the scheme. */
function slideRoot(page: Page, index: number): Locator {
  return page.locator('.reveal .slides > section').nth(index);
}

/** The viewer's counter button reads `2 / 3` and is named "Slide 2 of 3. Go to slide". */
async function expectStageSlide(page: Page, current: number): Promise<void> {
  await expect(page.getByRole('button', { name: new RegExp(`^Slide ${current} of \\d+\\.`) })).toBeVisible();
}

/**
 * The deck root is the largest 16:9 box of the stage surface, centred, and
 * reveal's canvas spans it exactly: the deck's paper ends where the slide
 * does. Returns the two boxes once they settle.
 */
async function expectLetterboxed(stage: Locator): Promise<{ surface: Box; root: Box }> {
  const surfaceEl = stage.locator('.deck-stage-surface');
  const rootEl = surfaceEl.locator(':scope > .deck-root');
  const read = async () => ({ surface: await boxOf(surfaceEl), root: await boxOf(rootEl) });
  await expect
    .poll(async () => {
      const { surface: s, root: r } = await read();
      const width = Math.min(s.width, (s.height * 16) / 9);
      const height = Math.min(s.height, (s.width * 9) / 16);
      const off = Math.max(
        Math.abs(r.width - width),
        Math.abs(r.height - height),
        Math.abs(r.x - (s.x + (s.width - width) / 2)),
        Math.abs(r.y - (s.y + (s.height - height) / 2)),
      );
      return off <= 1.5 ? 'fits' : `surface ${JSON.stringify(s)} root ${JSON.stringify(r)}`;
    })
    .toBe('fits');
  const boxes = await read();
  await expect
    .poll(async () => {
      const slides = await boxOf(stage.locator('.reveal .slides'));
      return Math.max(Math.abs(slides.width - boxes.root.width), Math.abs(slides.height - boxes.root.height));
    })
    .toBeLessThanOrEqual(1.5);
  return boxes;
}

/** The middle of each band the letterbox shows beside the deck root. */
function bandPoints(surface: Box, root: Box): Point[] {
  const side = root.x - surface.x;
  const top = root.y - surface.y;
  if (side >= top) {
    const y = Math.round(surface.y + surface.height / 2);
    const right = root.x + root.width;
    return [
      [Math.round(surface.x + side / 2), y],
      [Math.round(right + (surface.x + surface.width - right) / 2), y],
    ];
  }
  const x = Math.round(surface.x + surface.width / 2);
  const bottom = root.y + root.height;
  return [
    [x, Math.round(surface.y + top / 2)],
    [x, Math.round(bottom + (surface.y + surface.height - bottom) / 2)],
  ];
}

/** Every band is the surface, and its pixels are `expected`. */
async function expectBands(page: Page, bands: Point[], expected: Rgb): Promise<void> {
  for (const [x, y] of bands) {
    expect(await classAt(page, x, y), `at ${x},${y}`).toContain('deck-stage-surface');
    await expect
      .poll(async () => distance(await pixelAt(page, x, y), expected), { message: `pixel at ${x},${y} vs ${expected}` })
      .toBeLessThanOrEqual(3);
  }
}

/** A strip across one edge of the deck root, and whether it runs down (a side edge). */
type EdgeStrip = { name: string; clip: Box; vertical: boolean };

/** A strip 3 CSS px either side of every edge of the root that has a band beside it. */
function bandedEdges(surface: Box, root: Box): EdgeStrip[] {
  const strips: EdgeStrip[] = [];
  if (root.y - surface.y > 4) {
    const width = root.width - 24;
    strips.push({ name: 'top', clip: { x: root.x + 12, y: root.y - 3, width, height: 6 }, vertical: false });
    strips.push({ name: 'bottom', clip: { x: root.x + 12, y: root.y + root.height - 3, width, height: 6 }, vertical: false });
  }
  if (root.x - surface.x > 4) {
    const height = root.height - 24;
    strips.push({ name: 'left', clip: { x: root.x - 3, y: root.y + 12, width: 6, height }, vertical: true });
    strips.push({ name: 'right', clip: { x: root.x + root.width - 3, y: root.y + 12, width: 6, height }, vertical: true });
  }
  return strips;
}

/**
 * The mean luminance of every device-pixel line of a strip, from one side of
 * the edge to the other: rows across a top or bottom edge, columns across a
 * side.
 */
async function edgeProfile(page: Page, strip: EdgeStrip): Promise<number[]> {
  const png = await page.screenshot({ clip: strip.clip });
  return page.evaluate(
    async ([b64, vertical]) => {
      const img = new Image();
      img.src = `data:image/png;base64,${b64}`;
      await img.decode();
      const canvas = document.createElement('canvas');
      canvas.width = img.width;
      canvas.height = img.height;
      const ctx = canvas.getContext('2d')!;
      ctx.drawImage(img, 0, 0);
      const { data, width, height } = ctx.getImageData(0, 0, img.width, img.height);
      const lines = vertical ? width : height;
      const along = vertical ? height : width;
      const profile: number[] = [];
      for (let line = 0; line < lines; line += 1) {
        let sum = 0;
        for (let i = 0; i < along; i += 1) {
          const offset = ((vertical ? i : line) * width + (vertical ? line : i)) * 4;
          sum += 0.2126 * data[offset] + 0.7152 * data[offset + 1] + 0.0722 * data[offset + 2];
        }
        profile.push(Math.round(sum / along));
      }
      return profile;
    },
    [png.toString('base64'), strip.vertical] as const,
  );
}

/**
 * How far the extreme line of a profile lies outside its two ends - the band
 * and the slide: a paper leaking into the edge is a line lighter than both
 * (the deck's white along a dark cover) or darker than both (the deck's navy
 * along a light one).
 */
function beyondEnds(profile: number[]): number {
  const ends = [profile[0], profile[profile.length - 1]];
  return Math.max(Math.max(...profile) - Math.max(...ends), Math.min(...ends) - Math.min(...profile));
}

/** At every size, no device-pixel line across a banded edge of the deck lies outside the band and the slide. */
async function expectCleanEdges(page: Page, sizes: [number, number][]): Promise<void> {
  const stage = page.locator('.deck-stage');
  let edges = 0;
  for (const [width, height] of sizes) {
    await page.setViewportSize({ width, height });
    const { surface, root } = await expectLetterboxed(stage);
    for (const strip of bandedEdges(surface, root)) {
      const profile = await edgeProfile(page, strip);
      expect(beyondEnds(profile), `${width}x${height} ${strip.name} edge: ${profile.join(',')}`).toBeLessThanOrEqual(8);
      edges += 1;
    }
  }
  expect(edges, 'every size has banded edges to read').toBe(sizes.length * 2);
}

type Paper = { own: string; painted: string; ownImage: string; paintedImage: string; display: string };

/**
 * Per slide: the paper it draws and the one reveal paints under it - the
 * colour and the gradient layer over it - and whether that background is laid
 * out. A palette gradient is drawn over the scheme's own paper, which is then
 * the colour expected under it.
 */
function readPapers(page: Page): Promise<Paper[]> {
  return page.evaluate(() => {
    // Probed inside the deck root, where each scheme's paper token is declared.
    const root = document.querySelector('.deck-stage-surface > .deck-root') ?? document.body;
    const sections = Array.from(document.querySelectorAll('.reveal .slides > section'));
    const backgrounds = Array.from(document.querySelectorAll('.reveal .backgrounds > .slide-background'));
    return sections.map((section, i) => {
      const paper = getComputedStyle(section).getPropertyValue('--deck-bg');
      const probe = document.createElement('span');
      root.appendChild(probe);
      probe.style.backgroundImage = paper;
      const ownImage = getComputedStyle(probe).backgroundImage;
      probe.style.backgroundColor = ownImage === 'none' ? paper : `var(--deck-paper-${section.getAttribute('data-scheme')})`;
      const own = getComputedStyle(probe).backgroundColor;
      probe.remove();
      const background = backgrounds[i] ? getComputedStyle(backgrounds[i]) : null;
      return {
        own,
        ownImage,
        painted: background?.backgroundColor ?? '(none)',
        paintedImage: background?.backgroundImage ?? '(none)',
        display: background?.display ?? '(none)',
      };
    });
  });
}

/**
 * reveal paints each slide's background in that slide's own paper - a palette
 * gradient over the scheme's paper, and no gradient a previous paper left
 * behind - and on a stage every background stays laid out - those reveal
 * hides as too far from the current slide included - so a jump across the
 * deck never uncovers the stage while the incoming paper is on its way in.
 */
async function expectOwnPapers(page: Page, slides: number): Promise<void> {
  await expect
    .poll(async () => {
      const papers = await readPapers(page);
      if (papers.length !== slides) return `${papers.length} slides`;
      const off = papers.flatMap(({ own, painted, ownImage, paintedImage, display }, i) => [
        ...(painted === own ? [] : [`slide ${i + 1} painted ${painted}, draws ${own}`]),
        ...(paintedImage === ownImage ? [] : [`slide ${i + 1} painted ${paintedImage}, draws ${ownImage}`]),
        ...(display === 'block' ? [] : [`slide ${i + 1} background display: ${display}`]),
      ]);
      return off.join('; ') || 'own papers';
    })
    .toBe('own papers');
}

test.describe('Presentations - the stage letterboxes a deck in the app background', () => {
  let deckId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      templateName = `E2E stage letterbox template ${Date.now()}`;
      templateId = await createTwoSchemeTemplate(apiCtx, token, templateName);
      deckId = await importDeckOnTemplate(apiCtx, token, FIXTURE_SPEC, templateName, 'e2e-stage-letterbox.json');
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    await deleteFixtureDeck(deckId);
    await deleteTemplate(templateId);
  });

  test('a dark cover on a light deck sits on the app background, on every slide and in both app modes', async ({
    page,
  }) => {
    // A wide window: the stage is wider than 16:9 and pillarboxes the slide,
    // the screenshot of the report.
    await page.setViewportSize({ width: 1600, height: 800 });
    await page.goto(`/presentations/${deckId}`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    const stage = page.locator('.deck-stage');
    const root = stage.locator('.deck-stage-surface > .deck-root');
    await expect(root).toHaveAttribute('data-deck-theme', 'light');
    await expect(slideRoot(page, 0)).toHaveAttribute('data-scheme', 'dark');
    await expect(slideRoot(page, 0)).toHaveAttribute('data-scheme-override', 'true');

    const { surface, root: rootBox } = await expectLetterboxed(stage);
    expect(rootBox.x - surface.x, 'the stage pillarboxes the slide at this size').toBeGreaterThan(40);
    const bands = bandPoints(surface, rootBox);
    const dark = await appBackground(page);
    const paper = await resolvedColor(root, 'var(--deck-bg)');
    expect(distance(dark, paper), 'the fixture tells the app background from the deck paper').toBeGreaterThan(60);
    await expectBands(page, bands, dark);

    // A light slide: the bands follow the app, not the slide's scheme.
    await page.getByRole('button', { name: 'Next slide' }).click();
    await expectStageSlide(page, 2);
    await expect(slideRoot(page, 1)).toHaveAttribute('data-scheme', 'light');
    await expectBands(page, bands, dark);

    // The light app mode: the bands are the light app background - around
    // the dark cover too.
    await page.evaluate(() => {
      document.documentElement.dataset.mode = 'light';
    });
    const light = await appBackground(page);
    expect(distance(light, dark)).toBeGreaterThan(100);
    await expectBands(page, bands, light);
    await page.getByRole('button', { name: 'Previous slide' }).click();
    await expectStageSlide(page, 1);
    await expectBands(page, bands, light);
  });

  test('beside the studio rail the dark cover sits on the app background', async ({ page }) => {
    // The studio of the report: the rail takes the left of a wide screen and
    // the stage beside it is still wider than 16:9, so it pillarboxes the
    // cover.
    await page.setViewportSize({ width: 1920, height: 760 });
    await page.goto(`/presentations/${deckId}?studio=1`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    const rail = page.getByTestId('presentation.studio.rail');
    await expect(rail).toBeVisible();
    await expect(slideRoot(page, 0)).toHaveAttribute('data-scheme', 'dark');
    const stage = page.locator('.deck-stage');

    const { surface, root } = await expectLetterboxed(stage);
    const railBox = await boxOf(rail);
    expect(surface.x, 'the stage sits beside the rail').toBeGreaterThanOrEqual(railBox.x + railBox.width - 1);
    expect(root.x - surface.x, 'the stage pillarboxes the slide beside the rail').toBeGreaterThan(40);
    await expectBands(page, bandPoints(surface, root), await appBackground(page));
  });

  test('with the editor on the stage the bands stay the app background and a click still lands on its block', async ({
    page,
  }) => {
    // Beside the editor panel the stage is taller than 16:9: the slide is
    // letterboxed above and below.
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.goto(`/presentations/${deckId}?panel=editor`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    await expect(page.getByTestId('presentation.editorPanel')).toBeVisible();
    await expect(page.locator('[data-deck-editing="true"]')).toBeVisible();
    const stage = page.locator('.deck-stage');

    const { surface, root } = await expectLetterboxed(stage);
    expect(root.y - surface.y, 'the stage letterboxes the slide at this size').toBeGreaterThan(40);
    const bands = bandPoints(surface, root);
    const dark = await appBackground(page);
    await expectBands(page, bands, dark);

    // The editor's layer still covers the WHOLE surface: the deck root is
    // centred by the item, and a `place-items` on the grid would have
    // shrunk the layer with it.
    const layer = page.getByTestId('presentation.editLayer');
    await expect
      .poll(async () => {
        const box = await boxOf(layer);
        return Math.max(
          Math.abs(box.x - surface.x),
          Math.abs(box.y - surface.y),
          Math.abs(box.width - surface.width),
          Math.abs(box.height - surface.height),
        );
      })
      .toBeLessThanOrEqual(1);

    // A click on the heading's empty right-hand side selects the block, and
    // the block bar hangs just above it: the layer's boxes line up with the
    // slide inside the letterbox.
    await page.getByRole('button', { name: 'Next slide' }).click();
    await expectStageSlide(page, 2);
    const heading = page.locator('.reveal .deck-slide[data-slide-index="1"] [data-edit-block="blocks.0"]');
    const headingBox = await boxOf(heading);
    await heading.click({ position: { x: headingBox.width - 8, y: headingBox.height / 2 } });
    await expect(heading).toHaveAttribute('data-edit-selected', 'true');
    const bar = page.getByTestId('presentation.blockBar');
    await expect(bar).toBeVisible();
    await expect
      .poll(async () => {
        const barBox = await boxOf(bar);
        const target = await boxOf(heading);
        const bottom = barBox.y + barBox.height;
        return bottom <= target.y + 4 && bottom >= target.y - 24 && Math.abs(barBox.x - target.x) <= 2;
      })
      .toBe(true);
    await expectBands(page, bands, dark);
  });

  test('a deck theme picked in the editor repaints the paper under every slide at once', async ({ page }) => {
    // The stage shows the editor's draft: a new deck theme reaches the slides
    // that inherit it in the same render, and reveal - which caches a slide's
    // background - must paint each one's new paper at once. Its own deck: the
    // editor saves the theme on its own, and the other tests read the
    // shared deck's light theme.
    const themeDeckId = await asAdmin((apiCtx, token) =>
      importDeckOnTemplate(apiCtx, token, FIXTURE_SPEC, templateName, 'e2e-stage-letterbox-theme.json'),
    );
    try {
      await page.goto(`/presentations/${themeDeckId}?panel=editor`);
      await expect(page.locator('.reveal.ready')).toBeVisible();
      await expect(page.getByTestId('presentation.editorPanel')).toBeVisible();
      const root = page.locator('.deck-stage .deck-stage-surface > .deck-root');
      await expect(root).toHaveAttribute('data-deck-theme', 'light');
      await expectOwnPapers(page, FIXTURE_SPEC.slides.length);

      await page.getByTestId('presentation.editorTab.deck').click();
      const theme = page.getByTestId('presentation.editorPanel').getByRole('radiogroup', { name: 'Theme', exact: true });
      await theme.getByRole('radio', { name: 'Dark' }).click();
      await expect(root).toHaveAttribute('data-deck-theme', 'dark');
      await expect(slideRoot(page, 1)).toHaveAttribute('data-scheme', 'dark');
      await expectOwnPapers(page, FIXTURE_SPEC.slides.length);
      // Saved on its own, and the papers are still each slide's own after it.
      await expectEditorSaved(page);
      await expectOwnPapers(page, FIXTURE_SPEC.slides.length);
    } finally {
      await deleteFixtureDeck(themeDeckId);
    }
  });

  test('a jump across the deck keeps a paper under the whole slide while it runs', async ({ page }) => {
    // From the dark cover to the last slide, beyond reveal's view distance:
    // reveal hides the cover - its paper included - the moment the jump
    // starts, while the light paper slides in from the right. In the light
    // app mode the stage reads light wherever no paper is.
    await page.setViewportSize({ width: 1400, height: 800 });
    await page.goto(`/presentations/${deckId}`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    await page.evaluate(() => {
      document.documentElement.dataset.mode = 'light';
    });
    const { root } = await expectLetterboxed(page.locator('.deck-stage'));
    const cover = await resolvedColor(slideRoot(page, 0), 'var(--deck-bg)');
    expect(distance(cover, await appBackground(page)), 'the stage tells itself from the cover paper').toBeGreaterThan(60);

    await freezeSlideChange(page, () => page.keyboard.press('End'), 150);
    // 150 ms in, the outgoing cover paper still covers the left of the deck.
    const [x, y] = pointIn(root, 0.03, 0.5);
    expect(distance(await pixelAt(page, x, y), cover), `pixel at ${x},${y}`).toBeLessThanOrEqual(24);
  });

  test('a jump back across the deck keeps the leaving paper under the slide while it runs', async ({ page }) => {
    // Back from a last slide that carries its own dark theme to the first,
    // beyond reveal's view distance: the leaving slide is hidden at once and
    // its paper slides out to the right while the light one slides in from
    // the left. (Jumping TO a slide with a theme of its own needs no paper:
    // its section paints its own the moment it shows.)
    const backDeckId = await asAdmin((apiCtx, token) =>
      importDeckOnTemplate(apiCtx, token, JUMP_BACK_SPEC, templateName, 'e2e-stage-letterbox-back.json'),
    );
    try {
      await page.setViewportSize({ width: 1400, height: 800 });
      await page.goto(`/presentations/${backDeckId}`);
      await expect(page.locator('.reveal.ready')).toBeVisible();
      await page.evaluate(() => {
        document.documentElement.dataset.mode = 'light';
      });
      const { root } = await expectLetterboxed(page.locator('.deck-stage'));
      const last = JUMP_BACK_SPEC.slides.length;
      await page.keyboard.press('End');
      await expectStageSlide(page, last);
      await waitForSettledPapers(page);
      const closing = await resolvedColor(slideRoot(page, last - 1), 'var(--deck-bg)');
      expect(distance(closing, await appBackground(page)), 'the stage tells itself from the closing paper').toBeGreaterThan(60);

      await freezeSlideChange(page, () => page.keyboard.press('Home'), 150);
      // 150 ms in, the leaving paper still covers the right of the deck.
      const [x, y] = pointIn(root, 0.97, 0.5);
      expect(distance(await pixelAt(page, x, y), closing), `pixel at ${x},${y}`).toBeLessThanOrEqual(24);
    } finally {
      await deleteFixtureDeck(backDeckId);
    }
  });

  test('a fading deck cross-fades its papers under the fading slides, with no seam', async ({ page }) => {
    const fadeDeckId = await asAdmin((apiCtx, token) =>
      importDeckOnTemplate(apiCtx, token, FADE_SPEC, templateName, 'e2e-stage-letterbox-fade.json'),
    );
    try {
      await page.setViewportSize({ width: 1400, height: 800 });
      await page.goto(`/presentations/${fadeDeckId}`);
      await expect(page.locator('.reveal.ready')).toBeVisible();
      const { root } = await expectLetterboxed(page.locator('.deck-stage'));

      // Forward (the cover leaves) and back (the cover returns): the outgoing
      // paper is held whole under the incoming one, so the stage never shows
      // through mid-fade, and the blend is one colour edge to edge - no seam
      // sweeping across the slide.
      for (const [key, outgoing, incoming] of [
        ['ArrowRight', 0, 1],
        ['ArrowLeft', 1, 0],
      ] as const) {
        await waitForSettledPapers(page);
        await freezeSlideChange(page, () => page.keyboard.press(key), 150);
        const opacities = await readPaperOpacities(page);
        expect(opacities[outgoing], `${key}: the outgoing paper`).toBe(1);
        expect(opacities[incoming], `${key}: the incoming paper`).toBeGreaterThan(0);
        expect(opacities[incoming], `${key}: the incoming paper`).toBeLessThan(1);
        const left = await pixelAt(page, ...pointIn(root, 0.03, 0.5));
        const right = await pixelAt(page, ...pointIn(root, 0.97, 0.5));
        expect(distance(left, right), `${key}: left ${left} right ${right}`).toBeLessThanOrEqual(16);
        // Let the frozen change end before the next one starts.
        await page.evaluate(() => {
          for (const animation of document.getAnimations()) {
            try {
              animation.finish();
            } catch {
              // An endless animation (a spinner) has no end to jump to.
            }
          }
        });
      }
    } finally {
      await deleteFixtureDeck(fadeDeckId);
    }
  });

  test('a gradient template paper paints under its slides on the stage', async ({ page }) => {
    // A gradient is no colour: reveal's gradient layer carries it, over the
    // scheme's own paper, instead of the slide standing transparent.
    const name = `E2E stage letterbox gradient template ${Date.now()}`;
    const gradientTemplateId = await asAdmin((apiCtx, token) =>
      createTwoSchemeTemplate(apiCtx, token, name, { light: { background: GRADIENT_PAPER } }),
    );
    try {
      const gradientDeckId = await asAdmin((apiCtx, token) =>
        importDeckOnTemplate(apiCtx, token, GRADIENT_SPEC, name, 'e2e-stage-letterbox-gradient.json'),
      );
      try {
        await page.setViewportSize({ width: 1400, height: 800 });
        await page.goto(`/presentations/${gradientDeckId}`);
        await expect(page.locator('.reveal.ready')).toBeVisible();
        const { root } = await expectLetterboxed(page.locator('.deck-stage'));
        const layer = await page.evaluate(() => {
          const background = document.querySelector('.reveal .backgrounds > .slide-background.present');
          const style = background ? getComputedStyle(background) : null;
          return style ? { image: style.backgroundImage, color: style.backgroundColor } : null;
        });
        expect(layer?.image).toContain('linear-gradient');
        expect(layer?.color).not.toBe('rgba(0, 0, 0, 0)');
        // The pixels are the gradient's, from one side of the slide to the other.
        const orange: Rgb = [255, 138, 0];
        const blue: Rgb = [0, 163, 255];
        await expect.poll(async () => distance(await pixelAt(page, ...pointIn(root, 0.02, 0.5)), orange)).toBeLessThanOrEqual(40);
        await expect.poll(async () => distance(await pixelAt(page, ...pointIn(root, 0.98, 0.5)), blue)).toBeLessThanOrEqual(40);
      } finally {
        await deleteFixtureDeck(gradientDeckId);
      }
    } finally {
      await deleteTemplate(gradientTemplateId);
    }
  });

  test.describe('with a gradient palette a draft takes away', () => {
    // The template's dark palette names a gradient, its light variant a
    // colour: reveal draws the gradient on its gradient layer over the dark
    // paper. reveal's per-slide sync writes a gradient but never clears one,
    // so a draft that takes it away - a theme, a template - must still leave
    // every slide on its own paper alone.
    let gradientTemplateId = '';
    let gradientDeckId = '';

    test.beforeAll(async () => {
      const name = `E2E stage letterbox dark gradient template ${Date.now()}`;
      const dark = { background: 'linear-gradient(135deg, #070d18 0%, #1f3965 100%)' };
      gradientTemplateId = await asAdmin((apiCtx, token) => createTwoSchemeTemplate(apiCtx, token, name, { dark }));
      gradientDeckId = await asAdmin((apiCtx, token) =>
        importDeckOnTemplate(apiCtx, token, REVERSE_SPEC, name, 'e2e-stage-letterbox-dark-gradient.json'),
      );
    });

    test.afterAll(async () => {
      await deleteFixtureDeck(gradientDeckId);
      await deleteTemplate(gradientTemplateId);
    });

    test('the gradient paints under its slides and leaves nothing behind when a draft takes it away', async ({ page }) => {
      await page.goto(`/presentations/${gradientDeckId}?panel=editor`);
      await expect(page.locator('.reveal.ready')).toBeVisible();
      const panel = page.getByTestId('presentation.editorPanel');
      await expect(panel).toBeVisible();
      const root = page.locator('.deck-stage .deck-stage-surface > .deck-root');
      await expect(root).toHaveAttribute('data-deck-theme', 'dark');
      // The closing slide inherits the dark scheme: the gradient over the dark paper.
      await expect(slideRoot(page, 1)).toHaveAttribute('data-scheme', 'dark');
      await expect.poll(async () => (await readPapers(page))[1]?.paintedImage ?? '').toContain('linear-gradient');
      await expectOwnPapers(page, REVERSE_SPEC.slides.length);

      // First an edit that touches no slide and no palette: the stage renders
      // the draft's own copy of the slides from the moment the editor opens,
      // so every step below reaches reveal as a per-slide sync, which
      // never clears a leftover gradient by itself.
      await panel.getByTestId('presentation.editorTab.deck').click();
      await panel.getByRole('switch', { name: 'Show slide numbers', exact: true }).click();
      await expectOwnPapers(page, REVERSE_SPEC.slides.length);

      // Light: the light variant's colour, and no gradient left over it.
      const theme = panel.getByRole('radiogroup', { name: 'Theme', exact: true });
      await theme.getByRole('radio', { name: 'Light' }).click();
      await expect(slideRoot(page, 1)).toHaveAttribute('data-scheme', 'light');
      await expectOwnPapers(page, REVERSE_SPEC.slides.length);

      // Dark again: the gradient comes back.
      await theme.getByRole('radio', { name: 'Dark' }).click();
      await expect(slideRoot(page, 1)).toHaveAttribute('data-scheme', 'dark');
      await expectOwnPapers(page, REVERSE_SPEC.slides.length);

      // No template: each slide on its scheme's paper token, which reveal's
      // background layer resolves off the deck root - and the gradient gone.
      const template = panel.getByRole('combobox', { name: 'Template', exact: true });
      await template.click();
      await page.getByRole('option', { name: 'No template - plain look' }).click();
      await expect(template).toHaveValue(/No template/);
      await expectOwnPapers(page, REVERSE_SPEC.slides.length);
    });
  });

  test.describe('at a fractional device pixel ratio', () => {
    // A laptop at 125 %: the deck root lands between device pixels at most
    // sizes, and the deck's white paper used to show there as a hairline
    // along the dark cover - on the top, bottom or side edge depending on the
    // size. Each size below showed it before the fix.
    test.use({ deviceScaleFactor: 1.25 });

    const SIZES: [number, number][] = [
      [1200, 717],
      [1200, 751],
      [1267, 733],
      [1301, 751],
      [1366, 700],
      [1433, 787],
    ];

    let reverseDeckId = '';

    test.beforeAll(async () => {
      const { apiCtx, token } = await createAdminApiContext();
      try {
        reverseDeckId = await importDeckOnTemplate(apiCtx, token, REVERSE_SPEC, templateName, 'e2e-stage-letterbox-dark.json');
      } finally {
        await apiCtx.dispose();
      }
    });

    test.afterAll(async () => {
      await deleteFixtureDeck(reverseDeckId);
    });

    test('no edge of the dark cover shows the deck paper, whatever the size of the stage', async ({ page }) => {
      await page.setViewportSize({ width: SIZES[0][0], height: SIZES[0][1] });
      await page.goto(`/presentations/${deckId}`);
      await expect(page.locator('.reveal.ready')).toBeVisible();
      await expect(slideRoot(page, 0)).toHaveAttribute('data-scheme', 'dark');
      await expectCleanEdges(page, SIZES);

      // Why: the paper under every slide is the slide's own - reveal paints
      // each slide's background in that slide's paper.
      await expectOwnPapers(page, FIXTURE_SPEC.slides.length);
    });

    test('no edge of a light cover on a dark deck shows the deck paper, in the light app mode', async ({ page }) => {
      // The reverse: the deck's navy used to show as a dark line along the
      // light cover. Only the light app background shows it - in the dark app
      // mode the band is that navy too.
      await page.setViewportSize({ width: SIZES[0][0], height: SIZES[0][1] });
      await page.goto(`/presentations/${reverseDeckId}`);
      await expect(page.locator('.reveal.ready')).toBeVisible();
      const root = page.locator('.deck-stage .deck-stage-surface > .deck-root');
      await expect(root).toHaveAttribute('data-deck-theme', 'dark');
      await expect(slideRoot(page, 0)).toHaveAttribute('data-scheme', 'light');
      await page.evaluate(() => {
        document.documentElement.dataset.mode = 'light';
      });
      const band = await appBackground(page);
      const paper = await resolvedColor(root, 'var(--deck-bg)');
      expect(distance(band, paper), 'the light app background tells the band from the deck paper').toBeGreaterThan(60);
      await expectCleanEdges(page, SIZES);
      await expectOwnPapers(page, REVERSE_SPEC.slides.length);
    });
  });
});
