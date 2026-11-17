import { test, expect } from '@playwright/test';
import { createAdminApiContext, createTwoSchemeTemplate, deleteTemplate } from './helpers';

// ---------------------------------------------------------------------------
// "Preview the unsaved design" previews everything the form would save: the
// name, the aspect ratio and the layout catalog as well as the palettes.
// ---------------------------------------------------------------------------

test.describe('Templates - the unsaved-design preview reads the whole draft', () => {
  let templateId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      // Saved as 16:9 with no layout catalog: the preview shows the standard deck.
      templateId = await createTwoSchemeTemplate(apiCtx, token, `E2E draft preview ${Date.now()}`);
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    await deleteTemplate(templateId);
  });

  test('the aspect ratio, the layouts and the name typed in the form reach the preview', async ({ page }) => {
    await page.goto(`/templates/${templateId}/config`);
    await page.getByRole('button', { name: 'Edit', exact: true }).first().click();

    const draftName = `Draft deck ${Date.now()}`;
    await page.getByLabel('Template Name', { exact: true }).fill(draftName);
    // The template's own aspect ratio field; the deck defaults hold an "Aspect ratio" picker (a combobox) too.
    await page.getByRole('textbox', { name: 'Aspect ratio', exact: true }).fill('4:3');
    await page.getByText('Advanced (JSON)', { exact: true }).click();
    await page.getByLabel('Layouts', { exact: true }).fill('[{"id": "title"}, {"id": "closing"}]');

    await page.getByRole('button', { name: 'Preview the unsaved design' }).click();
    const dialog = page.getByRole('dialog', { name: 'Template preview' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText('2 layouts previewed')).toBeVisible();
    await expect(dialog.locator('[data-aspect="4:3"]').first()).toBeAttached();
    await expect(dialog.getByText(draftName).first()).toBeAttached();
  });
});
