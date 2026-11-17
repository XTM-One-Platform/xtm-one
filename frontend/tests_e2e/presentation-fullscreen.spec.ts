import { test, expect, type Locator, type Page } from '@playwright/test';
import { IDLE_HIDE_MS } from '../src/components/presentation/deck-idle';
import { buildCssVarsBlock } from '../src/lib/color-utils';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presenting a deck fills the screen edge to edge on a black stage.
//
// Driven through a template preview (a deck built client-side from a seeded
// template, no LLM call, no persisted presentation) like `presentations.spec.ts`:
// the preview renders the same `DeckStage` as the chat card and the viewer, so
// what is asserted here holds for a real deck. `Present` is exercised in both
// modes of `fullscreen.ts` - the browser's own fullscreen and the viewport
// fallback taken when the browser refuses the request.
//
// What the screenshot behind the issue showed: a 16:9 slide fitted into the
// height the toolbar's row left, pillarboxed in the deck's own (white) paper.
// So on a 16:9 screen the deck root - the box reveal fills exactly - must BE
// the viewport, the toolbar an overlay that is out of the way once idle; on a
// taller screen the root keeps its ratio, centred, and what shows around it is
// the surface, black, whatever mode the app is in.
// ---------------------------------------------------------------------------

/** How long past `IDLE_HIDE_MS` the stage may take to report itself idle. */
const IDLE_SLACK_MS = 1500;

async function findBuiltinPresentationTemplateId(): Promise<string> {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    const resp = await apiCtx.get('/api/v1/template-containers?kind=presentation', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok()) return '';
    const containers: { id: string; is_builtin: boolean; aspect_ratio?: string | null }[] = await resp.json();
    // A 16:9 template: the geometry below is written for that ratio.
    return containers.find((c) => c.is_builtin && (c.aspect_ratio ?? '16:9') === '16:9')?.id ?? '';
  } finally {
    await apiCtx.dispose();
  }
}

type Box = { x: number; y: number; width: number; height: number };

async function boxOf(locator: Locator): Promise<Box> {
  const box = await locator.boundingBox();
  if (!box) throw new Error('element has no box');
  return { x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height) };
}

async function openPreview(page: Page, templateId: string): Promise<{ dialog: Locator; stage: Locator; toolbar: Locator }> {
  // The idle timer is brought forward on the page clock (`waitOutStillness`).
  await page.clock.install();
  await page.goto(`/templates/${templateId}`);
  await page.getByRole('button', { name: 'Preview' }).click();
  const dialog = page.getByRole('dialog', { name: 'Template preview' });
  await expect(dialog).toBeVisible();
  const toolbar = dialog.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(dialog.locator('.reveal.ready')).toBeVisible();
  const stage = dialog.locator('.deck-stage');
  return { dialog, stage, toolbar };
}

/** Click `Present` and wait for the stage to report which mode it took. */
async function present(page: Page, toolbar: Locator, stage: Locator): Promise<'native' | 'viewport'> {
  await toolbar.getByTestId('presentation.present').click();
  await expect(stage).toHaveAttribute('data-deck-fullscreen', /^(native|viewport)$/);
  const mode = (await stage.getAttribute('data-deck-fullscreen')) as 'native' | 'viewport';
  // reveal re-lays the deck out from a ResizeObserver on the root: one frame.
  await page.waitForTimeout(150);
  return mode;
}

/**
 * Click `Present` on a browser that grants the Fullscreen API (headless
 * Chromium does): the NATIVE path is the one under test, and a stage that
 * quietly fell back to the viewport mode here would leave the browser's own
 * fullscreen - the case of the report - untested.
 */
async function presentNatively(page: Page, toolbar: Locator, stage: Locator): Promise<void> {
  const mode = await present(page, toolbar, stage);
  expect(mode, 'the browser grants fullscreen here: a viewport fallback means the native path regressed').toBe('native');
}

/**
 * The stillness the stage waits for before it goes idle: `IDLE_HIDE_MS` on the
 * page clock (installed by `openPreview`), jumped rather than waited out. The
 * stage is then given `IDLE_SLACK_MS` of real time to report itself idle.
 */
async function waitOutStillness(stage: Locator): Promise<void> {
  await stage.page().clock.fastForward(IDLE_HIDE_MS);
  await expect(stage).toHaveAttribute('data-deck-idle', 'true', { timeout: IDLE_SLACK_MS });
}

/** Wait for the stage to report itself idle within the time it promises. */
async function expectIdle(stage: Locator, toolbar: Locator): Promise<void> {
  await waitOutStillness(stage);
  // The bar leaves through a short opacity transition once idle.
  await expect.poll(() => toolbar.evaluate((el) => getComputedStyle(el).opacity)).toBe('0');
}

/** What is painted under a point: the element there and its own background. */
async function paintedAt(page: Page, x: number, y: number): Promise<{ className: string; background: string }> {
  return page.evaluate(
    ([px, py]) => {
      const el = document.elementFromPoint(px, py) as HTMLElement | null;
      if (!el) return { className: '(none)', background: '' };
      return { className: el.className, background: getComputedStyle(el).backgroundColor };
    },
    [x, y],
  );
}

/** The colour of one screen pixel, read off a fresh screenshot in the page. */
async function pixelAt(page: Page, x: number, y: number): Promise<[number, number, number]> {
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

async function expectEdgeToEdge(stage: Locator, toolbar: Locator, width: number, height: number) {
  await expect.poll(() => boxOf(stage)).toEqual({ x: 0, y: 0, width, height });
  // The toolbar is an overlay: out of flow, along the top edge, over the deck.
  const toolbarStyle = await toolbar.evaluate((el) => {
    const style = getComputedStyle(el);
    return { position: style.position, top: style.top, background: style.backgroundColor };
  });
  expect(toolbarStyle.position).toBe('absolute');
  expect(toolbarStyle.top).toBe('0px');
  expect(toolbarStyle.background).toBe('rgba(0, 0, 0, 0.55)');
  // The surface is the whole screen, black, and it is what the letterbox shows.
  const surface = stage.locator('.deck-stage-surface');
  await expect.poll(() => boxOf(surface)).toEqual({ x: 0, y: 0, width, height });
  expect(await surface.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe('rgb(0, 0, 0)');
  // The deck root - the box reveal fills exactly - is the largest 16:9 box in
  // the screen, centred: the viewport itself on a 16:9 screen.
  const rootHeight = Math.min(height, Math.round((width * 9) / 16));
  const rootWidth = Math.min(width, Math.round((height * 16) / 9));
  await expect.poll(() => boxOf(stage.locator('.deck-root'))).toEqual({
    x: Math.round((width - rootWidth) / 2),
    y: Math.round((height - rootHeight) / 2),
    width: rootWidth,
    height: rootHeight,
  });
  // reveal scaled its 1920x1080 canvas onto that box: the slide spans it.
  const slides = stage.locator('.reveal .slides');
  await expect.poll(async () => {
    const box = await boxOf(slides);
    return { width: box.width, height: box.height };
  }).toEqual({ width: rootWidth, height: rootHeight });
}

test.describe('Presentations - presenting fills the screen', () => {
  let templateId = '';

  test.beforeAll(async () => {
    templateId = await findBuiltinPresentationTemplateId();
  });

  test.beforeEach(() => {
    test.skip(!templateId, 'No built-in 16:9 presentation template seeded on this platform');
  });

  test('a 16:9 deck on a 16:9 screen is the whole screen, the bar out of the way once idle', async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    const { stage, toolbar } = await openPreview(page, templateId);
    await presentNatively(page, toolbar, stage);
    await expectEdgeToEdge(stage, toolbar, 1920, 1080);
    // The dark token set is forced on the stage: the bar reads light on black
    // whatever the app mode.
    await expect(stage).toHaveAttribute('data-mode', 'dark');

    // The bar leaves after a moment of stillness and takes the pointer with it;
    // the slide has not moved: it was never laid out under the bar.
    await expectIdle(stage, toolbar);
    // `cursor` is inherited, not cascaded: the hide reaches the surface AND
    // every slide element (a link, a chart legend button set their own), while
    // the bar keeps its pointer for whenever it is up.
    const surface = stage.locator('.deck-stage-surface');
    const currentSlide = stage.locator('.reveal .slides > section.present').first();
    expect(await surface.evaluate((el) => getComputedStyle(el).cursor)).toBe('none');
    expect(await currentSlide.evaluate((el) => getComputedStyle(el).cursor)).toBe('none');
    expect(await toolbar.evaluate((el) => getComputedStyle(el).cursor)).not.toBe('none');
    await expect.poll(() => boxOf(stage.locator('.deck-root'))).toEqual({ x: 0, y: 0, width: 1920, height: 1080 });
    // The top edge of the screen is the slide, not a bar and not a band.
    const underTheBar = await paintedAt(page, 960, 20);
    expect(underTheBar.className).not.toContain('deck-toolbar');
    expect(underTheBar.className).not.toContain('deck-stage-surface');

    // Pointer movement brings the bar back; keys do not.
    await page.mouse.move(600, 600);
    await page.mouse.move(640, 620);
    await expect(stage).not.toHaveAttribute('data-deck-idle', 'true');
    await expect.poll(() => toolbar.evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
    expect(await surface.evaluate((el) => getComputedStyle(el).cursor)).not.toBe('none');
    await expectIdle(stage, toolbar);
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(300);
    await expect(stage).toHaveAttribute('data-deck-idle', 'true');
    expect(await toolbar.evaluate((el) => getComputedStyle(el).opacity)).toBe('0');

    // A menu opened from the bar and read with the pointer at rest over the
    // slide keeps the bar up when the timer fires: the menu renders inline in
    // the bar, and a hidden bar would take the open menu away with it.
    await page.mouse.move(700, 700);
    await page.mouse.move(720, 710);
    await expect.poll(() => toolbar.evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
    await toolbar.getByRole('button', { name: 'More actions' }).click();
    const menu = toolbar.getByRole('menu', { name: 'More actions' });
    await expect(menu).toBeVisible();
    await page.mouse.move(600, 600);
    await waitOutStillness(stage);
    await page.waitForTimeout(300);
    expect(await toolbar.evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
    await expect(menu).toBeVisible();
    // Escape closes the menu (and only the menu); the bar is then free to leave.
    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
    await expect(stage).toHaveAttribute('data-deck-fullscreen', 'native');

    // Esc leaves, and the stage is a card in the dialog again.
    await page.keyboard.press('Escape');
    await expect(stage).not.toHaveAttribute('data-deck-fullscreen', /.+/);
    await expect(stage).not.toHaveAttribute('data-mode', /.+/);
    await expect(stage).not.toHaveAttribute('data-deck-idle', /.+/);
  });

  test('a 16:9 deck on a taller screen keeps its ratio inside black bands, in the light app mode too', async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1200 });
    const { stage, toolbar } = await openPreview(page, templateId);
    // The light mode is the case of the report: the deck's paper is white and
    // the bands used to be painted in it.
    await page.evaluate(() => {
      document.documentElement.dataset.mode = 'light';
    });
    await presentNatively(page, toolbar, stage);
    await expectEdgeToEdge(stage, toolbar, 1920, 1200);
    // 1920x1080, centred: 60px bands above and below, both the black surface.
    await expect.poll(() => boxOf(stage.locator('.deck-root'))).toEqual({ x: 0, y: 60, width: 1920, height: 1080 });
    for (const [x, y] of [
      [960, 1190],
      [20, 1190],
      [1900, 1190],
    ] as const) {
      const painted = await paintedAt(page, x, y);
      expect(painted.className, `at ${x},${y}`).toContain('deck-stage-surface');
      expect(painted.background, `at ${x},${y}`).toBe('rgb(0, 0, 0)');
      expect(await pixelAt(page, x, y), `pixel at ${x},${y}`).toEqual([0, 0, 0]);
    }
    // Once idle the top band is bare black as well: the bar is gone, and it
    // sat over the letterbox, never over the slide, while it was up.
    await expectIdle(stage, toolbar);
    for (const [x, y] of [
      [960, 10],
      [20, 30],
    ] as const) {
      expect((await paintedAt(page, x, y)).className, `at ${x},${y}`).toContain('deck-stage-surface');
      expect(await pixelAt(page, x, y), `pixel at ${x},${y}`).toEqual([0, 0, 0]);
    }
    // The bar itself reads from the dark tokens while the app is light: its
    // foreground token is the dark set's white, not the light set's near-black.
    await expect(stage).toHaveAttribute('data-mode', 'dark');
    await page.mouse.move(400, 400);
    await page.mouse.move(420, 410);
    await expect.poll(() => toolbar.evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
    // A token as the browser resolves it inside `scope`, as `rgb(...)`.
    const resolve = (scope: Locator, value: string) =>
      scope.evaluate((el, cssValue) => {
        const probe = document.createElement('span');
        probe.style.color = cssValue;
        el.appendChild(probe);
        const color = getComputedStyle(probe).color;
        probe.remove();
        return color;
      }, value);
    const body = page.locator('body');
    const readTokens = async () => ({
      foreground: await resolve(toolbar, 'var(--text-default-primary)'),
      accent: await resolve(toolbar, 'var(--color-accent)'),
    });
    const tokens = await readTokens();
    // The design system's dark set (`--gray-100` text), not the light set the
    // app around it reads (`--gray-900`).
    expect(tokens.foreground).toBe(await resolve(body, 'var(--gray-100)'));
    expect(await resolve(body, 'var(--text-default-primary)')).toBe(await resolve(body, 'var(--gray-900)'));
    // The forced-dark subtree flips the MODE, never the brand: under the XTM
    // One theme the bar keeps the purple accent (its dark-background value),
    // while the app around it reads the light-background purple.
    await page.evaluate(() => {
      document.documentElement.dataset.theme = 'xtm-one';
    });
    const darkPurple = await resolve(body, 'var(--purple-300)');
    await expect.poll(async () => (await readTokens()).accent).toBe(darkPurple);
    expect(await resolve(body, 'var(--color-accent)')).toBe(await resolve(body, 'var(--purple-500)'));
    expect(darkPurple).not.toBe(await resolve(body, 'var(--purple-500)'));
    // An administrator's accent (the branding block `usePlatformConfig`
    // renders, unlayered, after the bundled sheets) follows the bar onto the
    // stage as well, over the product theme's own subtree declaration.
    const branding = buildCssVarsBlock('#ff5500', null);
    expect(branding).not.toBeNull();
    await page.addStyleTag({ content: branding! });
    await expect.poll(async () => (await readTokens()).accent).toBe('rgb(255, 85, 0)');
    expect(await resolve(body, 'var(--color-accent)')).toBe('rgb(255, 85, 0)');
    await page.keyboard.press('Escape');
    await expect(stage).not.toHaveAttribute('data-deck-fullscreen', /.+/);
  });

  test('the viewport fallback presents the same picture when the browser refuses fullscreen', async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1200 });
    // The API is available and REFUSES the request (a kiosk policy, an
    // Electron webview): `Present` must present anyway, in the viewport mode.
    await page.addInitScript(() => {
      Element.prototype.requestFullscreen = () => Promise.reject(new DOMException('Permissions check failed', 'TypeError'));
    });
    const { stage, toolbar } = await openPreview(page, templateId);
    expect(await present(page, toolbar, stage)).toBe('viewport');
    await expect(stage).toHaveAttribute('data-deck-viewport', '');
    await expectEdgeToEdge(stage, toolbar, 1920, 1200);
    await expect.poll(() => boxOf(stage.locator('.deck-root'))).toEqual({ x: 0, y: 60, width: 1920, height: 1080 });
    const painted = await paintedAt(page, 960, 1190);
    expect(painted.className).toContain('deck-stage-surface');
    expect(painted.background).toBe('rgb(0, 0, 0)');
    // Idle hides the bar and the pointer over the surface in this mode too.
    await expectIdle(stage, toolbar);
    expect(await stage.locator('.deck-stage-surface').evaluate((el) => getComputedStyle(el).cursor)).toBe('none');
    // Resizing the window re-fits the deck (a projector picked up mid-talk).
    await page.setViewportSize({ width: 1600, height: 900 });
    await expectEdgeToEdge(stage, toolbar, 1600, 900);
    await page.keyboard.press('Escape');
    await expect(stage).not.toHaveAttribute('data-deck-viewport', /.*/);
    await expect(stage).not.toHaveAttribute('data-deck-fullscreen', /.+/);
  });
});
