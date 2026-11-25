import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Every resource a person lends through something shared is explicit.
//
// One confirmation, `useCredentialSharingConfirm()`, asks the read-only
// preview (`POST /api/v1/exposure/preview`) before a share, a binding, a
// creation, an unshare or a deletion, and opens only when a resource reaches
// new people or is taken from people who use it. Confirming proceeds; keeping
// things as they are writes nothing. The MCP server page stands for every
// resource here: every API call about it is mocked, so the spec pins what the
// page asks the preview and what it saves; the preview's answers are covered by
// backend/tests/test_resource_exposure_postgres.py.
// ---------------------------------------------------------------------------

const SERVER_ID = '7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f';
const OWNER_ID = '9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a';
const GROUP_ID = '1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d';
const AGENT_ID = '2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e';

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
  id: SERVER_ID,
  user_id: OWNER_ID,
  owner_name: 'Server Owner',
  name: 'E2E tickets',
  transport: 'sse',
  command: null,
  args: null,
  has_env: false,
  url: 'https://mcp.example.com/sse',
  has_headers: true,
  auth_mode: 'headers',
  has_client_id: false,
  oauth_status: 'disconnected',
  scopes: null,
  authorize_url: null,
  token_url: null,
  registration_url: null,
  agent_ids: [],
  tags: [],
  enabled: true,
  hide_from_chat: false,
  is_company_managed: false,
  is_public_catalog: false,
  shared_group_ids: null,
  shared_group_permissions: null,
  shared_user_ids: null,
  shared_user_permissions: null,
  access_restricted: false,
  can_manage: true,
  discovered_tools: [],
  tool_count: 0,
  tools_discovered_at: null,
  call_count: 0,
  last_called_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

function item(overrides: Json): Json {
  return {
    resource: {
      kind: 'mcp_server', id: SERVER_ID, name: 'E2E tickets', provider: null, auth_type: 'headers',
      is_company_managed: false, owner_id: OWNER_ID, owner_name: 'Server Owner',
    },
    secret: {
      kind: 'mcp_headers', holder_id: OWNER_ID, holder_name: 'Server Owner', is_company: false,
      account_shared: true, scopes: [], is_yours: false,
    },
    via: null,
    gained: NOBODY,
    lost: NOBODY,
    notes: [],
    opt_outs: ['company_resource'],
    ...overrides,
  };
}

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

/** Serve one MCP server, answer the preview with *items*, and capture writes.
 *  Playwright runs the LAST registered matching route first. */
async function mockServer(page: Page, items: Json[]) {
  const previews: Json[] = [];
  const saved: Json[] = [];
  const deleted: string[] = [];
  await page.route((url) => url.pathname.startsWith(`/api/v1/mcp-servers/${SERVER_ID}/`), notFound);
  await page.route((url) => url.pathname === '/api/v1/mcp-servers', (route) =>
    route.request().method() === 'GET' ? json(route, [SERVER]) : route.fallback());
  await page.route((url) => url.pathname === `/api/v1/mcp-servers/${SERVER_ID}`, (route) => {
    const method = route.request().method();
    if (method === 'PUT') {
      saved.push(route.request().postDataJSON() as Json);
      return json(route, SERVER);
    }
    if (method === 'DELETE') {
      deleted.push(SERVER_ID);
      return route.fulfill({ status: 204, body: '' });
    }
    if (method === 'GET') return json(route, SERVER);
    return route.fallback();
  });
  await page.route((url) => url.pathname === '/api/v1/exposure/preview', (route) => {
    previews.push(route.request().postDataJSON() as Json);
    return json(route, { items, requires_confirmation: items.length > 0, truncated: false });
  });
  return { previews, saved, deleted };
}

async function openEditForm(page: Page) {
  await page.goto(`/mcp-servers/${SERVER_ID}/config`);
  await page.getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save changes' })).toBeVisible();
}

test.describe('Lending an MCP server', () => {
  test('a save that lends nothing new goes straight through', async ({ page }) => {
    const { previews, saved } = await mockServer(page, []);
    await openEditForm(page);
    await page.getByRole('button', { name: 'Save changes' }).click();
    await expect.poll(() => saved.length).toBe(1);
    expect(previews[0]).toMatchObject({ entity_type: 'mcp_server', entity_id: SERVER_ID });
    await expect(page.getByTestId('credential-sharing-dialog')).toHaveCount(0);
  });

  test('a cut preview that lists nothing still asks, and says the list may be incomplete', async ({ page }) => {
    const { saved } = await mockServer(page, []);
    await page.route((url) => url.pathname === '/api/v1/exposure/preview', (route) =>
      json(route, { items: [], requires_confirmation: true, truncated: true }));
    await openEditForm(page);
    await page.getByRole('button', { name: 'Save changes' }).click();

    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog.getByTestId('credential-sharing-truncated')).toBeVisible();
    await expect(dialog.getByTestId('credential-sharing-confirm')).toHaveText('Share');
    expect(saved.length).toBe(0);
    await dialog.getByTestId('credential-sharing-confirm').click();
    await expect.poll(() => saved.length).toBe(1);
  });

  test('says whose server it is and who gets it, then saves on confirm', async ({ page }) => {
    const lent = item({
      gained: { ...NOBODY, people_count: 4, groups: [{ id: GROUP_ID, name: 'Support', member_count: 4 }] },
      via: { type: 'agent', id: AGENT_ID, name: 'Helpdesk' },
    });
    const { saved } = await mockServer(page, [lent]);
    await openEditForm(page);
    await page.getByRole('button', { name: 'Save changes' }).click();

    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText("Server Owner's MCP server E2E tickets and its headers")).toBeVisible();
    await expect(dialog.getByText(/Newly usable by 4 people \(Support\)/)).toBeVisible();
    await expect(dialog.getByText('Through the agent Helpdesk')).toBeVisible();
    expect(saved.length).toBe(0);

    await dialog.getByTestId('credential-sharing-confirm').click();
    await expect.poll(() => saved.length).toBe(1);
  });

  test('keeping things as they are saves nothing', async ({ page }) => {
    const { saved } = await mockServer(page, [
      item({ gained: { ...NOBODY, people_count: 2 } }),
    ]);
    await openEditForm(page);
    await page.getByRole('button', { name: 'Save changes' }).click();
    const dialog = page.getByTestId('credential-sharing-dialog');
    await dialog.getByTestId('credential-sharing-cancel').click();
    await expect(dialog).toBeHidden();
    expect(saved.length).toBe(0);
  });

  test('unsharing asks whether to unshare or keep sharing', async ({ page }) => {
    const { saved } = await mockServer(page, [
      item({ lost: { ...NOBODY, people_count: 3, groups: [{ id: GROUP_ID, name: 'Support', member_count: 3 }] } }),
    ]);
    await openEditForm(page);
    await page.getByRole('button', { name: 'Save changes' }).click();
    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog.getByText(/No longer usable by 3 people \(Support\)/)).toBeVisible();
    await expect(dialog.getByTestId('credential-sharing-cancel')).toHaveText('Keep sharing');
    await expect(dialog.getByTestId('credential-sharing-confirm')).toHaveText('Unshare');
    await dialog.getByTestId('credential-sharing-confirm').click();
    await expect.poll(() => saved.length).toBe(1);
  });

  test('deleting a server other people use says who loses it', async ({ page }) => {
    const { previews, deleted } = await mockServer(page, [
      item({ lost: { ...NOBODY, people_count: 5 }, via: null }),
    ]);
    await page.goto(`/mcp-servers/${SERVER_ID}`);
    await page.getByRole('button', { name: 'Delete server' }).click();
    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog.getByText(/No longer usable by 5 people/)).toBeVisible();
    expect(previews.at(-1)).toMatchObject({ entity_type: 'mcp_server', change: { delete: true } });
    await dialog.getByTestId('credential-sharing-confirm').click();
    await expect.poll(() => deleted.length).toBe(1);
  });

  test('deleting a server nobody else uses keeps its own confirmation', async ({ page }) => {
    const { deleted } = await mockServer(page, []);
    await page.goto(`/mcp-servers/${SERVER_ID}`);
    await page.getByRole('button', { name: 'Delete server' }).click();
    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog.getByText('Delete MCP Server')).toBeVisible();
    await dialog.getByTestId('credential-sharing-confirm').click();
    await expect.poll(() => deleted.length).toBe(1);
  });
});

// A deletion always asks, so its dialog opens on the click with the page's own
// text; the preview only adds who loses access, and never holds it back.
test.describe('Deleting asks at once', () => {
  test('the dialog is on screen before the preview answers, then says who loses access', async ({ page }) => {
    const { deleted } = await mockServer(page, []);
    let answer: (items: Json[]) => void = () => {};
    const answered = new Promise<Json[]>((resolve) => {
      answer = resolve;
    });
    await page.route((url) => url.pathname === '/api/v1/exposure/preview', async (route) => {
      const items = await answered;
      return json(route, { items, requires_confirmation: items.length > 0, truncated: false });
    });
    await page.goto(`/mcp-servers/${SERVER_ID}`);
    await page.getByRole('button', { name: 'Delete server' }).click();

    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog.getByText('Delete MCP Server')).toBeVisible();
    await expect(dialog.getByTestId('credential-sharing-checking')).toBeVisible();
    await expect(dialog.getByTestId('credential-sharing-confirm')).toBeDisabled();

    answer([item({ lost: { ...NOBODY, people_count: 5 }, via: null })]);
    await expect(dialog.getByText(/No longer usable by 5 people/)).toBeVisible();
    await expect(dialog.getByTestId('credential-sharing-checking')).toHaveCount(0);
    expect(deleted.length).toBe(0);
    await dialog.getByTestId('credential-sharing-confirm').click();
    await expect.poll(() => deleted.length).toBe(1);
  });

  test('a failed preview leaves the page\'s own confirmation, ready to delete', async ({ page }) => {
    const { deleted } = await mockServer(page, []);
    await page.route((url) => url.pathname === '/api/v1/exposure/preview', (route) =>
      json(route, { detail: 'Preview unavailable' }, 500));
    await page.goto(`/mcp-servers/${SERVER_ID}`);
    await page.getByRole('button', { name: 'Delete server' }).click();

    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog.getByText('Delete MCP Server')).toBeVisible();
    await expect(dialog.getByTestId('credential-sharing-confirm')).toBeEnabled();
    await expect(dialog.getByTestId('credential-sharing-checking')).toHaveCount(0);
    // The failed preview is not the person's problem: no error toast.
    await expect(page.getByText('Preview unavailable')).toHaveCount(0);
    await dialog.getByTestId('credential-sharing-confirm').click();
    await expect.poll(() => deleted.length).toBe(1);
  });
});

// A flow lends what its nodes wire to everyone it is shared with: deleting it
// takes those resources from them, and says so like every other deletion.
const FLOW_ID = '3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f';
const FLOW: Json = {
  id: FLOW_ID,
  user_id: OWNER_ID,
  owner_name: 'Server Owner',
  name: 'E2E triage flow',
  description: '',
  status: 'active',
  version_number: 3,
  node_count: 4,
  tags: [],
  is_company_managed: false,
  shared_group_ids: [GROUP_ID],
  can_manage: true,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

async function mockFlow(page: Page, items: Json[]) {
  const previews: Json[] = [];
  const deleted: string[] = [];
  await page.route((url) => url.pathname === '/api/v1/flows', (route) =>
    route.request().method() === 'GET' ? json(route, [FLOW]) : route.fallback());
  await page.route((url) => url.pathname === `/api/v1/flows/${FLOW_ID}`, (route) => {
    if (route.request().method() !== 'DELETE') return route.fallback();
    deleted.push(FLOW_ID);
    return route.fulfill({ status: 204, body: '' });
  });
  await page.route((url) => url.pathname === '/api/v1/exposure/preview', (route) => {
    previews.push(route.request().postDataJSON() as Json);
    return json(route, { items, requires_confirmation: items.length > 0, truncated: false });
  });
  return { previews, deleted };
}

async function deleteFromLibrary(page: Page) {
  // The map is the landing tab of /flows: the library is its own tab.
  // The mocked flow is not the signed-in user's: All, not the Mine default.
  await page.goto('/flows/library?vis=all');
  await page.getByRole('button', { name: 'List view' }).click();
  await page.getByRole('button', { name: 'Flow actions' }).first().click();
  await page.getByRole('menuitem', { name: 'Delete' }).click();
}

test.describe('Deleting a flow', () => {
  test('a flow that lends a resource says who loses it through the flow', async ({ page }) => {
    const { previews, deleted } = await mockFlow(page, [
      item({ lost: { ...NOBODY, people_count: 5 }, via: { type: 'flow', id: FLOW_ID, name: 'E2E triage flow' } }),
    ]);
    await deleteFromLibrary(page);
    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog.getByText(/No longer usable by 5 people/)).toBeVisible();
    expect(previews.at(-1)).toMatchObject({ entity_type: 'flow', entity_id: FLOW_ID, change: { delete: true } });
    expect(deleted.length).toBe(0);
    await dialog.getByTestId('credential-sharing-confirm').click();
    await expect.poll(() => deleted.length).toBe(1);
  });

  test('a flow nobody else uses keeps its own confirmation', async ({ page }) => {
    const { deleted } = await mockFlow(page, []);
    await deleteFromLibrary(page);
    const dialog = page.getByTestId('credential-sharing-dialog');
    await expect(dialog.getByText('Delete this flow?')).toBeVisible();
    await expect(dialog.getByText(/"E2E triage flow" and its saved versions will be permanently deleted/)).toBeVisible();
    await dialog.getByTestId('credential-sharing-cancel').click();
    await expect(dialog).toBeHidden();
    expect(deleted.length).toBe(0);
  });
});
