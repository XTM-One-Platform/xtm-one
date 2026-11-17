import { test, expect, type Page } from '@playwright/test';
import { ok, scriptedSkipReason, withAdmin } from './helpers';
import { marker, ScriptedModel } from './scripted-llm/client';
import { ChatPage } from './model/chat.page';

/**
 * The fleet setup interview (#11250), end to end: "Create with a prompt" on
 * the Fleets page opens a new chat with the default assistant and sends the
 * interview request on arrival; the assistant reads the steps with
 * `get_fleet_setup`, asks what the fleet is for, creates it with the existing
 * `create_fleet` function once the user confirmed, and reads it back. The
 * fleet is then read from the API: the chat alone created it, the form was
 * never opened.
 *
 * The request the button sends carries no marker, so the script is armed on
 * its own opening words (`createFleetPrompt`); every later turn carries them
 * too, in its history. The fleet and the conversation are removed in
 * `afterAll`.
 */

const WEB_SESSIONS = '/api/v1/web/chat/sessions';
const SETUP = 'get_fleet_setup';
const CREATE = 'create_fleet';
/** The opening words of the message the button sends: the script's match. */
const INTERVIEW = 'Help me set up a new fleet.';

interface FleetRow {
  id: string;
  name: string;
  description?: string | null;
}

async function fleetsNamed(name: string): Promise<FleetRow[]> {
  return withAdmin(async (apiCtx, headers) => {
    const rows: FleetRow[] = await (await ok(await apiCtx.get('/api/v1/fleets', { headers }), 'list the fleets')).json();
    return rows.filter((row) => row.name === name);
  });
}

const conversations: string[] = [];

function conversationCreated(page: Page) {
  return page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === WEB_SESSIONS,
  );
}

function offered(names: string[], tool: string): boolean {
  return names.some((name) => name === tool || name.endsWith(`:${tool}`));
}

test.describe.serial('Fleets: set up a fleet in chat', () => {
  test.describe.configure({ timeout: 180_000 });

  let scripted: ScriptedModel;
  let skipWhy = '';
  const fleetName = `E2E Chat Fleet ${Date.now()}`;

  test.beforeAll(async () => {
    skipWhy = await scriptedSkipReason(
      'chat_enabled',
      'chat is not enabled on this deployment; the interview would answer 403.',
    );
    if (skipWhy) return;
    scripted = await ScriptedModel.connect();
  });

  test.afterAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      for (const id of conversations) {
        await apiCtx.delete(`/api/v1/chat/conversations/${id}`, { headers });
      }
      for (const fleet of await fleetsNamed(fleetName)) {
        await apiCtx.delete(`/api/v1/fleets/${fleet.id}`, { headers });
      }
    }).catch(() => undefined);
    await scripted?.dispose();
  });

  test('the assistant interviews, creates the fleet on confirmation, and the fleet exists', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);

    const mark = marker('fleetsetup');
    const question = `What is this fleet for? ${mark}`;
    const proposal = `I will create the fleet "${fleetName}" for triaging phishing reports. Shall I? ${mark}`;
    const done = `Created the fleet "${fleetName}". ${mark}`;
    await scripted.arm(INTERVIEW, [
      { tool: SETUP, arguments: {} },
      { text: question },
      { text: proposal },
      { tool: CREATE, arguments: { name: fleetName, description: 'Triage of phishing reports' } },
      { text: done },
    ]);

    expect(await fleetsNamed(fleetName)).toHaveLength(0);

    await page.goto('/fleets');
    const start = page.getByRole('button', { name: 'Create with a prompt' }).first();
    await expect(start, 'the Fleets page offered no "Create with a prompt"').toBeEnabled({ timeout: 15_000 });

    const created = conversationCreated(page);
    await start.click();
    await expect(page).toHaveURL(/\/chat/);
    conversations.push((await (await created).json()).conversation_id);

    // The request went out on arrival, and the assistant asked the first question.
    const chat = new ChatPage(page);
    await expect(chat.transcript().getByText(INTERVIEW, { exact: false })).toBeVisible({ timeout: 30_000 });
    await expect(chat.transcript().getByText(question)).toBeVisible({ timeout: 60_000 });

    await chat.send(`Triage of phishing reports ${mark}`);
    await expect(chat.transcript().getByText(proposal)).toBeVisible({ timeout: 60_000 });

    await chat.send(`Yes, create it ${mark}`);
    await expect(chat.transcript().getByText(done)).toBeVisible({ timeout: 60_000 });

    // Both functions were offered; the fleet exists, read back from the API.
    const turns = await scripted.agentTurns(INTERVIEW);
    expect(turns.some((call) => offered(call.tools_offered, SETUP))).toBe(true);
    expect(turns.some((call) => offered(call.tools_offered, CREATE))).toBe(true);
    const fleets = await fleetsNamed(fleetName);
    expect(fleets).toHaveLength(1);
    expect(fleets[0].description).toBe('Triage of phishing reports');
  });
});
