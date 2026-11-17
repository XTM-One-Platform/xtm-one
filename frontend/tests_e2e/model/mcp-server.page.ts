import { expect, type Page } from '@playwright/test';

export class MCPServerPage {
  constructor(private readonly page: Page) {}

  /**
   * Open the list page and wait for the servers list to be drawn.
   *
   * Loaded directly, not through the sidebar: opening the "Tools &
   * Integrations" group navigates to its hub page, whose own "MCP Servers"
   * card link races the sidebar link, and a test then lands on whichever page
   * won. The sidebar path is covered once, unambiguously, in navigation.spec.ts.
   *
   * The h1 is the endpoints strip, drawn before the servers query settles; the
   * search box is drawn only once the list has loaded, empty or not, so it is
   * the signal that "Add Server" and the rows are where they will stay. It is
   * found by its accessible name: the records table toolbar (standard v2) shows
   * the short "Search..." placeholder and keeps the full wording as the label.
   * An empty list draws its empty state instead of the table and its toolbar.
   */
  async gotoList(): Promise<void> {
    await this.page.goto('/mcp-servers');
    await this.page.getByRole('heading', { level: 1 }).waitFor({ state: 'visible' });
    await this.listSettled();
  }

  /** The list has loaded: the table toolbar's search, or the empty state when there is no server. */
  async listSettled(): Promise<void> {
    await this.searchBox()
      .or(this.page.getByText('No MCP servers yet', { exact: true }))
      .first()
      .waitFor({ state: 'visible', timeout: 15_000 });
  }

  /** The servers search of the table toolbar, by its accessible name (its placeholder is the short "Search..."). */
  searchBox() {
    return this.page.getByLabel(/search servers/i);
  }

  async clickNew(): Promise<void> {
    await this.page.getByRole('button', { name: 'Add Server' }).first().click();
    await this.page.getByRole('heading', { name: 'Add MCP Server' }).waitFor({ state: 'visible' });
  }

  /** Click the "Custom Server" dashed card to bypass the preset picker. */
  async selectCustom(): Promise<void> {
    // The Custom Server card is uniquely identified by its dashed border (Tailwind `border-dashed`),
    // distinguishing it from regular preset cards in the grid.
    await this.page.locator('.cursor-pointer.border-dashed').click();
    // Wait for the name input that appears in step 2 of the form.
    await this.page.getByPlaceholder('e.g. GitHub MCP').waitFor({ state: 'visible', timeout: 15_000 });
  }

  /**
   * Click a preset card by its name on step 1 of the create form.
   *
   * Scoped to the heading text rather than the card: the name also appears in
   * the search box's filtered results announcement in some browsers.
   */
  async selectPreset(name: string): Promise<void> {
    await this.page.getByPlaceholder(/search presets/i).fill(name);
    await this.page.locator('.cursor-pointer:not(.border-dashed)').filter({ hasText: name }).first().click();
    await this.page.getByRole('heading', { name: `Configure ${name}` }).waitFor({ state: 'visible', timeout: 15_000 });
  }

  commandInput() {
    return this.page.getByPlaceholder('npx');
  }

  argsInput() {
    return this.page.getByPlaceholder('-y @modelcontextprotocol/server-github');
  }

  urlInput() {
    return this.page.getByPlaceholder('https://mcp.example.com/mcp');
  }

  /**
   * Open the import dialog from the list page header.
   *
   * `gotoList` returns as soon as the page's h1 is up, which is the endpoints
   * strip — the servers list is still loading behind it, and a click that
   * lands while React swaps the skeleton for the real tree is simply lost.
   * So: wait for the list to settle, then retry the click until the dialog is
   * actually up, rather than assuming the first one took.
   */
  async openImport(): Promise<void> {
    await this.listSettled();

    const button = this.page.getByRole("button", { name: "Import", exact: true }).first();
    const heading = this.page.getByRole("heading", { name: "Import MCP servers" });
    await expect(async () => {
      await button.click();
      await expect(heading).toBeVisible({ timeout: 2_000 });
    }).toPass({ timeout: 20_000 });
  }

  /**
   * The visible toasts.
   *
   * Radix mirrors every toast into an off-screen `aria-live` announcement for
   * about a second, so an unscoped `getByText` matches the same message twice
   * while it lasts. The viewport is the only `<ol tabindex="-1">` on the page
   * and the announcement sits outside it.
   */
  toasts() {
    return this.page.locator('ol[tabindex="-1"] > li');
  }

  /** Hand the dialog a configuration document as if picked from disk. */
  async importFile(name: string, contents: string): Promise<void> {
    await this.page.locator('input[type="file"]').setInputFiles({
      name,
      mimeType: "application/json",
      buffer: Buffer.from(contents, "utf-8"),
    });
  }

  async fillName(name: string): Promise<void> {
    await this.page.getByPlaceholder('e.g. GitHub MCP').fill(name);
  }

  /**
   * Hard-load a path under /mcp-servers.
   *
   * This is the OAuth redirect target, so it has to survive a cold navigation:
   * the dev proxy used to forward every path starting with "/mcp" to the
   * backend and rendered its 404 JSON instead of the app.
   */
  async gotoUrl(path: string): Promise<void> {
    await this.page.goto(path);
  }

  async fillCommand(cmd: string): Promise<void> {
    await this.page.getByPlaceholder('npx').fill(cmd);
  }

  /** Switch the create form to the Streamable HTTP remote transport. */
  async selectStreamableHttp(): Promise<void> {
    await this.page.getByRole('button', { name: 'Streamable HTTP' }).click();
    await this.page.getByPlaceholder('https://mcp.example.com/mcp').waitFor({ state: 'visible' });
  }

  async fillUrl(url: string): Promise<void> {
    await this.page.getByPlaceholder('https://mcp.example.com/mcp').fill(url);
  }

  /** Switch a remote server's authentication from secret headers to OAuth 2.0. */
  async selectOAuthAuth(): Promise<void> {
    await this.page.getByRole('button', { name: 'OAuth 2.0' }).click();
    await this.detectSettingsButton().waitFor({ state: 'visible' });
  }

  detectSettingsButton() {
    return this.page.getByRole('button', { name: 'Detect settings' });
  }

  /** The OAuth endpoint inputs, addressed by their placeholders. */
  authorizeUrlInput() {
    return this.page.getByPlaceholder('https://provider.com/oauth/authorize');
  }

  tokenUrlInput() {
    return this.page.getByPlaceholder('https://provider.com/oauth/token');
  }

  registrationUrlInput() {
    return this.page.getByPlaceholder('https://provider.com/oauth/register');
  }

  scopesInput() {
    return this.page.getByPlaceholder('read write openid');
  }

  async fillArgs(args: string): Promise<void> {
    await this.page.getByPlaceholder('-y @modelcontextprotocol/server-github').fill(args);
  }

  async clickCreate(): Promise<void> {
    await this.page.getByRole('button', { name: 'Create Server' }).click();
  }

  async waitForDetail(): Promise<void> {
    await this.page.waitForURL(/\/mcp-servers\/[a-f0-9]+-/);
  }

  /** The id of the server whose detail page is open. */
  currentServerId(): string {
    const match = /\/mcp-servers\/([0-9a-f-]+)/.exec(new URL(this.page.url()).pathname);
    if (!match) throw new Error(`Not on an MCP server detail page: ${this.page.url()}`);
    return match[1];
  }

  async openConfigTab(): Promise<void> {
    // A DetailTabs tab: still an anchor, but its explicit role="tab" wins over
    // the implicit link role, so it is not reachable as a link.
    await this.page.getByRole('tab', { name: 'Configuration', exact: true }).click();
    await this.page.getByRole('button', { name: 'Edit' }).waitFor({ state: 'visible' });
  }

  async clickTestConnection(): Promise<void> {
    await this.page.getByRole('button', { name: 'Test connection' }).click();
  }

  async deleteCurrentServer(): Promise<void> {
    await this.page.locator('button.text-red-400').first().click();
    await this.page.getByRole('button', { name: 'Delete' }).click();
    await this.page.waitForURL('/mcp-servers');
  }
}
