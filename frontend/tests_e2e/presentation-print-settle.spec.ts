import { test, expect, type Page } from '@playwright/test';
import { createAdminApiContext, createTwoSchemeTemplate, deleteTemplate, importDeckOnTemplate } from './helpers';
import { logoItems, uploadLogos, type Logo } from './presentation-logo-fixtures';

// ---------------------------------------------------------------------------
// Presentations - the browser print always starts.
//
// `Print to PDF (browser)` opens the deck at `?print-pdf`, waits for it to
// settle - `deckSettled()`: no `[data-deck-pending]` left, fonts and images in -
// and opens the print dialog. A figure that could never settle held that wait
// for good: a lazy chunk that failed to load reached the route's error boundary,
// a chunk or a picture the network held kept its placeholder pending, and the
// dialog never opened; an architecture logo whose file was still being fetched
// published no marker at all, so the print did not wait for it. Every figure
// now holds the signal while it waits and gives up on its own after a bound of
// visible time (`FIGURE_WAIT_MS`), a failed drawing shows the block's error
// state, and the print itself has a last resort (`PRINT_SETTLE_MS`); when what
// it printed was not finished, it says so.
//
// `window.print` is replaced before the page loads, so a test reads how many
// times the dialog would have opened. The deck sits on a private template
// holding two logos; it is imported through the API (no LLM call), owned by
// the admin and deleted afterwards. The file runs SERIALLY so the upload and
// the import happen once.
// ---------------------------------------------------------------------------

/** `FIGURE_WAIT_MS` in `components/presentation/ready.ts`. */
const FIGURE_WAIT_MS = 15_000;
/** `PRINT_SETTLE_MS` in the same module: the print's own last resort. */
const PRINT_SETTLE_MS = 2 * FIGURE_WAIT_MS;

/**
 * Runs `ms` of visible time on the page clock (installed before the page
 * opened) instead of waiting it out. `runFor` fires every frame on the way:
 * visible time counts a frame for at most 100 ms, so a `fastForward` jump
 * would count as one frame.
 */
async function runVisibleTime(page: Page, ms: number): Promise<void> {
  await page.clock.runFor(ms);
}

/**
 * The print's notice, among the visible toasts: Radix mirrors every toast into
 * an off-screen `aria-live` region, so the list is read from the viewport, the
 * only `<ol tabindex="-1">` on the page.
 */
function notice(page: Page) {
  return page.locator('ol[tabindex="-1"] > li').filter({ hasText: 'Part of the deck was not finished' });
}

/** Two icon nodes and a logo node: the logo is its own file, so a test can hold it alone. */
function architecture(logo: Logo) {
  return {
    type: 'architecture',
    nodes: [
      { id: 'cti', label: 'OpenCTI', icon: 'brain', row: 1, col: 1 },
      { id: 'aev', label: 'OpenAEV', icon: 'crosshair', row: 1, col: 3 },
      { id: 'partner', label: 'Partner', logo_file_id: logo.file_id, row: 1, col: 5 },
    ],
    edges: [
      { source: 'cti', target: 'aev' },
      { source: 'aev', target: 'partner' },
    ],
  };
}

/** The on-screen fetch of a platform file (`useDeckAsset`), whatever revision query it carries. */
const isFileDownload = (fileId: string) => (url: URL): boolean =>
  url.pathname.startsWith(`/api/v1/chat/files/${fileId}/download`);

function buildSlides(logo: Logo, architectureLogo: Logo) {
  const content = (id: string, title: string, block: object) => ({ id, layout: 'content', title, blocks: [block] });
  return [
    content('p-chart', 'A chart', {
      type: 'chart',
      kind: 'bar',
      series: [{ name: 'Trials', values: [31, 44, 52, 63] }],
      categories: ['Q4 25', 'Q1 26', 'Q2 26', 'Q3 26'],
    }),
    content('p-diagram', 'A diagram', { type: 'diagram', source: 'flowchart LR\n  A[Collect] --> B[Analyse]\n  B --> C[Act]' }),
    content('p-architecture', 'An architecture', architecture(architectureLogo)),
    content('p-callout', 'A callout', {
      type: 'callout',
      icon: 'shield-check',
      title: 'Checked',
      children: [{ type: 'list', items: ['First point', 'Second point'] }],
    }),
    content('p-logo', 'A logo', { type: 'logo_grid', columns: 2, items: logoItems([logo]) }),
  ];
}

/** Replaces `window.print` before the page loads: the dialog is counted, never opened. */
async function countPrints(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const calls = { count: 0 };
    (window as unknown as { __printCalls: { count: number } }).__printCalls = calls;
    window.print = () => {
      calls.count += 1;
    };
  });
}

function printCalls(page: Page): Promise<number> {
  return page.evaluate(() => (window as unknown as { __printCalls?: { count: number } }).__printCalls?.count ?? 0);
}

/** Two frames: what the page queued when the dialog opened (a notice) is on screen by then. */
function nextFrames(page: Page): Promise<void> {
  return page.evaluate(
    () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
  );
}

/** Answers the requests `matches` accepts only once `release()` is called - by aborting them. */
async function holdRequests(page: Page, matches: (url: URL) => boolean): Promise<() => void> {
  let release: () => void = () => undefined;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(matches, async (route) => {
    await held;
    await route.abort('failed').catch(() => undefined);
  });
  return release;
}

/** The lazy module of the `diagram` block (`blocks/Diagram.tsx`), in a build (`Diagram-<hash>.js`) or on the dev server. */
const isDiagramChunk = (url: URL): boolean => /\/Diagram[-.]/.test(url.pathname);

test.describe('Presentations - the browser print always starts', () => {
  test.describe.configure({ mode: 'serial' });

  let deckId = '';
  let templateId = '';
  let logo: Logo;
  let architectureLogo: Logo;

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const templateName = `E2E print settle ${Date.now()}`;
      templateId = await createTwoSchemeTemplate(apiCtx, token, templateName);
      [logo, architectureLogo] = await uploadLogos(apiCtx, token, templateId, 2);
      const shell = { title: 'E2E print settle', theme: 'dark', aspect_ratio: '16:9', transition: 'none', slides: [] };
      deckId = await importDeckOnTemplate(
        apiCtx,
        token,
        { ...shell, slides: [{ id: 'p-title', layout: 'title', title: 'Print settle' }] },
        templateName,
        'e2e-print-settle.json',
      );
      const deck = await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
      const { revision } = (await deck.json()) as { revision: number };
      const patched = await apiCtx.patch(`/api/v1/presentations/${deckId}`, {
        headers: { Authorization: `Bearer ${token}` },
        data: { slides: buildSlides(logo, architectureLogo), expected_revision: revision },
      });
      if (!patched.ok()) throw new Error(`Writing the fixture slides failed: ${patched.status()} ${await patched.text()}`);
    } finally {
      await apiCtx.dispose();
    }
  });

  // The icon catalog test proxies every `.js` request through `route.fetch()`
  // and reads the body; the hold helpers park requests until `release()`.
  // A request still in flight when a test ends has its response disposed with
  // the page, and the handler threw "Response has been disposed" against the
  // test. Drop the handlers first and let any still
  // running settle silently.
  test.afterEach(async ({ page }) => {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
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

  test('a deck whose figures all settle prints once, and says nothing', async ({ page }) => {
    await countPrints(page);
    await page.goto(`/presentations/${deckId}?print-pdf`);
    await expect.poll(() => printCalls(page), { timeout: 30_000 }).toBe(1);
    await expect(page.locator('.deck-diagram .deck-diagram-svg svg')).toBeVisible();
    await expect(page.locator('.deck-chart-canvas .recharts-surface')).toHaveCount(1);
    await expect(page.locator('.deck-arch-icon')).toHaveCount(2);
    await expect(page.locator('image.deck-arch-logo')).toHaveCount(1);
    await expect(page.locator('img.deck-logo')).toHaveCount(1);
    await expect(page.locator('[data-deck-pending], [data-deck-unfinished]')).toHaveCount(0);
    await nextFrames(page);
    await expect(notice(page)).toHaveCount(0);
  });

  test('a diagram whose drawing cannot load shows its error state, and the deck prints, saying so', async ({ page }) => {
    await countPrints(page);
    await page.route(isDiagramChunk, (route) => route.abort('failed'));
    await page.goto(`/presentations/${deckId}?print-pdf`);
    await expect.poll(() => printCalls(page), { timeout: 30_000 }).toBe(1);
    const diagram = page.locator('.deck-diagram');
    await expect(diagram).toHaveAttribute('data-state', 'error');
    await expect(diagram).toHaveAttribute('data-deck-unfinished', 'true');
    await expect(diagram.locator('.deck-diagram-error')).toContainText('Diagram could not be rendered');
    await expect(page.locator('[data-deck-pending]')).toHaveCount(0);
    // The rest of the deck printed as it always does.
    await expect(page.locator('.deck-chart-canvas .recharts-surface')).toHaveCount(1);
    await expect(notice(page)).toBeVisible();
  });

  test('a diagram whose drawing never arrives gives up after its bound, and the deck prints, saying so', async ({ page }) => {
    test.setTimeout(FIGURE_WAIT_MS + 60_000);
    await countPrints(page);
    const release = await holdRequests(page, isDiagramChunk);
    try {
      await page.clock.install();
      await page.goto(`/presentations/${deckId}?print-pdf`);
      const diagram = page.locator('.deck-diagram');
      await expect(diagram).toHaveAttribute('data-deck-pending', 'true');
      expect(await printCalls(page)).toBe(0);
      await runVisibleTime(page, FIGURE_WAIT_MS);
      // Its placeholder stands, no longer holding the print.
      await expect(diagram).toHaveAttribute('data-deck-unfinished', 'true', { timeout: FIGURE_WAIT_MS + 20_000 });
      await expect(diagram).not.toHaveAttribute('data-deck-pending', /.*/);
      await expect(diagram.locator('.deck-diagram-pending')).toBeVisible();
      await expect.poll(() => printCalls(page), { timeout: 30_000 }).toBe(1);
      await expect(notice(page)).toBeVisible();
    } finally {
      release();
    }
  });

  test('an icon whose catalog cannot load keeps its placeholder, and the deck prints, saying so', async ({ page }) => {
    await countPrints(page);
    // The icon catalog is ONE lazy chunk (`deck-icons.ts`), named `index-<hash>.js`
    // in a build like the entry itself: it is told apart by what it exports.
    await page.route(
      (url) => url.pathname.endsWith('.js'),
      async (route) => {
        if (/lucide-react.*icons/.test(new URL(route.request().url()).pathname)) return route.abort('failed');
        const response = await route.fetch();
        const body = await response.text();
        if (body.includes('AArrowDown') && body.includes('ZoomOut')) return route.abort('failed');
        return route.fulfill({ response, body });
      },
    );
    await page.goto(`/presentations/${deckId}?print-pdf`);
    await expect.poll(() => printCalls(page), { timeout: 30_000 }).toBe(1);
    // The architecture's icons and the callout's keep a placeholder box, not pending.
    const architectureIcons = page.locator('.deck-arch-icon-pending');
    await expect(architectureIcons).toHaveCount(2);
    await expect(page.locator('.deck-arch-icon-pending[data-deck-unfinished="true"]')).toHaveCount(2);
    await expect(page.locator('.deck-icon-pending[data-deck-unfinished="true"]').first()).toBeAttached();
    await expect(page.locator('[data-deck-pending]')).toHaveCount(0);
    await expect(notice(page)).toBeVisible();
  });

  test('an architecture logo whose file never arrives holds the print until its bound, and the deck prints, saying so', async ({
    page,
  }) => {
    test.setTimeout(FIGURE_WAIT_MS + 60_000);
    await countPrints(page);
    // The logo's file is fetched with the session (`useDeckAsset`): while the
    // fetch runs the node draws no `<image>`, and its placeholder holds the
    // ready signal until the file arrives or `FIGURE_WAIT_MS` has passed.
    const release = await holdRequests(page, isFileDownload(architectureLogo.file_id));
    try {
      await page.clock.install();
      await page.goto(`/presentations/${deckId}?print-pdf`);
      const placeholder = page.locator('.deck-architecture .deck-arch-logo-pending');
      await expect(placeholder).toHaveAttribute('data-deck-pending', 'true');
      await expect(page.locator('.deck-chart-canvas .recharts-surface')).toHaveCount(1);
      expect(await printCalls(page)).toBe(0);
      await runVisibleTime(page, FIGURE_WAIT_MS);
      await expect(placeholder).toHaveAttribute('data-deck-unfinished', 'true', { timeout: FIGURE_WAIT_MS + 20_000 });
      await expect(placeholder).not.toHaveAttribute('data-deck-pending', /.*/);
      await expect.poll(() => printCalls(page), { timeout: 30_000 }).toBe(1);
      await expect(notice(page)).toBeVisible();
      await expect(page.locator('image.deck-arch-logo')).toHaveCount(0);
    } finally {
      release();
    }
  });

  test('a picture that never arrives holds the print only until its last resort, and the deck prints, saying so', async ({
    page,
  }) => {
    test.setTimeout(PRINT_SETTLE_MS + 60_000);
    await countPrints(page);
    // A logo's bytes are fetched with the session (`useDeckAsset`), and the
    // grid holds the ready signal until they arrive: nothing bounds that wait
    // but the print's last resort.
    const release = await holdRequests(page, isFileDownload(logo.file_id));
    try {
      await page.clock.install();
      await page.goto(`/presentations/${deckId}?print-pdf`);
      const mark = page.locator('.deck-logo-grid .deck-logo-mark');
      await expect(mark).toHaveAttribute('data-deck-pending', 'true');
      await expect(page.locator('.deck-chart-canvas .recharts-surface')).toHaveCount(1);
      expect(await printCalls(page)).toBe(0);
      await runVisibleTime(page, PRINT_SETTLE_MS);
      await expect.poll(() => printCalls(page), { timeout: PRINT_SETTLE_MS + 30_000 }).toBe(1);
      await expect(notice(page)).toBeVisible();
      // What printed is what was on screen: the logo still on its way.
      await expect(mark).toHaveAttribute('data-deck-pending', 'true');
    } finally {
      release();
    }
  });
});
