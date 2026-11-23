import { expect, type Page } from '@playwright/test';

export class KnowledgeBasePage {
  constructor(private readonly page: Page) {}

  async gotoList(): Promise<void> {
    await this.page.goto('/knowledge');
    await this.page.getByRole('heading', { name: /Knowledge Base/i }).waitFor({ state: 'visible' });
    // Wait for the ?vis= landing replace: a click committed before it gets its navigation overwritten.
    await expect(this.page).toHaveURL(/\/knowledge\?vis=[a-z]+/);
  }

  async clickNewKB(): Promise<void> {
    // Until /platform/config resolves, the licence reads as Community Edition:
    // the button carries the "EE" badge (its name is "New Knowledge Base EE")
    // and AgenticGateDialog captures the click to open the upsell dialog, so a
    // click in that window never reaches /knowledge/new. The exact name only
    // matches once the licence is known and enabled, and click() waits for it.
    // first(): an empty list also shows the empty state's own action.
    await this.page.getByRole('button', { name: 'New Knowledge Base', exact: true }).first().click();
    await expect(this.page).toHaveURL(/\/knowledge\/new(\?|$)/);
    await expect(this.page.getByRole('heading', { name: 'New Knowledge Base' })).toBeVisible();
  }

  async fillName(name: string): Promise<void> {
    await this.page.getByPlaceholder('e.g. Product Documentation').fill(name);
  }

  async fillDescription(description: string): Promise<void> {
    await this.page.getByPlaceholder('What content will this knowledge base contain?').fill(description);
  }

  async clickCreate(): Promise<void> {
    await this.page.getByRole('button', { name: 'Create Knowledge Base' }).click();
  }

  async waitForDetail(): Promise<void> {
    await this.page.waitForURL(/\/knowledge\/[a-f0-9]+-/);
  }
}
