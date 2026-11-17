import { test, expect } from '@playwright/test';
import { createAdminApiContext, createTwoSchemeTemplate, deleteTemplate, importDeckOnTemplate } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - the alt text of a quote's pictures.
//
// A quote may show a company logo that no visible credit names ("Name, Role"
// beside the logo): it saves, and the viewer announces the logo by the
// template asset's own description, served with the deck (`picture_labels`).
// A logo the credit already names (its organization) keeps alt="" - the name
// is read beside it, and a second announcement is noise.
// ---------------------------------------------------------------------------

/** A 1 x 1 PNG: enough for the renderer to load and measure the picture. */
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
);
const DESCRIPTION = 'Acme Corporation logo';

const BASE_SPEC = {
  title: 'E2E quote pictures',
  theme: 'dark',
  aspect_ratio: '16:9',
  slides: [
    {
      layout: 'content',
      title: 'What customers say',
      blocks: [
        { type: 'quote', variant: 'testimonial', content: 'It changed how we work.', author: 'Dana Whitlock', role: 'VP' },
        { type: 'quote', variant: 'testimonial', content: 'We would not go back.', author: 'Jane Doe', organization: 'Acme Corporation' },
      ],
    },
  ],
};

test.describe('Presentations - quote picture alt text', () => {
  const templateName = `E2E quote pictures ${Date.now()}`;
  let templateId = '';
  let deckId = '';
  let logoId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    const headers = { Authorization: `Bearer ${token}` };
    try {
      templateId = await createTwoSchemeTemplate(apiCtx, token, templateName);
      const asset = await apiCtx.post(`/api/v1/template-containers/${templateId}/assets`, {
        headers,
        params: { asset_role: 'customer_logo', description: DESCRIPTION },
        multipart: { file: { name: 'acme.png', mimeType: 'image/png', buffer: PNG } },
      });
      if (!asset.ok()) throw new Error(`Uploading the logo failed: ${asset.status()} ${await asset.text()}`);
      logoId = ((await asset.json()) as { id: string }).id;
      deckId = await importDeckOnTemplate(apiCtx, token, BASE_SPEC, templateName, 'e2e-quote-pictures.json');
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

  test('should save a logo no credit names and announce it by the asset description', async ({ page }) => {
    const { apiCtx, token } = await createAdminApiContext();
    const headers = { Authorization: `Bearer ${token}` };
    try {
      // The editor's Save: the same PATCH, the full slides list, the revision it read.
      const before = (await (await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers })).json()) as {
        revision: number;
        spec: { slides: { blocks: Record<string, unknown>[] }[] };
      };
      const slides = before.spec.slides.map((slide) => ({
        ...slide,
        blocks: slide.blocks.map((block) => ({ ...block, logo_file_id: logoId })),
      }));
      const saved = await apiCtx.patch(`/api/v1/presentations/${deckId}`, {
        headers,
        data: { slides, expected_revision: before.revision },
      });
      expect(saved.ok(), await saved.text()).toBe(true);

      // The deck response names the picture for its readers.
      const deck = (await (await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers })).json()) as {
        picture_labels: Record<string, { description: string | null; filename: string | null }>;
      };
      expect(deck.picture_labels[logoId]?.description).toBe(DESCRIPTION);
    } finally {
      await apiCtx.dispose();
    }

    await page.goto(`/presentations/${deckId}`);
    await expect(page.locator('.reveal.ready')).toBeVisible();
    const logos = page.locator('.reveal .deck-quote-logo-image');
    await expect(logos).toHaveCount(2);
    // "Name, Role" beside the logo: nothing on screen names the company.
    await expect(logos.nth(0)).toHaveAttribute('alt', DESCRIPTION);
    // The organization beside the logo names it: a redundant image stays silent.
    await expect(logos.nth(1)).toHaveAttribute('alt', '');
  });
});
