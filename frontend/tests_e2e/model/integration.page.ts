import type { Page } from '@playwright/test';

export class IntegrationPage {
  constructor(private readonly page: Page) {}

  async gotoList(): Promise<void> {
    await this.page.goto('/integrations');
    await this.page.getByRole('heading', { name: /Integration/i }).waitFor({ state: 'visible' });
  }

  /** Switch the list page between the catalog grid and the instance table. */
  async setViewMode(mode: 'grid' | 'list'): Promise<void> {
    await this.page.getByRole('button', { name: mode === 'grid' ? 'Grid view' : 'List view' }).click();
  }

  /**
   * Pick a provider by display name on the catalog, landing on the creation
   * form.
   *
   * The catalog is /integrations itself; /integrations/new without a provider
   * redirects there, so a caller may start from either. A provider the viewer
   * already runs opens what they have rather than a blank form, so creating
   * another one goes through that card's own "Add another".
   */
  async selectProvider(displayName: string): Promise<void> {
    // Wait until the catalog has rendered at least one provider card.
    await this.page.getByTestId('provider-card').first().waitFor({ state: 'visible', timeout: 10_000 });

    const searchInput = this.page.getByPlaceholder(/[Ss]earch (the marketplace|integrations)/i);
    const providerLabel = this.page.getByText(displayName, { exact: true }).first();
    if (await searchInput.isVisible()) {
      await searchInput.fill(displayName);
      // Wait for the provider card to appear in the filtered results.
      await providerLabel.waitFor({ state: 'visible' });
    }

    const card = this.page
      .getByTestId('provider-card')
      .filter({ has: this.page.getByText(displayName, { exact: true }) })
      .first();
    const addAnother = card.getByRole('button', { name: /Add another/i });
    if (await addAnother.count() > 0) await addAnother.click();
    else await card.click();

    // Wait for the config form step to appear.
    await this.page.getByPlaceholder(/e\.g\. My/i).waitFor({ state: 'visible', timeout: 10_000 });
  }

  async fillName(name: string): Promise<void> {
    // Placeholder is dynamic: "e.g. My HubSpot" etc. depending on the selected provider.
    await this.page.getByPlaceholder(/e\.g\. My/i).fill(name);
  }

  async fillApiKey(key: string): Promise<void> {
    await this.page.getByPlaceholder(/Paste API key/i).first().fill(key);
  }

  async clickSave(): Promise<void> {
    await this.page.getByRole('button', { name: 'Create integration' }).click();
  }

  async waitForSave(): Promise<void> {
    // Integration creation navigates to the list page (/integrations), not a detail URL.
    await this.page.waitForURL((url) => !url.toString().includes('/integrations/new'));
  }

  async waitForDetail(): Promise<void> {
    await this.page.waitForURL(/\/integrations\/.{10,}/);
  }

  async deleteCurrentIntegration(): Promise<void> {
    await this.page.getByRole('button', { name: /delete|Delete/i }).first().click();
    await this.page.getByRole('button', { name: 'Delete' }).click();
    await this.page.waitForURL(/\/integrations(\?vis=[a-z]+)?$/);
  }
}
