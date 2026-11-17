import { expect, type Locator, type Page } from '@playwright/test';

export class AgentPage {
  constructor(private readonly page: Page) {}

  async gotoList(): Promise<void> {
    await this.page.goto('/agents');
    await this.page.getByRole('heading', { name: 'Agents', exact: false }).waitFor({ state: 'visible' });
  }

  async clickNewAgent(): Promise<void> {
    // Until /platform/config resolves, the licence reads as Community Edition:
    // the button carries the "EE" badge (its name is "New Agent EE") and
    // AgenticGateDialog captures the click to open the upsell dialog, so a
    // click in that window never reaches /agents/new (#11261, the cold first
    // test of a worker). The exact name only matches once the licence is known
    // and enabled, and click() waits for it; the URL is then asserted.
    await this.page.getByRole('button', { name: 'New Agent', exact: true }).click();
    await expect(this.page).toHaveURL(/\/agents\/new(\?|$)/);
    // /agents/new is the creation-mode chooser; Advanced opens the template picker.
    await this.page.getByRole('heading', { name: 'Create New Agent' }).waitFor({ state: 'visible' });
    await this.page.getByRole('button', { name: 'Advanced' }).click();
    // Exact: the page subtitle "Choose a template or start from scratch." also
    // contains the words, and a substring match resolves to both.
    await this.page.getByText('From Scratch', { exact: true }).waitFor({ state: 'visible' });
  }

  /** Select the "Empty agent" / from-scratch card and proceed to the config step. */
  async selectScratch(): Promise<void> {
    // The dashed card with "Empty agent" text triggers handleSelect(null).
    await this.page.getByText('Empty agent').first().click();
  }

  async fillName(name: string): Promise<void> {
    // By accessible name, not by label text: the label carries a required mark
    // ("Agent Name *"), the input keeps aria-label "Agent Name".
    await this.page.getByRole('textbox', { name: 'Agent Name', exact: true }).fill(name);
  }

  async fillPersona(persona: string): Promise<void> {
    await this.page.locator('textarea[placeholder*="Describe who you are"]').fill(persona);
  }

  async clickCreate(): Promise<void> {
    await this.page.getByRole('button', { name: 'Create Agent' }).click();
  }

  async waitForDetail(): Promise<void> {
    // UUID-like IDs contain hyphens; avoids matching /agents/new.
    await this.page.waitForURL(/\/agents\/[a-f0-9]+-/);
  }

  /**
   * The favorite star on a named agent's card (grid view, the default). Its
   * accessible name carries the state: "Add to favorites" when off, "Remove
   * from favorites" when on. Scoped by the card, not by its link: the star is
   * deliberately a sibling of the anchor, not a descendant.
   */
  favoriteStar(agentName: string) {
    return this.page
      .getByTestId('agent-card')
      .filter({ hasText: agentName })
      .first()
      .getByRole('button', { name: /favorites/ });
  }

  /** Stars of every card/row in DOM order — favorites are pinned to the front. */
  allFavoriteStars() {
    return this.page.getByRole('button', { name: /favorites/ });
  }

  /** The favorite star in a named agent's row (list view). */
  favoriteStarInRow(agentName: string) {
    return this.page
      .locator('tbody tr')
      .filter({ hasText: agentName })
      .first()
      .getByRole('button', { name: /favorites/ });
  }

  /** Switch the list page between the card grid and the sortable table. */
  async setViewMode(mode: 'grid' | 'list'): Promise<void> {
    await this.page.getByRole('button', { name: mode === 'grid' ? 'Grid view' : 'List view' }).click();
  }

  /** Click a column header to sort by it; clicking again reverses the order. */
  async sortByColumn(header: string): Promise<void> {
    // The star's tooltip ("Favorite — pinned to the top of the list") can stay
    // open after a toggle and cover the header. Close it deterministically:
    // Escape dismisses any open Radix tooltip, the blur removes the focus that
    // reopens it, and the pointer goes to the header itself rather than to a
    // corner of the page, where another control's tooltip can open (the old
    // move to (0, 0) did, and the unscoped assertion then counted it).
    const target = this.page.getByRole('button', { name: header, exact: true });
    await this.page.keyboard.press('Escape');
    await this.page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    await expect(
      this.page.getByRole('tooltip').filter({ hasText: /favorites|pinned to the top of the list/i }),
    ).toHaveCount(0);
    await target.hover();
    await target.click();
  }

  /**
   * Star/un-star and wait for the preference write to land. Without the wait a
   * following reload can abort the in-flight PUT, which is exactly the kind of
   * intermittent failure that erodes trust in the suite.
   */
  async toggleFavoriteAndWait(star: Locator): Promise<void> {
    const saved = this.page.waitForResponse(
      (r) => r.url().includes('/settings/preferences') && r.request().method() === 'PUT' && r.ok(),
    );
    await star.click();
    await saved;
  }

  async deleteCurrentAgent(): Promise<void> {
    // Was located by its red text colour, which is two kinds of fragile: the
    // codemod rewrote that palette class, and the button it pointed at had no
    // accessible name at all. Both are fixed at the source, so this can now ask
    // for the button by what it says it does.
    await this.page.getByRole('button', { name: 'Delete agent' }).click();
    await this.page.getByRole('button', { name: 'Delete' }).click();
    await this.page.waitForURL('/agents');
  }
}
