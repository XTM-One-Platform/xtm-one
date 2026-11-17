import { test, expect } from '@playwright/test';
import { LoginPage } from './model/login.page';

test.use({ storageState: { cookies: [], origins: [] } });

test.describe('Login', () => {
  test('should redirect unauthenticated user to login', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/login/);
  });

  test('should login with valid credentials', async ({ page }) => {
    const loginPage = new LoginPage(page);
    await loginPage.goto();
    await loginPage.login();
    await expect(page).not.toHaveURL(/\/login/);
  });

  test('should show error with invalid credentials', async ({ page }) => {
    const loginPage = new LoginPage(page);
    await loginPage.goto();

    await page.getByPlaceholder('user@mail.com').fill('wrong@user.com');
    await page.getByPlaceholder('••••••••').fill('wrongpassword');
    await page.getByRole('button', { name: 'Sign In' }).click();

    await expect(page.getByRole('paragraph').filter({ hasText: 'Invalid credentials' })).toBeVisible({ timeout: 10_000 });
  });

  // A federated sign-in that does not complete comes back here carrying a
  // marker instead of a token. Everything else about that path is
  // covered by backend tests; this is the only place the page a refused person
  // actually sees is exercised, which is what FR-007 is about.
  test.describe('SSO failure markers', () => {
    const cases = [
      { marker: 'not_authorised', text: /not authorised to sign in to this instance/i },
      { marker: 'account_deactivated', text: /This account has been deactivated/i },
      { marker: 'cancelled', text: /cancelled/i },
      { marker: 'misconfigured', text: /not configured correctly/i },
      { marker: 'failed', text: /Single sign-on failed/i },
      // Not a marker the backend emits. The page must still say something
      // rather than render an empty banner or leak the raw value.
      { marker: 'something_unexpected', text: /Single sign-on failed/i },
      // Inherited Object keys. A plain `messages[marker] ?? fallback` resolves
      // these to a function, which React runs as a state updater — the banner
      // then renders "[object Undefined]" instead of a message.
      { marker: 'toString', text: /Single sign-on failed/i },
      { marker: 'constructor', text: /Single sign-on failed/i },
      { marker: 'valueOf', text: /Single sign-on failed/i },
    ];

    for (const { marker, text } of cases) {
      test(`shows a readable message for ?sso_error=${marker}`, async ({ page }) => {
        await page.goto(`/login?sso_error=${marker}`);

        await expect(page.getByRole('paragraph').filter({ hasText: text })).toBeVisible({ timeout: 10_000 });
        // No raw error body, and nothing about how the IdP is configured.
        await expect(page.locator('body')).not.toContainText('{"detail"');
        await expect(page.locator('body')).not.toContainText('_groups');
      });
    }

    // The case that made this whole block necessary: an SSO-only deployment
    // renders no local-auth form, and the failure message used to live inside
    // it. Every test above passes with local auth enabled, so none of them
    // would have caught a refused person seeing a blank page.
    test('shows the message on an SSO-only deployment', async ({ page }) => {
      await page.route('**/api/v1/auth/config', async (route) => {
        const response = await route.fetch();
        const config = await response.json();
        await route.fulfill({
          json: { ...config, local_enabled: false, oidc_enabled: true, oidc_display_name: 'XTM Hub' },
        });
      });

      await page.goto('/login?sso_error=not_authorised');

      // Wait for something that only exists once the intercepted config has
      // been applied, BEFORE asserting the local form is gone. `toHaveCount(0)`
      // is satisfied by an element React has not rendered yet, so on its own it
      // would pass against a page that goes on to render the whole form.
      await expect(page.getByRole('button', { name: /Continue with XTM Hub/i })).toBeVisible({ timeout: 10_000 });
      await expect(page.getByPlaceholder('user@mail.com')).toHaveCount(0);
      await expect(
        page.getByRole('paragraph').filter({ hasText: /not authorised to sign in to this instance/i }),
      ).toBeVisible({ timeout: 10_000 });
    });

    test('scrubs the marker so a reload does not re-accuse', async ({ page }) => {
      await page.goto('/login?sso_error=not_authorised');
      await expect(page.getByRole('paragraph').filter({ hasText: /not authorised/i })).toBeVisible({ timeout: 10_000 });

      // Consumed on arrival: the address bar is clean straight away, so the
      // message cannot outlive the sign-in attempt that caused it.
      await expect(page).toHaveURL(/\/login$/);

      await page.reload();
      await expect(page.getByRole('paragraph').filter({ hasText: /not authorised/i })).toHaveCount(0);
    });
  });
});
