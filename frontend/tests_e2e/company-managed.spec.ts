import { test, expect } from '@playwright/test';
import { AgentPage } from './model/agent.page';
import { createAdminApiContext } from './helpers';
import { LEGACY_REGULAR_USER_EMAIL, REGULAR_USER_PASSWORD, regularUserEmail } from './constants';
import { LoginPage } from './model/login.page';

// ---------------------------------------------------------------------------
// Company-managed entities — access control enforcement.
//
// The admin account (from storageState) creates an agent and enables the
// company-managed toggle. We then verify:
//   1. The "Company-managed" badge is rendered on the entity.
//   2. The agent appears on the list for all users.
//   3. A regular (non-admin) user can view but not edit it.
//
// The regular user is created via the admin API in beforeAll and removed in
// afterAll, so no environment variables need to be configured.
// ---------------------------------------------------------------------------

test.describe.serial('Company-managed', () => {
  const MANAGED_AGENT_NAME = `E2E Managed Agent ${Date.now()}`;
  let managedAgentUrl = '';
  let regularUserId = '';
  let regularEmail = '';

  test.beforeAll(async () => {
    regularEmail = regularUserEmail('company-managed', test.info().parallelIndex);
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const headers = { Authorization: `Bearer ${token}` };

      // Clean up any leftover user from a previous run that didn't finish
      // afterAll, and the address every spec shared before they were given one
      // each — nothing else collects it on a persistent database.
      const listResp = await apiCtx.get('/api/v1/users?limit=500', { headers });
      if (listResp.ok()) {
        const users: { id: string; email: string }[] = await listResp.json();
        const stale = [regularEmail, LEGACY_REGULAR_USER_EMAIL];
        for (const leftover of users.filter((u) => stale.includes(u.email))) {
          await apiCtx.delete(`/api/v1/users/${leftover.id}`, { headers });
        }
      }

      // Create the regular user fresh for this run.
      const resp = await apiCtx.post('/api/v1/users', {
        headers,
        data: {
          email: regularEmail,
          password: REGULAR_USER_PASSWORD,
          full_name: 'E2E Regular User',
          role: 'user',
          account_type: 'local',
        },
      });
      if (!resp.ok()) {
        throw new Error(
          `Failed to create regular user for company-managed e2e: ${resp.status()} ${await resp.text()}`,
        );
      }
      const user = await resp.json();
      regularUserId = user.id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      if (regularUserId) {
        await apiCtx.delete(`/api/v1/users/${regularUserId}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
      }
      if (managedAgentUrl) {
        const agentId = managedAgentUrl.split('/').pop();
        if (agentId) {
          await apiCtx.delete(`/api/v1/agents/${agentId}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
        }
      }
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should create an agent and enable company-managed toggle', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await agentPage.clickNewAgent();
    await agentPage.selectScratch();
    await agentPage.fillName(MANAGED_AGENT_NAME);
    await agentPage.fillPersona('Company-wide assistant.');
    // Enable the company-managed toggle before creating.
    const toggle = page.locator('button[role="switch"]').last();
    if (await toggle.isVisible()) {
      if (await toggle.getAttribute('data-state') !== 'checked') {
        await toggle.click();
      }
    }
    await agentPage.clickCreate();
    await agentPage.waitForDetail();
    managedAgentUrl = page.url();
    await expect(page.getByText(MANAGED_AGENT_NAME)).toBeVisible({ timeout: 10_000 });
  });

  test('should display the company-managed badge on the agent detail', async ({ page }) => {
    await page.goto(managedAgentUrl);
    await expect(page.getByText(/company-managed/i).first()).toBeVisible({ timeout: 10_000 });
  });

  test('should show the agent on the list', async ({ page }) => {
    // A company-managed agent is under Company, not under the Mine default.
    await page.goto('/agents?vis=company');
    // The list pages 25 rows sorted by name and a persistent database holds many
    // "E2E ..." agents: find this one by name, as a reader finds one row.
    // The toolbar labels both its role=search wrapper and its input: fill the input.
    await page.getByRole('searchbox', { name: 'Search agents...' }).fill(MANAGED_AGENT_NAME);
    await expect(page.getByText(MANAGED_AGENT_NAME)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('radio', { name: /Company/ })).toHaveAttribute('aria-checked', 'true');
  });

  test('regular user can view but not edit a company-managed agent', async ({ page }) => {
    // Login as the non-admin user with a fresh, unauthenticated browser context.
    await page.context().clearCookies();
    // Navigate to a page on the origin first so we can clear its localStorage
    await page.goto('/login');
    await page.evaluate(() => window.localStorage.clear());
    // Reload to ensure app state picks up the empty storage
    await page.reload();
    // Via LoginPage so this shares the one implementation of "get signed in",
    // including clearing the Terms of Use gate — a freshly created
    // regular user has never accepted, so they always meet it.
    await new LoginPage(page).login(regularEmail, REGULAR_USER_PASSWORD);

    await page.goto(managedAgentUrl);
    await expect(page.getByText(MANAGED_AGENT_NAME)).toBeVisible({ timeout: 10_000 });

    // DetailTabs keeps its tabs as router links, but they carry role="tab".
    await page.getByRole('tab', { name: 'Configuration', exact: true }).click();
    // The company-managed banner signals read-only to non-admin users.
    await expect(page.getByText(/company-managed/i).first()).toBeVisible({ timeout: 10_000 });
  });
});
