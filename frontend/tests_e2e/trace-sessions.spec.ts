import { test, expect, type Page, type Route } from '@playwright/test';

const AGENT_ALPHA = { id: 'agent-alpha', name: 'Agent Alpha' };
const AGENT_BETA = { id: 'agent-beta', name: 'Agent Beta' };

const agentsFixture = [AGENT_ALPHA, AGENT_BETA];

const sessionsFixture = [
  {
    conversation_id: 'conv-session-1',
    // The Conversation column shows the title (the id moved to its tooltip).
    conversation_title: 'conv-session-1',
    first_activity_at: '2026-06-22T10:00:00.000Z',
    last_activity_at: '2026-06-22T10:05:00.000Z',
    trace_count: 3,
    error_count: 0,
    agent_count: 1,
    model_count: 1,
    input_tokens: 500,
    output_tokens: 200,
    total_tokens: 700,
    total_cost: 0.12,
  },
  {
    conversation_id: 'conv-session-2',
    conversation_title: 'conv-session-2',
    first_activity_at: '2026-06-21T08:00:00.000Z',
    last_activity_at: '2026-06-21T08:10:00.000Z',
    trace_count: 1,
    error_count: 1,
    agent_count: 1,
    model_count: 1,
    input_tokens: 80,
    output_tokens: 20,
    total_tokens: 100,
    total_cost: 0.02,
  },
];

async function mockAgentsAndSessions(
  page: Page,
  options: {
    items?: typeof sessionsFixture;
    /** Override the reported total to unlock pagination on a short fixture. */
    total?: number;
    onSessionsRequest?: (route: Route) => void;
  } = {},
) {
  const items = options.items ?? sessionsFixture;

  await page.route(/\/api\/v1\/agents(\?.*)?$/, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(agentsFixture) });
  });

  await page.route(/\/api\/v1\/traces\/sessions(\?.*)?$/, async (route) => {
    options.onSessionsRequest?.(route);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items, total: options.total ?? items.length, limit: 50 }),
    });
  });
}

test.describe('Trace Sessions — table display', () => {
  test('renders session rows with conversation, counts, tokens, and cost', async ({ page }) => {
    await mockAgentsAndSessions(page);
    await page.goto('/observability/sessions');

    await expect(page.getByRole('heading', { name: 'Observability', level: 1 })).toBeVisible();
    await expect(page.getByText('conv-session-1')).toBeVisible();
    await expect(page.getByText('conv-session-2')).toBeVisible();

    const row1 = page.locator('tr', { hasText: 'conv-session-1' });
    await expect(row1.getByText('$0.12')).toBeVisible();
    const row2 = page.locator('tr', { hasText: 'conv-session-2' });
    // Sessions with at least one error render the error badge with a non-zero count (last column = Errors).
    await expect(row2.locator('td').last()).toHaveText('1');
  });

  test('shows the empty state when no sessions match the current filters', async ({ page }) => {
    await mockAgentsAndSessions(page, { items: [] });
    await page.goto('/observability/sessions');

    await expect(page.getByText('No sessions found')).toBeVisible();
  });

  test('hides the Models filter by default (no models selected/known yet)', async ({ page }) => {
    await mockAgentsAndSessions(page);
    await page.goto('/observability/sessions');
    await openFilters(page);

    await expect(filterHeading(page, 'Agents')).toBeVisible();
    await expect(filterHeading(page, 'Models')).not.toBeVisible();
  });
});

test.describe('Trace Sessions — column sorting', () => {
  test('clicking a column header sorts it server-side and flips direction on a second click', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndSessions(page, {
      onSessionsRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/sessions');

    const errors = page.getByRole('columnheader', { name: 'Errors' });
    await expect(errors).toHaveAttribute('aria-sort', 'none');

    // "Which sessions are failing?" — the count column opens on the worst.
    await errors.getByRole('button').click();
    await expect(errors).toHaveAttribute('aria-sort', 'descending');
    await expect(page).toHaveURL(/sort=error_count&dir=desc/);
    await expect
      .poll(() => requestedUrls.some((url) => url.includes('sort_by=error_count') && url.includes('sort_dir=desc')))
      .toBe(true);

    await errors.getByRole('button').click();
    await expect(errors).toHaveAttribute('aria-sort', 'ascending');
    await expect
      .poll(() => requestedUrls.some((url) => url.includes('sort_by=error_count') && url.includes('sort_dir=asc')))
      .toBe(true);
  });

  test('the default last-activity order is shown as sorted and needs no URL params', async ({ page }) => {
    await mockAgentsAndSessions(page);
    await page.goto('/observability/sessions');

    await expect(page.getByRole('columnheader', { name: 'Last Activity' })).toHaveAttribute('aria-sort', 'descending');
    await expect(page).not.toHaveURL(/sort=/);
  });
});

/** The API's offset ceiling (TRACE_LIST_MAX_OFFSET in the backend's read_store.py). */
const MAX_OFFSET = 100_000;

function listParams(url: string): URLSearchParams {
  return new URL(url).searchParams;
}

test.describe('Trace Sessions — pages past the offset bound', () => {
  test('Last on a sorted store past the bound reads the final page from the other end', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndSessions(page, {
      total: 1_000_000,
      onSessionsRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/sessions?sort=error_count&dir=desc');

    await expect(page.getByText(/Page 1 of 20,?000/).first()).toBeVisible();
    // Exact: the "Last Activity" column header is a button too.
    await page.getByRole('button', { name: 'Last', exact: true }).first().click();

    await expect(page.getByText(/Page 20,?000 of 20,?000/).first()).toBeVisible();
    await expect
      .poll(() =>
        requestedUrls.some((url) => {
          const params = listParams(url);
          return params.get('sort_by') === 'error_count' && params.get('reverse') === 'true' && !params.has('offset');
        }),
      )
      .toBe(true);
    // The mirrored rows are put back in display order.
    await expect(page.locator('tbody tr').first()).toContainText('conv-session-2');
    expect(requestedUrls.every((url) => Number(listParams(url).get('offset') ?? 0) <= MAX_OFFSET)).toBe(true);
  });

  test('a page deeper than the bound from both ends is explained, never requested', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndSessions(page, {
      total: 1_000_000,
      onSessionsRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/sessions?sort=error_count&dir=desc&page=5000');

    await expect(page.getByText('This page is too deep to open in this order')).toBeVisible();
    await expect.poll(() => requestedUrls.some((url) => listParams(url).get('limit') === '1')).toBe(true);
    expect(requestedUrls.some((url) => listParams(url).get('sort_by') === 'error_count')).toBe(false);

    await page.getByRole('button', { name: 'Sort newest first' }).click();
    await expect(page).not.toHaveURL(/sort=/);
    await expect(page.getByText('conv-session-1')).toBeVisible();
  });
});

/** The page's filters sit in the table's Filters pill (#12070): open its panel. */
async function openFilters(page: Page) {
  const trigger = page.getByTestId('data-table-filters').locator('button[aria-expanded]');
  if ((await trigger.getAttribute('aria-expanded')) !== 'true') await trigger.click();
  await expect(page.getByTestId('trace-sessions-filters')).toBeVisible();
}

/** A field heading of the open Filters panel: the design-system Label, matched by its whole text. */
function filterHeading(page: Page, text: string) {
  return page.getByTestId('trace-sessions-filters').locator('label', { hasText: new RegExp(`^${text}$`) });
}

test.describe('Trace Sessions — filter usage', () => {
  test('selecting an agent marks the option, shows a chip, and is sent to the API', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndSessions(page, {
      onSessionsRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/sessions');
    await openFilters(page);

    await page.getByRole('combobox', { name: 'Select agents' }).click();
    await page.getByRole('option', { name: AGENT_BETA.name }).click();

    // The selection appears as a removable chip; the design system's Combobox
    // shows chips rather than a "N selected" count.
    await expect(page.getByRole('button', { name: `Remove ${AGENT_BETA.name}` })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`agent_id=${AGENT_BETA.id}`));

    // The selection is reflected back in the open list — this was the original
    // bug. The library keeps the list open after a pick in multiple mode, and the
    // filter card stays mounted while the filtered list loads (#11345: the page no
    // longer swaps to a page skeleton), so no reopen: a second click would close
    // it. `aria-selected` carries the meaning the hand-rolled control drew as a
    // check icon.
    await expect(page.getByRole('option', { name: AGENT_BETA.name, exact: true }))
      .toHaveAttribute('aria-selected', 'true');

    await expect.poll(() => requestedUrls.some((url) => url.includes(`agent_id=${AGENT_BETA.id}`))).toBe(true);
  });

  test('a pre-selected "Platform default" model in the URL shows the Models filter with a proper label', async ({ page }) => {
    await mockAgentsAndSessions(page);
    await page.goto('/observability/sessions?model=__platform_default__');
    await openFilters(page);

    await expect(filterHeading(page, 'Models')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Remove Platform default' })).toBeVisible();
  });

  test('Clear filters removes agent, model, status, and date filters', async ({ page }) => {
    await mockAgentsAndSessions(page);
    await page.goto(
      '/observability/sessions?agent_id=agent-alpha&model=__platform_default__&status=error&date_from=2026-01-01&date_to=2026-01-31',
    );
    await openFilters(page);

    await expect(page.getByRole('button', { name: /^Remove / }).first()).toBeVisible();

    await page.getByRole('button', { name: 'Clear all filters' }).first().click();

    await expect(page).not.toHaveURL(/agent_id=/);
    await expect(page).not.toHaveURL(/model=/);
    await expect(page).not.toHaveURL(/status=/);
    await expect(page).not.toHaveURL(/date_from=/);
    await openFilters(page);
    await expect(page.getByRole('combobox', { name: 'Select agents' })).toBeVisible();
  });
});
