import { test, expect, type Page } from '@playwright/test';
import { BASE_URL } from './constants';
import { createAdminApiContext } from './helpers';
import { LoginPage } from './model/login.page';

// ---------------------------------------------------------------------------
// Opening the waiting arcade from a link: `?arcade=play` on any route,
// what the chat embedded in OpenCTI / OpenAEV / OpenCRQ opens. The request has
// to survive every way a tab ends up signed in, and start one game at most.
// ---------------------------------------------------------------------------

const arcade = (page: Page) => page.getByRole('dialog', { name: 'Space Invaders arcade' });
const leaderboard = (page: Page) => page.getByRole('dialog', { name: 'Arcade', exact: true });

/** An absence has no event to wait for: leave a request the time to start a game. */
const SETTLE_MS = 2_000;

/** Answer the requests matching `pattern` late, the way a busy server would. */
async function delay(page: Page, pattern: string, ms: number): Promise<void> {
  await page.route(pattern, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, ms));
    // The tab may have left the page meanwhile, which cancels the request.
    await route.continue().catch(() => undefined);
  });
}

test.describe('Arcade link - signed in', () => {
  test('opens the arcade once and leaves the address bar', async ({ page }) => {
    await page.goto('/chat?arcade=play&from=e2e');
    await expect(arcade(page)).toBeVisible();
    await expect(page).toHaveURL(/\/chat\?from=e2e$/);

    await page.reload();
    await expect(page.locator('textarea').first()).toBeVisible();
    await page.waitForTimeout(SETTLE_MS);
    await expect(arcade(page)).toHaveCount(0);
  });

  test('opens the leaderboard, not a game, and leaves the address bar', async ({ page }) => {
    // What the reload offered for a leaderboard the page could not load opens.
    await page.goto('/chat?arcade=leaderboard&from=e2e');
    await expect(leaderboard(page)).toBeVisible();
    await expect(page).toHaveURL(/\/chat\?from=e2e$/);
    await expect(arcade(page)).toHaveCount(0);
  });

  test('waits for /auth/config, then drops the request when the arcade is off', async ({ page }) => {
    let answer = () => {};
    const answered = new Promise<void>((resolve) => {
      answer = resolve;
    });
    await page.route('**/api/v1/auth/config', async (route) => {
      const response = await route.fetch();
      const config = await response.json();
      await answered;
      await route.fulfill({ response, json: { ...config, arcade_enabled: false } });
    });

    await page.goto('/chat?arcade=play');
    await expect(page.locator('textarea').first()).toBeVisible();
    await page.waitForTimeout(SETTLE_MS);
    await expect(arcade(page)).toHaveCount(0);
    await expect(page).toHaveURL(/[?&]arcade=play/);

    answer();
    await expect(page).toHaveURL(/\/chat$/);
    await page.waitForTimeout(SETTLE_MS);
    await expect(arcade(page)).toHaveCount(0);
  });
});

test.describe('Arcade link - signed out', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test('survives the redirect to the sign-in page', async ({ page }) => {
    await page.goto('/chat?arcade=play');
    await expect(page).toHaveURL(/\/login$/);
    await new LoginPage(page).submit();
    await expect(arcade(page)).toBeVisible();
  });

  test('survives the single sign-on round trip', async ({ page }) => {
    const { apiCtx, token } = await createAdminApiContext();
    await apiCtx.dispose();

    await page.goto('/chat?arcade=play');
    await expect(page).toHaveURL(/\/login$/);
    // The identity provider: the tab leaves the platform, then comes back to
    // the callback with the session in the fragment.
    await page.goto('about:blank');
    await page.goto(`/login#token=${token}`);
    await expect(arcade(page)).toBeVisible();
  });
});

test.describe('Arcade link - a session the server no longer accepts', () => {
  test.use({
    storageState: {
      cookies: [],
      origins: [
        { origin: new URL(BASE_URL).origin, localStorage: [{ name: 'copilot_token', value: 'expired-session' }] },
      ],
    },
  });

  test('survives a rejection that sends the tab to the sign-in page before the app is up', async ({ page }) => {
    // The token's first request answers 401 and reloads /login while the app
    // still waits for the platform config. That reload is a navigation of its
    // own, hence `commit`: waiting for `load` could see it interrupted.
    await delay(page, '**/api/v1/platform/config', 1_500);
    await page.goto('/chat?arcade=play', { waitUntil: 'commit' });
    await expect(page).toHaveURL(/\/login$/);
    await new LoginPage(page).submit();
    await expect(arcade(page)).toBeVisible();
  });

  test('never plays on the token before the server accepted it', async ({ page }) => {
    // The rejection lands after /auth/config answered: a game started then
    // would be lost with the reload to /login.
    await delay(page, '**/api/v1/auth/me', 1_500);
    await page.goto('/chat?arcade=play', { waitUntil: 'commit' });
    await expect(page).toHaveURL(/\/login$/);
    await new LoginPage(page).submit();
    await expect(arcade(page)).toBeVisible();
  });
});
