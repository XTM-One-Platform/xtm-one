import { test, expect } from '@playwright/test';
import { SettingsBrandingPage } from './model/settings-branding.page';
import type { FilePayload } from './model/settings-branding.page';
import { ok, withAdmin } from './helpers';

// Minimal 1×1 transparent PNG — used for all image upload fields.
const PIXEL_PNG_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12NgAAIABQAABjE+ibYAAAAASUVORK5CYII=';

const pixelPng: FilePayload = {
  name: 'pixel.png',
  mimeType: 'image/png',
  buffer: Buffer.from(PIXEL_PNG_B64, 'base64'),
};

// The save test overwrites deployment-global branding; restore it after.
const BRANDING_FIELDS = [
  'platform_name',
  'platform_description',
  'platform_logo',
  'platform_accent_color',
  'platform_background_color',
  'platform_theme_color',
  'platform_favicon',
  'platform_og_image',
] as const;
type BrandingSnapshot = Record<(typeof BRANDING_FIELDS)[number], string | null>;

test.describe('Settings – Platform Branding', () => {
  let branding: SettingsBrandingPage;
  let brandingSnapshot: BrandingSnapshot | null = null;

  test.beforeAll(async () => {
    brandingSnapshot = await withAdmin(async (api, headers) => {
      const json = await (await ok(await api.get('/api/v1/settings/platform', { headers }), 'get platform settings')).json();
      return Object.fromEntries(BRANDING_FIELDS.map((f) => [f, json[f] ?? null] as const)) as BrandingSnapshot;
    });
  });

  test.afterAll(async () => {
    if (!brandingSnapshot) return;
    const snapshot = brandingSnapshot;
    await withAdmin(async (api, headers) =>
      ok(await api.put('/api/v1/settings/platform', { headers, data: snapshot }), 'restore branding'));
  });

  test.beforeEach(async ({ page }) => {
    branding = new SettingsBrandingPage(page);
    await branding.goto();
    await branding.waitForBrandingCard();
  });

  test('shows the branding card in read-only state', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Platform Branding' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Edit', exact: true })).toBeVisible();

    // At rest the card is a read view (#10855): label / value tiles, no form
    // control until Edit is clicked.
    await expect(page.getByTestId('settings.branding.read')).toBeVisible();
    await expect(page.getByLabel('Platform Name', { exact: true })).toHaveCount(0);
    await expect(page.getByLabel('Description', { exact: true })).toHaveCount(0);
  });

  test('Edit enables all identity and color fields', async ({ page }) => {
    await branding.clickEdit();

    await expect(page.getByLabel('Platform Name', { exact: true })).toBeEnabled();
    await expect(page.getByLabel('Description', { exact: true })).toBeEnabled();
    // The hex input is the control the label names (the swatch shares the name
    // by aria-labelledby, so it is excluded by type), as in the page object.
    await expect(
      page
        .getByLabel('Accent Color', { exact: true })
        .and(page.locator('input:not([type="color"])')),
    ).toBeEnabled();

    await expect(page.getByRole('button', { name: 'Save' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Cancel' })).toBeVisible();
  });

  test('can fill all branding fields and save', async ({ page }) => {
    await branding.clickEdit();

    await branding.fillName('Acme Security Platform');
    await branding.fillDescription('Internal threat intelligence platform');
    await branding.uploadLogo(pixelPng);
    await branding.setAccentColor('#1a9ff0');
    await branding.setBackgroundColor('#0d1117');
    await branding.setThemeColor('#161b22');
    await branding.uploadFavicon(pixelPng);
    await branding.uploadOgImage(pixelPng);

    await branding.save();

    // After a successful save the UI returns to read-only state.
    await expect(page.getByRole('button', { name: 'Edit', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save' })).not.toBeVisible();
    await expect(page.getByRole('button', { name: 'Cancel' })).not.toBeVisible();
  });

  test('Cancel discards changes and restores read-only state', async ({ page }) => {
    await branding.clickEdit();
    await branding.fillName('Name That Must Not Be Saved');
    await branding.cancel();

    await expect(page.getByRole('button', { name: 'Edit', exact: true })).toBeVisible();
    await expect(page.getByTestId('settings.branding.read')).toBeVisible();
    await expect(page.getByLabel('Platform Name', { exact: true })).toHaveCount(0);
    await expect(page.getByTestId('settings.branding.read')).not.toContainText('Name That Must Not Be Saved');
  });

  test('Save is disabled while an invalid hex color is set', async ({ page }) => {
    await branding.clickEdit();
    await branding.setAccentColor('notacolor');

    await expect(page.getByRole('button', { name: 'Save' })).toBeDisabled();
    await expect(page.locator('text=Must be a hex color')).toBeVisible();
  });

  test('logo upload is reflected as a preview after selecting a file', async ({ page }) => {
    await branding.clickEdit();
    await branding.uploadLogo(pixelPng);

    // FileUpload switches to the filled state and shows "Uploaded file" text.
    await expect(page.locator('text=Uploaded file').first()).toBeVisible();
  });
});
