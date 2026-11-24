import { test, expect, type Page } from '@playwright/test';
import { ok, scriptedSkipReason, withAdmin } from './helpers';
import { marker, ScriptedModel } from './scripted-llm/client';
import { ChatPage } from './model/chat.page';

/**
 * The chat onboarding of the organization profile (#11224), end to end:
 * "Continue onboarding in chat" on the profile page opens a new chat with the
 * default assistant and sends the interview request; the assistant reads the
 * next field with `organization_onboarding`, asks, saves the answer as a new
 * version, records a skip, and the profile page shows the result.
 *
 * The request the button sends carries no marker, so the script is armed on
 * its own opening words (`organizationOnboardingPrompt`); every later turn of
 * the conversation carries them too, in its history. The profile is put back
 * as it was in `afterAll`.
 */

const API = '/api/v1/organization-context';
const WEB_SESSIONS = '/api/v1/web/chat/sessions';
const TOOL = 'organization_onboarding';
/** The opening words of the message the button sends: the script's match. */
const INTERVIEW = 'Help me complete our organization profile.';

interface ProfileState {
  version: number | null;
  profile: Record<string, unknown>;
  missing_fields: string[];
}

async function readProfile(): Promise<ProfileState> {
  return withAdmin(async (apiCtx, headers) =>
    (await ok(await apiCtx.get(API, { headers }), 'read the organization profile')).json(),
  );
}

async function saveProfile(profile: Record<string, unknown>): Promise<ProfileState> {
  return withAdmin(async (apiCtx, headers) =>
    (await ok(await apiCtx.put(API, { headers, data: { profile, source: 'form' } }), 'save the organization profile')).json(),
  );
}

const conversations: string[] = [];

function conversationCreated(page: Page) {
  return page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === WEB_SESSIONS,
  );
}

test.describe.serial('Organization profile: onboarding in chat', () => {
  test.describe.configure({ timeout: 180_000 });

  let scripted: ScriptedModel;
  let skipWhy = '';
  let original: ProfileState | null = null;

  test.beforeAll(async () => {
    skipWhy = await scriptedSkipReason(
      'chat_enabled',
      'chat is not enabled on this deployment; the interview would answer 403.',
    );
    if (skipWhy) return;
    scripted = await ScriptedModel.connect();
    original = await readProfile();
    // A known start: a name and a sector, the currency and the hourly rate missing.
    await saveProfile({ name: 'E2E Onboarding Org', sector: 'Technology' });
  });

  test.afterAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      for (const id of conversations) {
        await apiCtx.delete(`/api/v1/chat/conversations/${id}`, { headers });
      }
    }).catch(() => undefined);
    if (original?.version != null) await saveProfile(original.profile).catch(() => undefined);
    await scripted?.dispose();
  });

  test('the assistant asks, saves the answer, records a skip, and the page shows it', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);

    const mark = marker('orgonboard');
    const question = `Which currency are your amounts in, and what is your loaded hourly rate? ${mark}`;
    const done = `Saved: EUR and 95 per hour; ticketing target skipped. ${mark}`;
    await scripted.arm(INTERVIEW, [
      { tool: TOOL, arguments: { action: 'next' } },
      { text: question },
      { tool: TOOL, arguments: { action: 'save', answers: { currency: 'EUR', loaded_hourly_rate: 95 } } },
      { tool: TOOL, arguments: { action: 'skip', fields: ['ticketing'] } },
      { text: done },
    ]);

    const before = await readProfile();
    expect(before.missing_fields).toContain('currency');

    await page.goto('/settings/organization');
    const resume = page.getByTestId('settings.organization.continue-onboarding');
    await expect(resume, 'the profile page offered no "Continue onboarding in chat"').toBeVisible({ timeout: 15_000 });

    const created = conversationCreated(page);
    await resume.click();
    await expect(page).toHaveURL(/\/chat/);
    conversations.push((await (await created).json()).conversation_id);

    // The request went out on arrival, and the assistant asked the first question.
    const chat = new ChatPage(page);
    await expect(chat.transcript().getByText(INTERVIEW, { exact: false })).toBeVisible({ timeout: 30_000 });
    await expect(chat.transcript().getByText(question)).toBeVisible({ timeout: 60_000 });

    await chat.send(`EUR, 95 an hour, and skip the ticketing target ${mark}`);
    await expect(chat.transcript().getByText(done)).toBeVisible({ timeout: 60_000 });

    // The tool was offered and called: the answers are a new version, read back from the API.
    const turns = await scripted.agentTurns(INTERVIEW);
    expect(turns.some((call) => call.tools_offered.some((name) => name === TOOL || name.endsWith(`:${TOOL}`)))).toBe(true);
    const after = await readProfile();
    expect(after.version ?? 0).toBeGreaterThan(before.version ?? 0);
    expect(after.profile.currency).toBe('EUR');
    expect(after.profile.loaded_hourly_rate).toBe(95);
    expect(after.profile.onboarding_skipped).toContain('ticketing');
    expect(after.missing_fields).not.toContain('currency');

    // The page reads the same state: the hourly rate is shown in the saved currency.
    await page.goto('/settings/organization');
    await expect(page.getByTestId('settings.organization.read.loaded_hourly_rate')).toContainText('EUR', { timeout: 15_000 });
  });
});
