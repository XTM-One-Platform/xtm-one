import { test, expect } from '@playwright/test';
import { MCPServerPage } from './model/mcp-server.page';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// MCP Servers — full CRUD lifecycle (no real subprocess spawned).
// A stdio-transport server is created with a fake command; it will appear
// as "Enabled" in the list (the server is never actually started by the UI).
// ---------------------------------------------------------------------------

test.describe.serial('MCP Servers — CRUD', () => {
  const SERVER_NAME = `E2E MCP Server ${Date.now()}`;

  test('should display the MCP Servers list page', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await expect(page).toHaveURL(/mcp-servers/);
    await expect(page.getByRole('button', { name: 'Add Server' }).first()).toBeVisible();
  });

  test('should navigate to the Add MCP Server form', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await mcpPage.clickNew();
    await expect(page.getByRole('heading', { name: 'Add MCP Server' })).toBeVisible();
  });

  test('should show the preset picker on the create page', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await mcpPage.clickNew();
    // A search field or preset cards appear on the first step.
    await expect(page.getByPlaceholder(/search/i).first()).toBeVisible({ timeout: 10_000 });
  });

  test('should create a custom stdio MCP server', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await mcpPage.clickNew();
    await mcpPage.selectCustom();

    await mcpPage.fillName(SERVER_NAME);
    // Transport defaults to stdio — fill command and args.
    await mcpPage.fillCommand('node');
    await mcpPage.fillArgs('/usr/local/bin/fake-mcp-server');
    await mcpPage.clickCreate();

    await mcpPage.waitForDetail();
    await expect(page.getByText(SERVER_NAME)).toBeVisible({ timeout: 10_000 });
  });

  test('should show the server on the list page', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await expect(page.getByText(SERVER_NAME)).toBeVisible({ timeout: 10_000 });
  });

  test('should open the server detail page from the list', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await page.getByText(SERVER_NAME).first().click();
    await mcpPage.waitForDetail();
    await expect(page.getByText(SERVER_NAME)).toBeVisible();
  });

  test('should display the Config tab on server detail', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await page.getByText(SERVER_NAME).first().click();
    await mcpPage.waitForDetail();

    const configTab = page.getByRole('tab', { name: 'Configuration', exact: true });
    await expect(configTab).toBeVisible({ timeout: 10_000 });
    await configTab.click();
    await expect(page.getByRole('button', { name: 'Edit' })).toBeVisible({ timeout: 10_000 });
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
// Presets — a preset exists to save typing, so what matters is that picking one
// lands the user on a form already filled in. The two shapes fill different
// fields: a stdio preset fills command/args, a remote one fills the URL and has
// to pre-select OAuth, since a hosted server has no secret header to fall back
// on. Loaded directly rather than through the sidebar: the nav path is covered
// above, and the "Add Server" button is hidden when agentic AI is switched off.
// ---------------------------------------------------------------------------

test.describe('MCP Servers — presets', () => {
  test('should prefill command and args from a stdio preset', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoUrl('/mcp-servers/new');
    await mcpPage.selectPreset('CrowdStrike Falcon');

    await expect(mcpPage.commandInput()).toHaveValue('uvx');
    await expect(mcpPage.argsInput()).toHaveValue('falcon-mcp');
    // The preset's env var keys are laid out ready for their values.
    await expect(page.getByText('Falcon API client ID')).toBeVisible();
  });

  for (const [preset, url] of [
    ['VirusTotal', 'https://ai.virustotal.com/mcp'],
    ['Recorded Future', 'https://mcp.recordedfuture.com/mcp'],
  ]) {
    test(`should prefill the URL and pick OAuth for a remote preset (${preset})`, async ({ page }) => {
      const mcpPage = new MCPServerPage(page);
      await mcpPage.gotoUrl('/mcp-servers/new');
      await mcpPage.selectPreset(preset);

      await expect(mcpPage.urlInput()).toHaveValue(url);
      // OAuth is the selected tab, so its endpoint inputs are already on screen.
      await expect(mcpPage.detectSettingsButton()).toBeVisible();
    });
  }

  test('should refuse to create a preset whose arguments still hold a placeholder path', async ({ page }) => {
    // Created as is, the server would look configured and fail at its first
    // tool call with an error about the path.
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoUrl('/mcp-servers/new');
    await mcpPage.selectPreset('Filesystem');
    await expect(mcpPage.argsInput()).toHaveValue(/\/path\/to\/allowed\/dir/);

    const creates: string[] = [];
    page.on('request', (req) => {
      if (req.method() === 'POST' && /\/api\/v1\/mcp-servers\/?$/.test(new URL(req.url()).pathname)) {
        creates.push(req.url());
      }
    });
    await mcpPage.clickCreate();

    const error = page.getByText(
      'Replace /path/to/allowed/dir in the arguments with a real path before creating the server.',
    );
    await expect(error).toBeVisible();
    await expect(mcpPage.argsInput()).toBeFocused();
    await expect(mcpPage.argsInput()).toHaveAttribute('aria-invalid', 'true');
    await expect(page.getByRole('heading', { name: 'Configure Filesystem' })).toBeVisible();
    expect(creates).toEqual([]);

    // Editing the field clears the error.
    await mcpPage.fillArgs('-y @modelcontextprotocol/server-filesystem /srv/shared');
    await expect(error).toBeHidden();
  });
});


// ---------------------------------------------------------------------------
// Importing a configuration document.
//
// The value of the feature is that a server the user already had configured
// elsewhere ends up in their list without being retyped, so that is what is
// asserted end to end: pick a file, review what was found, confirm, and find
// the server on the list page afterwards.
// ---------------------------------------------------------------------------

test.describe('MCP Servers — config import', () => {
  const IMPORTED = `E2E Imported ${Date.now()}`;

  test('should import a server from an mcpServers file and list it', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await mcpPage.openImport();

    await mcpPage.importFile(
      'claude_desktop_config.json',
      JSON.stringify({
        mcpServers: {
          [IMPORTED]: {
            command: 'npx',
            args: ['-y', '@modelcontextprotocol/server-everything'],
            env: { SOME_TOKEN: '<your-token>' },
          },
        },
      }),
    );

    // Review step: the entry and its command line, plus the warning that the
    // placeholder token was not imported as a value.
    await expect(page.getByText(IMPORTED)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('npx -y @modelcontextprotocol/server-everything')).toBeVisible();
    await expect(page.getByText(/Still needs a value for SOME_TOKEN/)).toBeVisible();

    await page.getByRole('button', { name: 'Import', exact: true }).last().click();

    // Wait for the dialog to close before navigating. It closes only once the
    // import mutation has resolved with nothing failed, so this is the signal
    // that the server exists — without it the list query can be issued while
    // the POST is still in flight and legitimately come back without it.
    await expect(
      page.getByRole('heading', { name: 'Import MCP servers' }),
    ).toBeHidden({ timeout: 15_000 });

    await mcpPage.gotoList();
    await expect(page.getByText(IMPORTED)).toBeVisible({ timeout: 15_000 });
  });

  test('should explain a document it cannot read', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();
    await mcpPage.openImport();
    await mcpPage.importFile('broken.json', 'this is not json');

    await expect(
      mcpPage.toasts().getByText('Could not read that configuration', { exact: true }),
    ).toBeVisible({ timeout: 15_000 });
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.get('/api/v1/mcp-servers?limit=500', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (resp.ok()) {
        const servers = await resp.json();
        // Exactly this run's server, not every name sharing the prefix: a
        // prefix sweep deletes what a concurrently running copy of this file
        // just created, which turns an unrelated assertion red for reasons
        // that look like a product bug.
        for (const s of servers as { name: string; id: string }[]) {
          if (s.name === IMPORTED) {
            await apiCtx.delete(`/api/v1/mcp-servers/${s.id}`, {
              headers: { Authorization: `Bearer ${token}` },
            });
          }
        }
      }
    } finally {
      await apiCtx.dispose();
    }
  });
});

// ---------------------------------------------------------------------------
// The MCP Endpoints strip — what the platform exposes, as opposed to what it
// connects out to.
//
// The regression worth guarding is that it renders at all: it used to hide
// every endpoint an administrator had switched off, and hide the whole strip
// when that left nothing, so a disabled endpoint looked identical to one that
// does not exist on this deployment.
// ---------------------------------------------------------------------------

test.describe('MCP Servers — platform endpoints', () => {
  test('should list every endpoint with a status, including disabled ones', async ({ page }) => {
    const mcpPage = new MCPServerPage(page);
    await mcpPage.gotoList();

    await expect(page.getByRole('heading', { name: 'MCP Endpoints' })).toBeVisible();

    // The three bridges are always listed, whatever their registration or the
    // admin toggle says — that is the point of the strip.
    const strip = page.locator('section').filter({ hasText: 'MCP Endpoints' }).first();
    for (const name of ['OpenCTI', 'OpenAEV', 'OpenCRQ']) {
      await expect(strip.getByText(name, { exact: true })).toBeVisible({ timeout: 15_000 });
    }

    // Every row states which of the four situations it is in, rather than
    // leaving the dot to carry it alone.
    const statuses = await strip.locator('span.sr-only').allTextContents();
    expect(statuses.length).toBeGreaterThanOrEqual(3);
    for (const status of statuses) {
      expect(['Enabled', 'Connected', 'Not connected', 'Unreachable', 'Disabled']).toContain(status);
    }

    // "Not connected" is distinct from the grey a disabled row gets: they need
    // different actions from the reader. Addressed by the tone the dot
    // PUBLISHES rather than by the class that paints it — the two were the
    // same thing until the design-system migration repainted them, and a test
    // that reads paint fails on a change that alters nothing it cares about.
    // Counted rather than walked up to a row, since every ancestor of a row
    // also "contains" its status and matches a looser locator.
    const notConnected = statuses.filter((s) => s === 'Not connected').length;
    await expect(strip.locator('span[data-tone="pending"]')).toHaveCount(notConnected);

    const disabled = statuses.filter((s) => s === 'Disabled').length;
    await expect(strip.locator('span[data-tone="disabled"]')).toHaveCount(disabled);
  });
});
