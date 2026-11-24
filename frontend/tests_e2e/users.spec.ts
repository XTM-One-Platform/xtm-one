import { test, expect } from '@playwright/test';
import { UserManagementPage } from './model/user-mgmt.page';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Users — admin verifies local user management UI.
// User creation uses the API directly (the React 19 + serial-mode combination
// prevents reliable form submission via Playwright fill()), so tests focus on
// the list and detail views that matter for the admin workflow.
// ---------------------------------------------------------------------------

test.describe.serial('Users — admin management', () => {
  const USER_EMAIL = `e2e-test-${Date.now()}@e2e-example.com`;
  const USER_NAME = `E2E Test User ${Date.now()}`;
  let createdUserId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.post('/api/v1/users', {
        headers: { Authorization: `Bearer ${token}` },
        data: { email: USER_EMAIL, full_name: USER_NAME, password: 'E2eTestPwd!9', role: 'user', account_type: 'local' },
      });
      if (!resp.ok()) {
        throw new Error(`Failed to create test user in beforeAll: ${resp.status()} ${resp.statusText()}`);
      }
      createdUserId = (await resp.json()).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should display the users list page (admin only)', async ({ page }) => {
    const userPage = new UserManagementPage(page);
    await userPage.gotoList();
    await expect(page).toHaveURL('/users-and-groups');
    await expect(page.getByRole('button', { name: 'New User' })).toBeVisible();
  });

  test('should navigate to the new user form', async ({ page }) => {
    const userPage = new UserManagementPage(page);
    await userPage.gotoList();
    await userPage.clickNewUser();
    await expect(page.getByRole('heading', { name: 'New User' })).toBeVisible();
  });

  test('should show the new user in the users list', async ({ page }) => {
    const userPage = new UserManagementPage(page);
    await userPage.gotoList();
    await expect(page.getByText(USER_EMAIL)).toBeVisible({ timeout: 10_000 });
  });

  test('should show user detail with role badge', async ({ page }) => {
    const userPage = new UserManagementPage(page);
    await userPage.gotoList();
    await page.getByText(USER_EMAIL).first().click();
    await userPage.waitForDetail();
    await expect(page.getByText(/^User$|^Admin$/i).first()).toBeVisible({ timeout: 10_000 });
  });

  test.afterAll(async () => {
    if (!createdUserId) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/users/${createdUserId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    } finally {
      await apiCtx.dispose();
    }
  });
});
