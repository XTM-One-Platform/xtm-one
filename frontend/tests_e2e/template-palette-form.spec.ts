import { test, expect, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext, createTwoSchemeTemplate, deleteTemplate } from './helpers';
import { PALETTE_KEY_LABELS } from '../src/components/presentation/palette-keys';

// ---------------------------------------------------------------------------
// The template form edits the chart series and the brand hues: in the
// base palette and in the scheme variant, with the series preview and the
// fallback of an empty field following every keystroke, the unsaved design
// previewable, and a save that keeps every palette key the form did not touch.
// ---------------------------------------------------------------------------

type Palette = Record<string, string>;

async function readTemplate(id: string): Promise<{ theme_config: Palette; extra_metadata: Record<string, unknown> }> {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    const resp = await apiCtx.get(`/api/v1/template-containers/${id}`, { headers: { Authorization: `Bearer ${token}` } });
    expect(resp.ok()).toBeTruthy();
    return resp.json();
  } finally {
    await apiCtx.dispose();
  }
}

type PaletteKey = keyof typeof PALETTE_KEY_LABELS;

/** A palette key's text field, named by its readable caption; the raw key under it is aria-hidden. */
const field = (palette: Locator, key: PaletteKey) =>
  palette.getByRole('textbox', { name: PALETTE_KEY_LABELS[key], exact: true });

/** The placeholder of an empty field whose colour falls back to another key, named by its caption. */
const fallsBackTo = (key: PaletteKey) => `Falls back to ${PALETTE_KEY_LABELS[key]}`;

async function openDesign(page: Page, id: string): Promise<{ dark: Locator; light: Locator }> {
  await page.goto(`/templates/${id}/config`);
  await page.getByRole('button', { name: 'Edit', exact: true }).first().click();
  const dark = page.locator('[data-palette-scheme="dark"]');
  const light = page.locator('[data-palette-scheme="light"]');
  await expect(dark).toBeVisible();
  await expect(light).toBeVisible();
  return { dark, light };
}

test.describe('Templates - chart series and brand hues in the design form', () => {
  let templateId = '';
  let before: { theme_config: Palette; extra_metadata: Record<string, unknown> };

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      templateId = await createTwoSchemeTemplate(apiCtx, token, `E2E palette form ${Date.now()}`);
    } finally {
      await apiCtx.dispose();
    }
    before = await readTemplate(templateId);
  });

  test.afterAll(async () => {
    await deleteTemplate(templateId);
  });

  test('edits both groups in the base and the variant, previews them live and keeps every other key', async ({ page }) => {
    const { dark, light } = await openDesign(page, templateId);

    // Both groups are offered in both palettes, filled from the stored values.
    for (const palette of [dark, light]) {
      for (const key of ['chart_1', 'chart_6', 'brand_1', 'brand_4'] as const) await expect(field(palette, key)).toBeVisible();
    }
    await expect(field(dark, 'chart_2')).toHaveValue(before.theme_config.chart_2);

    // An empty brand hue shows the series of its rank; a variant shows what it inherits.
    await expect(field(dark, 'brand_2')).toHaveAttribute('placeholder', fallsBackTo('chart_2'));
    await expect(field(light, 'brand_4')).toHaveAttribute('placeholder', fallsBackTo('chart_4'));

    // The series preview follows the field as it is typed.
    await field(dark, 'chart_1').fill('#ff3fa4');
    await expect(dark.locator('[data-series="chart_1"]')).toHaveCSS('background-color', 'rgb(255, 63, 164)');
    await field(dark, 'brand_1').fill('#a64dff');
    await expect(field(light, 'brand_1')).toHaveAttribute('placeholder', 'Falls back to Base palette');
    await field(light, 'brand_4').fill('#123456');
    await field(light, 'chart_1').fill('#0a0b0c');
    await expect(light.locator('[data-series="chart_1"]')).toHaveCSS('background-color', 'rgb(10, 11, 12)');

    // The unsaved design is what the preview draws.
    await page.getByRole('button', { name: 'Preview the unsaved design' }).click();
    const dialog = page.getByRole('dialog', { name: 'Template preview' });
    await expect(dialog).toBeVisible();
    const slide = dialog.locator('[style*="--deck-chart-1"]').first();
    await expect(slide).toBeAttached();
    await expect
      .poll(() => slide.evaluate((el) => (el as HTMLElement).style.getPropertyValue('--deck-chart-1').trim()))
      .toBe('#ff3fa4');
    await expect
      .poll(() => slide.evaluate((el) => (el as HTMLElement).style.getPropertyValue('--deck-brand-1').trim()))
      .toBe('#a64dff');
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);

    // Saved: the edited keys land, every other key is kept as it was.
    const saved = page.waitForResponse(
      (resp) => resp.request().method() === 'PUT' && resp.url().includes(`/template-containers/${templateId}`),
    );
    await page.getByRole('button', { name: 'Save changes' }).click();
    expect((await saved).ok()).toBeTruthy();

    const after = await readTemplate(templateId);
    expect(after.theme_config).toEqual({ ...before.theme_config, chart_1: '#ff3fa4', brand_1: '#a64dff' });
    const variants = (m: Record<string, unknown>) => (m.theme_variants as Record<string, Palette>) ?? {};
    expect(variants(after.extra_metadata).light).toEqual({
      ...variants(before.extra_metadata).light,
      chart_1: '#0a0b0c',
      brand_4: '#123456',
    });
    expect(after.extra_metadata.supported_schemes).toEqual(before.extra_metadata.supported_schemes);
  });

  test('a variant falls back to what its decks draw: every base colour when the base has no background', async ({ page }) => {
    const { apiCtx, token } = await createAdminApiContext();
    let id = '';
    try {
      // No background: the base reads as neither theme, so a light deck lays the
      // light variant over ALL of it - its chart series included - like the renderer.
      const resp = await apiCtx.post('/api/v1/template-containers', {
        headers: { Authorization: `Bearer ${token}` },
        data: {
          name: `E2E palette inheritance ${Date.now()}`,
          kind: 'presentation',
          aspect_ratio: '16:9',
          theme_config: { foreground: '#f2f2f3', accent: '#42caff', chart_2: '#00f0bc' },
          extra_metadata: {
            supported_schemes: ['dark', 'light'],
            theme_variants: { light: { background: '#ffffff', foreground: '#18191b', accent: '#0015a8' } },
          },
        },
      });
      expect(resp.ok()).toBeTruthy();
      id = ((await resp.json()) as { id: string }).id;
    } finally {
      await apiCtx.dispose();
    }
    try {
      const { dark, light } = await openDesign(page, id);

      // The base's scheme-bound chart series reaches the light variant.
      await expect(field(light, 'chart_2')).toHaveAttribute('placeholder', 'Falls back to Base palette');
      await expect(light.locator('[data-series="chart_2"]')).toHaveCSS('background-color', 'rgb(0, 240, 188)');

      // An empty chart_1 follows the accent of its palette, and brand_1 follows chart_1.
      await expect(field(dark, 'chart_1')).toHaveAttribute('placeholder', fallsBackTo('accent'));
      await expect(light.locator('[data-series="chart_1"]')).toHaveCSS('background-color', 'rgb(0, 21, 168)');
      await expect(field(light, 'brand_1')).toHaveAttribute('placeholder', fallsBackTo('chart_1'));
    } finally {
      await deleteTemplate(id);
    }
  });

  test('a variant stored for the base palette\'s own theme is named, and a base field edit takes the key back', async ({ page }) => {
    const { apiCtx, token } = await createAdminApiContext();
    let id = '';
    try {
      // Valid JSON the form never writes itself: a dark variant over a dark base,
      // which every dark deck lays over the base.
      const resp = await apiCtx.post('/api/v1/template-containers', {
        headers: { Authorization: `Bearer ${token}` },
        data: {
          name: `E2E palette shadow ${Date.now()}`,
          kind: 'presentation',
          aspect_ratio: '16:9',
          theme_config: { background: '#070d18', foreground: '#f2f2f3', accent: '#42caff', chart_1: '#42caff' },
          extra_metadata: {
            supported_schemes: ['dark', 'light'],
            theme_variants: {
              dark: { chart_1: '#ff0000' },
              light: { background: '#ffffff', foreground: '#18191b' },
            },
          },
        },
      });
      expect(resp.ok()).toBeTruthy();
      id = ((await resp.json()) as { id: string }).id;
    } finally {
      await apiCtx.dispose();
    }
    try {
      const { dark } = await openDesign(page, id);
      const notice = dark.getByText(/variant palette, which decks in this theme lay over this one: chart_1 come from it/);

      // The preview draws what dark decks draw - the variant's value - and says why.
      await expect(notice).toBeVisible();
      await expect(dark.locator('[data-series="chart_1"]')).toHaveCSS('background-color', 'rgb(255, 0, 0)');

      // Editing the base field hands chart_1 back to the base palette.
      await field(dark, 'chart_1').fill('#00ff00');
      await expect(notice).toHaveCount(0);
      await expect(dark.locator('[data-series="chart_1"]')).toHaveCSS('background-color', 'rgb(0, 255, 0)');

      const saved = page.waitForResponse(
        (resp) => resp.request().method() === 'PUT' && resp.url().includes(`/template-containers/${id}`),
      );
      await page.getByRole('button', { name: 'Save changes' }).click();
      expect((await saved).ok()).toBeTruthy();
      const after = await readTemplate(id);
      expect(after.theme_config.chart_1).toBe('#00ff00');
      expect(after.extra_metadata.theme_variants).toEqual({ light: { background: '#ffffff', foreground: '#18191b' } });
    } finally {
      await deleteTemplate(id);
    }
  });

  test('a base background typed key by key keeps its field, and the other theme keeps its own background', async ({ page }) => {
    const { apiCtx, token } = await createAdminApiContext();
    let id = '';
    try {
      id = await createTwoSchemeTemplate(apiCtx, token, `E2E palette typing ${Date.now()}`);
    } finally {
      await apiCtx.dispose();
    }
    try {
      const stored = await readTemplate(id);
      const { dark } = await openDesign(page, id);
      const background = field(dark, 'background');
      await background.click();
      await background.press('Control+a');

      // `#1a2` reads light on the way to `#1a2230`: the base palette card now
      // reads Light, and still holds the field being typed in.
      await background.pressSequentially('#1a2');
      const flipped = field(page.locator('[data-palette-scheme="light"]'), 'background');
      await expect(flipped).toBeFocused();
      await expect(flipped).toHaveValue('#1a2');
      await page.keyboard.type('230');
      await expect(background).toBeFocused();
      await expect(background).toHaveValue('#1a2230');

      const saved = page.waitForResponse(
        (resp) => resp.request().method() === 'PUT' && resp.url().includes(`/template-containers/${id}`),
      );
      await page.getByRole('button', { name: 'Save changes' }).click();
      expect((await saved).ok()).toBeTruthy();
      const after = await readTemplate(id);
      expect(after.theme_config.background).toBe('#1a2230');
      // A background edit never takes the key back: the light variant, which a
      // passing value read as the base's own theme, keeps its background.
      expect(after.extra_metadata.theme_variants).toEqual(stored.extra_metadata.theme_variants);
    } finally {
      await deleteTemplate(id);
    }
  });
});
