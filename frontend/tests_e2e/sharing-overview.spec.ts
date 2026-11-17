import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Each lent resource shows to whom it is shared and through what.
//
// The MCP server stands for every non-integration resource (custom tools,
// channel bots, variables use the same card and badge): its overview carries a
// "Sharing and access" card listing each path with a link to what the viewer
// can open, and its list card a "Shared - N ways" badge. The exposure routes
// are mocked; their answers are covered by
// backend/tests/test_resource_exposure_postgres.py.
// ---------------------------------------------------------------------------

const SERVER_ID = '8d2e3f4a-5b6c-4d7e-8f9a-0b1c2d3e4f5a';
const OWNER_ID = '9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a';
const AGENT_ID = '3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f';
const GROUP_ID = '1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d';

type Json = Record<string, unknown>;

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

const SERVER: Json = {
  id: SERVER_ID, user_id: OWNER_ID, owner_name: 'Server Owner', name: 'E2E lent server',
  transport: 'sse', command: null, args: null, has_env: false, url: 'https://mcp.example.com/sse',
  has_headers: true, auth_mode: 'headers', has_client_id: false, oauth_status: 'disconnected',
  scopes: null, authorize_url: null, token_url: null, registration_url: null, agent_ids: [], tags: [],
  enabled: true, hide_from_chat: false, is_company_managed: false, is_public_catalog: false,
  shared_group_ids: [GROUP_ID], shared_group_permissions: { [GROUP_ID]: 'read_only' },
  shared_user_ids: null, shared_user_permissions: null, access_restricted: false, can_manage: true,
  discovered_tools: [], tool_count: 0, tools_discovered_at: null, call_count: 0, last_called_at: null,
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
};

const EXPOSURE: Json = {
  resource: {
    kind: 'mcp_server', id: SERVER_ID, name: 'E2E lent server', provider: null, auth_type: 'headers',
    is_company_managed: false, owner_id: OWNER_ID, owner_name: 'Server Owner',
  },
  secret: {
    kind: 'mcp_headers', holder_id: OWNER_ID, holder_name: 'Server Owner', is_company: false,
    account_shared: true, scopes: [],
  },
  direct: { ...NOBODY, people_count: 3 },
  paths: [
    {
      kind: 'group', reasons: [], restricted: false, entity: null, parent: null, usable: true, notes: [],
      audience: { ...NOBODY, people_count: 3, groups: [{ id: GROUP_ID, name: 'Support', member_count: 3 }] },
    },
    {
      kind: 'agent', reasons: ['binding'], restricted: false,
      entity: { type: 'agent', id: AGENT_ID, name: 'Helpdesk', visible: true, is_company_managed: false, owner_name: 'Server Owner' },
      parent: null, usable: true, notes: [],
      audience: { ...NOBODY, people_count: 8, public_link: true },
    },
    {
      kind: 'flow', reasons: ['flow_node'], restricted: false,
      entity: { type: 'flow', id: null, name: null, visible: false, is_company_managed: false, owner_name: 'Bob' },
      parent: null, usable: true, notes: [],
      audience: { ...NOBODY, people_count: 2 },
    },
  ],
  reached: { ...NOBODY, people_count: 11, public_link: true },
  truncated: false,
};

const SUMMARY: Json = {
  summaries: {
    [SERVER_ID]: {
      id: SERVER_ID, kind: 'mcp_server', secret_kind: 'mcp_headers', is_company: false, account_shared: true,
      ways: 3, counts: { group: 1, agent: 1, flow: 1 }, public_link: true,
    },
  },
  truncated: false,
};

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

// Every read the page makes about the server is answered. A read left to the
// broad 404 fails its query, which the app hands to the page's ErrorBoundary
// after one retry: the test then passed or failed on how fast the runner was.
// Any request reaching the fallback fails the test instead.
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

async function mockServer(page: Page) {
  await page.route((url) => url.pathname.startsWith(`/api/v1/mcp-servers/${SERVER_ID}/`), notFound);
  await page.route((url) => url.pathname === '/api/v1/mcp-servers', (route) =>
    route.request().method() === 'GET' ? json(route, [SERVER]) : route.fallback());
  await page.route((url) => url.pathname === `/api/v1/mcp-servers/${SERVER_ID}`, (route) => json(route, SERVER));
  await page.route((url) => url.pathname === `/api/v1/exposure/mcp-servers/${SERVER_ID}`, (route) => json(route, EXPOSURE));
  await page.route((url) => url.pathname === '/api/v1/exposure/mcp-servers/summary', (route) => json(route, SUMMARY));
}

test.describe('Sharing and access of a lent resource', () => {
  test('the overview lists every path, with a link to what the viewer can open', async ({ page }) => {
    await mockServer(page);
    await page.goto(`/mcp-servers/${SERVER_ID}`);
    const card = page.getByTestId('resource-sharing-access');
    await expect(card).toBeVisible();
    await expect(card.getByText("Server Owner's MCP server E2E lent server and its headers")).toBeVisible();
    await expect(card.getByText('Shared with the group Support')).toBeVisible();
    await expect(card.getByText('Agent - Helpdesk')).toBeVisible();
    await expect(card.getByText(/anyone with its public chat link/).first()).toBeVisible();
    // A flow the viewer cannot open is named by its owner, without a link.
    await expect(card.getByText('A Flow you cannot open, of Bob')).toBeVisible();

    await card.getByRole('button', { name: /Agent - Helpdesk/ }).click();
    await expect(page).toHaveURL(new RegExp(`/agents/${AGENT_ID}`));
  });

  test('the list card says how many ways it is shared', async ({ page }) => {
    await mockServer(page);
    // The server belongs to another user: it is under Shared, not under the Mine default.
    await page.goto('/mcp-servers?vis=all');
    await expect(page.getByTestId('sharing-ways-badge').first()).toHaveText(/Shared - 3 ways/);
  });

  test('a resource the viewer does not manage shows no sharing card', async ({ page }) => {
    await mockServer(page);
    await page.route((url) => url.pathname === `/api/v1/exposure/mcp-servers/${SERVER_ID}`, (route) =>
      json(route, { detail: 'Only people who can manage this MCP server can see who can use it.' }, 403));
    await page.goto(`/mcp-servers/${SERVER_ID}`);
    await expect(page.getByText('E2E lent server').first()).toBeVisible();
    await expect(page.getByTestId('resource-sharing-access')).toHaveCount(0);
  });
});
