import { test, expect } from '@playwright/test';
import { ok, withAdmin } from './helpers';

// ---------------------------------------------------------------------------
// Fleets: edit a fleet from its page.
//
// The admin (storageState) opens a fleet it owns, renames it from the
// settings side panel, adds a home member from the members panel, re-parents
// a member through the row menu (the keyboard path of the drag), then removes
// a member. Every step is checked against /api/v1/fleets, not only the toast.
// Fleets are an agentic AI feature of the Enterprise Edition: the file skips
// when the deployment answers 403 on fleet creation.
// ---------------------------------------------------------------------------

interface Member {
  id: string;
  agent_id: string | null;
  role: string;
  parent_member_id: string | null;
}

test.describe.serial('Fleets — edit a fleet from its page', () => {
  const stamp = Date.now();
  const FLEET_NAME = `E2E Fleet ${stamp}`;
  const RENAMED = `E2E Fleet renamed ${stamp}`;
  const LEAD = `E2E Fleet lead ${stamp}`;
  const ANALYST = `E2E Fleet analyst ${stamp}`;
  const NEWCOMER = `E2E Fleet newcomer ${stamp}`;
  const agentIds: Record<string, string> = {};
  let fleetId = '';
  let workspaceId = '';
  let skipReason = '';

  async function readMembers(): Promise<Member[]> {
    return withAdmin(async (apiCtx, headers) => {
      const resp = await ok(await apiCtx.get(`/api/v1/fleets/${fleetId}/members`, { headers }), 'list members');
      return (await resp.json()) as Member[];
    });
  }

  const memberOf = (members: Member[], agentName: string) =>
    members.find((m) => m.role === 'member' && m.agent_id === agentIds[agentName]);

  test.beforeAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      for (const name of [LEAD, ANALYST, NEWCOMER]) {
        const resp = await ok(
          await apiCtx.post('/api/v1/agents', { headers, data: { name, persona: 'Temp agent for fleet edition tests.' } }),
          'create agent',
        );
        agentIds[name] = (await resp.json()).id;
      }
      const fleetResp = await apiCtx.post('/api/v1/fleets', { headers, data: { name: FLEET_NAME } });
      if (fleetResp.status() === 403) {
        skipReason = 'Fleets are not licensed on this deployment (agentic AI, Enterprise Edition).';
        return;
      }
      const fleet = await (await ok(fleetResp, 'create fleet')).json();
      fleetId = fleet.id;
      workspaceId = fleet.workspace_id ?? '';
      for (const name of [LEAD, ANALYST]) {
        await ok(
          await apiCtx.post(`/api/v1/fleets/${fleetId}/members`, {
            headers,
            data: { role: 'member', agent_id: agentIds[name] },
          }),
          'add member',
        );
      }
    });
  });

  test.afterAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      // Deleting a fleet keeps its workspace and its agents: both go explicitly.
      if (fleetId) await apiCtx.delete(`/api/v1/fleets/${fleetId}`, { headers });
      if (workspaceId) await apiCtx.delete(`/api/v1/workspaces/${workspaceId}`, { headers });
      for (const id of Object.values(agentIds)) await apiCtx.delete(`/api/v1/agents/${id}`, { headers });
    }).catch(() => undefined);
  });

  test.beforeEach(() => {
    test.skip(!!skipReason, skipReason);
  });

  test('the settings side panel renames the fleet', async ({ page }) => {
    await page.goto(`/fleets/${fleetId}`);
    await expect(page.getByRole('heading', { level: 1, name: FLEET_NAME })).toBeVisible();

    await page.getByRole('button', { name: 'Settings', exact: true }).click();
    const panel = page.getByRole('dialog', { name: 'Fleet settings' });
    await expect(panel).toBeVisible();
    await panel.getByRole('textbox', { name: 'Name' }).fill(RENAMED);
    await panel.getByRole('button', { name: 'Save', exact: true }).click();

    await expect(panel).toBeHidden();
    await expect(page.getByRole('heading', { level: 1, name: RENAMED })).toBeVisible();
    await expect
      .poll(() =>
        withAdmin(async (apiCtx, headers) => {
          const resp = await ok(await apiCtx.get(`/api/v1/fleets/${fleetId}`, { headers }), 'read fleet');
          return (await resp.json()).name as string;
        }),
      )
      .toBe(RENAMED);
  });

  test('the members panel adds an agent with no home fleet', async ({ page }) => {
    await page.goto(`/fleets/${fleetId}/graph`);
    const picker = page.getByRole('combobox', { name: 'Add a member' });
    await picker.click();
    await picker.fill(NEWCOMER);
    await page.getByRole('option', { name: NEWCOMER }).click();

    await expect.poll(async () => !!memberOf(await readMembers(), NEWCOMER)).toBe(true);
    await expect(page.getByRole('button', { name: `Actions for ${NEWCOMER}` })).toBeVisible();
  });

  test('the row menu re-parents a member under another', async ({ page }) => {
    await page.goto(`/fleets/${fleetId}/graph`);
    await page.getByRole('button', { name: `Actions for ${ANALYST}` }).click();
    await page.getByRole('menuitem', { name: /Change parent/ }).click();

    const dialog = page.getByRole('dialog', { name: 'Change parent' });
    await expect(dialog).toBeVisible();
    const parent = dialog.getByRole('combobox', { name: 'Parent' });
    await parent.click();
    await parent.fill(LEAD);
    await page.getByRole('option', { name: LEAD }).click();
    await dialog.getByRole('button', { name: 'Save', exact: true }).click();

    await expect
      .poll(async () => {
        const members = await readMembers();
        return memberOf(members, ANALYST)?.parent_member_id === memberOf(members, LEAD)?.id;
      })
      .toBe(true);
  });

  test('the row menu removes a member after a confirmation, the agent kept', async ({ page }) => {
    await page.goto(`/fleets/${fleetId}/graph`);
    await page.getByRole('button', { name: `Actions for ${NEWCOMER}` }).click();
    await page.getByRole('menuitem', { name: 'Remove from the fleet' }).click();
    // Removing asks first, as from the graph's node menu.
    const confirm = page.getByRole('dialog').filter({ hasText: `Remove ${NEWCOMER} from the fleet?` });
    await expect(confirm).toBeVisible();
    await confirm.getByRole('button', { name: 'Remove', exact: true }).click();

    await expect.poll(async () => !!memberOf(await readMembers(), NEWCOMER)).toBe(false);
    await expect(page.getByRole('button', { name: `Actions for ${NEWCOMER}` })).toBeHidden();
    // Leaving a fleet is never an agent edit: the agent is still there.
    const status = await withAdmin(async (apiCtx, headers) =>
      (await apiCtx.get(`/api/v1/agents/${agentIds[NEWCOMER]}`, { headers })).status(),
    );
    expect(status).toBe(200);
  });
});
