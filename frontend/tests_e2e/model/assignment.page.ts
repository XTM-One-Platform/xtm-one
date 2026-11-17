import type { Page } from '@playwright/test';

export class AssignmentPage {
  constructor(private readonly page: Page) {}

  /** Click the "Add Assignment" button on the agent overview tab. */
  async clickAddAssignment(): Promise<void> {
    await this.page.getByRole('button', { name: 'Add Assignment' }).click();
  }

  async fillName(name: string): Promise<void> {
    await this.page.getByPlaceholder('Assignment name').fill(name);
  }

  async fillPrompt(prompt: string): Promise<void> {
    await this.page.getByPlaceholder('What should the agent do?').fill(prompt);
  }

  async clickCreate(): Promise<void> {
    await this.page.getByRole('button', { name: 'Create Assignment' }).click();
  }

  /** Wait until the assignment detail page is loaded. */
  async waitForDetail(): Promise<void> {
    await this.page.waitForURL(/\/assignments\/.{10,}/);
  }
}
