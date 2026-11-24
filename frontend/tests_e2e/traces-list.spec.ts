import { test, expect, type Page, type Route } from '@playwright/test';

const AGENT_ALPHA = { id: 'agent-alpha', name: 'Agent Alpha' };
const AGENT_BETA = { id: 'agent-beta', name: 'Agent Beta' };

const agentsFixture = [AGENT_ALPHA, AGENT_BETA];

const tracesFixture = [
  {
    id: 'trace-1',
    agent_id: AGENT_ALPHA.id,
    agent_name: AGENT_ALPHA.name,
    conversation_id: 'conv-1',
    source: 'anthropic',
    name: 'Trace with model',
    model: 'claude-sonnet-4-6',
    started_at: '2026-06-22T10:00:00.000Z',
    duration_ms: 4200,
    input_tokens: 100,
    output_tokens: 50,
    total_tokens: 150,
    total_cost: 0.03,
    status: 'ok',
    has_scores: false,
  },
  {
    id: 'trace-2',
    agent_id: AGENT_BETA.id,
    agent_name: AGENT_BETA.name,
    conversation_id: 'conv-2',
    source: 'openai',
    name: 'Trace without explicit model',
    // A null/empty model must render as "Platform default", never a blank cell.
    model: '',
    started_at: '2026-06-22T09:00:00.000Z',
    duration_ms: 1800,
    input_tokens: 40,
    output_tokens: 20,
    total_tokens: 60,
    total_cost: 0.01,
    status: 'error',
    has_scores: false,
  },
];

async function mockAgentsAndTraces(
  page: Page,
  options: {
    items?: typeof tracesFixture;
    /** Override the reported total to unlock pagination on a short fixture. */
    total?: number;
    onTracesRequest?: (route: Route) => void;
  } = {},
) {
  const items = options.items ?? tracesFixture;

  await page.route(/\/api\/v1\/agents(\?.*)?$/, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(agentsFixture) });
  });

  await page.route(/\/api\/v1\/traces(\?.*)?$/, async (route) => {
    options.onTracesRequest?.(route);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items, total: options.total ?? items.length, limit: 50 }),
    });
  });
}

/** The API's offset ceiling (TRACE_LIST_MAX_OFFSET in the backend's read_store.py). */
const MAX_OFFSET = 100_000;

function listParams(url: string): URLSearchParams {
  return new URL(url).searchParams;
}

/** The page's filters sit in the table's Filters pill (#12070): open its panel. */
async function openFilters(page: Page) {
  const trigger = page.getByTestId('data-table-filters').locator('button[aria-expanded]');
  if ((await trigger.getAttribute('aria-expanded')) !== 'true') await trigger.click();
  await expect(page.getByTestId('traces-filters')).toBeVisible();
}

/** A field heading of the open Filters panel: the design-system Label, matched by its whole text. */
function filterHeading(page: Page, text: string) {
  return page.getByTestId('traces-filters').locator('label', { hasText: new RegExp(`^${text}$`) });
}

test.describe('Traces list — table display', () => {
  test('renders trace rows with the agent name and a "Platform default" fallback for empty models', async ({ page }) => {
    await mockAgentsAndTraces(page);
    await page.goto('/observability/traces');

    await expect(page.getByRole('heading', { name: 'Observability', level: 1 })).toBeVisible();
    await expect(page.getByText('Trace with model')).toBeVisible();
    await expect(page.getByText('Trace without explicit model')).toBeVisible();

    // The agent cell shows the agent's name, not the raw id, as plain text:
    // the whole row opens the trace, so no cell carries a link (#12070).
    const row1 = page.locator('tr', { hasText: 'Trace with model' });
    const row2 = page.locator('tr', { hasText: 'Trace without explicit model' });
    await expect(row1.getByText(AGENT_ALPHA.name, { exact: true })).toBeVisible();
    await expect(row2.getByText(AGENT_BETA.name, { exact: true })).toBeVisible();
    await expect(row1.getByRole('link', { name: AGENT_ALPHA.name })).toHaveCount(0);

    // Real model renders as-is; null/empty model renders the fallback label, never blank.
    await expect(row1.getByText('claude-sonnet-4-6')).toBeVisible();
    await expect(row2.getByText('Platform default')).toBeVisible();
  });

  test('shows the empty state when no traces match the current filters', async ({ page }) => {
    await mockAgentsAndTraces(page, { items: [] });
    await page.goto('/observability/traces');

    await expect(page.getByText('No traces found')).toBeVisible();
  });

  test('hides the Models filter entirely when no models are available', async ({ page }) => {
    await mockAgentsAndTraces(page, { items: [] });
    await page.goto('/observability/traces?status=error');
    await openFilters(page);

    await expect(filterHeading(page, 'Agents')).toBeVisible();
    await expect(filterHeading(page, 'Models')).not.toBeVisible();
  });

  test('hides the Models filter when every trace ran on the platform default (no explicit model)', async ({ page }) => {
    const onlyDefaultTraces = tracesFixture
      .filter((trace) => !trace.model)
      .map((trace) => ({ ...trace }));
    await mockAgentsAndTraces(page, { items: onlyDefaultTraces });
    await page.goto('/observability/traces');

    await expect(page.getByText('Trace without explicit model')).toBeVisible();
    await openFilters(page);
    // A Models filter whose only option is "Platform default" cannot discriminate anything.
    await expect(filterHeading(page, 'Models')).not.toBeVisible();
  });

  test('keeps the Models filter when the platform-default sentinel is pre-selected in the URL', async ({ page }) => {
    const onlyDefaultTraces = tracesFixture.filter((trace) => !trace.model).map((trace) => ({ ...trace }));
    await mockAgentsAndTraces(page, { items: onlyDefaultTraces });
    await page.goto('/observability/traces?model=__platform_default__');
    await openFilters(page);

    // Even without explicit models, an active selection must stay visible and removable.
    await expect(filterHeading(page, 'Models')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Remove Platform default' })).toBeVisible();
  });
});

test.describe('Traces list — column sorting', () => {
  test('clicking a column header sorts it server-side and flips direction on a second click', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/traces');

    const duration = page.getByRole('columnheader', { name: 'Duration' });
    // A sortable-but-unsorted column announces itself to screen readers.
    await expect(duration).toHaveAttribute('aria-sort', 'none');

    // Durations open on the slowest traces: a number column starts descending.
    await duration.getByRole('button').click();
    await expect(duration).toHaveAttribute('aria-sort', 'descending');
    await expect(page).toHaveURL(/sort=duration_ms/);
    await expect(page).toHaveURL(/dir=desc/);
    await expect
      .poll(() => requestedUrls.some((url) => url.includes('sort_by=duration_ms') && url.includes('sort_dir=desc')))
      .toBe(true);

    await duration.getByRole('button').click();
    await expect(duration).toHaveAttribute('aria-sort', 'ascending');
    await expect
      .poll(() => requestedUrls.some((url) => url.includes('sort_by=duration_ms') && url.includes('sort_dir=asc')))
      .toBe(true);
  });

  test('the default newest-first order is shown as sorted and needs no URL params', async ({ page }) => {
    await mockAgentsAndTraces(page);
    await page.goto('/observability/traces');

    // The list is already ordered by timestamp: the header must say so even
    // though nothing was clicked yet.
    const timestamp = page.getByRole('columnheader', { name: 'Timestamp' });
    await expect(timestamp).toHaveAttribute('aria-sort', 'descending');
    await expect(page).not.toHaveURL(/sort=/);

    await timestamp.getByRole('button').click();
    await expect(timestamp).toHaveAttribute('aria-sort', 'ascending');
    await expect(page).toHaveURL(/sort=started_at&dir=asc/);

    // Flipping back lands on the default again, which carries no params.
    await timestamp.getByRole('button').click();
    await expect(timestamp).toHaveAttribute('aria-sort', 'descending');
    await expect(page).not.toHaveURL(/sort=/);
    await expect(page).not.toHaveURL(/dir=/);
  });

  test('a sorted list pages by offset instead of the newest-first cursor', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      total: 120,
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/traces?sort=total_cost&dir=desc');

    await expect(page.getByText('Page 1 of 3').first()).toBeVisible();
    await page.getByRole('button', { name: 'Last' }).first().click();

    // The keyset cursor only describes the newest-first order, so a cost sort
    // must page arithmetically — and can jump straight to the last page.
    await expect(page.getByText('Page 3 of 3').first()).toBeVisible();
    await expect
      .poll(() => requestedUrls.some((url) => url.includes('sort_by=total_cost') && url.includes('offset=100')))
      .toBe(true);
    expect(requestedUrls.some((url) => url.includes('cursor='))).toBe(false);
  });

  test('the newest-first order jumps to its last page by offset', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      total: 120,
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/traces');

    await expect(page.getByText('Page 1 of 3').first()).toBeVisible();
    // The keyset walk holds no cursor for page 3: within the offset bound it
    // is read by offset instead of Last doing nothing.
    await page.getByRole('button', { name: 'Last', exact: true }).first().click();

    await expect(page.getByText('Page 3 of 3').first()).toBeVisible();
    await expect
      .poll(() =>
        requestedUrls.some((url) => {
          const params = listParams(url);
          return params.get('sort_by') === 'started_at' && params.get('offset') === '100' && !params.has('cursor');
        }),
      )
      .toBe(true);
  });

  test('an unknown sort column in the URL falls back to the default order', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/traces?sort=agent_name&dir=asc');

    // A stale or hand-edited link must not send the API a column it rejects.
    await expect(page.getByText('Trace with model')).toBeVisible();
    await expect
      .poll(() => requestedUrls.every((url) => !url.includes('sort_by=agent_name')))
      .toBe(true);
    await expect
      .poll(() => requestedUrls.some((url) => url.includes('sort_by=started_at')))
      .toBe(true);
  });
});

test.describe('Traces list — pages past the offset bound', () => {
  test('Last on a sorted store past the bound reads the final page from the other end', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      total: 1_000_000,
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/traces?sort=total_cost&dir=desc');

    await expect(page.getByText(/Page 1 of 20,?000/).first()).toBeVisible();
    await page.getByRole('button', { name: 'Last', exact: true }).first().click();

    await expect(page.getByText(/Page 20,?000 of 20,?000/).first()).toBeVisible();
    // Offset 999,950 would be refused: the last page is the first one of the
    // mirrored order instead, and its rows are put back in display order.
    await expect
      .poll(() =>
        requestedUrls.some((url) => {
          const params = listParams(url);
          return params.get('sort_by') === 'total_cost' && params.get('reverse') === 'true' && !params.has('offset');
        }),
      )
      .toBe(true);
    await expect(page.locator('tbody tr').first()).toContainText('Trace without explicit model');
    expect(requestedUrls.every((url) => Number(listParams(url).get('offset') ?? 0) <= MAX_OFFSET)).toBe(true);
  });

  test('the newest-first order reaches its last page too', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      total: 1_000_000,
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/traces');

    await expect(page.getByText(/Page 1 of 20,?000/).first()).toBeVisible();
    // The keyset walk has no cursor for the last page: Last used to do nothing.
    await page.getByRole('button', { name: 'Last', exact: true }).first().click();

    await expect(page.getByText(/Page 20,?000 of 20,?000/).first()).toBeVisible();
    await expect
      .poll(() =>
        requestedUrls.some((url) => {
          const params = listParams(url);
          return params.get('sort_by') === 'started_at' && params.get('reverse') === 'true' && !params.has('cursor');
        }),
      )
      .toBe(true);
  });

  test('a page deeper than the bound from both ends is explained, never requested', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      total: 1_000_000,
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/traces?sort=total_cost&dir=desc&page=5000');

    await expect(page.getByText('This page is too deep to open in this order')).toBeVisible();
    // Only the count was read, in the default order.
    await expect.poll(() => requestedUrls.some((url) => listParams(url).get('limit') === '1')).toBe(true);
    expect(requestedUrls.some((url) => listParams(url).get('sort_by') === 'total_cost')).toBe(false);

    await page.getByRole('button', { name: 'Sort newest first' }).click();
    await expect(page).not.toHaveURL(/sort=/);
    await expect(page).not.toHaveURL(/page=/);
    await expect(page.getByText('Trace with model')).toBeVisible();
  });
});

test.describe('Traces list — filter usage', () => {
  test('selecting an agent marks the option, shows a chip, and is sent to the API', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/traces');
    await openFilters(page);

    await page.getByRole('combobox', { name: 'Select agents' }).click();
    await page.getByRole('option', { name: AGENT_ALPHA.name }).click();

    // The selection appears as a removable chip in the field, and the URL carries
    // the filter. There is no "N selected" trigger any more: the design system's
    // Combobox shows the selection as chips rather than a count.
    await expect(page.getByRole('button', { name: `Remove ${AGENT_ALPHA.name}` })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`agent_id=${AGENT_ALPHA.id}`));

    // The selection is reflected back in the open list — this was the original
    // bug. The library keeps the list open after a pick in multiple mode, and the
    // filter card stays mounted while the filtered list loads (#11345: the page no
    // longer swaps to a page skeleton), so no reopen: a second click would close
    // it. `aria-selected` carries the meaning the hand-rolled control drew as a
    // check icon.
    await expect(page.getByRole('option', { name: AGENT_ALPHA.name, exact: true }))
      .toHaveAttribute('aria-selected', 'true');

    await expect.poll(() => requestedUrls.some((url) => url.includes(`agent_id=${AGENT_ALPHA.id}`))).toBe(true);
  });

  test('selecting the "Platform default" model reflects correctly and maps to an empty model on the API call', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto('/observability/traces');
    await openFilters(page);

    await page.getByRole('combobox', { name: 'Select models' }).click();
    await page.getByRole('option', { name: 'Platform default' }).click();

    await expect(page.getByRole('button', { name: 'Remove Platform default' })).toBeVisible();

    // The URL stores the internal sentinel (never a raw empty string, which CSV parsing would strip).
    await expect(page).toHaveURL(/model=__platform_default__/);

    // But the actual outgoing API request translates the sentinel back to an empty model value.
    await expect.poll(() => requestedUrls.some((url) => /([?&])model=(&|$)/.test(url))).toBe(true);
  });

  test('Clear filters removes agent, model, status, and date filters', async ({ page }) => {
    await mockAgentsAndTraces(page);
    await page.goto('/observability/traces?agent_id=agent-alpha&model=claude-sonnet-4-6&status=error&date_from=2026-01-01&date_to=2026-01-31');
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

const SCOPED_ASSIGNMENT = { id: '5d1c0a7e-0000-4000-8000-000000000001', name: 'Nightly triage' };
const SCOPED_RUN_ID = '9f2e1a3b-0000-4000-8000-000000000002';

/**
 * The assignment the scope chip names; `found: false` answers like a deleted
 * one. Returns the paths of the lookups the page made.
 */
async function mockScopedAssignment(page: Page, { found = true }: { found?: boolean } = {}) {
  const lookups: string[] = [];
  await page.route(/\/api\/v1\/assignments\/[^/?]+(\?.*)?$/, async (route) => {
    lookups.push(new URL(route.request().url()).pathname);
    await route.fulfill(
      found
        ? { status: 200, contentType: 'application/json', body: JSON.stringify(SCOPED_ASSIGNMENT) }
        : { status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Assignment not found' }) },
    );
  });
  return lookups;
}

test.describe('Traces list — assignment and run scope', () => {
  test('an assignment shortcut scopes the list, names the assignment and links back to it', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await mockScopedAssignment(page);
    await page.goto(`/observability/traces?assignment_id=${SCOPED_ASSIGNMENT.id}`);

    await expect(page.getByText('Scoped to:')).toBeVisible();
    const chip = page.getByRole('link', { name: SCOPED_ASSIGNMENT.name, exact: true });
    await expect(chip).toHaveAttribute('href', new RegExp(`/agents/assignments/${SCOPED_ASSIGNMENT.id}$`));
    await expect(page.getByRole('button', { name: 'Remove the assignment scope' })).toBeVisible();
    await expect
      .poll(() => requestedUrls.some((url) => listParams(url).get('assignment_id') === SCOPED_ASSIGNMENT.id))
      .toBe(true);
    expect(requestedUrls.some((url) => listParams(url).has('assignment_run_id'))).toBe(false);
  });

  test('removing the assignment scope lists every trace again, from page one', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      total: 120,
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await mockScopedAssignment(page);
    await page.goto(`/observability/traces?assignment_id=${SCOPED_ASSIGNMENT.id}&page=1`);

    await expect(page.getByText('Page 2 of 3').first()).toBeVisible();
    await page.getByRole('button', { name: 'Remove the assignment scope' }).click();

    await expect(page).not.toHaveURL(/assignment_id=/);
    await expect(page).not.toHaveURL(/[?&]page=/);
    await expect(page.getByText('Scoped to:')).toHaveCount(0);
    await expect(page.getByText('Page 1 of 3').first()).toBeVisible();
    await expect
      .poll(() => {
        const last = requestedUrls[requestedUrls.length - 1];
        return last !== undefined && !listParams(last).has('assignment_id');
      })
      .toBe(true);
  });

  test('an assignment the API no longer returns keeps a removable chip, named by its id', async ({ page }) => {
    await mockAgentsAndTraces(page);
    const lookups = await mockScopedAssignment(page, { found: false });
    await page.goto(`/observability/traces?assignment_id=${SCOPED_ASSIGNMENT.id}`);

    await expect.poll(() => lookups.length).toBe(1);
    // A thrown 404 would be retried once, about a second later, then replace
    // the page with the ErrorBoundary: no second lookup may come in that
    // window (this fails as soon as one does, rather than sleeping it out).
    await expect(
      page.waitForRequest(
        (request) => new URL(request.url()).pathname === `/api/v1/assignments/${SCOPED_ASSIGNMENT.id}`,
        { timeout: 2_500 },
      ),
    ).rejects.toThrow();
    expect(lookups).toEqual([`/api/v1/assignments/${SCOPED_ASSIGNMENT.id}`]);
    await expect(page.getByRole('link', { name: 'Assignment 5d1c0a7e…', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Remove the assignment scope' })).toBeVisible();
  });

  test('a scope value that is no id is never spliced into an API path', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    const lookups = await mockScopedAssignment(page);
    // Where `/assignments/../users/me` resolves to.
    const escapedReads: string[] = [];
    page.on('request', (request) => {
      const { pathname } = new URL(request.url());
      if (pathname === '/api/v1/users/me') escapedReads.push(pathname);
    });
    await page.goto(`/observability/traces?assignment_id=${encodeURIComponent('../users/me')}`);

    await expect(page.getByRole('link', { name: 'Assignment ../users…', exact: true })).toBeVisible();
    // The list still carries the value, for the API to refuse (422).
    await expect
      .poll(() => requestedUrls.some((url) => listParams(url).get('assignment_id') === '../users/me'))
      .toBe(true);
    expect(lookups).toEqual([]);
    expect(escapedReads).toEqual([]);
  });

  test('a run shortcut scopes the list to that run, and Clear filters drops it', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await page.goto(`/observability/traces?assignment_run_id=${SCOPED_RUN_ID}&status=error`);

    const chip = page.getByRole('link', { name: 'Run 9f2e1a3b…', exact: true });
    await expect(chip).toHaveAttribute('href', new RegExp(`/operations/executions/${SCOPED_RUN_ID}$`));
    await expect
      .poll(() =>
        requestedUrls.some((url) => {
          const params = listParams(url);
          return params.get('assignment_run_id') === SCOPED_RUN_ID && params.get('status') === 'error';
        }),
      )
      .toBe(true);

    await page.getByRole('button', { name: 'Clear all filters' }).first().click();

    await expect(page).not.toHaveURL(/assignment_run_id=/);
    await expect(page).not.toHaveURL(/status=/);
    await expect(page.getByText('Scoped to:')).toHaveCount(0);
  });

  test('a page too deep to open still counts within the scope', async ({ page }) => {
    const requestedUrls: string[] = [];
    await mockAgentsAndTraces(page, {
      total: 1_000_000,
      onTracesRequest: (route) => requestedUrls.push(route.request().url()),
    });
    await mockScopedAssignment(page);
    await page.goto(`/observability/traces?assignment_id=${SCOPED_ASSIGNMENT.id}&sort=total_cost&dir=desc&page=5000`);

    await expect(page.getByText('This page is too deep to open in this order')).toBeVisible();
    // The count is the scoped list's, never the whole store's.
    await expect
      .poll(() =>
        requestedUrls.some((url) => {
          const params = listParams(url);
          return params.get('limit') === '1' && params.get('assignment_id') === SCOPED_ASSIGNMENT.id;
        }),
      )
      .toBe(true);
    // Every list read is scoped, except the one unfiltered read of the viewer's
    // whole total that the toolbar shows as "n of N" (#12039): one row, no filter.
    const unscoped = requestedUrls.filter((url) => listParams(url).get('assignment_id') !== SCOPED_ASSIGNMENT.id);
    for (const url of unscoped) {
      const params = listParams(url);
      expect(params.get('limit')).toBe('1');
      expect([...params.keys()].filter((key) => key !== 'limit')).toEqual([]);
    }
  });
});
