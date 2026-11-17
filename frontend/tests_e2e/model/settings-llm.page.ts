import type { Page } from '@playwright/test';

export class SettingsLLMPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/settings/ai/models');
    // DetailTabs renders as <Link> (role="link"), not role="tab".
    // A DetailTabs tab, so role="tab" rather than the implicit link role.
    await this.page.getByRole('tab', { name: 'AI and models' }).waitFor({ state: 'visible' });
  }

  /** Click the first visible "Add Provider" button. */
  async clickAddProvider(): Promise<void> {
    await this.page.getByRole('button', { name: 'Add Provider' }).first().click();
    await this.page.getByRole('dialog').waitFor({ state: 'visible' });
  }

  async selectProvider(providerKey: string): Promise<void> {
    // The provider picker is a searchable field (role="combobox") labelled
    // "Provider"; a click opens the whole list.
    await this.page.getByRole('dialog').getByRole('combobox', { name: 'Provider', exact: true }).click();
    await this.page.getByRole('option', { name: new RegExp(providerKey, 'i') }).first().click();
  }

  async fillApiKey(key: string): Promise<void> {
    await this.page.getByRole('dialog').getByLabel('API key', { exact: true }).fill(key);
  }

  async closeDialog(): Promise<void> {
    await this.page.keyboard.press('Escape');
  }

  /** Returns the All Providers card locator. */
  allProvidersCard() {
    return this.page.locator('text=All Providers').first().locator('../..');
  }

  /** Returns all model tile buttons inside the All Providers card. */
  modelTiles() {
    return this.allProvidersCard().locator('button:has(svg.lucide-sparkles)');
  }

  /** Navigate to the models tab and wait for the All Providers heading. */
  async gotoModels(): Promise<void> {
    await this.goto();
    await this.page.getByText('All Providers').first().waitFor({ state: 'visible' });
  }
}
