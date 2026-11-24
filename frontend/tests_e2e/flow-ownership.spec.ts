import { test, expect, type APIRequestContext } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { REGULAR_USER_PASSWORD, regularUserEmail } from './constants';
import { LoginPage } from './model/login.page';

// ---------------------------------------------------------------------------
// Flow builder — Owner and access.
//
// The admin (storageState) opens a flow it created, makes it company-managed
// and back through the drawer's switch, then transfers it to a regular user
// through the Owner card. The regular user, now its owner, opens the same
// builder and must not be offered the drawer: both acts are admin-only.
// Every step is checked against the API, not only the toast.
// ---------------------------------------------------------------------------

test.describe.serial('Flows — owner and access', () => {
  const FLOW_NAME = `E2E Ownership Flow ${Date.now()}`;
  let flowId = '';
  let regularUserId = '';
  let regularEmail = '';

  async function readFlow(): Promise<{ user_id: string | null; is_company_managed: boolean }> {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.get(`/api/v1/flows/${flowId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(resp.ok()).toBeTruthy();
      return await resp.json();
    } finally {
      await apiCtx.dispose();
    }
  }

  async function deleteUserByEmail(apiCtx: APIRequestContext, headers: Record<string, string>) {
    const listResp = await apiCtx.get('/api/v1/users?limit=500', { headers });
    if (!listResp.ok()) return;
    const users: { id: string; email: string }[] = await listResp.json();
    const leftover = users.find((u) => u.email === regularEmail);
    if (leftover) await apiCtx.delete(`/api/v1/users/${leftover.id}`, { headers });
  }

  test.beforeAll(async () => {
    regularEmail = regularUserEmail('flow-ownership', test.info().parallelIndex);
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const headers = { Authorization: `Bearer ${token}` };
      await deleteUserByEmail(apiCtx, headers);
      const userResp = await apiCtx.post('/api/v1/users', {
        headers,
        data: {
          email: regularEmail,
          password: REGULAR_USER_PASSWORD,
          full_name: 'E2E Flow Owner',
          role: 'user',
          account_type: 'local',
        },
      });
      if (!userResp.ok()) {
        throw new Error(`Failed to create the regular user: ${userResp.status()} ${await userResp.text()}`);
      }
      regularUserId = (await userResp.json()).id;

      const flowResp = await apiCtx.post('/api/v1/flows', { headers, data: { name: FLOW_NAME } });
      if (!flowResp.ok()) {
        throw new Error(`Failed to create the flow: ${flowResp.status()} ${await flowResp.text()}`);
      }
      flowId = (await flowResp.json()).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const headers = { Authorization: `Bearer ${token}` };
      if (flowId) await apiCtx.delete(`/api/v1/flows/${flowId}`, { headers });
      if (regularUserId) await apiCtx.delete(`/api/v1/users/${regularUserId}`, { headers });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('?panel=owner-access opens the drawer for an admin and is consumed', async ({ page }) => {
    // The paused-flow notice links here: the URL is the contract.
    await page.goto(`/flows/library/${flowId}?panel=owner-access`);
    await expect(page.getByRole('complementary', { name: 'Owner and access' })).toBeVisible();
    await expect(page).not.toHaveURL(/panel=owner-access/);
  });

  test('an admin makes the flow company-managed and back from the builder', async ({ page }) => {
    await page.goto(`/flows/library/${flowId}`);
    await page.getByRole('button', { name: 'Owner and access' }).click();
    const drawer = page.getByRole('complementary', { name: 'Owner and access' });
    await expect(drawer).toBeVisible();

    const toggle = drawer.getByRole('switch', { name: 'Company-managed flow' });
    await expect(toggle).toHaveAttribute('data-state', 'unchecked');
    await toggle.click();
    await expect(toggle).toHaveAttribute('data-state', 'checked');
    await expect.poll(async () => (await readFlow()).is_company_managed).toBe(true);

    await toggle.click();
    await expect(toggle).toHaveAttribute('data-state', 'unchecked');
    await expect.poll(async () => (await readFlow()).is_company_managed).toBe(false);
  });

  test('an admin transfers the flow to another active user', async ({ page }) => {
    await page.goto(`/flows/library/${flowId}`);
    await page.getByRole('button', { name: 'Owner and access' }).click();
    const drawer = page.getByRole('complementary', { name: 'Owner and access' });

    await drawer.getByRole('combobox').click();
    await page.getByRole('option', { name: new RegExp(regularEmail.replace(/[.+]/g, '\\$&')) }).click();
    await expect.poll(async () => (await readFlow()).user_id).toBe(regularUserId);
  });

  test('the new owner, not an admin, is not offered the drawer', async ({ page }) => {
    await page.context().clearCookies();
    await page.goto('/login');
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
    await new LoginPage(page).login(regularEmail, REGULAR_USER_PASSWORD);

    await page.goto(`/flows/library/${flowId}`);
    await expect(page.getByRole('textbox', { name: 'Flow name' })).toHaveValue(FLOW_NAME);
    await expect(page.getByRole('button', { name: 'Owner and access' })).toHaveCount(0);

    // The deep link does not open it for them either, and is still consumed.
    await page.goto(`/flows/library/${flowId}?panel=owner-access`);
    await expect(page.getByRole('textbox', { name: 'Flow name' })).toHaveValue(FLOW_NAME);
    await expect(page).not.toHaveURL(/panel=owner-access/);
    await expect(page.getByRole('complementary', { name: 'Owner and access' })).toHaveCount(0);
  });
});
