import { test, expect, type Page } from '@playwright/test';
import { deleteAgent, ok, withAdmin } from './helpers';

// ---------------------------------------------------------------------------
// One visibility filter on every list page.
//
// Each page draws the same radio group, Mine | Company | Shared | All,
// writes the choice to ?vis= and reads it back on a reload. The [DEPRECATED]
// ?visibility= alias of an old link is read, then replaced by ?vis= on the
// next change. A list filtered in the browser lands, with no segment in its
// URL, on the first segment holding an item: Mine, else Company, else Shared,
// else All, and writes it to ?vis=.
// ---------------------------------------------------------------------------

// Pages that draw the filter on an empty library too. Not /investigation-packs:
// it is behind the DEEP_INVESTIGATION feature, off in the E2E stack. The
// Operations tabs and Files filter on the server (`?vis=` sent to the API).
// /flows is the Agentic Flow map: the quick filter sits on the graph toolbar row.
// Not /prompts, /mcp-servers, /skills nor /presentations: on the standard v2 toolbar of
// the records table (#10977, #9378 slice 7), an empty library shows its empty state
// alone, no toolbar; /prompts is checked below on served rows (Skills shares its
// toolbar and its reader).
const PAGES = [
  '/templates',
  '/operations/executions',
  '/tasks/work-items',
  '/operations/shared-state',
  '/files',
  '/flows',
];

function segment(page: Page, name: string) {
  return page.getByRole('radiogroup', { name: 'Visibility' }).getByRole('radio', { name: new RegExp(`^${name}`) });
}

const ORDER = [/^Mine/, /^Company/, /^Shared/, /^All/];

// The core project runs at the Desktop Chrome viewport (1280 px). The Operations lists keep
// one filter row (`filtersInPill`): with the Status pill on the work items row (#11917) the
// Visibility segments that do not fit fold into "More tabs" at 1280 by design. The specs
// that read every segment on the row therefore open the page at 1920 px, where they fit.
async function wide(page: Page) {
  await page.setViewportSize({ width: 1920, height: 1080 });
}

/** A list row of the given segment for user `me` (the shape `lib/visibility.ts` reads). */
function row(id: string, segment: 'mine' | 'company' | 'shared', me: string) {
  return {
    id,
    name: `Prompt ${id}`,
    description: null,
    content: '',
    tags: [],
    user_id: segment === 'mine' || segment === 'company' ? me : 'someone-else',
    is_company_managed: segment === 'company',
    shared_group_ids: segment === 'shared' ? ['g1'] : [],
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  };
}

/** A company deck of `GET /presentations` (the `PresentationSummary` shape the library reads). */
function companyDeck(id: string) {
  return {
    id,
    title: `Deck ${id}`,
    theme: 'dark',
    aspect_ratio: '16:9',
    kind: 'deck',
    slide_count: 1,
    revision: 1,
    conversation_id: null,
    assignment_run_id: null,
    template_container_id: null,
    template_name: null,
    user_id: 'someone-else',
    owner_name: 'Someone else',
    is_company_managed: true,
    shared_group_ids: [],
    shared_group_permissions: {},
    shared_user_ids: [],
    shared_user_permissions: {},
    access_restricted: false,
    permission: 'read_only',
    can_manage: false,
    updated_by_user_id: null,
    updated_by_name: null,
    open_comment_count: 0,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  };
}

/** Serve /prompts with rows of the given segments, for the signed-in user. */
async function servePrompts(page: Page, segments: ('mine' | 'company' | 'shared')[]) {
  // The app sends its bearer token from localStorage (lib/api.ts): a bare fetch is a 401.
  const me = await page.evaluate(async () => {
    const token = localStorage.getItem('copilot_token');
    const resp = await fetch('/api/v1/auth/me', { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!resp.ok) throw new Error(`/auth/me answered ${resp.status}`);
    return (await resp.json()).id as string;
  });
  await page.route(/\/api\/v1\/prompts(\?|$)/, (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({ json: segments.map((s, i) => row(`p${i}`, s, me)) })
      : route.fallback(),
  );
}

test.describe('Visibility filter', () => {
  for (const path of PAGES) {
    test(`${path} draws Mine | Company | Shared | All and keeps the segment in the URL`, async ({ page }) => {
      await wide(page);
      await page.goto(`${path}${path.includes('?') ? '&' : '?'}vis=mine`);

      const group = page.getByRole('radiogroup', { name: 'Visibility' });
      await expect(group).toBeVisible({ timeout: 15_000 });
      await expect(group.getByRole('radio')).toHaveText(ORDER);
      await expect(segment(page, 'Mine')).toHaveAttribute('aria-checked', 'true');

      await segment(page, 'Company').click();
      await expect(page).toHaveURL(/[?&]vis=company(&|$)/);
      await expect(segment(page, 'Company')).toHaveAttribute('aria-checked', 'true');

      await page.reload();
      await expect(segment(page, 'Company')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });

      // Keyboard: one tab stop, arrows move the selection in display order.
      await segment(page, 'Company').focus();
      await page.keyboard.press('ArrowRight');
      await expect(segment(page, 'Shared')).toHaveAttribute('aria-checked', 'true');
      await expect(page).toHaveURL(/[?&]vis=shared(&|$)/);
    });
  }

  // Every visible segment carries its count, the paginated lists included:
  // a number, never a bare label, and Mine + Shared + Company = All under the same filters.
  // Opened with the built-ins shown: hidden, they leave Company's count (its tip
  // says how many, #11885) but stay in All, so the segments partition All only then.
  for (const path of [...PAGES, '/operations/background-tasks']) {
    test(`${path} shows a count on every visible segment`, async ({ page }) => {
      await page.goto(`${path}${path.includes('?') ? '&' : '?'}builtin=show`);
      const radios = page.getByRole('radiogroup', { name: 'Visibility' }).getByRole('radio');
      await expect(radios.first()).toBeVisible({ timeout: 15_000 });
      await expect(radios.first()).toHaveText(/\d+$/, { timeout: 15_000 });
      const texts = await radios.allTextContents();
      const counts = Object.fromEntries(
        texts.map((text) => {
          const match = /^(\D+?)\s*(\d+)$/.exec(text.trim());
          expect(match, `${path}: "${text}" carries a count`).not.toBeNull();
          return [match![1].trim(), Number(match![2])];
        }),
      );
      if ('Shared' in counts && 'Company' in counts) {
        expect(counts.Mine + counts.Shared + counts.Company).toBe(counts.All);
      }
    });
  }

  test('/prompts on the records table toolbar draws the four segments with their counts and keeps the segment in the URL', async ({ page }) => {
    await page.goto('/');
    await servePrompts(page, ['mine', 'company', 'shared', 'shared']);
    await page.goto('/prompts?vis=mine');

    const radios = page.getByRole('radiogroup', { name: 'Visibility' }).getByRole('radio');
    await expect(radios).toHaveText([/^Mine\s*1$/, /^Company\s*1$/, /^Shared\s*2$/, /^All\s*4$/], { timeout: 15_000 });
    await expect(segment(page, 'Mine')).toHaveAttribute('aria-checked', 'true');
    await segment(page, 'Company').click();
    await expect(page).toHaveURL(/[?&]vis=company(&|$)/);
    await expect(segment(page, 'Company')).toHaveAttribute('aria-checked', 'true');
  });

  test('with nothing of mine and company items, a list lands on Company and says so in the URL', async ({ page }) => {
    await page.goto('/');
    await servePrompts(page, ['company', 'company', 'shared']);
    await page.goto('/prompts');

    await expect(segment(page, 'Company')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await expect(page).toHaveURL(/[?&]vis=company(&|$)/);
    await expect(page.getByRole('radiogroup', { name: 'Visibility' }).getByRole('radio')).toHaveText(ORDER);
  });

  test('with nothing of mine nor company, a list lands on Shared', async ({ page }) => {
    await page.goto('/');
    await servePrompts(page, ['shared']);
    await page.goto('/prompts');

    await expect(segment(page, 'Shared')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await expect(page).toHaveURL(/[?&]vis=shared(&|$)/);
  });

  test('an explicit ?vis=mine stays on Mine, empty or not', async ({ page }) => {
    await page.goto('/');
    await servePrompts(page, ['company']);
    await page.goto('/prompts?vis=mine');

    await expect(segment(page, 'Mine')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await expect(page.getByTestId('visibility-empty-hint')).toBeVisible();
  });

  test('the Operations lists ask the API for the segment shown', async ({ page }) => {
    const asked: string[] = [];
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.pathname === '/api/v1/work-items') asked.push(url.searchParams.get('vis') ?? 'all');
    });
    // The URL names Mine: with none named, the tab lands by its counts.
    await wide(page);
    await page.goto('/tasks/work-items?vis=mine');
    await expect(segment(page, 'Mine')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await expect.poll(() => asked.at(-1)).toBe('mine');

    await segment(page, 'Company').click();
    await expect(page).toHaveURL(/[?&]vis=company(&|$)/);
    await expect.poll(() => asked.at(-1)).toBe('company');
  });

  // A list filtered on the server lands from GET <list>/segment-counts before
  // its first list request, so it never asks for an empty Mine.
  test('with no deck of mine, the presentation library lands on Company without asking for Mine', async ({ page }) => {
    const asked: string[] = [];
    await page.route(/\/api\/v1\/presentations\/segment-counts(\?|$)/, (route) =>
      route.fulfill({ json: { mine: 0, company: 2, shared: 1, all: 3 } }),
    );
    // The library draws its toolbar (and the filter) over decks only: an empty one shows
    // its empty state alone. Serve the list too, so the test never depends on a deck an
    // earlier test of the shard left behind.
    await page.route(/\/api\/v1\/presentations(\?|$)/, (route) =>
      route.request().method() === 'GET'
        ? route.fulfill({ json: { items: [companyDeck('d1'), companyDeck('d2')], total: 3 } })
        : route.fallback(),
    );
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.pathname === '/api/v1/presentations') asked.push(url.searchParams.get('vis') ?? 'all');
    });
    await page.goto('/presentations');

    await expect(segment(page, 'Company')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await expect(page).toHaveURL(/[?&]vis=company(&|$)/);
    await expect.poll(() => asked.at(-1)).toBe('company');
    expect(asked).not.toContain('mine');
  });

  test('an explicit ?vis=mine keeps a server-filtered list on an empty Mine', async ({ page }) => {
    await page.route(/\/api\/v1\/files\/segment-counts(\?|$)/, (route) =>
      route.fulfill({ json: { mine: 0, company: 2, shared: 1, all: 3 } }),
    );
    await page.goto('/files?vis=mine');

    await expect(segment(page, 'Mine')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await expect(page).toHaveURL(/[?&]vis=mine(&|$)/);
  });

  test('an old ?ownership=shared link on Operations (shared + company) opens on All, then moves to ?vis=', async ({ page }) => {
    await wide(page);
    await page.goto('/tasks/work-items?ownership=shared');

    await expect(segment(page, 'All')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await segment(page, 'Mine').click();
    await expect(page).toHaveURL(/[?&]vis=mine(&|$)/);
    expect(new URL(page.url()).searchParams.has('ownership')).toBe(false);
  });

  test('an old ?owner=shared link on Files (shared + company) opens on All, then moves to ?vis=', async ({ page }) => {
    await page.goto('/files?owner=shared');

    await expect(segment(page, 'All')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await segment(page, 'Mine').click();
    await expect(page).toHaveURL(/[?&]vis=mine(&|$)/);
    expect(new URL(page.url()).searchParams.has('owner')).toBe(false);
  });

  // The library pages filter in the browser (useEntityFilter): the same alias,
  // read in lib/visibility.ts for every page on it. The pages that draw
  // the filter on an empty library; Agents, Knowledge and Skills share the same reader.
  for (const path of ['/prompts']) {
    test(`an old ?owner=shared link on ${path} opens on All, then moves to ?vis=`, async ({ page }) => {
      // A v2 records table draws its toolbar over rows only: serve some.
      await page.goto('/');
      await servePrompts(page, ['mine', 'company', 'shared']);
      await page.goto(`${path}?owner=shared`);

      await expect(segment(page, 'All')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
      await segment(page, 'Mine').click();
      await expect(page).toHaveURL(/[?&]vis=mine(&|$)/);
      expect(new URL(page.url()).searchParams.has('owner')).toBe(false);
    });
  }

  test('Background tasks offers the four segments and moves an old ?scope= link to ?vis=', async ({ page }) => {
    // On the shared DataTable (#9378 slice 8e) the list keeps one filter row: read the four segments where they fit.
    await wide(page);
    await page.goto('/operations/background-tasks?scope=all');

    const group = page.getByRole('radiogroup', { name: 'Visibility' });
    await expect(group.getByRole('radio')).toHaveText(ORDER, { timeout: 15_000 });
    await expect(segment(page, 'All')).toHaveAttribute('aria-checked', 'true');
    await segment(page, 'Mine').click();
    await expect(page).toHaveURL(/[?&]vis=mine(&|$)/);
    expect(new URL(page.url()).searchParams.has('scope')).toBe(false);
  });

  test('the Agentic Flow map draws the quick filter on the graph toolbar row', async ({ page }) => {
    await page.goto('/flows?vis=mine');

    const toolbar = page.getByRole('toolbar', { name: 'Graph toolbar' });
    await expect(toolbar.getByRole('radiogroup', { name: 'Visibility' })).toBeVisible({ timeout: 15_000 });
    await expect(segment(page, 'Mine')).toHaveAttribute('aria-checked', 'true');
    await expect(page.getByRole('status', { name: 'Graph status' })).toBeVisible();

    await segment(page, 'All').click();
    await expect(page).toHaveURL(/[?&]vis=all(&|$)/);
    await expect(page.getByRole('status', { name: 'Graph status' })).toBeVisible();
  });

  // The URL names Mine: with no segment named, the map lands on the first
  // segment holding an agent, which the E2E stack does not fix.
  test('the Agentic Flow Schedule view opens a named Mine with the same quick filter as the Graph view', async ({ page }) => {
    await page.goto('/flows/map/schedule?vis=mine');

    const toolbar = page.getByRole('toolbar', { name: 'Schedule toolbar' });
    await expect(toolbar.getByRole('radiogroup', { name: 'Visibility' })).toBeVisible({ timeout: 15_000 });
    await expect(segment(page, 'Mine')).toHaveAttribute('aria-checked', 'true');

    await segment(page, 'Company').click();
    await expect(page).toHaveURL(/[?&]vis=company(&|$)/);
    await expect(page).toHaveURL(/\/flows\/map\/schedule(\?|$)/);

    await page.reload();
    await expect(segment(page, 'Company')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
  });

  test('the Schedule view opens its Filters panel and its Focus list unclipped and usable', async ({ page }) => {
    // One agent of our own, so the Focus check always has a row to pick and
    // fails on an empty list instead of passing without testing.
    const agentName = `E2E focus agent ${Date.now()}`;
    const agentId = await withAdmin(async (apiCtx, headers) => {
      const resp = await ok(
        await apiCtx.post('/api/v1/agents', { headers, data: { name: agentName, persona: 'Temp agent for the Focus picker check.' } }),
        'create agent',
      );
      return (await resp.json()).id as string;
    });
    try {
      await page.goto('/flows/map/schedule?vis=all');
      const toolbar = page.getByRole('toolbar', { name: 'Schedule toolbar' });
      await expect(toolbar).toBeVisible({ timeout: 15_000 });

      // Filters: the panel shows whole and a switch in it can be clicked (UX B1).
      await toolbar.getByRole('button', { name: /^Filters/ }).click();
      const panel = page.getByRole('region', { name: 'Filters' });
      await expect(panel).toBeVisible();
      const box = await panel.boundingBox();
      expect(box?.height ?? 0).toBeGreaterThan(100);
      const option = panel.getByRole('switch').first();
      await expect(option).toBeVisible();
      await expect(option).toHaveAttribute('aria-checked', 'false');
      await option.click();
      // The filters live in the URL: the second click must read the state the
      // first one wrote, or it turns the filter on again and the seeded agent
      // leaves the Focus list.
      await expect(option).toHaveAttribute('aria-checked', 'true');
      // Back to no filter, so the seeded agent stays in the Focus list.
      await option.click();
      await expect(option).toHaveAttribute('aria-checked', 'false');
      await expect(page).not.toHaveURL(/[?&]kinds=/);
      await page.keyboard.press('Escape');

      // Focus: the agent list shows and an agent can be picked (UX B2). The
      // seeded agent is always there: no row is a failure.
      const input = toolbar.getByPlaceholder('All agents');
      await input.click();
      await input.fill(agentName);
      const firstAgent = toolbar.getByRole('listbox', { name: 'Agents' }).getByRole('option').first();
      await expect(firstAgent).toBeVisible();
      await expect(firstAgent).toBeInViewport();
      await expect(firstAgent).toHaveText(agentName);
      await firstAgent.click();
      await expect(toolbar.getByText(agentName, { exact: true })).toBeVisible();
    } finally {
      await deleteAgent(agentId);
    }
  });

  // #12071: a Mine picked on an earlier visit (stored by an older build) never
  // lands the map on an empty Mine. The agents list is the real one with every
  // agent made a company agent of someone else, so Mine is empty and Company holds them.
  for (const path of ['/flows/map', '/flows/map/schedule']) {
    test(`${path} with a stored pick of Mine and Mine empty lands on Company`, async ({ page }) => {
      await page.goto('/settings');
      const me = await page.evaluate(async () => {
        const token = localStorage.getItem('copilot_token');
        const resp = await fetch('/api/v1/auth/me', { headers: token ? { Authorization: `Bearer ${token}` } : {} });
        if (!resp.ok) throw new Error(`/auth/me answered ${resp.status}`);
        return (await resp.json()).id as string;
      });
      await page.evaluate((key) => {
        localStorage.setItem(key, JSON.stringify({ layout: 'force', visibility: 'mine', visibilityChosen: true }));
      }, `xtmone.graphView.${me}.map`);
      await page.route(/\/api\/v1\/agents(\?|$)/, async (route) => {
        if (route.request().method() !== 'GET') return route.fallback();
        const resp = await route.fetch();
        const agents = (await resp.json()) as Record<string, unknown>[];
        await route.fulfill({
          response: resp,
          json: agents.map((a) => ({ ...a, user_id: 'someone-else', is_company_managed: true, shared_group_ids: [] })),
        });
      });

      await page.goto(path);
      await expect(segment(page, 'Company')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
      await expect(page).toHaveURL(/[?&]vis=company(&|$)/);
      await expect(page.getByText('Nothing of yours here yet')).toHaveCount(0);
    });
  }

  test('the segment is kept when switching the map between Graph and Schedule', async ({ page }) => {
    await page.goto('/flows/map?vis=mine');
    await expect(segment(page, 'Mine')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await segment(page, 'All').click();
    await expect(page).toHaveURL(/[?&]vis=all(&|$)/);

    await page.getByRole('radio', { name: 'Schedule' }).or(page.getByRole('button', { name: 'Schedule', exact: true })).first().click();
    await expect(page.getByRole('toolbar', { name: 'Schedule toolbar' })).toBeVisible({ timeout: 15_000 });
    await expect(segment(page, 'All')).toHaveAttribute('aria-checked', 'true');

    await segment(page, 'Shared').click();
    await expect(page).toHaveURL(/[?&]vis=shared(&|$)/);
    await page.getByRole('radio', { name: 'Graph' }).or(page.getByRole('button', { name: 'Graph', exact: true })).first().click();
    await expect(page.getByRole('toolbar', { name: 'Graph toolbar' })).toBeVisible({ timeout: 15_000 });
    await expect(segment(page, 'Shared')).toHaveAttribute('aria-checked', 'true');
  });

  test('an old ?visibility= link still opens its segment, then moves to ?vis=', async ({ page }) => {
    // /files filters on the server and draws the filter on an empty library too.
    await page.goto('/files?visibility=company');

    await expect(segment(page, 'Company')).toHaveAttribute('aria-checked', 'true', { timeout: 15_000 });
    await segment(page, 'All').click();
    await expect(page).toHaveURL(/[?&]vis=all(&|$)/);
    expect(new URL(page.url()).searchParams.has('visibility')).toBe(false);
  });
});
