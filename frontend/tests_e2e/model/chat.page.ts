import { expect, type Locator, type Page } from '@playwright/test';

/**
 * The /chat page: the shared `@filigran/chatbot` panel, through its
 * `filigran-chat-*` test ids. The specs ask for what they need (the composer,
 * the transcript, a send) and this model answers with the panel's handles.
 */
export class ChatPage {
  constructor(private readonly page: Page) {}

  /**
   * The chat page on a new conversation. `?new=1` keeps it there: the bare
   * `/chat` resumes the last conversation opened, which on a shared
   * database is whatever an earlier test left behind.
   */
  async goto(): Promise<void> {
    await this.page.goto('/chat?new=1');
    await this.root().waitFor({ state: 'attached', timeout: 15_000 });
  }

  /** The panel's root. */
  root(): Locator {
    return this.page.getByTestId('filigran-chat-panel');
  }

  /** Start a new conversation from the conversation list. */
  async clickNewConversation(): Promise<void> {
    await this.newConversationButton().click();
  }

  /** The control that starts a new conversation, atop the conversation list. */
  newConversationButton(): Locator {
    return this.root().getByRole('button', { name: 'New conversation', exact: true }).first();
  }

  /** The composer's text field. */
  composer(): Locator {
    return this.root().getByTestId('filigran-chat-composer').locator('textarea');
  }

  /** The button that sends the composer's text. */
  sendButton(): Locator {
    return this.root().getByTestId('filigran-chat-composer').getByRole('button', { name: 'Send', exact: true });
  }

  /** The composer's Stop, shown in place of Send while a turn runs. */
  stopButton(): Locator {
    return this.root().getByTestId('filigran-chat-composer').getByRole('button', { name: 'Stop generating', exact: true });
  }

  /** Wait until no turn is running, so the next send starts a new turn. */
  async waitForTurnSettled(timeout = 60_000): Promise<void> {
    await expect(this.stopButton()).toHaveCount(0, { timeout });
  }

  /** The notice under the composer that AI can be wrong. */
  disclaimer(): Locator {
    return this.page.getByTestId('filigran-chat-disclaimer');
  }

  /** What that notice says. */
  disclaimerText(): string {
    return 'AI can make mistakes. Check important information.';
  }

  /**
   * The message list. Scope text assertions here: the sidebar shows the same
   * string, since the first message becomes the conversation title.
   */
  transcript(): Locator {
    return this.page.getByTestId('filigran-chat-transcript');
  }

  /** The user's messages in the transcript. */
  userMessages(): Locator {
    return this.page.getByTestId('filigran-chat-user-message');
  }

  /** The assistant's replies in the transcript. */
  assistantMessages(): Locator {
    return this.page.getByTestId('filigran-chat-assistant-message');
  }

  /** A tool-approval prompt. */
  approval(): Locator {
    return this.page.getByTestId('filigran-chat-approval');
  }

  /** Wait for the chat composer textarea to be ready (new or empty conversation). */
  async waitForComposer(): Promise<void> {
    await this.composer().waitFor({ state: 'visible', timeout: 10_000 });
  }

  /**
   * Wait until the panel has picked the agent *name*, which it does once its
   * agent list has loaded: a message sent before that goes with no agent, to
   * the user's default one. The header names it on a link to the agent where
   * the user may open it, on the picker's button otherwise.
   */
  async waitForAgent(name: string): Promise<void> {
    const named = this.root().getByRole('link', { name }).or(this.root().getByRole('button', { name }));
    await named.first().waitFor({ state: 'visible', timeout: 15_000 });
  }

  /** Type *text* into the composer and send it. */
  async send(text: string): Promise<void> {
    await this.composer().fill(text);
    await this.composer().press('Enter');
  }
}
