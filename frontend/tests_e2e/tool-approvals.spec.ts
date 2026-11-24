import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Tool approvals: one home per decision, and nothing shown while nothing asks.
//
// The platform's side is Settings > Security > Tool approvals, a person's own
// approvals are Profile > Tool approvals, and the old Tools > Approvals tab
// redirects. Whether approvals are in use, and who asks, are fixtures, so the
// spec pins what the screens say for them rather than this stack's agents.
// Read-only: nothing here changes a setting.
// ---------------------------------------------------------------------------

const USAGE_PATH = '/api/v1/tool-approvals/usage';
const MINE_PATH = '/api/v1/tool-approvals';

function usage(overrides: Record<string, unknown> = {}) {
  return {
    in_use: false,
    configured: false,
    suspended: false,
    asks_by_default: false,
    general_assistant_available: false,
    general_assistant_asks: false,
    asking_agents: [],
    agents_following_default: 0,
    agents_set_to_ask: 0,
    agents_set_not_to_ask: 0,
    ...overrides,
  };
}

function mine(overrides: Record<string, unknown> = {}) {
  return {
    approve_all_tools: false,
    tool_approvals_enabled: true,
    approve_tool_calls_default: false,
    approvals_in_use: false,
    preferences: [],
    recently_invalidated: [],
    ...overrides,
  };
}

async function serve(page: Page, pathname: string, body: unknown) {
  await page.route(
    (url: URL) => url.pathname === pathname,
    (route: Route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }),
  );
}

const status = (page: Page) => page.getByTestId('tool-approvals-status');
const approvalsTab = (page: Page) => page.locator('a[href$="/profile/approvals"]');

test.describe('Tool approvals', () => {
  test('an administrator opens the platform rules at their own address', async ({ page }) => {
    await serve(page, USAGE_PATH, usage());
    await page.goto('/settings/security/approvals');
    await expect(page).toHaveURL(/\/settings\/security\/approvals$/);
    await expect(status(page)).toHaveAttribute('data-state', 'not_in_use');
    await expect(page.getByText('No agent asks before running tools, so every tool call runs as it always has.')).toBeVisible();
  });

  test('the status names the General Assistant beside the agents that ask', async ({ page }) => {
    await serve(
      page,
      USAGE_PATH,
      usage({
        in_use: true,
        configured: true,
        asks_by_default: true,
        general_assistant_available: true,
        general_assistant_asks: true,
        asking_agents: [{ id: '00000000-0000-4000-8000-000000005167', name: 'E2E Mail Desk', follows_default: true }],
        agents_following_default: 1,
      }),
    );
    await page.goto('/settings/security/approvals');
    await expect(status(page)).toHaveAttribute('data-state', 'in_use');
    await expect(page.getByText('The General Assistant and 1 agent ask before running tools:')).toBeVisible();
    await expect(page.getByTestId('tool-approvals-asking')).toContainText('E2E Mail Desk');
  });

  test('without chat, turning the default on names only the agents it reaches', async ({ page }) => {
    await serve(page, USAGE_PATH, usage({ agents_following_default: 2 }));
    await page.goto('/settings/security/approvals');
    await expect(page.getByText('Applies to every agent left on Follow the platform default (2 agents today).', { exact: false })).toBeVisible();

    const defaultSwitch = page.getByRole('switch', { name: 'Every agent asks by default' });
    await expect(defaultSwitch).not.toBeChecked();
    await defaultSwitch.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('2 agents left on Follow the platform default will wait for a person');
    await expect(dialog).not.toContainText('General Assistant');
    // Cancelled: the spec changes no setting.
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(defaultSwitch).not.toBeChecked();
  });

  test('the profile offers Tool approvals only once something asks', async ({ page }) => {
    await serve(page, MINE_PATH, mine());
    await page.goto('/profile');
    await expect(page.getByText('Your tool approvals')).toHaveCount(0);
    await expect(approvalsTab(page)).toHaveCount(0);

    // A link to it still works, and says why nothing asks yet.
    await page.goto('/profile/approvals');
    await expect(approvalsTab(page)).toBeVisible();
    await expect(page.getByTestId('personal-approvals-not-in-use')).toBeVisible();
  });

  test('the profile tab appears while an agent asks', async ({ page }) => {
    await serve(page, MINE_PATH, mine({ approvals_in_use: true }));
    await page.goto('/profile');
    await expect(approvalsTab(page)).toBeVisible();
    await approvalsTab(page).click();
    await expect(page).toHaveURL(/\/profile\/approvals$/);
    await expect(page.getByText('Tools that ask first')).toBeVisible();
    await expect(page.getByTestId('personal-approvals-not-in-use')).toHaveCount(0);
  });
});
