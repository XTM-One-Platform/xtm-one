import { test, expect, type Locator, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Integration status ribbon — the Certified / Preview corner badge on catalog
// cards, in both the browsing catalog (/integrations) and the administration
// catalog (/settings/integrations).
//
// The catalog is mocked, so the spec pins the RENDERING contract of the
// ribbon: a catalog entry served with is_certified shows Certified, every
// other first-party entry shows Preview, and a custom bundle is not ours to
// qualify. Which providers are certified is the backend's one list
// (integration_catalog.CERTIFIED_PROVIDERS), pinned by
// backend/tests/test_integration_certified_providers.py.
// ---------------------------------------------------------------------------

type Json = Record<string, unknown>;

/** `opencrq` is the catalog key of the product marketed as OpenCRQ. */
const CERTIFIED = 'opencrq';
const PREVIEW = 'slack';
const DISABLED = 'jira';
const CUSTOM = 'acme_custom_bundle';

function catalogEntry(provider: string, name: string, overrides: Json = {}): Json {
  return {
    provider,
    name,
    category: 'integration',
    integration_category: 'development',
    auth_type: 'api_key',
    fields: ['api_key'],
    extra_fields: null,
    extra_field_options: null,
    extra_field_visible_when: null,
    description: `${name} test entry`,
    logo: null,
    integration_types: [],
    available_events: [],
    available_tools: [],
    element_types: [],
    webhook_path: null,
    needs_shared_webhook: false,
    supports_assignment_webhooks: false,
    supports_webhook_management: false,
    setup_guide: null,
    has_oauth_defaults: false,
    has_oauth_extra_config: false,
    is_bot: false,
    is_custom: false,
    is_disabled: false,
    is_registration_only: false,
    ...overrides,
  };
}

// The catalog sections its results by the category taxonomy, so each fixture
// entry claims a real category.
const CATALOG: Json[] = [
  catalogEntry(CERTIFIED, 'OpenCRQ', { integration_category: 'grc', is_certified: true }),
  catalogEntry(PREVIEW, 'Slack', { integration_category: 'communication' }),
  catalogEntry(DISABLED, 'Jira', { integration_category: 'development', is_disabled: true }),
  catalogEntry(CUSTOM, 'Acme Custom Bundle', { integration_category: 'automation', is_custom: true }),
];

/**
 * Serve the fixture catalog to both surfaces.
 *
 * Matched on pathname so it answers the browsing catalog
 * (`include_disabled=true`) and the administration catalog alike; the admin
 * section renders the disabled entry too, with its toggle switched off.
 */
async function mockCatalog(page: Page) {
  await page.route(
    (url) => url.pathname === '/api/v1/integrations/catalog',
    (route: Route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(CATALOG),
    }),
  );
}

const ribbonOf = (page: Page, cardTestId: string, provider: string) =>
  page.locator(`[data-testid="${cardTestId}"][data-provider="${provider}"]`)
    .getByTestId('integration-status-ribbon');

const cardOf = (page: Page, cardTestId: string, provider: string) =>
  page.locator(`[data-testid="${cardTestId}"][data-provider="${provider}"]`);

/**
 * The ribbon's offsets from its card's top-right corner, read in ONE frame.
 * The cards animate in, so two separate boundingBox() calls can land on
 * different frames and disagree by a couple of pixels.
 */
async function cornerOffsets(card: Locator) {
  return card.evaluate((el) => {
    const ribbon = el.querySelector('[data-testid="integration-status-ribbon"]');
    if (!ribbon) return null;
    const c = el.getBoundingClientRect();
    const r = ribbon.getBoundingClientRect();
    const footer = Array.from(el.querySelectorAll('div')).find(
      (d) => d.textContent?.trim() === 'Disabled by your administrator',
    );
    return {
      top: Math.abs(r.top - c.top),
      right: Math.abs(c.right - r.right),
      ribbonBottom: r.bottom,
      footerTop: footer ? footer.getBoundingClientRect().top : null,
    };
  });
}

test.describe('Integration status ribbon', () => {
  test.beforeEach(async ({ page }) => {
    await mockCatalog(page);
  });

  test('catalog cards carry the ribbon their provider earns', async ({ page }) => {
    await page.goto('/integrations');
    await page.getByTestId('provider-card').first().waitFor({ state: 'visible', timeout: 15_000 });

    const certified = ribbonOf(page, 'provider-card', CERTIFIED);
    await expect(certified).toHaveAttribute('data-status', 'certified');
    await expect(certified).toHaveText('Certified');
    await expect(certified).toHaveAttribute('aria-label', 'Certified integration');

    const preview = ribbonOf(page, 'provider-card', PREVIEW);
    await expect(preview).toHaveAttribute('data-status', 'preview');
    await expect(preview).toHaveText('Preview');
    await expect(preview).toHaveAttribute('aria-label', 'Preview integration');

    // A customer's own imported bundle is not ours to qualify: no ribbon, and
    // the card still says what it is through its own Custom badge.
    await expect(cardOf(page, 'provider-card', CUSTOM)).toBeVisible();
    await expect(ribbonOf(page, 'provider-card', CUSTOM)).toHaveCount(0);
  });

  test('a disabled catalog card anchors its ribbon to its own corner', async ({ page }) => {
    await page.goto('/integrations');
    await page.getByTestId('provider-card-disabled').first().waitFor({ state: 'visible', timeout: 15_000 });

    const card = cardOf(page, 'provider-card-disabled', DISABLED);
    const ribbon = ribbonOf(page, 'provider-card-disabled', DISABLED);
    await expect(ribbon).toHaveAttribute('data-status', 'preview');

    // The ribbon is absolutely positioned. The disabled card nests its padded
    // body inside an outer box that also holds the "Disabled by your
    // administrator" footer, so a lost positioning context would slide the
    // ribbon off the corner it belongs to. Pin the corner, and pin that it
    // stays clear of the footer.
    const offsets = await cornerOffsets(card);
    expect(offsets).not.toBeNull();
    expect(offsets!.footerTop).not.toBeNull();

    // Flush with the card's top-right corner, allowing for the 1px border.
    expect(offsets!.top).toBeLessThanOrEqual(2);
    expect(offsets!.right).toBeLessThanOrEqual(2);
    expect(offsets!.ribbonBottom).toBeLessThanOrEqual(offsets!.footerTop!);
  });

  // The positioning context belongs to the shared card body, not to
  // whichever card variant wraps it, so every variant anchors the same way.
  for (const [surface, cardTestId, provider] of [
    ['/integrations', 'provider-card', PREVIEW],
    ['/integrations', 'provider-card-disabled', DISABLED],
    ['/settings/integrations', 'catalog-provider-card', CERTIFIED],
  ] as const) {
    test(`the ribbon of a ${cardTestId} sits in its own corner, anchored by the card body`, async ({ page }) => {
      await page.goto(surface);
      const card = cardOf(page, cardTestId, provider);
      await card.waitFor({ state: 'visible', timeout: 15_000 });
      const ribbon = ribbonOf(page, cardTestId, provider);

      const anchoredByBody = await ribbon.evaluate(
        (el) => (el as HTMLElement).offsetParent?.getAttribute('data-testid') ?? null,
      );
      expect(anchoredByBody).toBe('provider-card-body');

      const offsets = await cornerOffsets(card);
      expect(offsets).not.toBeNull();
      expect(offsets!.top).toBeLessThanOrEqual(2);
      expect(offsets!.right).toBeLessThanOrEqual(2);
    });
  }

  test('a long provider name stays clear of the ribbon band at the two-column breakpoint', async ({ page }) => {
    const longName = 'Continuous Threat Exposure Management';
    await page.route(
      (url) => url.pathname === '/api/v1/integrations/catalog',
      (route: Route) => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([catalogEntry(PREVIEW, longName, { integration_category: 'communication' })]),
      }),
    );
    await page.setViewportSize({ width: 700, height: 900 });
    await page.goto('/integrations');
    const card = cardOf(page, 'provider-card', PREVIEW);
    await card.waitFor({ state: 'visible', timeout: 15_000 });

    const band = card.getByTestId('integration-status-ribbon').locator('div').first();
    const name = card.getByText(longName, { exact: true });
    const nameBox = await name.boundingBox();
    const cardBox = await card.boundingBox();
    expect(nameBox).not.toBeNull();
    expect(cardBox).not.toBeNull();

    // The band is rotated 45deg: its inner (lower-left) edge is the line
    // y - top = (x - left) - offset. Read it off the band's own geometry
    // instead of trusting the classes, and require the name's top-right
    // corner to sit strictly outside it.
    const clearance = await band.evaluate((el, corner) => {
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const halfThickness = (el as HTMLElement).offsetHeight / 2;
      // Signed distance of the name's top-right corner from the band's
      // centre line (direction (1, 1)), minus the band's half thickness.
      const d = ((corner.y - cy) - (corner.x - cx)) / Math.SQRT2;
      return d - halfThickness;
    }, { x: nameBox!.x + nameBox!.width, y: nameBox!.y });
    expect(clearance).toBeGreaterThan(0);
  });

  test('administration catalog cards classify the same way', async ({ page }) => {
    await page.goto('/settings/integrations');
    await page.getByTestId('catalog-provider-card').first().waitFor({ state: 'visible', timeout: 15_000 });

    await expect(ribbonOf(page, 'catalog-provider-card', CERTIFIED))
      .toHaveAttribute('data-status', 'certified');
    await expect(ribbonOf(page, 'catalog-provider-card', PREVIEW))
      .toHaveAttribute('data-status', 'preview');
    await expect(ribbonOf(page, 'catalog-provider-card', DISABLED))
      .toHaveAttribute('data-status', 'preview');

    await expect(cardOf(page, 'catalog-provider-card', CUSTOM)).toBeVisible();
    await expect(ribbonOf(page, 'catalog-provider-card', CUSTOM)).toHaveCount(0);
  });

  // The disabled count is ONE plural message, number included: it
  // used to be a number glued onto a fragment ("1 disabled by your
  // administrator"), which has no plural form and no noun.
  test('the browsing catalog counts admin-disabled integrations in one sentence', async ({ page }) => {
    await page.goto('/integrations');
    await page.getByTestId('provider-card-disabled').first().waitFor({ state: 'visible', timeout: 15_000 });
    await expect(page.getByText('1 integration disabled by your administrator', { exact: true })).toBeVisible();
  });

  test('the administration catalog counts disabled integrations in one sentence', async ({ page }) => {
    await page.goto('/settings/integrations');
    await page.getByTestId('catalog-provider-card').first().waitFor({ state: 'visible', timeout: 15_000 });
    await expect(page.getByText('1 integration disabled', { exact: true })).toBeVisible();
  });

  test('both catalogs count several disabled integrations in the plural', async ({ page }) => {
    // A second disabled entry exercises the ICU `other` branch. Registered
    // after the beforeEach mock, so this handler answers first.
    const twoDisabled = [
      ...CATALOG,
      catalogEntry('asana', 'Asana', { integration_category: 'development', is_disabled: true }),
    ];
    await page.route(
      (url) => url.pathname === '/api/v1/integrations/catalog',
      (route: Route) => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(twoDisabled),
      }),
    );

    await page.goto('/integrations');
    await page.getByTestId('provider-card-disabled').first().waitFor({ state: 'visible', timeout: 15_000 });
    await expect(page.getByText('2 integrations disabled by your administrator', { exact: true })).toBeVisible();

    await page.goto('/settings/integrations');
    await page.getByTestId('catalog-provider-card').first().waitFor({ state: 'visible', timeout: 15_000 });
    await expect(page.getByText('2 integrations disabled', { exact: true })).toBeVisible();
  });
});
