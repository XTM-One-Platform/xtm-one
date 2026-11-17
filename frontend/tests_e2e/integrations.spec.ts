import { test, expect } from '@playwright/test';
import { IntegrationPage } from './model/integration.page';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Integrations — browse catalog, create an enabled API-key integration,
// verify it appears in the list, then delete it.
// No real API calls are made — the integration is saved with a fake key.
// ---------------------------------------------------------------------------

test.describe.serial('Integrations — CRUD', () => {
  let providerDisplayName = '';
  let integrationName = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.get('/api/v1/integrations/catalog?category=integration', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok()) {
        throw new Error(`Failed to load provider catalog: ${resp.status()} ${await resp.text()}`);
      }

      const catalog = await resp.json();
      const eligibleProviders = catalog.filter((item: {
        provider: string;
        name: string;
        auth_type: string;
        fields?: string[];
        is_disabled?: boolean;
        is_registration_only?: boolean;
      }) => (
        item.auth_type === 'api_key'
        && item.fields?.includes('api_key')
        && !item.is_disabled
        && !item.is_registration_only
      ));

      const provider = (
        eligibleProviders.find((item: { provider: string }) => item.provider === 'opencti')
        ?? eligibleProviders[0]
      );

      if (!provider) {
        throw new Error('No enabled API-key provider available in the integration catalog');
      }

      providerDisplayName = provider.name;
      integrationName = `E2E ${providerDisplayName} ${Date.now()}`;
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should display the integrations list page', async ({ page }) => {
    const intPage = new IntegrationPage(page);
    await intPage.gotoList();
    await expect(page).toHaveURL(/\/integrations(\?vis=[a-z]+)?$/);
    // The page IS the catalog: provider cards and a search field, no separate
    // "New Integration" step to walk to.
    await expect(page.getByTestId('provider-card').first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByPlaceholder(/[Ss]earch integrations/i)).toBeVisible();
  });

  test('should redirect /integrations/new without a provider back to the catalog', async ({ page }) => {
    await page.goto('/integrations/new');
    await expect(page).toHaveURL(/\/integrations(\?vis=[a-z]+)?$/);
    await expect(page.getByTestId('provider-card').first()).toBeVisible({ timeout: 15_000 });
  });

  test('should select an enabled API-key provider and advance to the config step', async ({ page }) => {
    await page.goto('/integrations/new');
    const intPage = new IntegrationPage(page);

    await intPage.selectProvider(providerDisplayName);

    // The config form should now render.
    await expect(page.getByPlaceholder(/e\.g\. My/i)).toBeVisible({ timeout: 10_000 });
  });

  test('should create an enabled API-key integration with a fake API key', async ({ page }) => {
    await page.goto('/integrations/new');
    const intPage = new IntegrationPage(page);

    await intPage.selectProvider(providerDisplayName);
    await intPage.fillName(integrationName);
    await intPage.fillApiKey('e2e-fake-api-key');
    await intPage.clickSave();

    await intPage.waitForSave();
    // Creation lands back on /integrations on Mine (the new integration is the
    // user's, whatever a cached list would land on), which is the catalog: the
    // provider now reads as installed. The instance's own name lives in the list view.
    await expect(page).toHaveURL(/\/integrations\?vis=mine$/);
    await expect(
      page.locator('[data-testid="provider-card"][data-installed="true"]').first(),
    ).toBeVisible({ timeout: 15_000 });
    await intPage.setViewMode('list');
    await expect(page.getByText(integrationName)).toBeVisible({ timeout: 10_000 });
  });

  test('should show the integration on the list page', async ({ page }) => {
    const intPage = new IntegrationPage(page);
    await intPage.gotoList();
    // The catalog marks the provider installed; the instance keeps its own
    // name, which the list view is where you read.
    await expect(
      page.locator('[data-testid="provider-card"][data-installed="true"]').first(),
    ).toBeVisible({ timeout: 15_000 });
    await intPage.setViewMode('list');
    await expect(page.getByText(integrationName)).toBeVisible({ timeout: 10_000 });
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.get('/api/v1/integrations?limit=500', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (resp.ok()) {
        const integrations = await resp.json();
        const target = integrations.find((i: { name: string; id: string }) => i.name === integrationName);
        if (target) {
          await apiCtx.delete(`/api/v1/integrations/${target.id}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
        }
      }
    } finally {
      await apiCtx.dispose();
    }
  });
});
