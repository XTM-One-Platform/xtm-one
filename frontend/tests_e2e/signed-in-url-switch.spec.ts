import { test, expect, request, type Page } from '@playwright/test';
import { BASE_URL } from './constants';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// A signed-in URL (`/login#token=...`, the SSO callback form the labs print)
// replaces whatever session the tab holds: another account's, or none while
// the tab already sits on /login, where opening the URL changes the fragment
// alone and the page is not loaded again (#9510).
// ---------------------------------------------------------------------------

const PEER_PASSWORD = 'E2eSignedInUrlPeer2026';

/** A page.evaluate error raised because the document navigated mid-call. */
const NAVIGATION_ERROR = /Execution context was destroyed|Cannot find context with specified id|frame was detached|navigat/i;

/**
 * The account the tab's stored session answers for. The app navigates away
 * from `/login#token=...` once it has consumed the token: a read that lands
 * during that navigation answers null so `expect.poll` asks again (#9526).
 */
async function signedInEmail(page: Page): Promise<string | null> {
  try {
    return await page.evaluate(async () => {
      const token = localStorage.getItem('copilot_token');
      if (!token) return null;
      const resp = await fetch('/api/v1/auth/me', { headers: { Authorization: `Bearer ${token}` } });
      return resp.ok ? ((await resp.json()) as { email: string }).email : null;
    });
  } catch (error) {
    if (error instanceof Error && NAVIGATION_ERROR.test(error.message)) return null;
    throw error;
  }
}

/** Waits until the app has consumed the token and its redirect has settled. */
async function tokenConsumed(page: Page): Promise<void> {
  await expect(page).not.toHaveURL(/token=/);
  await page.waitForLoadState();
}

test.describe('Signed-in URL - switching sessions in one tab', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  let adminToken = '';
  let peerToken = '';
  let peerEmail = '';
  let peerId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    adminToken = token;
    peerEmail = `e2e-signed-in-url-${Date.now()}@e2e-example.com`;
    try {
      const created = await apiCtx.post('/api/v1/users', {
        headers: { Authorization: `Bearer ${token}` },
        data: { email: peerEmail, full_name: 'E2E signed-in URL peer', password: PEER_PASSWORD, role: 'user', account_type: 'local' },
      });
      expect(created.ok(), await created.text()).toBe(true);
      peerId = ((await created.json()) as { id: string }).id;
    } finally {
      await apiCtx.dispose();
    }
    const anonymous = await request.newContext({ baseURL: BASE_URL });
    try {
      const login = await anonymous.post('/api/v1/auth/login', { data: { email: peerEmail, password: PEER_PASSWORD } });
      expect(login.ok(), await login.text()).toBe(true);
      peerToken = ((await login.json()) as { access_token: string }).access_token;
    } finally {
      await anonymous.dispose();
    }
  });

  test.afterAll(async () => {
    if (!peerId) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/users/${peerId}`, { headers: { Authorization: `Bearer ${token}` } });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('the peer URL replaces the administrator session', async ({ page }) => {
    await page.goto(`/login#token=${adminToken}`);
    await expect(page).not.toHaveURL(/\/login/);
    await tokenConsumed(page);
    await expect.poll(() => signedInEmail(page)).not.toBeNull();

    await page.goto(`/login#token=${peerToken}`);
    await tokenConsumed(page);
    await expect.poll(() => signedInEmail(page)).toBe(peerEmail);
    await expect(page).not.toHaveURL(/token=/);
  });

  test('the peer URL signs in a tab already on the sign-in page', async ({ page }) => {
    await page.goto('/login');
    await expect(page).toHaveURL(/\/login$/);
    // Same document, new fragment: no load, only a hashchange.
    await page.goto(`/login#token=${peerToken}`);
    await tokenConsumed(page);
    await expect.poll(() => signedInEmail(page)).toBe(peerEmail);
    await expect(page).not.toHaveURL(/\/login/);
  });
});
