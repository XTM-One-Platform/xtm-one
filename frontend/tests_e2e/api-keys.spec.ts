import { test, expect, request as apiRequest, type APIRequestContext, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Scoped API keys, against the live stack.
//
// A key created in the Profile with its scopes is held to them by the API
// (read-only: a GET answers, a write is refused), an admin revokes it from
// Settings > Security > API keys, and the API refuses it from then on. The
// key is used through a request context of its own, so no session of the
// signed-in browser rides along with it.
// ---------------------------------------------------------------------------

const keyRow = (page: Page, name: string) =>
  page.locator(`[data-testid="api-key-row"][data-key-name="${name}"]`);

const adminKeyRow = (page: Page, name: string) =>
  page.locator(`[data-testid="admin-api-key-row"][data-key-name="${name}"]`);

async function keyClient(secret: string): Promise<APIRequestContext> {
  return apiRequest.newContext({
    baseURL: test.info().project.use.baseURL,
    extraHTTPHeaders: { Authorization: `Bearer ${secret}` },
  });
}

test.describe('Scoped API keys', () => {
  test('a read-only key reads, cannot write, and stops working once revoked', async ({ page }) => {
    const name = `e2e-scoped-${Date.now()}`;

    // Create the key: Platform API only, read-only, 30 days.
    await page.goto('/profile/api-keys');
    await page.getByRole('button', { name: 'Create Key', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('New API key')).toBeVisible();
    await dialog.locator('#api-key-name').fill(name);
    for (const surface of ['MCP endpoints', 'LLM proxy']) {
      const toggle = dialog.getByRole('switch', { name: surface });
      if ((await toggle.count()) > 0 && (await toggle.getAttribute('aria-checked')) === 'true') {
        await toggle.click();
      }
    }
    await expect(dialog.getByRole('switch', { name: 'Platform API' })).toHaveAttribute('aria-checked', 'true');
    await dialog.getByRole('switch', { name: 'Read-only' }).click();
    await dialog.getByRole('combobox', { name: 'Expiry' }).click();
    await page.getByRole('option', { name: '30 days', exact: true }).click();
    await dialog.getByRole('button', { name: 'Create key', exact: true }).click();
    await expect(dialog).toBeHidden();

    const secret = (await page.getByTestId('api-key-created-secret').textContent())?.trim() ?? '';
    expect(secret).toMatch(/^fcp-/);

    const row = keyRow(page, name);
    await expect(row).toBeVisible();
    await expect(row.getByText('Platform API', { exact: true })).toBeVisible();
    await expect(row.getByText('Read-only', { exact: true })).toBeVisible();
    await expect(row.getByText('Full access', { exact: true })).toHaveCount(0);

    // The API holds the key to its scopes.
    const client = await keyClient(secret);
    try {
      const me = await client.get('/api/v1/auth/me');
      expect(me.status()).toBe(200);
      const write = await client.post('/api/v1/api-keys', { data: { name: `${name}-child` } });
      expect(write.status()).toBe(403);
      expect((await write.json()).detail).toContain('read-only');

      // An admin revokes it from Settings > Security > API keys.
      await page.goto('/settings/security/api-keys');
      await page.getByPlaceholder('Search by key name, prefix or owner').fill(name);
      const adminRow = adminKeyRow(page, name);
      await expect(adminRow).toBeVisible();
      await adminRow.getByTestId('admin-api-key-revoke').click();
      await page.getByRole('dialog').getByRole('button', { name: 'Revoke', exact: true }).click();
      await expect(adminRow.getByText('Revoked', { exact: true })).toBeVisible();

      const afterRevoke = await client.get('/api/v1/auth/me');
      expect(afterRevoke.status()).toBe(401);
    } finally {
      await client.dispose();
    }

    // Its owner still sees it as revoked, and deletes it.
    await page.goto('/profile/api-keys');
    await expect(row.getByText('Revoked', { exact: true })).toBeVisible();
    await row.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete', exact: true }).click();
    await expect(row).toHaveCount(0);
  });
});
