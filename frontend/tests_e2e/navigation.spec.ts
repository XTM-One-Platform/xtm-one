import { test, expect } from '@playwright/test';
import { expandNavGroup } from './helpers';
import { ChatPage } from './model/chat.page';

// ---------------------------------------------------------------------------
// Smoke — Navigation
//
// Verifies that each main route loads correctly: URL is correct and the
// primary page heading is visible, confirming the component actually rendered.
// MCP Servers is navigated via the sidebar because the /mcp-servers path is
// intercepted by the Vite dev-server MCP proxy.
// ---------------------------------------------------------------------------

test.describe('Smoke — Navigation', () => {
  test('should display the dashboard', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL('/');
    // The dashboard routes between multiple states (WelcomeLanding,
    // CtemCommandCenter, CtemTrialSplash) depending on platform tier and
    // registrations. Asserting the
    // sidebar navigation ensures the layout rendered without errors regardless of state.
    await expect(page.getByRole('link', { name: 'Home', exact: true })).toBeVisible();
  });

  test('should navigate to the Agents page', async ({ page }) => {
    await page.goto('/agents');
    await expect(page).toHaveURL(/\/agents(\?vis=[a-z]+)?$/);
    await expect(page.getByRole('heading', { name: 'Agents', level: 1 })).toBeVisible();
  });

  test('should navigate to the Knowledge Bases page', async ({ page }) => {
    await page.goto('/knowledge');
    await expect(page).toHaveURL(/\/knowledge(\?vis=[a-z]+)?$/);
    await expect(page.getByRole('heading', { name: 'Knowledge Bases', level: 1 })).toBeVisible();
  });

  test('should navigate to the Integrations page', async ({ page }) => {
    await page.goto('/integrations');
    await expect(page).toHaveURL(/\/integrations(\?vis=[a-z]+)?$/);
    await expect(page.getByRole('heading', { name: 'Integrations', level: 1 })).toBeVisible();
  });

  test('should navigate to the Flows page', async ({ page }) => {
    await page.goto('/flows');
    await expect(page).toHaveURL(/\/flows(\?vis=[a-z]+)?$/);
    await expect(page.getByRole('heading', { name: 'Agentic Flow', level: 1 })).toBeVisible();
  });

  test('should navigate to the Chat page', async ({ page }) => {
    const chat = new ChatPage(page);
    await page.goto('/chat');
    // The bare `/chat` resumes the last conversation: `?c=` may follow.
    await expect(page).toHaveURL(/\/chat(\?c=[^&]+)?$/);
    // The chat panel, with its conversation list's "New conversation" and the
    // composer, renders whatever the conversations are.
    await expect(chat.root()).toBeVisible();
    await expect(chat.newConversationButton()).toBeVisible();
    await expect(chat.composer()).toBeVisible();
  });

  test('should navigate to the MCP servers page', async ({ page }) => {
    await page.goto('/');
    // First entry of the "Tools & Integrations" group: an MCP server is where
    // most agent tools come from, so it sits with the tools it feeds rather
    // than at the top level. The group is collapsed on a fresh profile, so it
    // is opened before the link is clicked.
    await expandNavGroup(page, 'Tools & Integrations');
    // Scoped to the sidebar and exact: opening the group also navigates to its
    // hub page, whose "MCP Servers" card is a link too.
    await page
      .getByRole('navigation', { name: 'Main navigation' })
      .getByRole('link', { name: 'MCP Servers', exact: true })
      .click();
    await expect(page).toHaveURL(/\/mcp-servers(\?vis=[a-z]+)?$/);
    // Two sections, opposite directions. Asserting both names guards the
    // distinction itself: they are deliberately given different head nouns so
    // they cannot be read as two spellings of one thing.
    await expect(page.getByRole('heading', { name: 'MCP Endpoints', level: 1 })).toBeVisible();
    await expect(
      page.getByRole('heading', { name: 'Connected MCP Servers', level: 2 }),
    ).toBeVisible();
  });

  test('should open the Agent Studio hub from the sidebar group heading', async ({ page }) => {
    await page.goto('/');
    // The heading itself opens the hub and unfolds the group beside it. The
    // design system's expanded heading is an accordion trigger, so it is a
    // button rather than the link it used to be.
    const heading = page
      .getByRole('navigation', { name: 'Main navigation' })
      .getByRole('button', { name: 'Agent Studio' });
    await heading.click();
    await expect(page).toHaveURL('/agent-studio');
    await expect(heading).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByRole('heading', { name: 'Agent Studio', level: 1 })).toBeVisible();
    // The hub is a management surface, not a copy of the menu: it features the
    // agent builder and gives every resource its own create action.
    // (`.first()` — the sidebar carries the same labels; either match proves the
    // hub rendered, since the heading assertion above already pins the page.)
    await expect(page.getByRole('link', { name: /Create an agent/ }).first()).toBeVisible();
  });

  test('should collapse to a rail showing the product mark, every icon on one axis', async ({ page }) => {
    await page.goto('/');
    const nav = page.getByRole('navigation', { name: 'Main navigation' });
    await nav.getByRole('button', { name: 'Collapse' }).click();
    await expect(nav.getByRole('button', { name: 'Expand' })).toBeVisible();

    // A mark, not the wordmark squeezed into the 48px rail: the bundled
    // wordmarks are 5 to 6 times wider than tall, the marks square.
    const mark = nav.getByRole('link', { name: / home$/ }).locator('img');
    await expect(mark).toBeVisible();
    // Visible is not decoded: the box is sized by its class, so the natural
    // size reads 0 x 0 (a NaN ratio) until the bitmap lands. An
    // undecoded image is reported as Infinity, a failing value the poll retries.
    await expect.poll(() => mark.evaluate((img: HTMLImageElement) => (
      img.complete && img.naturalHeight > 0 ? img.naturalWidth / img.naturalHeight : Infinity
    ))).toBeLessThan(1.5);

    // The rows the library draws, the slotted ones and the footer entries
    // all centre their glyph on the same vertical line. Polled, so a row still
    // mid-way through the collapse transition is measured again once settled.
    const centres = () => nav.evaluate((el) => Array.from(el.querySelectorAll('a, button')).flatMap((row) => {
      const glyph = row.querySelector('svg');
      const box = glyph?.getBoundingClientRect();
      return box && box.width > 0 ? [box.left + box.width / 2] : [];
    }));
    await expect.poll(async () => (await centres()).length).toBeGreaterThan(5);
    await expect.poll(async () => {
      const xs = await centres();
      return Math.max(...xs) - Math.min(...xs);
    }).toBeLessThanOrEqual(0.5);
  });

  test('should list the suite in the product switcher, connected or not', async ({ page }) => {
    // OpenCTI and OpenAEV always offer their siblings; so does XTM One, each
    // product opening its platform or its public page.
    await page.goto('/');
    const nav = page.getByRole('navigation', { name: 'Main navigation' });
    await nav.getByRole('button', { name: 'Switch product' }).click();
    await expect(page.getByRole('menuitem', { name: /XTM Hub/ })).toBeVisible();
    // OpenCTI, OpenAEV and the XTM Hub at least, one row per connected instance.
    await expect.poll(() => page.getByRole('menuitem').count()).toBeGreaterThanOrEqual(3);
  });

  test('should open About from the profile menu, not from the left navigation', async ({ page }) => {
    await page.goto('/');
    // Checked before About opens: the modal dialog hides the page behind it
    // from the accessibility tree, the navigation included.
    const nav = page.getByRole('navigation', { name: 'Main navigation' });
    await expect(nav.getByRole('link', { name: 'Made by Filigran' })).toBeVisible();
    await expect(nav.getByText('About', { exact: true })).toHaveCount(0);
    await page.getByTestId('user-menu-trigger').click();
    await page.getByTestId('user-menu-about').click();
    await expect(page.getByRole('dialog')).toContainText('Version');
  });

  test('should navigate to a page from the inline top bar search', async ({ page }) => {
    await page.goto('/');
    // Ctrl K focuses the inline field: no dialog opens (#11201).
    const field = page.getByRole('combobox', { name: 'Search the platform' });
    // The field must be rendered and the GlobalSearchProvider keydown listener
    // mounted before the press lands, so wait for it, then retry the press.
    await expect(field).toBeVisible();
    await expect(async () => {
      await page.keyboard.press('Control+k');
      await expect(field).toBeFocused({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await field.fill('Skills');
    await page.getByRole('option', { name: /^Skills/ }).first().click();
    await expect(page).toHaveURL(/\/skills(\?vis=[a-z]+)?$/);
  });

  test('should open the search results page on Enter', async ({ page }) => {
    await page.goto('/');
    const field = page.getByRole('combobox', { name: 'Search the platform' });
    await field.fill('agent');
    await field.press('Enter');
    await expect(page).toHaveURL(/\/search\?q=agent$/);
    await expect(page.getByRole('heading', { name: 'Search', level: 1 })).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Entity types' })).toBeVisible();
  });

  test('should navigate to the Settings page', async ({ page }) => {
    await page.goto('/settings');
    await expect(page).toHaveURL('/settings');
    await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible();
  });

  test('should navigate to the Users page', async ({ page }) => {
    await page.goto('/users-and-groups');
    await expect(page).toHaveURL('/users-and-groups');
    // A page of the Users and groups family (#9301 N3b): the family's h1, then
    // one tab row whose Groups tab opens /users-and-groups/groups under the same header. The
    // DetailTabs trigger wraps a router Link but carries the tab role.
    await expect(page.getByRole('heading', { name: 'Users and groups', level: 1 })).toBeVisible();
    await page.getByRole('tab', { name: 'Groups', exact: true }).click();
    await expect(page).toHaveURL('/users-and-groups/groups');
    await expect(page.getByRole('heading', { name: 'Users and groups', level: 1 })).toBeVisible();
  });
});
