import { test, expect } from '@playwright/test';
import { ChatPage } from './model/chat.page';

// ---------------------------------------------------------------------------
// Chat — conversation creation and UI structure.
// No messages are sent; we verify the chat shell renders correctly without
// invoking any LLM. The page is the shared chat panel (see model/chat.page.ts).
// ---------------------------------------------------------------------------

test.describe('Chat — conversation creation', () => {
  test('should load the chat page', async ({ page }) => {
    const chatPage = new ChatPage(page);
    await chatPage.goto();
    await expect(page).toHaveURL(/\/chat/);
  });

  test('should render the conversation sidebar', async ({ page }) => {
    const chatPage = new ChatPage(page);
    await chatPage.goto();
    // The panel's list opens with its "New conversation" control.
    await expect(chatPage.newConversationButton()).toBeVisible({ timeout: 10_000 });
  });

  test('should show a New conversation button', async ({ page }) => {
    const chatPage = new ChatPage(page);
    await chatPage.goto();
    await expect(chatPage.newConversationButton()).toBeVisible({ timeout: 10_000 });
  });

  test('should open a blank composer when starting a new conversation', async ({ page }) => {
    const chatPage = new ChatPage(page);
    await chatPage.goto();
    await chatPage.clickNewConversation();
    await chatPage.waitForComposer();

    // The composer should be empty and ready.
    await expect(chatPage.composer()).toBeVisible();
    await expect(chatPage.composer()).toBeEmpty();
  });

  test('should keep the send button disabled when no text is typed', async ({ page }) => {
    const chatPage = new ChatPage(page);
    await chatPage.goto();
    await chatPage.clickNewConversation();
    await chatPage.waitForComposer();

    // The send button must be disabled until the user types something.
    await expect(chatPage.sendButton()).toBeDisabled();
  });

  test('should show the AI limitations notice under the composer', async ({ page }) => {
    const chatPage = new ChatPage(page);
    await chatPage.goto();
    await chatPage.waitForComposer();

    await expect(chatPage.disclaimer()).toHaveText(chatPage.disclaimerText());
  });
});
