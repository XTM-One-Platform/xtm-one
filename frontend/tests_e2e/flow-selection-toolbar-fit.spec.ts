import { test, expect, type Page, type Route } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Flow builder, the selection toolbar stays inside the canvas.
//
// With the summary on one line, the toolbar was 719 px
// (fr) and 734 px (es) wide inside a 522 px canvas at 1024 px with the node
// palette open. It ran past both edges, covered the palette search, and
// Duplicate, Delete and Clear were unreachable; with the node editor open it
// covered the editor's close button. The canvas is now a size container and
// the text buttons are icon-only below 48rem of canvas.
//
// Real layout, real sizes: a flow with four nodes is created through the API,
// opened at 1024 px in each locale, everything selected with Ctrl/Cmd+A, and
// every toolbar button is checked to sit inside the canvas area and to be the
// element actually under its own centre.
// ---------------------------------------------------------------------------

const GRAPH = {
  nodes: [
    { id: 't1', kind: 'trigger.manual', position: { x: 40, y: 40 }, config: {} },
    { id: 's1', kind: 'util.stop', position: { x: 320, y: 0 }, config: {}, label: 'Done A' },
    { id: 's2', kind: 'util.stop', position: { x: 320, y: 160 }, config: {}, label: 'Done B' },
    { id: 's3', kind: 'util.stop', position: { x: 320, y: 320 }, config: {}, label: 'Done C' },
  ],
  edges: [
    { id: 'e1', source: 't1', source_handle: 'out', target: 's1', target_handle: 'in', kind: 'data' },
    { id: 'e2', source: 't1', source_handle: 'out', target: 's2', target_handle: 'in', kind: 'data' },
    { id: 'e3', source: 't1', source_handle: 'out', target: 's3', target_handle: 'in', kind: 'data' },
  ],
};

async function withAdmin<T>(fn: (apiCtx: Awaited<ReturnType<typeof createAdminApiContext>>['apiCtx'], headers: Record<string, string>) => Promise<T>): Promise<T> {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    return await fn(apiCtx, { Authorization: `Bearer ${token}` });
  } finally {
    await apiCtx.dispose();
  }
}

async function expectToolbarInsideCanvas(page: Page) {
  const toolbar = page.getByTestId('flow-selection-toolbar');
  await expect(toolbar).toBeVisible({ timeout: 15_000 });
  const report = await page.evaluate(() => {
    const area = document.querySelector('[data-testid="flow-canvas-area"]')!.getBoundingClientRect();
    const bar = document.querySelector('[data-testid="flow-selection-toolbar"]')!;
    const buttons = Array.from(bar.querySelectorAll('button')).map((button) => {
      const r = button.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return {
        name: button.getAttribute('aria-label') ?? button.textContent ?? '',
        inside: r.left >= area.left && r.right <= area.right && r.top >= area.top && r.bottom <= area.bottom,
        reachable: !!hit && button.contains(hit),
      };
    });
    const b = bar.getBoundingClientRect();
    return { area: { left: area.left, right: area.right }, bar: { left: b.left, right: b.right }, buttons };
  });
  expect(report.buttons.length, JSON.stringify(report)).toBeGreaterThanOrEqual(4);
  expect(report.bar.left, JSON.stringify(report)).toBeGreaterThanOrEqual(report.area.left);
  expect(report.bar.right, JSON.stringify(report)).toBeLessThanOrEqual(report.area.right);
  for (const button of report.buttons) {
    expect(button.inside, `${button.name} inside the canvas: ${JSON.stringify(report)}`).toBe(true);
    expect(button.reachable, `${button.name} under its own centre: ${JSON.stringify(report)}`).toBe(true);
  }
}

test.describe('Flow builder, selection toolbar fits the canvas at 1024 px', () => {
  let flowId = '';

  test.beforeAll(async () => {
    flowId = await withAdmin(async (apiCtx, headers) => {
      const resp = await apiCtx.post('/api/v1/flows', {
        headers,
        data: { name: `E2E selection toolbar ${Date.now()}`, graph: GRAPH },
      });
      expect(resp.ok(), await resp.text()).toBe(true);
      return ((await resp.json()) as { id: string }).id;
    });
  });

  test.afterAll(async () => {
    if (!flowId) return;
    await withAdmin((apiCtx, headers) => apiCtx.delete(`/api/v1/flows/${flowId}`, { headers }));
  });

  for (const language of ['en-us', 'fr-fr', 'es-es', 'ja-jp']) {
    test(`every button is inside the canvas and reachable in ${language}`, async ({ page }) => {
      await page.setViewportSize({ width: 1024, height: 900 });
      await page.route('**/api/v1/auth/me', async (route: Route) => {
        const response = await route.fetch();
        const me = await response.json();
        await route.fulfill({ response, json: { ...me, language } });
      });
      await page.goto(`/flows/library/${flowId}`);
      const pane = page.locator('.react-flow__pane');
      await expect(pane).toBeVisible({ timeout: 15_000 });
      await expect(page.locator('.react-flow__node')).toHaveCount(4, { timeout: 15_000 });

      // Palette open (the default), everything selected.
      await pane.click({ position: { x: 20, y: 20 } });
      await page.keyboard.press('ControlOrMeta+a');
      await expectToolbarInsideCanvas(page);

      // With the node editor open the canvas is narrower still.
      await page.locator('.react-flow__node').first().click();
      await page.keyboard.press('ControlOrMeta+a');
      await expectToolbarInsideCanvas(page);
    });
  }
});
