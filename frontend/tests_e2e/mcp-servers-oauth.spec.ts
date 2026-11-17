import { test, expect, type Page } from '@playwright/test';
import { MCPServerPage } from './model/mcp-server.page';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// MCP Servers — connecting a remote server over OAuth with **no**
// pre-provisioned client_id / client_secret.
//
// The MCP authorization spec is built so a client onboards from a URL alone:
// the platform discovers the authorization server (RFC 9728 -> RFC 8414) and
// registers itself dynamically (RFC 7591). These tests cover the UI half of
// that — the create form's "Detect settings", the credential-free Authorize
// button, and the connectivity test's third outcome, `auth_required`.
//
// Discovery and connectivity are stubbed at the network boundary: both make
// the *backend* talk to a third-party authorization server, which an E2E run
// has none of. Everything else — creating the server, reading it back, the
// OAuth status card — goes through the real API.
// ---------------------------------------------------------------------------

const SERVER_URL = 'https://mcp.e2e-example.com/mcp';

const DISCOVER_ROUTE = '**/api/v1/mcp-servers/oauth/discover';

/** A server that publishes metadata *and* offers dynamic registration. */
const DCR_CAPABLE = {
  authorize_url: 'https://auth.e2e-example.com/oauth/authorize',
  token_url: 'https://auth.e2e-example.com/oauth/token',
  registration_url: 'https://auth.e2e-example.com/oauth/register',
  registration_url_advertised: true,
  resource: SERVER_URL,
  scopes: 'mcp:read mcp:write',
  issuer: 'https://auth.e2e-example.com',
  discovered: true,
  supports_dynamic_registration: true,
};

/** A server that publishes nothing — the operator has to type the URLs in. */
const NO_METADATA = {
  authorize_url: null,
  token_url: null,
  registration_url: null,
  registration_url_advertised: false,
  resource: null,
  scopes: null,
  issuer: null,
  discovered: false,
  supports_dynamic_registration: false,
};

/**
 * The visible toast.
 *
 * Radix mirrors every toast into an off-screen `aria-live` region, so an
 * unscoped `getByText` matches the same message twice. The viewport is the
 * only `<ol tabindex="-1">` on the page and the live region sits outside it.
 */
function toast(page: Page) {
  return page.locator('ol[tabindex="-1"] > li');
}

async function stubDiscovery(page: Page, body: unknown): Promise<void> {
  await page.route(DISCOVER_ROUTE, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }),
  );
}

/** Walk the create form up to the point where the OAuth block is showing. */
async function openOAuthForm(page: Page, name: string): Promise<MCPServerPage> {
  const mcpPage = new MCPServerPage(page);
  await mcpPage.gotoList();
  await mcpPage.clickNew();
  await mcpPage.selectCustom();
  await mcpPage.fillName(name);
  await mcpPage.selectStreamableHttp();
  await mcpPage.fillUrl(SERVER_URL);
  await mcpPage.selectOAuthAuth();
  return mcpPage;
}

test.describe.serial('MCP Servers — OAuth without pre-provisioned credentials', () => {
  const SERVER_NAME = `E2E MCP OAuth ${Date.now()}`;
  let serverId = '';

  test('should render the app when /mcp-servers is loaded directly', async ({ page }) => {
    // The OAuth callback redirects the browser straight to /mcp-servers/{id},
    // so a cold load of this path must reach the SPA. The dev proxy used to
    // match "/mcp" as a prefix and answer with the backend's 404 JSON instead.
    const response = await page.goto('/mcp-servers');

    expect(response?.headers()['content-type'] ?? '').toContain('text/html');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Add Server' }).first()).toBeVisible();
  });

  test('should fill the OAuth endpoints a server advertises', async ({ page }) => {
    await stubDiscovery(page, DCR_CAPABLE);
    const mcpPage = await openOAuthForm(page, SERVER_NAME);

    // Credentials are no longer the headline: the form leads with the
    // endpoints and says the client ID is optional.
    await expect(page.getByText(/Client credentials are optional/)).toBeVisible();

    await mcpPage.detectSettingsButton().click();

    await expect(mcpPage.authorizeUrlInput()).toHaveValue(DCR_CAPABLE.authorize_url);
    await expect(mcpPage.tokenUrlInput()).toHaveValue(DCR_CAPABLE.token_url);
    await expect(mcpPage.registrationUrlInput()).toHaveValue(DCR_CAPABLE.registration_url);
    await expect(mcpPage.scopesInput()).toHaveValue(DCR_CAPABLE.scopes);
    await expect(
      page.getByText(/supports automatic client registration/),
    ).toBeVisible();
  });

  test('should not overwrite an endpoint the operator typed in', async ({ page }) => {
    await stubDiscovery(page, DCR_CAPABLE);
    const mcpPage = await openOAuthForm(page, SERVER_NAME);

    const typed = 'https://tenant.e2e-example.com/oauth/authorize';
    await mcpPage.authorizeUrlInput().fill(typed);
    await mcpPage.detectSettingsButton().click();

    // Detection fills blanks only — a hand-entered value was entered on
    // purpose and outranks whatever the server advertises.
    await expect(mcpPage.tokenUrlInput()).toHaveValue(DCR_CAPABLE.token_url);
    await expect(mcpPage.authorizeUrlInput()).toHaveValue(typed);
  });

  test('should say so when a server publishes no OAuth metadata', async ({ page }) => {
    await stubDiscovery(page, NO_METADATA);
    const mcpPage = await openOAuthForm(page, SERVER_NAME);

    await mcpPage.detectSettingsButton().click();

    await expect(page.getByText(/No OAuth metadata published by this server/)).toBeVisible();
    await expect(mcpPage.authorizeUrlInput()).toHaveValue('');
  });

  test('should create an OAuth server with no client ID or secret', async ({ page }) => {
    const mcpPage = await openOAuthForm(page, SERVER_NAME);

    // Deliberately nothing else: no endpoints, no credentials. This is the
    // A remote MCP server configurable from its URL.
    await mcpPage.clickCreate();
    await mcpPage.waitForDetail();

    serverId = mcpPage.currentServerId();
    await expect(page.getByText(SERVER_NAME).first()).toBeVisible({ timeout: 10_000 });
  });

  test('should offer Authorize even though no client is registered', async ({ page }) => {
    test.skip(!serverId, 'server was not created');
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoUrl(`/mcp-servers/${serverId}`);
    await mcpPage.openConfigTab();

    await expect(page.getByText(/Not registered yet/)).toBeVisible();
    // Previously gated on a stored client ID, which made a credential-free
    // server impossible to authorize at all.
    await expect(page.getByRole('button', { name: 'Authorize', exact: true })).toBeVisible();
    await expect(page.getByText(/No client credentials needed/)).toBeVisible();
  });

  test('should report a reachable but unauthorized server as auth_required', async ({ page }) => {
    test.skip(!serverId, 'server was not created');
    // A server that answers tools/list to anyone and gates tools/call behind a
    // token: reported as a plain success, it produced a green test followed by
    // "Authorization required" on every real call.
    await page.route(`**/api/v1/mcp-servers/${serverId}/test`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'auth_required',
          tools: [
            { name: 'search', description: 'Search' },
            { name: 'fetch', description: 'Fetch' },
          ],
          tool_count: 2,
          message: 'This server requires authorization.',
          oauth_configured: true,
        }),
      }),
    );

    const mcpPage = new MCPServerPage(page);
    // Stays on Overview, where the OAuth status card — which carries the same
    // "Authorization required" wording — is not rendered.
    await mcpPage.gotoUrl(`/mcp-servers/${serverId}`);
    await expect(page.getByRole('button', { name: 'Test connection' })).toBeVisible();
    await mcpPage.clickTestConnection();

    await expect(toast(page).getByText('Authorization required')).toBeVisible();
    await expect(toast(page).getByText(/refuses tool calls until you authorize it/)).toBeVisible();
    await expect(toast(page).getByText('Connection successful')).toHaveCount(0);
  });

  test('should confirm the connection when returning from the OAuth redirect', async ({ page }) => {
    test.skip(!serverId, 'server was not created');
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoUrl(`/mcp-servers/${serverId}?connected=true`);

    // No `.first()` needed any more: the effect is guarded, so StrictMode's
    // double invocation no longer raises the toast twice in dev. The count is
    // asserted on its own below, in "should announce a successful redirect
    // exactly once".
    await expect(toast(page).getByText('OAuth connected')).toBeVisible();
    // The query param is consumed so a reload does not re-announce it.
    await expect(page).toHaveURL(new RegExp(`/mcp-servers/${serverId}$`));
  });

  // One failure, one toast. The mutation raises a message naming what to fix,
  // and the global MutationCache handler in main.tsx raises a vaguer "Error /
  // Request failed" for every failed mutation — so without opting out, a
  // single click stacked two.
  test('should raise exactly one toast when Authorize fails', async ({ page }) => {
    test.skip(!serverId, 'server was not created');
    const mcpPage = new MCPServerPage(page);
    await page.route(`**/api/v1/mcp-servers/${serverId}/authorize`, (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'boom' }),
      }),
    );
    await mcpPage.gotoUrl(`/mcp-servers/${serverId}`);
    await mcpPage.openConfigTab();

    await page.getByRole('button', { name: 'Authorize', exact: true }).click();

    await expect(toast(page).getByText('OAuth failed')).toBeVisible();
    await expect(toast(page)).toHaveCount(1);
  });

  // Same rule for the redirect announcement, which React.StrictMode used to
  // raise twice in development by double-invoking the effect.
  test('should announce a successful redirect exactly once', async ({ page }) => {
    test.skip(!serverId, 'server was not created');
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoUrl(`/mcp-servers/${serverId}?connected=true`);

    await expect(toast(page).getByText('OAuth connected')).toBeVisible();
    await expect(toast(page)).toHaveCount(1);
  });

  // A disabled Create button cannot state why it is disabled, so an empty name
  // on a long form reads as "the button is broken" — the name field is at the
  // top, the button at the bottom.
  test('should name the missing field instead of disabling Create', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await mcpPage.clickNew();
    await mcpPage.selectCustom();

    const create = page.getByRole('button', { name: 'Create Server' });
    await expect(create).toBeEnabled();

    await create.click();

    await expect(page.getByText('Enter a name for this server.')).toBeVisible();
    await expect(page.getByPlaceholder('e.g. GitHub MCP')).toBeFocused();
    // Still on the form — nothing was submitted.
    await expect(page.getByRole('heading', { name: /Custom MCP Server/ })).toBeVisible();

    // Typing clears it again rather than leaving a stale error on screen.
    await mcpPage.fillName('named at last');
    await expect(page.getByText('Enter a name for this server.')).toBeHidden();
  });

  // The callback redirects here on failure too rather than rendering an error
  // page, so an unread `error` param is a failure the user never sees: they
  // land on a page that looks normal, still showing "Not connected".
  test('should report a failed token exchange from the OAuth redirect', async ({ page }) => {
    test.skip(!serverId, 'server was not created');
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoUrl(`/mcp-servers/${serverId}?error=token_exchange_failed`);

    await expect(toast(page).getByText('OAuth failed').first()).toBeVisible();
    await expect(
      toast(page).getByText(/authorization could not be completed/i).first(),
    ).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/mcp-servers/${serverId}$`));
  });

  test('should name the cause when the token endpoint returns no token', async ({ page }) => {
    test.skip(!serverId, 'server was not created');
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoUrl(`/mcp-servers/${serverId}?error=no_access_token`);

    // Distinct from the generic message: this one points at the Token URL and
    // scopes, which is where the fault actually is.
    await expect(
      toast(page).getByText(/returned no access token/i).first(),
    ).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/mcp-servers/${serverId}$`));
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.get('/api/v1/mcp-servers?limit=500', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (resp.ok()) {
        const servers = await resp.json();
        const target = servers.find((s: { name: string; id: string }) => s.name === SERVER_NAME);
        if (target) {
          await apiCtx.delete(`/api/v1/mcp-servers/${target.id}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
        }
      }
    } finally {
      await apiCtx.dispose();
    }
  });
});

// ---------------------------------------------------------------------------
// MCP Servers — a **company-managed** server in OAuth mode.
//
// The card used to say "Authorization required. Click the button below" while
// rendering no button at all: the Authorize button was hidden for every
// company-managed server, and the backend refused the flow outright — so a
// company-managed OAuth server could be created and never connected. The
// connection is a shared one (its token lives on the server row, like the
// shared API key of a header-authenticated company-managed server), which is
// why an admin, and only an admin, can authorize it.
// ---------------------------------------------------------------------------

test.describe.serial('MCP Servers — company-managed OAuth server', () => {
  const SERVER_NAME = `E2E MCP Company OAuth ${Date.now()}`;
  let serverId = '';

  test.beforeAll(async () => {
    // Created through the API: this test is about the detail page, and the
    // company-managed flag needs an admin either way.
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.post('/api/v1/mcp-servers', {
        headers: { Authorization: `Bearer ${token}` },
        data: {
          name: SERVER_NAME,
          transport: 'streamable_http',
          url: SERVER_URL,
          auth_mode: 'oauth',
          is_company_managed: true,
        },
      });
      expect(resp.status(), await resp.text()).toBe(201);
      serverId = (await resp.json()).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should offer Authorize to an admin and say the connection is shared', async ({ page }) => {
    test.skip(!serverId, 'server was not created');
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoUrl(`/mcp-servers/${serverId}`);
    await mcpPage.openConfigTab();

    await expect(page.getByRole('button', { name: 'Authorize', exact: true })).toBeVisible();
    await expect(page.getByText(/shared by every user of the platform/)).toBeVisible();
    // And the card no longer points at a button it does not render.
    await expect(page.getByText(/Click the button below to authorize/)).toBeVisible();
  });

  test.afterAll(async () => {
    if (!serverId) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/mcp-servers/${serverId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    } finally {
      await apiCtx.dispose();
    }
  });
});
