import type { Page } from '@playwright/test';

export class UserManagementPage {
  constructor(private readonly page: Page) {}

  async gotoList(): Promise<void> {
    await this.page.goto('/users-and-groups');
    await this.page.getByRole('heading', { name: /Users/i }).waitFor({ state: 'visible' });
  }

  async clickNewUser(): Promise<void> {
    await this.page.getByRole('button', { name: 'New User' }).click();
    await this.page.getByRole('heading', { name: 'New User' }).waitFor({ state: 'visible' });
  }

  async waitForDetail(): Promise<void> {
    await this.page.waitForURL(/\/users\/.{10,}/);
  }
}
