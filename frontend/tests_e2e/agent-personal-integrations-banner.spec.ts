import { test, expect } from '@playwright/test';
import { ok, withAdmin } from './helpers';
import { REGULAR_USER_PASSWORD, regularUserEmail } from './constants';
import { LoginPage } from './model/login.page';

// ---------------------------------------------------------------------------
// A shared agent bound to its owner's personal integration (issue #8824).
//
// The settings page explains what the people it is shared with get and lets
// the owner use the integration through the agent only (lent_integration_ids,
// an agent-scoped grant): the warning then collapses to one green line that
// survives a reload, and Review reopens every choice. The backend contract
// (owner only, nothing listed or bindable outside the agent) is covered by
// backend/tests/test_agent_integration_lending.py.
// ---------------------------------------------------------------------------

const stamp = Date.now();
let agentId = '';
let groupId = '';
let integrationId = '';
let editorId = '';
let editorEmail = '';

test.describe.serial('Personal integrations on a shared agent', () => {
  test.beforeAll(async () => {
    editorEmail = regularUserEmail('lending-editor', test.info().parallelIndex);
    await withAdmin(async (apiCtx, headers) => {
      const users: { id: string; email: string }[] = await (await apiCtx.get('/api/v1/users?limit=500', { headers })).json();
      const leftover = users.find((u) => u.email === editorEmail);
      if (leftover) await apiCtx.delete(`/api/v1/users/${leftover.id}`, { headers });
      editorId = (await (await ok(await apiCtx.post('/api/v1/users', {
        headers,
        data: { email: editorEmail, password: REGULAR_USER_PASSWORD, full_name: 'E2E Lending Editor', role: 'user', account_type: 'local' },
      }), 'create editor')).json()).id;
      const catalog = await (await ok(await apiCtx.get('/api/v1/integrations/catalog?category=integration', { headers }), 'catalog')).json();
      const provider = catalog.find((p: { auth_type: string; fields?: string[]; is_disabled?: boolean; is_registration_only?: boolean }) =>
        p.auth_type === 'api_key' && p.fields?.includes('api_key') && !p.is_disabled && !p.is_registration_only);
      if (!provider) throw new Error('No enabled API-key provider in the catalog');
      integrationId = (await (await ok(await apiCtx.post('/api/v1/integrations', {
        headers,
        data: { provider: provider.provider, name: `E2E lent ${stamp}`, auth_type: 'api_key', api_key: 'e2e-fake-api-key' },
      }), 'create integration')).json()).id;
      groupId = (await (await ok(await apiCtx.post('/api/v1/groups', { headers, data: { name: `E2E lending ${stamp}` } }), 'create group')).json()).id;
      await ok(await apiCtx.post(`/api/v1/groups/${groupId}/members`, { headers, data: { user_ids: [editorId] } }), 'add editor to group');
      agentId = (await (await ok(await apiCtx.post('/api/v1/agents', {
        headers,
        data: { name: `E2E lending agent ${stamp}`, persona: 'Temp agent for the lending banner.' },
      }), 'create agent')).json()).id;
      await ok(await apiCtx.put(`/api/v1/agents/${agentId}`, {
        headers,
        data: {
          integration_bindings: { [provider.provider]: [integrationId] },
          shared_group_ids: [groupId],
          shared_group_permissions: { [groupId]: 'config' },
        },
      }), 'bind and share agent');
    });
  });

  test.afterAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      if (agentId) await apiCtx.delete(`/api/v1/agents/${agentId}`, { headers });
      if (integrationId) await apiCtx.delete(`/api/v1/integrations/${integrationId}`, { headers });
      if (groupId) await apiCtx.delete(`/api/v1/groups/${groupId}`, { headers });
      if (editorId) await apiCtx.delete(`/api/v1/users/${editorId}`, { headers });
    }).catch(() => undefined);
  });

  test('the owner uses the integration through the agent, the warning collapses and Review reopens it', async ({ page }) => {
    await page.goto(`/agents/${agentId}/config`);
    const banner = page.getByTestId('personal-integrations-banner');
    await expect(banner).toBeVisible({ timeout: 15_000 });
    await expect(banner).not.toContainText('recommended');

    await banner.getByRole('button', { name: 'Use my integrations through this agent' }).click();
    const lent = page.getByTestId('personal-integrations-lent');
    await expect(lent).toBeVisible({ timeout: 15_000 });
    await expect(lent).toContainText('Personal integrations used through this agent: 1');

    await page.reload();
    await expect(page.getByTestId('personal-integrations-lent')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('personal-integrations-banner')).toHaveCount(0);

    await page.getByTestId('personal-integrations-lent').getByRole('button', { name: 'Review' }).click();
    await expect(page.getByTestId('personal-integrations-banner')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Stop using my integrations through this agent' })).toBeVisible();
  });

  test('an editor who cannot read the owner\'s integration still sees the agent\'s state', async ({ page }) => {
    await page.context().clearCookies();
    await page.goto('/login');
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
    await new LoginPage(page).login(editorEmail, REGULAR_USER_PASSWORD);

    await page.goto(`/agents/${agentId}/config`);
    const lent = page.getByTestId('personal-integrations-lent');
    await expect(lent).toBeVisible({ timeout: 15_000 });
    await expect(lent).toContainText('Personal integrations used through this agent: 1');
    await expect(lent.getByRole('button', { name: 'Review' })).toHaveCount(0);
  });
});
