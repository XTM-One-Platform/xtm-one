import { test, expect, type Page } from '@playwright/test';
import { scriptedSkipReason, withAdmin } from './helpers';
import { marker, ScriptedModel } from './scripted-llm/client';
import { ChatPage } from './model/chat.page';

/**
 * A chat turn: sent from the composer, answered, rendered, persisted, on the
 * chat panel (see model/chat.page.ts). chat.spec.ts covers the shell and needs
 * no model.
 *
 * One prose step per script, because the conversation-naming call carries the
 * same text and a spent script replays its trailing prose for both.
 */

/** Conversations "+" created, removed in `afterAll`. */
const conversations: string[] = [];

/** The POST that creates the conversation, with its first message. */
function conversationCreated(page: Page) {
  return page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/web/chat/sessions',
  );
}

/** Start a new conversation: the panel creates it with the first message (`send`). */
async function newConversation(chat: ChatPage): Promise<void> {
  await chat.clickNewConversation();
  await chat.waitForComposer();
}

/** Type into the composer and send. */
async function send(page: Page, chat: ChatPage, text: string): Promise<void> {
  const created = new URL(page.url()).searchParams.get('c') ? null : conversationCreated(page);
  await chat.send(text);
  if (created) conversations.push((await (await created).json()).conversation_id);
}

test.describe.serial('Chat — a turn with a scripted model', () => {
  // The first test declares 120s of sequential waits: 30s for the message to
  // render, 60s for the answer, 30s after the reload.
  test.describe.configure({ timeout: 150_000 });

  let scripted: ScriptedModel;
  let skipWhy = '';

  test.beforeAll(async () => {
    // `require_chat_enabled` answers 403 rather than skipping.
    skipWhy = await scriptedSkipReason(
      'chat_enabled',
      'chat is not enabled on this deployment; the send would answer 403.',
    );
    if (skipWhy) return;
    scripted = await ScriptedModel.connect();
  });
  test.afterAll(async () => {
    // "+" leaves a conversation behind on every test; on a persistent database
    // they pile up in the admin's sidebar.
    await withAdmin(async (apiCtx, headers) => {
      for (const id of conversations) {
        await apiCtx.delete(`/api/v1/chat/conversations/${id}`, { headers });
      }
    }).catch(() => undefined);
    await scripted?.dispose();
  });

  test('the answer reaches the screen and survives a reload', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);

    const mark = marker('chatturn');
    const answer = `The scripted assistant answers ${mark}.`;
    await scripted.arm(mark, [{ text: answer }]);

    const chat = new ChatPage(page);
    await chat.goto();
    await newConversation(chat);

    await send(page, chat, `${mark} say the scripted thing`);

    // The user's message first: it separates "no answer came" from "nothing was sent".
    await expect(chat.transcript().getByText(`${mark} say the scripted thing`)).toBeVisible({ timeout: 30_000 });
    await expect(chat.transcript().getByText(answer)).toBeVisible({ timeout: 60_000 });

    // A turn held in React state alone looks identical until the page reloads.
    await page.reload();
    await expect(chat.transcript().getByText(answer)).toBeVisible({ timeout: 30_000 });
  });

  test('the turn goes over the streaming path, not the REST one', async ({ page }) => {
    test.skip(!!skipWhy, skipWhy);

    const mark = marker('chatagent');
    await scripted.arm(mark, [{ text: `Streamed answer ${mark}` }]);

    const chat = new ChatPage(page);
    await chat.goto();
    await newConversation(chat);
    await send(page, chat, `${mark} answer me`);
    await expect(chat.transcript().getByText(`Streamed answer ${mark}`)).toBeVisible({ timeout: 60_000 });

    // `agentTurns` drops the conversation-naming call, which carries the same text.
    const turns = await scripted.agentTurns(mark);
    expect(turns.length, 'expected exactly one agent turn').toBe(1);
    // A fallback to REST answers correctly but loses streamed rendering and the
    // tool-call trace, which only the stream writes.
    expect(turns[0].streamed).toBe(true);
    // Tools are not asserted: whether the general assistant carries any is a
    // property of the deployment, not of the chat page.
  });
});
