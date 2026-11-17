import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Integrations - what the OAuth callbacks' redirects say.
//
// The GitHub App install callback lands on /integrations and the Connect
// callback on /integrations/<id>, on failure too, rather than rendering an
// error page: an unread `error` param is a failure the user never sees.
// ---------------------------------------------------------------------------

/** The toast viewport: the only `<ol tabindex="-1">` on the page. */
function toast(page: Page) {
  return page.locator('ol[tabindex="-1"] > li');
}

const INSTALL_ERRORS = [
  { error: 'github_token_exchange_failed', text: /did not accept the code of this installation/i },
  { error: 'github_no_token', text: /answered this installation without an access token/i },
  { error: 'no_github_credentials', text: /No GitHub OAuth app is configured to complete this installation/i },
  { error: 'something_else', text: /authorization could not be completed/i },
];

test.describe('Integrations - OAuth redirect messages', () => {
  for (const { error, text } of INSTALL_ERRORS) {
    test(`the Integrations page says why a GitHub App install failed (${error})`, async ({ page }) => {
      await page.goto(`/integrations?error=${error}`);

      await expect(toast(page).getByText('GitHub App installation failed').first()).toBeVisible();
      await expect(toast(page).getByText(text).first()).toBeVisible();
      // The redirect's `error` is removed; the landed `?vis=` stays.
      await expect(page).toHaveURL(/\/integrations(\?vis=[a-z]+)?$/);
    });
  }

  test('the Integrations page says where an install from GitHub is recorded', async ({ page }) => {
    await page.goto('/integrations?github_installed=true');

    await expect(toast(page).getByText('GitHub App installed').first()).toBeVisible();
    await expect(page).toHaveURL(/\/integrations(\?vis=[a-z]+)?$/);
  });

  test("an integration's page says its OAuth authorization failed and keeps its other parameters", async ({ page }) => {
    const id = '00000000-0000-4000-8000-000000004775';
    await page.goto(`/integrations/${id}?error=token_exchange_failed&provider=github&keep=1`);

    await expect(toast(page).getByText('OAuth failed').first()).toBeVisible();
    await expect(toast(page).getByText(/authorization could not be completed/i).first()).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/integrations/${id}\\?keep=1$`));
  });
});
