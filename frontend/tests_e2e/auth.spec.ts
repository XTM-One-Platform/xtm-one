import { test, expect } from '@playwright/test';
import { LoginPage } from './model/login.page';

// ---------------------------------------------------------------------------
// Authentication — redirect, login, invalid creds, and logout flow.
// Extends login.spec.ts with the logout round-trip.
// ---------------------------------------------------------------------------

test.describe('Auth — logout', () => {
  test('should log out and redirect to /login', async ({ page }) => {
    // Already authenticated via storageState; go to dashboard first.
    await page.goto('/');
    await expect(page).toHaveURL('/');

    const loginPage = new LoginPage(page);
    await loginPage.logout();

    await expect(page).toHaveURL(/\/login/);
  });

  test('should not be able to reach a protected page after logout', async ({ page }) => {
    await page.goto('/');
    const loginPage = new LoginPage(page);
    await loginPage.logout();

    // Attempt to navigate directly to a protected route.
    await page.goto('/agents');
    await expect(page).toHaveURL(/\/login/);
  });
});
