import { test, expect, type Page } from '@playwright/test';
import {
  createAdminApiContext,
  createTwoSchemeTemplate,
  deleteTemplate,
  expectEditorSaved,
  importDeckOnTemplate,
} from './helpers';

// ---------------------------------------------------------------------------
// Presentations - a slide's own theme and the hero backdrops.
//
// The Selection tab of the editor panel carries every slide-level look: the
// Theme picker (Inherit / Dark / Light), the Hero thumbnails, the Hero colour
// chips, the accent-line switch. This spec drives them with real clicks on a
// LIGHT deck and reads what the stage renders - the slide root's
// `data-scheme` / `data-scheme-override`, the `.deck-hero-backdrop` under the
// content, its `data-hero-tone`, the accent span - then saves ONE revision,
// checks the stored spec through the API, reloads and reads the same
// attributes off the fresh render. A source grep proves the wiring exists;
// only the browser proves the slide actually changes scheme and backdrop.
//
// The deck is imported through the API (no LLM call) on a template of its own
// that dresses both schemes: the install's default may dress one only (the XTM
// One Brand Deck is dark only), and a light deck under a dark-only template is
// snapped to dark by its first save. Deck and template are owned by the admin
// and deleted afterwards.
// ---------------------------------------------------------------------------

const FIXTURE_SPEC = {
  title: 'E2E slide theme deck',
  theme: 'light',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'title', title: 'Uncover Threats.\nTake Action.', subtitle: 'A cover of its own' },
    {
      layout: 'content',
      title: 'The body stays light',
      blocks: [{ type: 'text', content: 'A paragraph that follows the deck theme.' }],
    },
    { layout: 'closing', title: 'Thank you' },
  ],
};

/** The reveal section of a slide - the SLIDE ROOT that carries the scheme. */
function slideRoot(page: Page, index: number) {
  return page.locator('.reveal .slides > section').nth(index);
}

/** Opens the viewer with the editor panel on slide 1 (the title slide) and its Selection tab. */
async function openSelection(page: Page, deckId: string): Promise<void> {
  await page.goto(`/presentations/${deckId}?panel=editor`);
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(page.getByTestId('presentation.editorPanel')).toBeVisible();
  await expect(page.locator('[data-deck-editing="true"]')).toBeVisible();
  await page.getByTestId('presentation.editorTab.selection').click();
  await expect(page.getByTestId('presentation.slideThemePicker')).toBeVisible();
}

test.describe('Presentations - per-slide theme and hero', () => {
  let deckId = '';
  let templateId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const templateName = `E2E slide theme template ${Date.now()}`;
      templateId = await createTwoSchemeTemplate(apiCtx, token, templateName);
      deckId = await importDeckOnTemplate(apiCtx, token, FIXTURE_SPEC, templateName, 'e2e-slide-theme.json');
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    if (deckId) {
      const { apiCtx, token } = await createAdminApiContext();
      try {
        await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
      } finally {
        await apiCtx.dispose();
      }
    }
    await deleteTemplate(templateId);
  });

  test('should give the cover its own dark theme and a gradient hero, save once, and render them after a reload', async ({
    page,
  }) => {
    await openSelection(page, deckId);
    const cover = slideRoot(page, 0);
    const body = slideRoot(page, 1);

    // Every slide inherits the light deck: the roots publish the scheme and
    // no override, and the plain cover draws no backdrop.
    await expect(cover).toHaveAttribute('data-scheme', 'light');
    await expect(cover).not.toHaveAttribute('data-scheme-override', 'true');
    await expect(cover.locator('.deck-hero-backdrop')).toHaveCount(0);
    await expect(body).toHaveAttribute('data-scheme', 'light');

    // Picking the scheme the slide already renders with (Inherit) is a no-op:
    // nothing to save, no phantom revision on a deck stored before the field.
    await page.getByTestId('presentation.slideThemePicker').locator('[data-value="inherit"]').click();
    await expectEditorSaved(page);

    // Dark: the cover's root flips to its own scheme, the body keeps the deck's.
    await page.getByTestId('presentation.slideThemePicker').locator('[data-value="dark"]').click();
    await expect(cover).toHaveAttribute('data-scheme', 'dark');
    await expect(cover).toHaveAttribute('data-scheme-override', 'true');
    await expect(body).toHaveAttribute('data-scheme', 'light');
    await expect(body).not.toHaveAttribute('data-scheme-override', 'true');

    // The gradient hero paints a backdrop UNDER the content, in the brand
    // colour by default; the colour chips appear for a non-plain hero.
    await page.getByTestId('presentation.heroPicker').locator('[data-value="gradient"]').click();
    const backdrop = cover.locator('.deck-hero-backdrop[data-hero="gradient"]');
    await expect(backdrop).toHaveCount(1);
    await expect(backdrop).toHaveAttribute('data-hero-tone', 'brand');
    await expect(backdrop).toHaveAttribute('aria-hidden', 'true');
    await expect(backdrop).toHaveAttribute('data-export-backdrop', 'true');
    await expect(cover.locator('.deck-title')).toHaveAttribute('data-hero', 'gradient');

    // Accent: one palette colour alone, read off the slide's own root.
    await page.getByTestId('presentation.heroTonePicker').locator('[data-value="accent"]').click();
    await expect(backdrop).toHaveAttribute('data-hero-tone', 'accent');

    // The accent line: the last line of the headline, no markup in the value.
    await page.getByRole('switch', { name: 'Accent the last line of the title' }).click();
    await expect(cover.locator('h1 .deck-hero-accent')).toHaveText('Take Action.');

    // The Deck tab says how many slides keep their own theme.
    await page.getByTestId('presentation.editorTab.deck').click();
    await expect(page.getByTestId('presentation.ownThemeHint')).toContainText('1 slide keeps its own theme');

    // Saved on its own: the four fields on the cover and nothing on the body.
    await expectEditorSaved(page);

    const { apiCtx, token } = await createAdminApiContext();
    try {
      const deck = await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
      expect(deck.ok()).toBeTruthy();
      const stored = (await deck.json()) as {
        revision: number;
        spec: {
          theme: string;
          slides: { theme?: string; hero?: string; hero_tone?: string; hero_accent_word?: boolean }[];
        };
      };
      expect(stored.revision).toBeGreaterThanOrEqual(2);
      expect(stored.spec.theme).toBe('light');
      expect(stored.spec.slides[0].theme).toBe('dark');
      expect(stored.spec.slides[0].hero).toBe('gradient');
      expect(stored.spec.slides[0].hero_tone).toBe('accent');
      expect(stored.spec.slides[0].hero_accent_word).toBe(true);
      expect(stored.spec.slides[1].theme).toBeUndefined();
      expect(stored.spec.slides[2].theme).toBeUndefined();
    } finally {
      await apiCtx.dispose();
    }

    // A fresh render reads the same look off the stored spec.
    await page.reload();
    await expect(page.locator('.reveal.ready')).toBeVisible();
    await expect(slideRoot(page, 0)).toHaveAttribute('data-scheme', 'dark');
    await expect(slideRoot(page, 0)).toHaveAttribute('data-scheme-override', 'true');
    await expect(slideRoot(page, 0).locator('.deck-hero-backdrop[data-hero="gradient"]')).toHaveAttribute(
      'data-hero-tone',
      'accent',
    );
    await expect(slideRoot(page, 0).locator('h1 .deck-hero-accent')).toHaveText('Take Action.');
    await expect(slideRoot(page, 1)).toHaveAttribute('data-scheme', 'light');
    await expect(slideRoot(page, 1)).not.toHaveAttribute('data-scheme-override', 'true');

    // Inherit hands the cover back to the deck: light again, the hero kept.
    await page.getByTestId('presentation.editorTab.selection').click();
    await page.getByTestId('presentation.slideThemePicker').locator('[data-value="inherit"]').click();
    await expect(slideRoot(page, 0)).toHaveAttribute('data-scheme', 'light');
    await expect(slideRoot(page, 0)).not.toHaveAttribute('data-scheme-override', 'true');
    await expect(slideRoot(page, 0).locator('.deck-hero-backdrop[data-hero="gradient"]')).toHaveCount(1);
    await expectEditorSaved(page);
  });
});
