import { test, expect, type Browser, type BrowserContext } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - the public link.
//
// A deck's managers turn on a link anyone can open without an account. The
// deck here is imported through the API from a bare spec WITH speaker notes
// (no LLM call), owned by the admin; its link is turned on through the API,
// then opened from a browser context that holds no session at all, and from
// one that holds a DEAD session (which must not be sent to sign-in). The
// admin then turns the link off from the Share dialog and the same address
// stops opening the deck. A second deck offers its PDF: a visitor
// downloads it - rendered for real by the platform's headless browser - and
// the button goes once the owner turns the download off.
// ---------------------------------------------------------------------------

const NOTE = 'Say the pipeline number slowly: it is the whole story.';

const FIXTURE_SPEC = {
  title: 'E2E public link deck',
  theme: 'dark',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'title', title: 'E2E public link', subtitle: 'Anyone with the link', notes: NOTE },
    {
      layout: 'content',
      title: 'Slide two',
      blocks: [{ type: 'text', content: 'The second slide of the public deck.' }],
      notes: 'Second note, never shown publicly.',
    },
    { layout: 'closing', title: 'Thank you' },
  ],
};

/** A browser context with no session: what a customer who received the link has. */
async function visitorContext(browser: Browser, baseURL: string, staleToken?: string): Promise<BrowserContext> {
  return browser.newContext({
    baseURL,
    storageState: {
      cookies: [],
      origins: staleToken ? [{ origin: new URL(baseURL).origin, localStorage: [{ name: 'copilot_token', value: staleToken }] }] : [],
    },
  });
}

/** Import the fixture deck as the admin and turn its public link on; `allowPdf` also offers its PDF. */
async function deckWithPublicLink(name: string, allowPdf = false): Promise<{ deckId: string; publicPath: string }> {
  const { apiCtx, token } = await createAdminApiContext();
  const headers = { Authorization: `Bearer ${token}` };
  try {
    const imported = await apiCtx.post('/api/v1/presentations/import', {
      headers,
      multipart: {
        file: {
          name: `${name}.json`,
          mimeType: 'application/json',
          buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8'),
        },
      },
    });
    if (!imported.ok()) throw new Error(`Importing the fixture deck failed: ${imported.status()} ${await imported.text()}`);
    const deckId = ((await imported.json()) as { id: string }).id;
    const link = await apiCtx.post(`/api/v1/presentations/${deckId}/public-link`, { headers, data: {} });
    if (!link.ok()) throw new Error(`Turning the public link on failed: ${link.status()} ${await link.text()}`);
    const publicPath = ((await link.json()) as { path: string }).path;
    if (allowPdf) {
      const offered = await apiCtx.patch(`/api/v1/presentations/${deckId}/public-link`, {
        headers,
        data: { allow_pdf_download: true },
      });
      if (!offered.ok()) throw new Error(`Allowing the PDF download failed: ${offered.status()} ${await offered.text()}`);
    }
    return { deckId, publicPath };
  } finally {
    await apiCtx.dispose();
  }
}

async function deleteDeck(deckId: string): Promise<void> {
  if (!deckId) return;
  const { apiCtx, token } = await createAdminApiContext();
  try {
    await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
  } finally {
    await apiCtx.dispose();
  }
}

test.describe('Presentations - public link', () => {
  let deckId = '';
  let publicPath = '';

  test.beforeAll(async () => {
    ({ deckId, publicPath } = await deckWithPublicLink('e2e-public-link'));
  });

  test.afterAll(async () => {
    await deleteDeck(deckId);
  });

  test('should open the deck without an account, slides only, and stop once the link is off', async ({
    browser,
    page,
  }, testInfo) => {
    const baseURL = String(testInfo.project.use.baseURL ?? 'http://localhost:3100');
    expect(publicPath).toMatch(/^\/public\/presentations\/[A-Za-z0-9_-]{43}$/);

    // A visitor with no session opens the deck where the link points.
    const visitor = await visitorContext(browser, baseURL);
    const deck = await visitor.newPage();
    await deck.goto(publicPath);
    await expect(deck.getByTestId('presentation.public')).toBeVisible();
    await expect(deck.locator('.reveal.ready')).toBeVisible();
    expect(new URL(deck.url()).pathname).toBe(publicPath);
    await expect(deck.getByRole('heading', { name: FIXTURE_SPEC.title, exact: true })).toBeVisible();
    await expect(deck.getByTestId('presentation.public.badge')).toBeVisible();
    // A new link offers no download.
    await expect(deck.getByTestId('presentation.public.downloadPdf')).toHaveCount(0);
    // Slides only: the speaker notes never reached the page.
    expect(await deck.content()).not.toContain(NOTE);
    // It is navigated like the viewer, the fragment following the slide.
    const toolbar = deck.getByRole('toolbar', { name: 'Presentation controls' });
    await expect(toolbar.getByRole('button', { name: /^Slide 1 of 3\./ })).toBeVisible();
    await deck.keyboard.press('ArrowRight');
    await expect(toolbar.getByRole('button', { name: /^Slide 2 of 3\./ })).toBeVisible();
    await expect.poll(() => new URL(deck.url()).hash).toBe('#/1');

    // A browser holding a dead session stays on the deck, never sent to sign in.
    const stale = await visitorContext(browser, baseURL, 'expired.session.token');
    const staleDeck = await stale.newPage();
    await staleDeck.goto(publicPath);
    await expect(staleDeck.getByTestId('presentation.public')).toBeVisible();
    await expect(staleDeck.locator('.reveal.ready')).toBeVisible();
    expect(new URL(staleDeck.url()).pathname).toBe(publicPath);
    await stale.close();

    // The owner sees the link in the Share dialog, and turns it off there.
    await page.goto(`/presentations/${deckId}`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    await page.getByRole('button', { name: 'Share', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Share presentation' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByTestId('presentation.share.publicLink.url')).toHaveText(new RegExp(`${publicPath}$`));
    await dialog.getByTestId('presentation.share.publicLink.switch').click();
    await page.getByRole('button', { name: 'Turn off', exact: true }).click();
    await expect(dialog.getByTestId('presentation.share.publicLink.url')).toHaveCount(0);

    // The same address no longer opens it, for anyone.
    await deck.reload();
    await expect(deck.getByText('This link no longer opens a presentation.')).toBeVisible();
    await expect(deck.getByTestId('presentation.public')).toHaveCount(0);
    await visitor.close();
  });
});

test.describe('Presentations - public link PDF download', () => {
  let deckId = '';
  let publicPath = '';

  test.beforeAll(async () => {
    ({ deckId, publicPath } = await deckWithPublicLink('e2e-public-link-pdf', true));
  });

  test.afterAll(async () => {
    await deleteDeck(deckId);
  });

  test('should let a visitor download the PDF while the owner allows it', async ({ browser, page }, testInfo) => {
    // A render launches the platform's headless browser: give it room.
    test.setTimeout(120_000);
    const baseURL = String(testInfo.project.use.baseURL ?? 'http://localhost:3100');

    const visitor = await visitorContext(browser, baseURL);
    const deck = await visitor.newPage();
    await deck.goto(publicPath);
    await expect(deck.locator('.reveal.ready')).toBeVisible();
    const button = deck.getByTestId('presentation.public.downloadPdf');
    await expect(button).toBeVisible();
    const [download] = await Promise.all([deck.waitForEvent('download', { timeout: 90_000 }), button.click()]);
    expect(download.suggestedFilename()).toBe('e2e-public-link-deck.pdf');
    const path = await download.path();
    const { readFileSync } = await import('node:fs');
    expect(readFileSync(path).subarray(0, 5).toString('latin1')).toBe('%PDF-');

    // The owner turns the download off in the Share dialog: the button goes.
    await page.goto(`/presentations/${deckId}`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    await page.getByRole('button', { name: 'Share', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Share presentation' });
    const pdfSwitch = dialog.getByTestId('presentation.share.publicLink.pdf');
    await expect(pdfSwitch).toHaveAttribute('data-state', 'checked');
    await pdfSwitch.click();
    await expect(pdfSwitch).toHaveAttribute('data-state', 'unchecked');

    await deck.reload();
    await expect(deck.locator('.reveal.ready')).toBeVisible();
    await expect(deck.getByTestId('presentation.public.downloadPdf')).toHaveCount(0);
    await visitor.close();
  });
});
