import { test, expect, type Page } from '@playwright/test';
import { ok, withAdmin } from './helpers';

// ---------------------------------------------------------------------------
// Header height budget of an entity page (#10215, header and hero density
// sweep, criterion 2): at 1440 px the header, from the back link to the
// first block under it, is at most 140 px, a long AI-written description
// included (clamped to two lines, its "Read more" at the end of the second
// line, never a row of its own). Measured on a fleet page and an agent page,
// both seeded with a description far longer than two lines.
// ---------------------------------------------------------------------------

const BUDGET_PX = 140;
const LONG = 'An AI-written description long enough to run well past two lines on a wide screen. '.repeat(12).trim();

async function headerHeight(page: Page): Promise<number> {
  const header = page.getByTestId('entity-page-header');
  await expect(header).toBeVisible();
  const more = page.getByRole('button', { name: 'Read more' });
  await expect(more).toBeVisible();
  // UX round 2 B1: the toggle sits on the second line only, never over line one.
  const overlap = await page.getByTestId('entity-page-header-description').evaluate((box) => {
    const p = box.querySelector('p') as HTMLElement;
    const button = box.querySelector('button') as HTMLElement;
    const lineHeight = parseFloat(getComputedStyle(p).lineHeight);
    const lineTwoTop = p.getBoundingClientRect().top + lineHeight;
    return Math.round(lineTwoTop - button.getBoundingClientRect().top);
  });
  expect(overlap).toBeLessThanOrEqual(0);
  return header.evaluate((el) => {
    const next = el.nextElementSibling as HTMLElement | null;
    const top = el.getBoundingClientRect().top;
    const bottom = next ? next.getBoundingClientRect().top : el.getBoundingClientRect().bottom;
    return Math.round(bottom - top);
  });
}

test.describe.serial('Entity page header — height budget at 1440 px', () => {
  const stamp = Date.now();
  let agentId = '';
  let fleetId = '';
  let workspaceId = '';
  let fleetSkip = '';
  let forkId = '';
  let forkWorkspaceId = '';

  test.beforeAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      const agent = await ok(
        await apiCtx.post('/api/v1/agents', {
          headers,
          data: { name: `E2E header budget ${stamp}`, persona: 'Temp agent for the header budget test.', description: LONG },
        }),
        'create agent',
      );
      agentId = (await agent.json()).id;
      const fleetResp = await apiCtx.post('/api/v1/fleets', {
        headers,
        data: { name: `E2E header budget fleet ${stamp}`, description: LONG },
      });
      if (fleetResp.status() === 403) {
        fleetSkip = 'Fleets are not licensed on this deployment (agentic AI, Enterprise Edition).';
        return;
      }
      const fleet = await (await ok(fleetResp, 'create fleet')).json();
      fleetId = fleet.id;
      workspaceId = fleet.workspace_id ?? '';
      // A fork draws the fullest meta row: orchestrator, status, "Forked from",
      // the workspace chip (UX round 1 B1 on #10287). A "keep" fork needs an
      // orchestrator on its source (400 otherwise, which skipped this test
      // silently in UX round 2): the agent becomes the source's orchestrator.
      await ok(
        await apiCtx.post(`/api/v1/fleets/${fleetId}/members`, { headers, data: { role: 'member', agent_id: agentId } }),
        'add the orchestrator member',
      );
      await ok(
        await apiCtx.put(`/api/v1/fleets/${fleetId}`, { headers, data: { orchestrator_agent_id: agentId } }),
        'set the orchestrator',
      );
      const fork = (
        await (
          await ok(
            await apiCtx.post(`/api/v1/fleets/${fleetId}/fork`, {
              headers,
              data: { name: `E2E header budget fork with a long name ${stamp}`, workspace_id: workspaceId || null, members: 'keep' },
            }),
            'fork the fleet',
          )
        ).json()
      ).fleet;
      forkId = fork.id;
      forkWorkspaceId = fork.workspace_id && fork.workspace_id !== workspaceId ? fork.workspace_id : '';
    });
  });

  test.afterAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      if (forkId) await apiCtx.delete(`/api/v1/fleets/${forkId}`, { headers });
      if (forkWorkspaceId) await apiCtx.delete(`/api/v1/workspaces/${forkWorkspaceId}`, { headers });
      if (fleetId) await apiCtx.delete(`/api/v1/fleets/${fleetId}`, { headers });
      if (workspaceId) await apiCtx.delete(`/api/v1/workspaces/${workspaceId}`, { headers });
      if (agentId) await apiCtx.delete(`/api/v1/agents/${agentId}`, { headers });
    }).catch(() => undefined);
  });

  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test('the agent page header stays within the budget', async ({ page }) => {
    await page.goto(`/agents/${agentId}`);
    expect(await headerHeight(page)).toBeLessThanOrEqual(BUDGET_PX);
  });

  test('the fleet page header stays within the budget', async ({ page }) => {
    test.skip(!!fleetSkip, fleetSkip);
    await page.goto(`/fleets/${fleetId}`);
    expect(await headerHeight(page)).toBeLessThanOrEqual(BUDGET_PX);
  });

  // UX rounds 1 and 2 B1 on #10287: at 12 px the meta row of a fork, its
  // fullest form (orchestrator name, status, "Forked from", workspace chip),
  // wrapped to two lines at 1024. It stays one line (24 px, 26 with rounding)
  // in en and fr, the named facts truncated with their full text on hover.
  for (const language of ['en-us', 'fr-fr']) {
    test(`the meta row of a forked fleet stays one line at 1024 px in ${language}`, async ({ page }) => {
      test.skip(!!fleetSkip, fleetSkip);
      await page.route('**/api/v1/auth/me', async (route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...(await response.json()), language } });
      });
      await page.setViewportSize({ width: 1024, height: 900 });
      await page.goto(`/fleets/${forkId}`);
      const meta = page.getByTestId('entity-page-header-meta');
      await expect(meta).toBeVisible();
      await expect(meta).toHaveAttribute('data-one-line', 'true');
      await expect(page.getByTestId('fleet-provenance')).toBeVisible();
      // The orchestrator's name is drawn, not "No orchestrator" or a skeleton.
      await expect(page.getByTestId('fleet-orchestrator')).toContainText(`E2E header budget ${stamp}`);
      await expect(page.getByTestId('fleet-workspace-chip-workspace')).toBeVisible();
      const height = await meta.evaluate((el) => Math.round(el.getBoundingClientRect().height));
      expect(height).toBeLessThanOrEqual(26);
      // Nothing hangs past the row: every fact of the row ends inside it.
      const overflow = await meta.evaluate((el) => {
        const right = el.getBoundingClientRect().right;
        const facts = Array.from(el.querySelectorAll(':scope > [data-slot] > *'));
        return Math.max(0, ...facts.map((c) => Math.round(c.getBoundingClientRect().right - right)));
      });
      expect(overflow).toBeLessThanOrEqual(0);
    });
  }

  // UX round 4 B1 of #10181: with Modify with AI open the 400 px panel narrows the column; the
  // actions keep the title's line only and the header still fits the budget.
  test('the fleet page header stays within the budget with Modify with AI open', async ({ page }) => {
    test.skip(!!fleetSkip, fleetSkip);
    await page.goto(`/fleets/${fleetId}?ai=1`);
    await expect(page.getByTestId('entity-page-header-row')).toHaveAttribute('data-actions-placement', 'title');
    expect(await headerHeight(page)).toBeLessThanOrEqual(BUDGET_PX);
  });
});
