import { test, expect, type Page } from '@playwright/test';
import { ok, platformConfig, requireScriptedServer, withAdmin } from './helpers';
import { marker, ScriptedModel, SCRIPTED_MODEL, SCRIPTED_PROVIDER } from './scripted-llm/client';
import { ChatPage } from './model/chat.page';

/**
 * A turn outlives its connection (#6291, spec US2): it runs server-side, and
 * every view of the conversation catches up with it and follows it to its end.
 *
 * - a reload mid-turn shows the turn and keeps following it; a second tab on
 *   the conversation follows it too, and Stop pressed there ends it in both;
 * - a message sent while the conversation's event stream is reconnecting is
 *   shown once and answered once;
 * - a reload while a turn waits on a tool approval brings the prompt back, and
 *   the decision resumes the turn.
 *
 * The agents are pinned to the scripted model. A long turn is the model
 * calling the platform's own `wait`; the approval is `generate_file` on an
 * agent that asks before its tools (`approve_tool_calls`).
 */

const WEB_SESSIONS = '/api/v1/web/chat/sessions';
const EVENTS = /\/api\/v1\/web\/chat\/conversations\/[^/]+\/events$/;

const conversations: string[] = [];
const agents: string[] = [];
/** The name each agent was created with, by its id. */
const agentNames = new Map<string, string>();

async function createAgent(name: string, data: Record<string, unknown>): Promise<string> {
  return withAdmin(async (apiCtx, headers) => {
    const resp = await ok(
      await apiCtx.post('/api/v1/agents', {
        headers,
        data: { name, persona: 'You follow the script.', llm_provider: SCRIPTED_PROVIDER, llm_model: SCRIPTED_MODEL, ...data },
      }),
      'create agent',
    );
    const id = (await resp.json()).id as string;
    agents.push(id);
    agentNames.set(id, name);
    return id;
  });
}

/** Open a new chat with *agentId* and send *text*: the conversation the send created. */
async function startConversation(page: Page, agentId: string, text: string): Promise<string> {
  const chat = new ChatPage(page);
  const created = page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === WEB_SESSIONS,
  );
  await page.goto(`/chat?agent=${agentId}`);
  await chat.waitForComposer();
  await chat.waitForAgent(agentNames.get(agentId)!);
  await chat.send(text);
  const id = ((await (await created).json()) as { conversation_id: string }).conversation_id;
  conversations.push(id);
  await expect(page).toHaveURL(new RegExp(`[?&]c=${id}`));
  return id;
}

/** The composer's Stop: shown while a turn runs on screen. */
const stopButton = (page: Page) =>
  page.getByTestId('filigran-chat-composer').getByRole('button', { name: 'Stop generating', exact: true });

test.describe.serial('Chat - a turn outlives its connection', () => {
  test.describe.configure({ timeout: 180_000 });

  let scripted: ScriptedModel;
  let skipWhy = '';
  let longAgent = '';
  let askingAgent = '';

  test.beforeAll(async () => {
    skipWhy = await requireScriptedServer();
    if (!skipWhy && (await platformConfig()).chat_enabled === false) {
      skipWhy = 'chat is not enabled on this deployment; the send would answer 403.';
    }
    if (skipWhy) return;
    scripted = await ScriptedModel.connect();
    const stamp = Date.now();
    longAgent = await createAgent(`E2E resume ${stamp}`, { builtin_tool_ids: ['web_fetch'] });
    askingAgent = await createAgent(`E2E resume approval ${stamp}`, {
      builtin_tool_ids: ['generate_file'],
      approve_tool_calls: true,
    });
  });

  test.afterAll(async () => {
    await withAdmin(async (apiCtx, headers) => {
      for (const id of conversations) await apiCtx.delete(`/api/v1/chat/conversations/${id}`, { headers });
      for (const id of agents) await apiCtx.delete(`/api/v1/agents/${id}`, { headers });
    }).catch(() => undefined);
    await scripted?.dispose();
  });

  test('a reload and a second tab follow the running turn, and Stop in the second tab ends it in both', async ({
    page,
    context,
  }) => {
    test.skip(!!skipWhy, skipWhy);
    const mark = marker('resume');
    await scripted.arm(mark, [
      { tool: 'wait', arguments: { duration: 150, reason: 'e2e long turn' } },
      { text: `This answer is never written ${mark}` },
    ]);

    const chat = new ChatPage(page);
    const conversationId = await startConversation(page, longAgent, `${mark} take your time`);
    await expect(stopButton(page)).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => scripted.turnsServed(mark), { timeout: 30_000 }).toBeGreaterThanOrEqual(1);

    // The reload drops the stream that started the turn: the turn goes on, and
    // the page finds it again.
    await page.reload();
    await expect(chat.userMessages().filter({ hasText: `${mark} take your time` })).toHaveCount(1, { timeout: 30_000 });
    await expect(stopButton(page)).toBeVisible({ timeout: 30_000 });

    const second = await context.newPage();
    const chat2 = new ChatPage(second);
    await second.goto(`/chat?c=${conversationId}`);
    await expect(chat2.userMessages().filter({ hasText: `${mark} take your time` })).toHaveCount(1, { timeout: 30_000 });
    await expect(stopButton(second)).toBeVisible({ timeout: 30_000 });

    await stopButton(second).click();
    await expect(stopButton(second)).toBeHidden({ timeout: 30_000 });
    await expect(stopButton(page)).toBeHidden({ timeout: 30_000 });
    await expect(chat.transcript().getByText(`This answer is never written ${mark}`)).toHaveCount(0);
    await second.close();
  });

  test('a message sent while the event stream reconnects is shown once and answered once', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);
    const mark = marker('offline');
    await scripted.arm(mark, [{ text: `The only answer ${mark}` }]);

    // The conversation's event stream cannot be reached: the panel keeps
    // reconnecting while the message goes out and its answer streams back.
    let refused = 0;
    await page.route(
      (url) => EVENTS.test(url.pathname),
      async (route) => {
        refused += 1;
        await route.abort('internetdisconnected');
      },
    );
    const chat = new ChatPage(page);
    await startConversation(page, longAgent, `${mark} once`);
    await expect(chat.transcript().getByText(`The only answer ${mark}`)).toBeVisible({ timeout: 60_000 });
    await expect.poll(() => refused, { timeout: 30_000 }).toBeGreaterThan(0);

    // The stream comes back: what it catches up on is not shown again.
    await page.unroute((url) => EVENTS.test(url.pathname));
    const reconnected = page.waitForRequest((r) => EVENTS.test(new URL(r.url()).pathname), { timeout: 60_000 });
    await reconnected;
    await page.waitForTimeout(3_000);
    await expect(chat.userMessages().filter({ hasText: `${mark} once` })).toHaveCount(1);
    await expect(chat.assistantMessages().filter({ hasText: `The only answer ${mark}` })).toHaveCount(1);
    expect(await scripted.turnsServed(mark)).toBe(1);
  });

  test('a reload while the turn waits on an approval brings the prompt back, and the decision resumes it', async ({
    page,
  }) => {
    test.skip(!!skipWhy, skipWhy);
    const mark = marker('approval');
    await scripted.arm(mark, [
      { tool: 'generate_file', arguments: { filename: `resume-${mark}.txt`, content: 'resumed' } },
      { text: `Approved and written ${mark}` },
    ]);

    const chat = new ChatPage(page);
    await startConversation(page, askingAgent, `${mark} write the note`);
    await expect(chat.approval()).toBeVisible({ timeout: 60_000 });

    await page.reload();
    await expect(chat.approval()).toBeVisible({ timeout: 30_000 });
    await chat.approval().getByRole('button', { name: 'Yes', exact: true }).click();
    await expect(chat.transcript().getByText(`Approved and written ${mark}`)).toBeVisible({ timeout: 60_000 });
  });
});
