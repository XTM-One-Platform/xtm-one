import { test, expect, type Page, type Route } from '@playwright/test';
import { IntegrationPage } from './model/integration.page';

// ---------------------------------------------------------------------------
// Integration extra fields — creation form and configuration card.
//
// A field left empty on the edit form keeps its stored value: the form sends
// the stored value back. The update endpoint merges the config key by key, so
// a stored key the form dropped is sent as null, which removes it. Every API
// call is mocked, so the spec pins the PAYLOAD the form sends; the backend
// contract (mask round-trip, key-by-key merge, null removal) is covered in
// backend/tests/test_integration_bundles_api.py and
// backend/tests/test_integration_extra_config_merge.py.
// ---------------------------------------------------------------------------

const INTEGRATION_ID = '0f1e2d3c-4b5a-4968-8776-655443322110';
const CARD_URLS = 'https://partner.example/a2a/abc/.well-known/agent-card.json';
const TOKEN_URL = 'http://localhost:8100/mcp/oauth/token';
const MASK = '********';

type Json = Record<string, unknown>;

function integrationRow(provider: string, extra_config: Json): Json {
  return {
    id: INTEGRATION_ID,
    user_id: null,
    owner_name: null,
    provider,
    auth_type: 'none',
    status: 'connected',
    name: `E2E ${provider} outbound`,
    description: null,
    is_company_managed: false,
    is_builtin: false,
    is_custom: false,
    shared_group_ids: null,
    shared_group_permissions: null,
    tags: [],
    scopes: null,
    required_scopes: null,
    has_client_id: false,
    has_client_secret: false,
    has_access_token: false,
    has_api_key: false,
    has_extra_config: true,
    oauth_share_acknowledged: false,
    extra_config,
    scope_config: null,
    call_count: 0,
    last_called_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  };
}

function catalogEntry(provider: string, name: string, overrides: Json): Json {
  return {
    provider,
    name,
    category: 'integration',
    integration_category: 'a2a',
    auth_type: 'none',
    fields: [],
    a2a_directions: ['inbound', 'outbound'],
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
    ...overrides,
  };
}

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

/**
 * Serve one outbound A2A integration and capture what the form saves.
 *
 * Routes are matched on pathname, and Playwright runs the LAST registered
 * matching route first, so the broad 404 fallbacks are registered before the
 * specific answers they must not shadow.
 */
async function mockOutboundIntegration(page: Page, integration: Json, catalog: Json) {
  const saved: Json[] = [];

  // Anything else under the A2A surface, and any sub-resource of the
  // integration (a2a-activity, tools, webhooks...): an ordinary 404, which
  // the page treats as "nothing here".
  await page.route((url) => url.pathname.startsWith('/api/v1/a2a/'), (route) =>
    json(route, { detail: 'Not Found' }, 404));
  await page.route(
    (url) => url.pathname.startsWith(`/api/v1/integrations/${INTEGRATION_ID}/`),
    (route) => json(route, { detail: 'Not Found' }, 404),
  );

  // The detail page finds its integration in the LIST.
  await page.route((url) => url.pathname === '/api/v1/integrations', (route) =>
    route.request().method() === 'GET' ? json(route, [integration]) : route.fallback());
  await page.route((url) => url.pathname === '/api/v1/integrations/catalog', (route) =>
    json(route, [catalog]));

  // A present verification state is what tells the config tab this
  // registration faces OUTBOUND, and therefore which fields to edit.
  await page.route((url) => url.pathname === `/api/v1/a2a/client/${INTEGRATION_ID}`, (route) =>
    json(route, {
      targets: [{
        id: 'a3c0d2e1-1111-4222-8333-444455556666',
        card_url: CARD_URLS,
        usability: 'usable',
        card_change_pending: false,
        signature_state: 'unsigned',
      }],
    }));
  await page.route(
    (url) => url.pathname === `/api/v1/a2a/client/${INTEGRATION_ID}/delegations`,
    (route) => json(route, []),
  );

  // The save. Captured, then answered with the row as the backend would
  // echo it, so the form leaves edit mode the way it does for real.
  await page.route((url) => url.pathname === `/api/v1/integrations/${INTEGRATION_ID}`, (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    const body = route.request().postDataJSON() as Json;
    saved.push(body);
    return json(route, { ...integration, ...(body.extra_config ? { extra_config: body.extra_config } : {}) });
  });

  return saved;
}

async function openEditForm(page: Page) {
  await page.goto(`/integrations/${INTEGRATION_ID}/config`);
  // The Edit button is the configuration card's own; it appears once the
  // catalog and the direction have both resolved.
  await page.getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save changes' })).toBeVisible();
}

test.describe('Integration configuration', () => {
  test('a field left empty keeps its stored value', async ({ page }) => {
    const stored = {
      card_urls: CARD_URLS,
      token_url: TOKEN_URL,      // an endpoint, not a secret: the API returns it in clear
      client_id: 'partner-issued-client-id',
      client_secret: MASK,
    };
    const saved = await mockOutboundIntegration(
      page,
      integrationRow('oauth_a2a', stored),
      catalogEntry('oauth_a2a', 'OAuth A2A', {
        variant_selector: 'a2a_direction',
        variants: {
          outbound: {
            extra_fields: {
              card_urls: 'Partner agent card URLs (required)',
              token_url: 'Partner token endpoint (required)',
              client_id: 'Client ID the partner issued you (required)',
              client_secret: 'Client secret the partner issued you (required)',
            },
          },
        },
      }),
    );

    await openEditForm(page);

    // The endpoint is a plain, readable input. The secret starts EMPTY rather
    // than showing the API's mask under password dots: the only masked field
    // here is the client secret, and its placeholder says a value is stored.
    // Fields are found by their name (the label read off the catalog), never
    // by the raw key: the placeholder is a sentence on the name.
    const tokenUrl = page.getByLabel('Partner token endpoint', { exact: true });
    await expect(tokenUrl).toHaveValue(TOKEN_URL);
    await expect(tokenUrl).toHaveAttribute('type', 'text');
    const storedSecret = page.getByPlaceholder('Stored: type to replace');
    await expect(storedSecret).toHaveCount(1);
    await expect(storedSecret).toHaveValue('');

    // No label carries a "leave empty" hint any more: pre-filled fields speak
    // for themselves and stored secrets say so in their placeholder.
    await expect(page.getByText('(leave empty to keep current)')).toHaveCount(0);

    // The field is pre-filled with the stored value. Empty it and save,
    // touching nothing else.
    const cardUrls = page.getByLabel('Partner agent card URLs', { exact: true });
    await expect(cardUrls).toHaveValue(CARD_URLS);
    await cardUrls.fill('');
    await page.getByRole('button', { name: 'Save changes' }).click();

    await expect.poll(() => saved.length).toBe(1);
    const extra = saved[0].extra_config as Json;
    // The emptied field goes back as it was stored...
    expect(extra.card_urls).toBe(CARD_URLS);
    // ...and every untouched field round-trips: secrets as the mask the update
    // endpoint reads as "keep the stored value", the rest as themselves.
    expect(extra).toEqual(stored);
  });

  test('a field hidden by a credential-mode switch is sent as null, so switching clears it', async ({ page }) => {
    const saved = await mockOutboundIntegration(
      page,
      integrationRow('google_a2a', {
        card_urls: CARD_URLS,
        credential_mode: 'key',
        service_account_json: MASK,
      }),
      catalogEntry('google_a2a', 'Google Agent Platform A2A', {
        variant_selector: 'a2a_direction',
        variants: {
          outbound: {
            extra_fields: {
              card_urls: 'Partner agent card URLs (required)',
              credential_mode: 'How this platform authenticates to the partner',
              impersonate_service_account: 'The partner service account to impersonate (required)',
              service_account_json: 'The partner service account key JSON (required)',
            },
          },
        },
        extra_field_options: {
          credential_mode: [
            { value: 'impersonation', label: 'Impersonate a service account — keyless (recommended)' },
            { value: 'key', label: 'Service account key JSON' },
          ],
        },
        extra_field_visible_when: {
          impersonate_service_account: { credential_mode: ['impersonation'] },
          service_account_json: { credential_mode: ['key'] },
        },
      }),
    );

    await openEditForm(page);

    // The stored key shows as an empty "stored" input. Switch from the key
    // to impersonation: that input disappears with its mode, and the
    // impersonation target is filled in.
    const storedKey = page.getByPlaceholder('Stored: type to replace');
    await expect(storedKey).toHaveCount(1);
    await page.getByRole('combobox').filter({ hasText: 'Service account key JSON' }).click();
    await page.getByRole('option', { name: /Impersonate a service account/ }).click();
    await expect(storedKey).toHaveCount(0);
    await page.getByLabel('Impersonate service account', { exact: true }).fill('caller@their-project.iam.gserviceaccount.com');
    await page.getByRole('button', { name: 'Save changes' }).click();

    await expect.poll(() => saved.length).toBe(1);
    const extra = saved[0].extra_config as Json;
    expect(extra).toEqual({
      card_urls: CARD_URLS,
      credential_mode: 'impersonation',
      impersonate_service_account: 'caller@their-project.iam.gserviceaccount.com',
      // The abandoned key is NOT restored: the update merges key by key, so
      // sending it as null is what clears it.
      service_account_json: null,
    });
  });

  test('a direction the server cannot answer keeps every outbound field on save', async ({ page }) => {
    // A 403 on the direction endpoint is not an answer. Read as "inbound",
    // a save touching nothing stripped the outbound variant's keys.
    const stored = {
      card_urls: CARD_URLS,
      token_url: TOKEN_URL,
      client_id: 'partner-issued-client-id',
      client_secret: MASK,
    };
    const saved = await mockOutboundIntegration(
      page,
      integrationRow('oauth_a2a', stored),
      catalogEntry('oauth_a2a', 'OAuth A2A', {
        variant_selector: 'a2a_direction',
        variants: {
          inbound: { extra_fields: { allowed_client_ids: 'Allowed partner client ids' } },
          outbound: {
            extra_fields: {
              card_urls: 'Partner agent card URLs (required)',
              token_url: 'Partner token endpoint (required)',
              client_id: 'Client ID the partner issued you (required)',
              client_secret: 'Client secret the partner issued you (required)',
            },
          },
        },
      }),
    );
    // Registered after the mock's own answer, so it wins.
    await page.route((url) => url.pathname === `/api/v1/a2a/client/${INTEGRATION_ID}`, (route) =>
      json(route, { detail: 'Forbidden' }, 403));

    await openEditForm(page);
    // Not read as inbound: the inbound-only credential card stays away too.
    await expect(page.getByText('Partner credential')).toHaveCount(0);
    await page.getByRole('button', { name: 'Save changes' }).click();

    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0].extra_config).toEqual(stored);
  });

  test('the creation form follows the direction: guide, masked secret, readable endpoint', async ({ page }) => {
    // Every A2A mechanism creates with auth type "none", the one branch of the
    // creation form that used to render every extra field in clear. The four
    // branches now share one renderer with the configuration card.
    await mockOutboundIntegration(
      page,
      integrationRow('oauth_a2a', {}),
      catalogEntry('oauth_a2a', 'OAuth A2A', {
        variant_selector: 'a2a_direction',
        variants: {
          outbound: {
            extra_fields: {
              card_urls: 'Partner agent card URLs (required)',
              token_url: 'Partner token endpoint (required)',
              client_id: 'Client ID the partner issued you (required)',
              client_secret: 'Client secret the partner issued you (required)',
            },
            setup_guide: { title: 'OAuth A2A Setup', url: null, steps: ['OUTBOUND-ONLY: paste the partner card URLs and credential.'], permissions: [], apis_to_enable: null, notes: null, manifest_example: null },
          },
        },
        // The base guide is the inbound procedure; outbound has its own.
        setup_guide: { title: 'OAuth A2A Setup', url: null, steps: ['INBOUND-ONLY: generate the credential and send the partner the card URL.'], permissions: [], apis_to_enable: null, notes: null, manifest_example: null },
      }),
    );

    await page.goto('/integrations/new');
    await new IntegrationPage(page).selectProvider('OAuth A2A');

    // The guide follows the direction: inbound is the default, outbound once picked.
    await page.getByRole('button', { name: 'Setup guide' }).click();
    await expect(page.getByText('INBOUND-ONLY: generate the credential and send the partner the card URL.')).toBeVisible();
    await expect(page.getByText(/OUTBOUND-ONLY/)).toHaveCount(0);
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: /Outbound: our agents task a partner/ }).click();
    await page.getByRole('button', { name: 'Setup guide' }).click();
    await expect(page.getByText('OUTBOUND-ONLY: paste the partner card URLs and credential.')).toBeVisible();
    await expect(page.getByText(/INBOUND-ONLY/)).toHaveCount(0);
    await page.keyboard.press('Escape');

    const secret = page.getByLabel('Client secret', { exact: true });
    await expect(secret).toHaveAttribute('type', 'password');
    await expect(secret).toHaveAttribute('placeholder', 'Paste client secret...');
    await expect(page.getByLabel('Partner token endpoint', { exact: true })).toHaveAttribute('type', 'text');
    await expect(page.getByLabel('Partner agent card URLs', { exact: true })).toHaveAttribute('type', 'text');

    // "Show" is for checking what was just typed, and only that.
    await page.getByRole('button', { name: 'Show', exact: true }).click();
    await expect(secret).toHaveAttribute('type', 'text');
  });

  test('a stored top-level credential renders as an empty input that says so', async ({ page }) => {
    // Client id, secret and API key never come back from the API. Their inputs
    // follow the same convention as a masked extra field: empty, a placeholder
    // saying a value is stored, no label hint - and a pre-filled plain field
    // beside them shows its value.
    await mockOutboundIntegration(
      page,
      { ...integrationRow('acme', { base_url: 'https://acme.example' }), auth_type: 'api_key', has_api_key: true },
      catalogEntry('acme', 'Acme', {
        integration_category: 'ticketing',
        auth_type: 'api_key',
        fields: ['api_key'],
        a2a_directions: undefined,
        extra_fields: { base_url: 'Acme instance URL' },
      }),
    );

    await openEditForm(page);
    const apiKey = page.getByPlaceholder('Stored: type to replace');
    await expect(apiKey).toHaveCount(1);
    await expect(apiKey).toHaveValue('');
    await expect(apiKey).toHaveAttribute('type', 'password');
    await expect(page.getByLabel('Acme instance URL', { exact: true })).toHaveValue('https://acme.example');
    await expect(page.getByText('(leave empty to keep current)')).toHaveCount(0);
  });

  // Pairs with a2a/04's "the detail page picks the guide for the direction it
  // stored": that drives the real catalogue, this mocks one with unambiguous
  // strings to isolate variant-over-base selection, and needs no A2A stack.
  test('the detail page prefers the variant guide over the base guide', async ({ page }) => {
    // The mocked verification state answers, which is how the page learns
    // this registration is OUTBOUND - so it must show the outbound guide.
    await mockOutboundIntegration(
      page,
      integrationRow('oauth_a2a', { card_urls: CARD_URLS, token_url: TOKEN_URL }),
      catalogEntry('oauth_a2a', 'OAuth A2A', {
        variant_selector: 'a2a_direction',
        variants: {
          outbound: {
            extra_fields: { card_urls: 'Partner agent card URLs (required)', token_url: 'Partner token endpoint (required)' },
            setup_guide: { title: 'OAuth A2A Setup', url: null, steps: ['OUTBOUND-ONLY: paste the partner card URLs and credential.'], permissions: [], apis_to_enable: null, notes: null, manifest_example: null },
          },
        },
        setup_guide: { title: 'OAuth A2A Setup', url: null, steps: ['INBOUND-ONLY: generate the credential and send the partner the card URL.'], permissions: [], apis_to_enable: null, notes: null, manifest_example: null },
      }),
    );

    await page.goto(`/integrations/${INTEGRATION_ID}`);
    await page.getByRole('button', { name: 'Setup guide' }).click();
    await expect(page.getByText('OUTBOUND-ONLY: paste the partner card URLs and credential.')).toBeVisible();
    await expect(page.getByText(/INBOUND-ONLY/)).toHaveCount(0);
  });
});
