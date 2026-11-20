import { test, expect } from '@playwright/test';
import { ok, withAdmin } from './helpers';

// ---------------------------------------------------------------------------
// Workspace knowledge: a workspace's instructions and a text source,
// from its project page to the knowledge drawer of a conversation started
// there. Nothing is embedded or sent to a model: the journey reads what the
// pages show.
// ---------------------------------------------------------------------------

test.describe.serial('Workspace knowledge — project page and drawer', () => {
  const NAME = `E2E workspace ${Date.now()}`;
  const INSTRUCTIONS = 'Answer in English and cite the source document.';
  let workspaceId = '';

  test.beforeAll(async () => {
    workspaceId = await withAdmin(async (api, headers) => {
      const ws = await (await ok(await api.post('/api/v1/workspaces', { headers, data: { name: NAME } }), 'create workspace')).json();
      await ok(await api.put(`/api/v1/workspaces/${ws.id}`, { headers, data: { instructions: INSTRUCTIONS } }), 'set instructions');
      const src = await (await ok(await api.post(`/api/v1/workspaces/${ws.id}/sources`, { headers }), 'open sources')).json();
      await ok(
        await api.post(`/api/v1/knowledge-bases/${src.knowledge_base_id}/documents/text`, {
          headers,
          data: { filename: 'e2e-brief.md', content: '# Brief\nThe launch is on Friday.', source_type: 'markdown' },
        }),
        'add source',
      );
      return ws.id as string;
    });
  });

  test.afterAll(async () => {
    if (!workspaceId) return;
    await withAdmin(async (api, headers) => {
      await api.delete(`/api/v1/workspaces/${workspaceId}`, { headers });
    });
  });

  test('the project page shows the instructions and the sources', async ({ page }) => {
    await page.goto(`/tasks/workspaces/${workspaceId}`);
    await expect(page.getByRole('heading', { name: NAME })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('textbox', { name: 'Instructions' })).toHaveValue(INSTRUCTIONS);

    await page.getByRole('button', { name: 'Manage sources' }).click();
    const dialog = page.getByRole('dialog', { name: `Sources of ${NAME}` });
    await expect(dialog.getByText('e2e-brief.md')).toBeVisible({ timeout: 10_000 });
    await page.keyboard.press('Escape');
  });

  test('a conversation started there opens the workspace knowledge beside it', async ({ page }) => {
    await page.goto(`/tasks/workspaces/${workspaceId}`);
    await page.getByRole('button', { name: 'New conversation' }).click();
    await expect(page).toHaveURL(/\/chat\?c=/, { timeout: 15_000 });

    await page.getByRole('button', { name: 'Conversation options' }).click();
    await page.getByRole('menuitem', { name: 'Workspace knowledge' }).click();
    const drawer = page.getByRole('dialog', { name: 'Workspace knowledge' });
    await expect(drawer.getByRole('textbox', { name: 'Instructions' })).toHaveValue(INSTRUCTIONS, { timeout: 10_000 });
    // Whatever the source's processing state: the worker may have run.
    await expect(drawer.getByRole('button', { name: 'Manage sources' })).toBeVisible();
  });
});
