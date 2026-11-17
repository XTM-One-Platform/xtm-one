import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// The arcade from the user menu: outside an agent wait, the user menu
// opens the game and its leaderboard, and offers neither when the arcade is off
// or before /auth/config said whether it is on.
// ---------------------------------------------------------------------------

const arcade = (page: Page) => page.getByRole('dialog', { name: 'Space Invaders arcade' });
// Exact: a name match is a case-insensitive substring, which the game's
// "Space Invaders arcade" would satisfy too.
const leaderboard = (page: Page) => page.getByRole('dialog', { name: 'Arcade', exact: true });
const playEntry = (page: Page) => page.getByTestId('user-menu-arcade-play');
const leaderboardEntry = (page: Page) => page.getByTestId('user-menu-arcade-leaderboard');

/** The menu's trigger is the last button of the top bar while the menu is closed. */
async function openUserMenu(page: Page): Promise<void> {
  await page.goto('/');
  await page.locator('header button').last().click();
  await expect(page.getByText('Sign out')).toBeVisible();
}

test.describe('Arcade from the user menu', () => {
  // A route callback still inside route.fetch when a test ends is reported as an
  // error outside any test and tears the worker down (#9175): end them all.
  test.afterEach(async ({ page }) => {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
  });

  test('plays Space Invaders and closes the menu', async ({ page }) => {
    await openUserMenu(page);
    await playEntry(page).click();
    await expect(arcade(page)).toBeVisible();
    await expect(playEntry(page)).toHaveCount(0);
  });

  test('opens the leaderboard and closes the menu', async ({ page }) => {
    await openUserMenu(page);
    await leaderboardEntry(page).click();
    await expect(leaderboard(page)).toBeVisible();
    await expect(leaderboardEntry(page)).toHaveCount(0);
  });

  test('offers neither entry when the arcade is off', async ({ page }) => {
    await page.route('**/api/v1/auth/config', async (route) => {
      const response = await route.fetch();
      const config = await response.json();
      await route.fulfill({ response, json: { ...config, arcade_enabled: false } });
    });
    // The absence is asserted once the patched config answered, not before it loads.
    const configAnswered = page.waitForResponse('**/api/v1/auth/config');
    await openUserMenu(page);
    await configAnswered;
    await expect(playEntry(page)).toHaveCount(0);
    await expect(leaderboardEntry(page)).toHaveCount(0);
  });

  test('offers neither entry until /auth/config answers', async ({ page }) => {
    let answer = () => {};
    const answered = new Promise<void>((resolve) => {
      answer = resolve;
    });
    await page.route('**/api/v1/auth/config', async (route) => {
      const response = await route.fetch();
      await answered;
      await route.fulfill({ response });
    });
    await openUserMenu(page);
    await expect(playEntry(page)).toHaveCount(0);
    await expect(leaderboardEntry(page)).toHaveCount(0);

    answer();
    await expect(playEntry(page)).toBeVisible();
    await expect(leaderboardEntry(page)).toBeVisible();
  });
});
