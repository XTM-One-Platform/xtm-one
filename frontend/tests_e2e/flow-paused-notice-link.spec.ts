import { test, expect } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { REGULAR_USER_PASSWORD, regularUserEmail } from './constants';

test.describe.serial('Flows - paused-flow notice links to Owner and access', () => {
  let flowId = '';
  let userId = '';

  test.beforeAll(async () => {
    const email = regularUserEmail('flow-paused-link', test.info().parallelIndex);
    const { apiCtx, token } = await createAdminApiContext();
    const headers = { Authorization: `Bearer ${token}` };
    try {
      const users: { id: string; email: string }[] = await (await apiCtx.get('/api/v1/users?limit=500', { headers })).json();
      const leftover = users.find((u) => u.email === email);
      if (leftover) await apiCtx.delete(`/api/v1/users/${leftover.id}`, { headers });
      userId = (
        await (
          await apiCtx.post('/api/v1/users', {
            headers,
            data: { email, password: REGULAR_USER_PASSWORD, full_name: 'E2E Paused Owner', role: 'user', account_type: 'local' },
          })
        ).json()
      ).id;
      flowId = (await (await apiCtx.post('/api/v1/flows', { headers, data: { name: `E2E Paused Link ${Date.now()}` } })).json()).id;
      expect((await apiCtx.put(`/api/v1/flows/${flowId}`, { headers, data: { user_id: userId } })).ok()).toBeTruthy();
      // Toggles the account: deactivated from here on.
      expect((await apiCtx.put(`/api/v1/users/${userId}/active`, { headers })).ok()).toBeTruthy();
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    const headers = { Authorization: `Bearer ${token}` };
    try {
      if (flowId) await apiCtx.delete(`/api/v1/flows/${flowId}`, { headers });
      if (userId) await apiCtx.delete(`/api/v1/users/${userId}`, { headers });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('an admin gets a link to the Owner and access drawer in the notice', async ({ page }) => {
    await page.goto(`/flows/library/${flowId}`);
    await expect(page.getByText("Paused: the owner's account is deactivated").first()).toBeVisible();
    await expect(page.getByRole('link', { name: 'Owner and access' })).toHaveAttribute(
      'href',
      new RegExp(`/flows/library/${flowId}\\?panel=owner-access$`),
    );
  });
});
