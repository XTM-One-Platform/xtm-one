import { test, expect } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Agentic Flow map: opening an agent's detail drawer keeps the map drawn.
// The drawer's backdrop once painted the opaque page surface over the
// whole viewport, so the map went blank the moment the drawer opened.
// No LLM call: the agent is created through the API and never run.
// ---------------------------------------------------------------------------

test.describe.serial('Agentic Flow map - the detail drawer keeps the map drawn', () => {
  const AGENT_NAME = `E2E Map Drawer Agent ${Date.now()}`;
  let agentId = '';

  test.beforeAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.post('/api/v1/agents', {
        headers: { Authorization: `Bearer ${token}` },
        data: { name: AGENT_NAME, persona: 'Temp agent for the map drawer test.' },
      });
      if (!resp.ok()) {
        throw new Error(`Failed to create test agent: ${resp.status()} ${await resp.text()}`);
      }
      agentId = (await resp.json()).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    if (!agentId) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/agents/${agentId}`, { headers: { Authorization: `Bearer ${token}` } });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('opening and closing the drawer leaves the map drawn and the node marked', async ({ page }) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (err) => pageErrors.push(err.message));

    await page.goto('/flows');
    const node = page.locator(`[data-node-id="${agentId}"]`);
    await expect(node).toBeVisible({ timeout: 20_000 });

    // Agents created and deleted by specs running in parallel join and leave
    // the map at any time, so the global node count is not this test's to
    // assert. The map staying drawn is asserted on this test's own node: it
    // stays in the map, visible, and the map renders it among its nodes.
    const mapNodes = page.locator('[data-node-id]');
    const ownNodeDrawn = () =>
      mapNodes.evaluateAll(
        (els, id) => els.some((el) => el.getAttribute('data-node-id') === id),
        agentId,
      );
    expect(await ownNodeDrawn()).toBe(true);

    // Open the drawer from the node (Enter on a focused node, as the info
    // affordance does on click).
    await node.focus();
    await page.keyboard.press('Enter');
    const drawer = page.getByRole('dialog', { name: `${AGENT_NAME} details` });
    await expect(drawer).toBeVisible({ timeout: 10_000 });

    // The map is still drawn, the node is marked as selected.
    await expect.poll(ownNodeDrawn).toBe(true);
    await expect(node).toBeVisible();
    await expect(node).toHaveAttribute('data-selected', 'true');

    // Nothing paints over the map: the backdrop is see-through.
    const backdropColor = await page
      .getByTestId('agent-flow-drawer-backdrop')
      .evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(backdropColor).toMatch(/^(transparent|rgba\(0, 0, 0, 0\))$/);

    // Closing the drawer leaves the map as it was.
    await page.keyboard.press('Escape');
    await expect(drawer).toBeHidden();
    await expect.poll(ownNodeDrawn).toBe(true);
    await expect(node).toBeVisible();
    await expect(node).not.toHaveAttribute('data-selected', 'true');

    expect(pageErrors).toEqual([]);
  });
});
