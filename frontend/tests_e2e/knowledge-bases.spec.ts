import { test, expect } from '@playwright/test';
import { KnowledgeBasePage } from './model/knowledge-base.page';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Knowledge Bases — create a KB and verify list + detail navigation (Documents/Configuration tabs).
// No embedding calls are triggered.
// ---------------------------------------------------------------------------

test.describe.serial('Knowledge Bases — lifecycle', () => {
  const KB_NAME = `E2E KB ${Date.now()}`;

  test('should display the knowledge bases list page', async ({ page }) => {
    const kbPage = new KnowledgeBasePage(page);
    await kbPage.gotoList();
    await expect(page).toHaveURL(/\/knowledge(\?vis=[a-z]+)?$/);
    await expect(page.getByRole('button', { name: 'New Knowledge Base' })).toBeVisible();
  });

  test('should create a new knowledge base', async ({ page }) => {
    const kbPage = new KnowledgeBasePage(page);
    await kbPage.gotoList();
    await kbPage.clickNewKB();
    await kbPage.fillName(KB_NAME);
    await kbPage.fillDescription('Created by E2E test suite.');
    await kbPage.clickCreate();

    await kbPage.waitForDetail();
    await expect(page.getByText(KB_NAME)).toBeVisible({ timeout: 10_000 });
  });

  test('should show the KB on the list after creation', async ({ page }) => {
    const kbPage = new KnowledgeBasePage(page);
    await kbPage.gotoList();
    await expect(page.getByText(KB_NAME)).toBeVisible({ timeout: 10_000 });
  });

  test('should show the Documents tab on KB detail page', async ({ page }) => {
    const kbPage = new KnowledgeBasePage(page);
    await kbPage.gotoList();
    await page.getByText(KB_NAME).first().click();
    await kbPage.waitForDetail();

    // Documents tab should exist even when empty.
    await expect(page.getByRole('tab', { name: 'Documents' })).toBeVisible({ timeout: 10_000 });
  });

  test('should open the Config tab and show KB name as read-only', async ({ page }) => {
    const kbPage = new KnowledgeBasePage(page);
    await kbPage.gotoList();
    await page.getByText(KB_NAME).first().click();
    await kbPage.waitForDetail();

    const configTab = page.getByRole('tab', { name: 'Configuration', exact: true });
    await configTab.click();
    // The config card should show the KB name.
    await expect(page.getByRole('button', { name: 'Edit' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(KB_NAME).first()).toBeVisible();
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.get('/api/v1/knowledge-bases?limit=500', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (resp.ok()) {
        const kbs = await resp.json();
        const target = kbs.find((kb: { name: string; id: string }) => kb.name === KB_NAME);
        if (target) {
          await apiCtx.delete(`/api/v1/knowledge-bases/${target.id}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
        }
      }
    } finally {
      await apiCtx.dispose();
    }
  });
});
