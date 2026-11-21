import { test, expect, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Flows — visual builder lifecycle (no LLM calls, no worker required).
//
// Everything here works without a background worker: a started run is
// asserted as *created and visible in the run history*, never as completed —
// execution semantics live in the backend walker tests.
//
// Connections between nodes are deliberately out of scope: xyflow edges are
// drawn through d3-drag, which does not respond reliably to synthetic
// pointer sequences, so edge-level behaviour is covered by the seeded
// example graphs (rendered below) and the backend validator tests.
// ---------------------------------------------------------------------------

const FLOW_NAME = `E2E Flow ${Date.now()}`;

/**
 * Adds a node through the double-click quick-add menu.
 *
 * The canvas places the node where the double-click landed, so each call
 * needs its own position or the nodes stack on top of each other.
 */
async function quickAddNode(
  page: Page,
  position: { x: number; y: number },
  query: string,
  label: string,
): Promise<void> {
  // Adding a node selects it and opens the right drawer, which overlays the
  // east side of the pane and would swallow the next double-click. Escape
  // deselects and closes it. Positions must also stay in the west half —
  // the default Playwright viewport is narrower than the dev one.
  await page.keyboard.press('Escape');
  await page.locator('.react-flow__pane').dblclick({ position });
  const menu = page.getByTestId('flow-quick-add');
  await expect(menu).toBeVisible();
  await menu.locator('input').fill(query);
  // Click the exact label instead of pressing Enter: the search also matches
  // descriptions and searchable terms, so the first result is not guaranteed
  // to be the kind under test.
  await menu.getByText(label, { exact: true }).first().click();
  await expect(
    page.locator('.react-flow__node').filter({ hasText: label }).first(),
  ).toBeVisible();
}

test.describe.serial('Flows — builder create/save/run lifecycle', () => {
  let builderUrl = '';

  test('should create a flow from the library', async ({ page }) => {
    // The map is the landing tab of /flows: the library is its own tab.
    await page.goto('/flows/library');
    await page.getByRole('button', { name: /New flow/ }).click();

    // The builder lives under its menu entry: /flows/library/<id> (#797 S4).
    await expect(page).toHaveURL(/\/flows\/library\/[0-9a-f-]{36}(?:[?#]|$)/);
    builderUrl = page.url();

    // Rename the fresh "Untitled flow" so cleanup and later assertions can
    // find it unambiguously. The name commits on blur.
    const nameInput = page.getByRole('textbox', { name: 'Flow name' });
    await nameInput.fill(FLOW_NAME);
    await nameInput.press('Tab');
  });

  test('should add nodes on the canvas with the quick-add menu', async ({ page }) => {
    test.skip(!builderUrl, 'Flow creation failed in the previous test');
    await page.goto(builderUrl);

    await quickAddNode(page, { x: 260, y: 140 }, 'manual', 'Manual');
    await quickAddNode(page, { x: 300, y: 330 }, 'cast', 'Cast');

    expect(await page.locator('.react-flow__node').count()).toBe(2);

    // The canvas is client state until saved — later tests reload the page.
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeVisible();
  });

  test('should configure a node in the drawer', async ({ page }) => {
    test.skip(!builderUrl, 'Flow creation failed in the previous test');
    await page.goto(builderUrl);

    await page.locator('.react-flow__node').filter({ hasText: 'Cast' }).click();
    // The right drawer renders the kind's config fields with wired labels.
    const value = page.getByLabel(/^Value/);
    await expect(value).toBeVisible();
    await value.fill('42');

    // Unsaved edits disable Run and flip the save button back to "Save".
    await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeVisible();
  });

  test('should persist the graph across a reload', async ({ page }) => {
    test.skip(!builderUrl, 'Flow creation failed in the previous test');
    await page.goto(builderUrl);

    await expect(
      page.locator('.react-flow__node').filter({ hasText: 'Manual' }).first(),
    ).toBeVisible();
    const cast = page.locator('.react-flow__node').filter({ hasText: 'Cast' }).first();
    await expect(cast).toBeVisible();

    await cast.click();
    await expect(page.getByLabel(/^Value/)).toHaveValue('42');
  });

  test('should declare a typed parameter on the manual trigger', async ({ page }) => {
    test.skip(!builderUrl, 'Flow creation failed in the previous test');
    await page.goto(builderUrl);

    await page.locator('.react-flow__node').filter({ hasText: 'Manual' }).click();
    await page.getByRole('button', { name: 'Add parameter' }).click();
    await page.getByPlaceholder('parameter_name').fill('who');

    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeVisible();
  });

  test('should run the flow through the typed inputs dialog', async ({ page }) => {
    test.skip(!builderUrl, 'Flow creation failed in the previous test');
    await page.goto(builderUrl);

    // A saved graph with declared parameters opens the run dialog.
    await page.getByRole('button', { name: 'Run', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('Run with inputs')).toBeVisible();

    await dialog.locator('#run-param-who').fill('world');
    await dialog.getByRole('button', { name: 'Run', exact: true }).click();

    // The run history opens with the new run listed. Without a worker the
    // run may sit in a queued state forever — presence is the assertion,
    // not completion.
    await expect(page.getByText('Run history')).toBeVisible();
    await expect(page.getByText('No runs yet', { exact: false })).toBeHidden();
    await expect(page.getByText('manual', { exact: true }).first()).toBeVisible();
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const headers = { Authorization: `Bearer ${token}` };
      const resp = await apiCtx.get('/api/v1/flows?limit=200', { headers });
      if (resp.ok()) {
        const body = await resp.json();
        const flows: { id: string; name: string }[] = Array.isArray(body)
          ? body
          : (body.items ?? []);
        const target = flows.find((f) => f.name === FLOW_NAME);
        if (target) {
          await apiCtx.delete(`/api/v1/flows/${target.id}`, { headers });
        }
      }
    } finally {
      await apiCtx.dispose();
    }
  });
});

test.describe('Flows — seeded examples', () => {
  test('should render a seeded example graph in the builder', async ({ page }) => {
    const { apiCtx, token } = await createAdminApiContext();
    let exampleId = '';
    try {
      const resp = await apiCtx.get('/api/v1/flows?limit=200', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (resp.ok()) {
        const body = await resp.json();
        const flows: { id: string; name: string }[] = Array.isArray(body)
          ? body
          : (body.items ?? []);
        exampleId = flows.find((f) => f.name.startsWith('Example:'))?.id ?? '';
      }
    } finally {
      await apiCtx.dispose();
    }
    test.skip(!exampleId, 'No seeded example flows on this platform');

    await page.goto(`/flows/library/${exampleId}`);
    // Example graphs carry several nodes and edges — rendering both proves
    // the catalog-driven canvas can display a real saved document.
    await expect(page.locator('.react-flow__node').first()).toBeVisible();
    expect(await page.locator('.react-flow__node').count()).toBeGreaterThanOrEqual(2);
    await expect(page.locator('.react-flow__edge').first()).toBeVisible();
  });
});

test.describe('Flows - read-only builder', () => {
  test('should read a disabled picker row in full from the keyboard', async ({ page }) => {
    const { apiCtx, token } = await createAdminApiContext();
    const headers = { Authorization: `Bearer ${token}` };
    let flowId = '';
    try {
      const resp = await apiCtx.post('/api/v1/flows', {
        headers,
        data: {
          name: `E2E Read-only Flow ${Date.now()}`,
          graph: {
            nodes: [
              {
                id: 'tools',
                kind: 'capability.tool',
                position: { x: 0, y: 0 },
                config: { tool_ids: ['builtin:web_fetch'] },
              },
            ],
            edges: [],
            viewport: { x: 0, y: 0, zoom: 1 },
          },
        },
      });
      expect(resp.ok()).toBeTruthy();
      flowId = (await resp.json()).id;

      // Served the way a read-only share is: the builder opens read-only and
      // disables every picker row.
      await page.route(
        (url) => url.pathname.endsWith(`/api/v1/flows/${flowId}`),
        async (route) => {
          if (route.request().method() !== 'GET') return route.fallback();
          const response = await route.fetch();
          await route.fulfill({ json: { ...(await response.json()), can_manage: false } });
        },
      );
      await page.goto(`/flows/library/${flowId}`);
      await page.locator('.react-flow__node').first().click();

      const row = page
        .locator('span[aria-disabled="true"]')
        .filter({ has: page.getByRole('button', { name: /Web fetch \(built-in\)/ }) });
      await expect(row.getByRole('button')).toBeDisabled();
      await row.focus();
      await expect(row).toHaveAttribute('data-state', /open/);
      const tooltip = page.getByRole('tooltip', { name: /Web fetch \(built-in\)/ }).first();
      await expect(tooltip).toBeAttached();
      await expect(tooltip).toContainText('Fetch and read a web page.');
    } finally {
      if (flowId) await apiCtx.delete(`/api/v1/flows/${flowId}`, { headers });
      await apiCtx.dispose();
    }
  });
});

test.describe('Flows - node catalog refresh', () => {
  test('should keep the builder and say so when the catalog cannot be refreshed', async ({ page }) => {
    const isCatalog = (url: string) => new URL(url).pathname.endsWith('/api/v1/flows/node-catalog');
    const { apiCtx, token } = await createAdminApiContext();
    const headers = { Authorization: `Bearer ${token}` };
    let flowId = '';
    try {
      const resp = await apiCtx.post('/api/v1/flows', {
        headers,
        data: {
          name: `E2E Catalog Refresh Flow ${Date.now()}`,
          graph: {
            nodes: [
              {
                id: 'tools',
                kind: 'capability.tool',
                position: { x: 0, y: 0 },
                config: { tool_ids: ['builtin:web_fetch'] },
              },
            ],
            edges: [],
            viewport: { x: 0, y: 0, zoom: 1 },
          },
        },
      });
      expect(resp.ok()).toBeTruthy();
      flowId = (await resp.json()).id;

      const catalogLoaded = page.waitForResponse((r) => isCatalog(r.url()) && r.ok());
      await page.goto(`/flows/library/${flowId}`);
      await catalogLoaded;
      const node = page.locator('.react-flow__node').first();
      await expect(node).toBeVisible();

      // Every catalog refetch fails from here on. A rename changes what the
      // catalog lists, so the builder refetches it.
      await page.route('**/api/v1/flows/node-catalog', (route) =>
        route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'E2E: catalog down' }) }),
      );
      const nameInput = page.getByRole('textbox', { name: 'Flow name' });
      await nameInput.fill(`E2E Catalog Refresh Flow renamed ${Date.now()}`);
      await nameInput.press('Tab');

      // The canvas stays, not the page's error boundary, and says its options
      // may be out of date.
      const stale = page.getByRole('status').filter({ hasText: 'could not be refreshed' });
      await expect(stale).toBeVisible();
      await expect(node).toBeVisible();
      await expect(page.getByText('Failed to load this section')).toHaveCount(0);

      // Once the catalog answers again, Retry refreshes it and the notice goes.
      await page.unroute('**/api/v1/flows/node-catalog');
      await stale.getByRole('button', { name: 'Retry' }).click();
      await expect(stale).toHaveCount(0);
    } finally {
      if (flowId) await apiCtx.delete(`/api/v1/flows/${flowId}`, { headers });
      await apiCtx.dispose();
    }
  });
});

test.describe('Flows - selections the catalog does not list', () => {
  test('should show a stored id the catalog does not list as a removable row, never dropping it by itself', async ({ page }) => {
    // A custom tool deleted, or never visible to this user, since the flow
    // stored it.
    const gone = '00000000-0000-4000-8000-000000004068';
    const { apiCtx, token } = await createAdminApiContext();
    const headers = { Authorization: `Bearer ${token}` };
    let flowId = '';
    try {
      const resp = await apiCtx.post('/api/v1/flows', {
        headers,
        data: {
          name: `E2E Unlisted Tool Flow ${Date.now()}`,
          graph: {
            nodes: [
              {
                id: 'tools',
                kind: 'capability.tool',
                position: { x: 0, y: 0 },
                config: { tool_ids: ['builtin:web_fetch', gone] },
              },
            ],
            edges: [],
            viewport: { x: 0, y: 0, zoom: 1 },
          },
        },
      });
      expect(resp.ok()).toBeTruthy();
      flowId = (await resp.json()).id;

      await page.goto(`/flows/library/${flowId}`);
      await page.locator('.react-flow__node').first().click();

      // Shown first and counted, rather than counted and nowhere to be seen.
      const unavailable = page.getByRole('button', { name: /^Unavailable/ });
      await expect(unavailable).toBeVisible();
      await expect(page.getByText('2 selected', { exact: true })).toBeVisible();

      // It leaves the selection only when the user removes it.
      await unavailable.click();
      await expect(unavailable).toHaveCount(0);
      await expect(page.getByText('1 selected', { exact: true })).toBeVisible();

      // Nothing was saved behind the user's back: the stored flow still holds it.
      const stored = await (await apiCtx.get(`/api/v1/flows/${flowId}`, { headers })).json();
      const node = stored.graph.nodes.find((n: { id: string }) => n.id === 'tools');
      expect(node.config.tool_ids).toContain(gone);
    } finally {
      if (flowId) await apiCtx.delete(`/api/v1/flows/${flowId}`, { headers });
      await apiCtx.dispose();
    }
  });
});
