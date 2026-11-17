import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Sharing an OAuth integration shares its connected account.
//
// The configuration card's "Don't share ... connected account" switch is the
// opt-out: OFF by default, ON only where the owner already chose it. Every
// save of an integration that ends up shared over OAuth carries the choice as
// `keep_oauth_credentials_on_share`, so an unrelated edit never flips it.
// Every API call about the integration is mocked, so the spec pins the
// PAYLOAD the card sends; the backend contract is covered by
// backend/tests/test_integration_oauth_sharing_guard.py.
// ---------------------------------------------------------------------------

const INTEGRATION_ID = '4b3a2c1d-0e9f-4a8b-9c7d-6e5f4a3b2c1d';
// Someone else's integration: the signed-in admin may choose for it.
const OWNER_ID = '9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a';
const GROUP_ID = '1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d';
const SWITCH_NAME = "Don't share the owner's connected account";

type Json = Record<string, unknown>;

function sharedOauthRow(overrides: Json = {}): Json {
  return {
    id: INTEGRATION_ID,
    user_id: OWNER_ID,
    owner_name: 'Integration Owner',
    provider: 'acme',
    auth_type: 'oauth',
    status: 'connected',
    name: 'E2E shared OAuth',
    description: null,
    is_company_managed: false,
    is_builtin: false,
    is_custom: false,
    shared_group_ids: [GROUP_ID],
    shared_group_permissions: { [GROUP_ID]: 'read_only' },
    shared_user_ids: null,
    shared_user_permissions: null,
    access_restricted: false,
    can_manage: true,
    tags: [],
    scopes: null,
    required_scopes: null,
    has_client_id: true,
    has_client_secret: true,
    has_access_token: true,
    has_api_key: false,
    has_extra_config: false,
    oauth_share_acknowledged: true,
    extra_config: null,
    scope_config: null,
    call_count: 0,
    last_called_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

const CATALOG_ENTRY: Json = {
  provider: 'acme',
  name: 'Acme',
  category: 'integration',
  integration_category: 'ticketing',
  auth_type: 'oauth',
  supported_auth_types: ['oauth'],
  fields: ['client_id', 'client_secret'],
  extra_fields: null,
  extra_field_options: null,
  extra_field_visible_when: null,
  description: null,
  logo: null,
  integration_types: [],
  available_events: [],
  available_tools: [],
  element_types: [],
  webhook_path: null,
  needs_shared_webhook: false,
  supports_assignment_webhooks: false,
  supports_webhook_management: false,
  setup_guide: null,
  has_oauth_defaults: false,
  has_oauth_extra_config: false,
  is_bot: false,
  is_disabled: false,
  is_registration_only: false,
};

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

// Every read the page makes about the integration is answered. A read left to
// the broad 404 fails its query, which the app hands to the page's
// ErrorBoundary after one retry: the test then passed or failed on how fast
// the runner was. Any request reaching the fallback fails the test instead.
let unanswered: string[] = [];
test.beforeEach(() => {
  unanswered = [];
});
test.afterEach(() => {
  expect(unanswered, 'requests the mocks do not answer').toEqual([]);
});
const notFound = (route: Route) => {
  unanswered.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
  return json(route, { detail: 'Not Found' }, 404);
};

/**
 * Serve one integration and capture what the configuration card saves.
 *
 * Playwright runs the LAST registered matching route first, so the broad 404
 * fallback is registered before the specific answers it must not shadow.
 */
async function mockIntegration(page: Page, integration: Json, previewCredentials: Json[] = []) {
  const saved: Json[] = [];
  // The sharing confirmation's read-only preview: nothing newly
  // lent by default, so the save goes straight through.
  await page.route((url) => url.pathname === '/api/v1/exposure/preview', (route) =>
    json(route, { items: previewCredentials, requires_confirmation: previewCredentials.length > 0, truncated: false }));
  await page.route((url) => url.pathname.startsWith('/api/v1/a2a/'), notFound);
  await page.route((url) => url.pathname.startsWith(`/api/v1/integrations/${INTEGRATION_ID}/`), notFound);
  // The configuration tab reads the live tool schemas; the catalog entry
  // declares no tools, so the integration serves none.
  await page.route((url) => url.pathname === `/api/v1/integrations/${INTEGRATION_ID}/tools`, (route) =>
    json(route, { tools: [] }));
  await page.route((url) => url.pathname === '/api/v1/integrations', (route) =>
    route.request().method() === 'GET' ? json(route, [integration]) : route.fallback());
  await page.route((url) => url.pathname === '/api/v1/integrations/catalog', (route) =>
    json(route, [CATALOG_ENTRY]));
  await page.route((url) => url.pathname === `/api/v1/integrations/${INTEGRATION_ID}`, (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    const body = route.request().postDataJSON() as Json;
    saved.push(body);
    return json(route, { ...integration, name: body.name ?? integration.name });
  });
  return saved;
}

async function openEditForm(page: Page) {
  await page.goto(`/integrations/${INTEGRATION_ID}/config`);
  await page.getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save changes' })).toBeVisible();
}

test.describe('Sharing an OAuth integration', () => {
  test('shares the connected account by default', async ({ page }) => {
    const saved = await mockIntegration(page, sharedOauthRow());
    await openEditForm(page);

    // The notice says what sharing does; nothing about losing credentials.
    await expect(page.getByText(/everyone it is shared with acts using its owner's connected account/)).toBeVisible();
    await expect(page.getByText(/will need to reconnect with their own account/)).toHaveCount(0);
    const optOut = page.getByRole('switch', { name: SWITCH_NAME });
    await expect(optOut).toHaveAttribute('aria-checked', 'false');

    await page.getByRole('button', { name: 'Save changes' }).click();
    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0].keep_oauth_credentials_on_share).toBe(true);
  });

  test('sends the opt-out when the switch is turned on', async ({ page }) => {
    const saved = await mockIntegration(page, sharedOauthRow());
    await openEditForm(page);

    const optOut = page.getByRole('switch', { name: SWITCH_NAME });
    await optOut.click();
    await expect(optOut).toHaveAttribute('aria-checked', 'true');

    await page.getByRole('button', { name: 'Save changes' }).click();
    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0].keep_oauth_credentials_on_share).toBe(false);
  });

  test('keeps an opted-out integration opted out on an unrelated save', async ({ page }) => {
    // Shared, no consent recorded, no token: its owner opted out.
    const saved = await mockIntegration(
      page,
      sharedOauthRow({ oauth_share_acknowledged: false, has_access_token: false, status: 'configured' }),
    );
    await openEditForm(page);

    await expect(page.getByRole('switch', { name: SWITCH_NAME })).toHaveAttribute('aria-checked', 'true');
    await page.getByRole('button', { name: 'Save changes' }).click();
    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0].keep_oauth_credentials_on_share).toBe(false);
  });

  test('shares a token nobody consented for once the configuration is saved', async ({ page }) => {
    // Shared and holding a token, but no consent recorded (left from before
    // sharing recorded it): the switch starts off, so saving shares it.
    const saved = await mockIntegration(page, sharedOauthRow({ oauth_share_acknowledged: false }));
    await page.goto(`/integrations/${INTEGRATION_ID}/config`);
    await expect(page.getByText(/were never shared: other members cannot pick this integration in a chat or test it/)).toBeVisible();

    await openEditForm(page);
    await expect(page.getByRole('switch', { name: SWITCH_NAME })).toHaveAttribute('aria-checked', 'false');
    await page.getByRole('button', { name: 'Save changes' }).click();
    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0].keep_oauth_credentials_on_share).toBe(true);
  });
});

// One confirmation before a save lends a stored credential to new people:
// it says whose credential it is and who gets it; confirming saves
// with the explicit choice, cancelling saves nothing.
const NOBODY: Json = {
  everyone: false,
  people_count: 0,
  people: [],
  groups: [],
  public_link: false,
  anonymous_channels: [],
  mcp_key_holders: false,
  a2a_partners: false,
  webhook_callers: false,
};
const LENT_CREDENTIAL: Json = {
  resource: {
    kind: 'integration', id: INTEGRATION_ID, name: 'E2E shared OAuth', provider: 'acme', auth_type: 'oauth',
    is_company_managed: false, owner_id: OWNER_ID, owner_name: 'Integration Owner',
  },
  secret: {
    kind: 'oauth_account',
    holder_id: OWNER_ID,
    holder_name: 'Integration Owner',
    is_company: false,
    account_shared: true,
    scopes: ['read', 'write'],
    is_yours: false,
  },
  via: null,
  gained: {
    ...NOBODY,
    people_count: 3,
    groups: [{ id: GROUP_ID, name: 'Support', member_count: 3 }],
  },
  lost: NOBODY,
  notes: [],
  opt_outs: ['withhold_connected_account'],
};

test.describe('Confirming a share of a stored credential', () => {
  test('names whose credential it is and who gets it, then saves the explicit choice', async ({ page }) => {
    const saved = await mockIntegration(page, sharedOauthRow(), [LENT_CREDENTIAL]);
    await openEditForm(page);
    await page.getByRole('button', { name: 'Save changes' }).click();

    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(/Integration Owner's .* connected account/)).toBeVisible();
    await expect(dialog.getByText(/Newly usable by 3 people \(Support\)/)).toBeVisible();
    await expect(dialog.getByText(/scopes: read, write/)).toBeVisible();
    expect(saved.length).toBe(0);

    await dialog.getByTestId('credential-sharing-confirm').click();
    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0].keep_oauth_credentials_on_share).toBe(true);
  });

  test('saves nothing when cancelled', async ({ page }) => {
    const saved = await mockIntegration(page, sharedOauthRow(), [LENT_CREDENTIAL]);
    await openEditForm(page);
    await page.getByRole('button', { name: 'Save changes' }).click();

    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog).toBeVisible();
    await dialog.getByTestId('credential-sharing-cancel').click();
    await expect(dialog).toBeHidden();
    expect(saved.length).toBe(0);
    await expect(page.getByRole('button', { name: 'Save changes' })).toBeVisible();
  });
});
