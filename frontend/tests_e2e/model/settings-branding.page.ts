import type { Page } from '@playwright/test';

export interface FilePayload {
  name: string;
  mimeType: string;
  buffer: Buffer;
}

export class SettingsBrandingPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/settings/platform');
  }

  async waitForBrandingCard(): Promise<void> {
    await this.page
      .getByRole('heading', { name: 'Platform Branding' })
      .waitFor({ state: 'visible' });
  }

  async clickEdit(): Promise<void> {
    await this.page.getByRole('button', { name: 'Edit', exact: true }).click();
  }

  async cancel(): Promise<void> {
    await this.page.getByRole('button', { name: 'Cancel' }).click();
  }

  async save(): Promise<void> {
    await this.page.getByRole('button', { name: 'Save' }).click();
    await this.page
      .getByRole('button', { name: 'Edit', exact: true })
      .waitFor({ state: 'visible' });
  }

  async fillName(value: string): Promise<void> {
    await this.page.getByLabel('Platform Name', { exact: true }).fill(value);
  }

  async fillDescription(value: string): Promise<void> {
    await this.page.getByLabel('Description', { exact: true }).fill(value);
  }

  // The hex input is the control each colour label names (the swatch shares the
  // name by aria-labelledby, so it is excluded by type).
  private hexInput(label: string) {
    return this.page.getByLabel(label, { exact: true }).and(this.page.locator('input:not([type="color"])'));
  }

  async setAccentColor(hex: string): Promise<void> {
    await this.hexInput('Accent Color').fill(hex);
  }

  async setBackgroundColor(hex: string): Promise<void> {
    await this.hexInput('Background Color').fill(hex);
  }

  async setThemeColor(hex: string): Promise<void> {
    await this.hexInput('Theme Color').fill(hex);
  }

  // Each upload label names its (hidden) file input through htmlFor.
  private fileInput(label: string) {
    return this.page.getByLabel(label, { exact: true }).and(this.page.locator('input[type="file"]'));
  }

  async uploadLogo(file: FilePayload): Promise<void> {
    await this.fileInput('Logo').setInputFiles(file);
  }

  async uploadFavicon(file: FilePayload): Promise<void> {
    await this.fileInput('Favicon').setInputFiles(file);
  }

  async uploadOgImage(file: FilePayload): Promise<void> {
    await this.fileInput('Social Preview Image (OG Image)').setInputFiles(file);
  }
}
