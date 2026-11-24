import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Operations lists on one grid.
//
// The Work Items rows used to grow with their metadata (2 to 5 lines) and
// the "updated" label sat at a different x and y on each row. The Operations
// list contract makes every row of a tab the same height and puts the updated
// cell and the actions in columns of their own.
//
// The real list is fetched and three rows of different weights are added in
// front of it: a bare one, a blocked one with a long key and every provenance
// fact, and a scheduled one, so the widest and the narrowest rows are on
// screen whatever the deployment holds. Nothing is written.
// ---------------------------------------------------------------------------

const NOW = new Date().toISOString();
const agent = (id: string, name: string) => ({ id, name, icon: null, accessible: true });
const pickup = (state: string) => ({
  state,
  inboxes: [],
  available_at: null,
  auto_cancel_at: null,
  purge_at: null,
  dispatch_attempts: 0,
  max_dispatch_attempts: 3,
});
const item = (id: string, overrides: Record<string, unknown>) => ({
  id: `00000000-0000-4000-8000-00000000${id}`,
  user_id: '00000000-0000-4000-8000-0000000083ff',
  is_company_managed: false,
  owner_agent_id: null,
  created_by_agent_id: null,
  title: 'Operations grid row',
  description: 'A row of the Operations grid test.',
  payload: {},
  status: 'open',
  priority: 'medium',
  correlation_key: null,
  due_at: null,
  claimed_by_agent_id: null,
  claimed_at: null,
  source: 'agent',
  result: null,
  is_stale: false,
  scheduled: null,
  last_activity_at: NOW,
  created_at: NOW,
  updated_at: NOW,
  ...overrides,
});

const ROWS = [
  item('8395', { title: 'A bare row' }),
  item('8396', {
    title: 'A blocked row with a long correlation key and every provenance fact a row can carry',
    status: 'in_progress',
    correlation_key: 'dev:operations-lists-best-in-class-with-a-very-long-suffix',
    context: {
      owner_agent: agent('a1', '[CEO Office] Developer'),
      created_by_agent: agent('a2', '[CEO Office] Orchestrator'),
      claimed_by_agent: agent('a1', '[CEO Office] Developer'),
      person: { is_you: true, name: null },
      visibility: { reason: 'own', workspace: null },
      origin: { kind: 'manual', source: 'agent', conversation: { id: 'c1', title: 'A long chat title' }, conversation_hidden: false, run: null, flow_run: null },
      pickup: pickup('manual'),
    },
  }),
  item('8398', {
    title: 'A row seen through a workspace',
    context: {
      owner_agent: null,
      created_by_agent: null,
      claimed_by_agent: null,
      person: null,
      visibility: { reason: 'workspace', workspace: { id: 'w1', name: 'Digital Security Office' } },
      origin: { kind: 'manual', source: 'agent', conversation: null, conversation_hidden: false, run: null, flow_run: null },
      pickup: pickup('manual'),
    },
  }),
  item('8397', {
    title: 'A scheduled row',
    due_at: NOW,
    scheduled: {
      mode: 'instruction',
      tool: null,
      target: 'A channel',
      preview: 'Post it',
      state: 'scheduled',
      run_id: null,
      conversation_id: null,
      outcome_code: null,
    },
  }),
];

// The chips of every Operations chip strip on the page (`*-chips`) that end
// below their strip, as `text: N px`. A chip on the strip's hidden second line
// (display none) has no box and is skipped. Passed to page.evaluate whole:
// it reads nothing outside its body.
function chipsPastTheirStrip(): string[] {
  return Array.from(document.querySelectorAll('[data-testid$="-chips"]')).flatMap((strip) => {
    const clip = strip.getBoundingClientRect();
    return Array.from(strip.children)
      .filter((chip) => chip.getBoundingClientRect().width > 0 && chip.getBoundingClientRect().top < clip.bottom - 0.5)
      .flatMap((chip) => [chip, ...Array.from(chip.querySelectorAll('[data-slot="badge"]'))])
      .map((chip) => ({ text: chip.textContent?.trim().slice(0, 30), past: chip.getBoundingClientRect().bottom - clip.bottom }))
      .filter((c) => c.past > 0.01)
      .map((c) => `${c.text}: ${c.past.toFixed(2)} px`);
  });
}

// The slots of every shared row grid (`ops-row-grid`) inside the matched
// lines whose vertical centre is more than 1 px off the line's centre, as
// `text: N px` (Sam, 12 Nov 2026: the date and the actions sat at the top of a
// card whose owner and key formed a two-line stack). Passed to evaluateAll
// whole: it reads nothing outside its body.
function slotsOffCentre(els: Element[]): string[] {
  return els.flatMap((el) => {
    const grid = el.querySelector('[data-testid="ops-row-grid"]');
    if (!grid) return [`${el.textContent?.trim().slice(0, 30)}: no ops-row-grid`];
    const row = el.getBoundingClientRect();
    const centre = row.top + row.height / 2;
    return Array.from(grid.children)
      .map((slot) => slot.getBoundingClientRect())
      .filter((r) => r.width > 0 && r.height > 0)
      .map((r) => Math.abs(r.top + r.height / 2 - centre))
      .filter((delta) => delta > 1)
      .map((delta) => `${el.textContent?.trim().slice(0, 30)}: ${delta.toFixed(1)} px`);
  });
}

async function openWorkItems(page: Page) {
  await page.route(
    (url) => url.pathname.endsWith('/api/v1/work-items'),
    async (route: Route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const items = (await response.json()) as Record<string, unknown>[];
      await route.fulfill({ response, json: [...ROWS, ...items].slice(0, 20) });
    },
  );
  await page.goto('/tasks/work-items?vis=mine');
}

// A route callback still inside route.fetch when a test ends (a list refetch
// in flight) is reported as an error outside any test and tears the worker
// down, so the next test never runs (#9462, as #9175): end them all.
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test.describe('Operations Work Items, one grid', () => {
  for (const width of [1024, 1440]) {
    test(`every row the same height and the updated column aligned at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openWorkItems(page);

      const rows = page.getByTestId('work-item-row');
      await expect(rows.filter({ hasText: 'A bare row' })).toBeVisible({ timeout: 15_000 });

      const heights = await rows.evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().height)));
      expect(new Set(heights).size, `row heights ${heights.join(', ')}`).toBe(1);

      // The updated cells end at the same x on every row, and sit at the same
      // offset from their row's top.
      const updated = await rows.evaluateAll((els) =>
        els
          .map((el) => {
            const cell = el.querySelector('[data-testid="work-item-updated"]');
            if (!cell) return null;
            const r = cell.getBoundingClientRect();
            return { right: Math.round(r.right), top: Math.round(r.top - el.getBoundingClientRect().top) };
          })
          .filter((v): v is { right: number; top: number } => v !== null),
      );
      expect(new Set(updated.map((u) => u.right)).size).toBe(1);
      expect(new Set(updated.map((u) => u.top)).size).toBe(1);

      // Every slot of the card (title block, meta stack, updated cell,
      // actions) shares the card's vertical centre within 1 px (Sam, 12 Nov 2026).
      const offCentre = await rows.evaluateAll(slotsOffCentre);
      expect(offCentre, offCentre.join('; ')).toEqual([]);

      // The actions start at the same x on every row.
      const actions = await page
        .getByTestId('work-item-actions')
        .evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().left)));
      expect(new Set(actions).size).toBe(1);

      // No chip is cut on line 1: every chip shown ends inside
      // its line, and a chip that does not fit wraps onto the strip's hidden
      // second line. A chip is shown when it sits on its strip's first line
      // (the strip clips at h-5), whatever the height of line 1.
      const cut = await rows.evaluateAll((els) =>
        els.flatMap((el) => {
          const line = el.querySelector('[data-testid="work-item-line1"]');
          if (!line) return [];
          const box = line.getBoundingClientRect();
          const strips = Array.from(line.querySelectorAll('[data-testid="work-item-state-chip"], [data-testid="work-item-chips"]'));
          return strips.flatMap((strip) => {
            const clip = strip.getBoundingClientRect();
            return Array.from(strip.children)
              .map((chip) => chip.getBoundingClientRect())
              .filter((r) => r.width > 0 && r.top < clip.bottom - 0.5 && r.bottom > clip.top + 0.5)
              .filter((r) => r.right > box.right + 0.5)
              .map((r) => Math.round(r.right - box.right));
          });
        }),
      );
      expect(cut, `chips past line 1 by ${cut.join(', ')} px`).toEqual([]);

      // Every chip shown ends inside its strip, border included: a 21 px badge
      // in the 20 px strip lost the bottom edge of an outline chip (#9774).
      const clipped = await page.evaluate(chipsPastTheirStrip);
      expect(clipped, `chips cut at the bottom: ${clipped.join('; ')}`).toEqual([]);
      // The same at 125 % (CSS zoom rounds the boxes differently), then back.
      await page.evaluate(() => { document.documentElement.style.zoom = '1.25'; });
      const zoomed = await page.evaluate(chipsPastTheirStrip);
      await page.evaluate(() => { document.documentElement.style.zoom = ''; });
      expect(zoomed, `chips cut at the bottom at 125 %: ${zoomed.join('; ')}`).toEqual([]);

      // A cut title never sits beside empty room in its chip strip: a hidden chip keeps no width, so the strip is as
      // wide as the chips it shows.
      const blank = await rows.evaluateAll((els) =>
        els.flatMap((el) => {
          const title = el.querySelector('[data-testid="work-item-title"]');
          const strip = el.querySelector('[data-testid="work-item-chips"]');
          if (!(title instanceof HTMLElement) || !strip) return [];
          if (title.scrollWidth <= title.clientWidth + 1) return [];
          const box = strip.getBoundingClientRect();
          const shown = Array.from(strip.children)
            .map((chip) => chip.getBoundingClientRect())
            .filter((r) => r.width > 0 && r.top < box.bottom - 0.5);
          const used = shown.length ? Math.max(...shown.map((r) => r.right)) - box.left : 0;
          const left = Math.round(box.width - used);
          return left >= 4 ? [`${title.textContent?.slice(0, 30)}: ${left} px`] : [];
        }),
      );
      expect(blank, `cut titles beside a blank strip: ${blank.join('; ')}`).toEqual([]);

      // No horizontal scroll.
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    });
  }

  // At 1024 px the strip drops the chips that do not
  // fit; the priority stays on the row, every pickup label is in the hover
  // card, and no focusable element sits on the strip's hidden line.
  test('nothing a narrow row hides is lost or focusable at 1024 px', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 900 });
    await openWorkItems(page);
    const rows = page.getByTestId('work-item-row');
    await expect(rows.filter({ hasText: 'A bare row' })).toBeVisible({ timeout: 15_000 });

    // The priority is shown whole on every row, inside line 1.
    const priorities = await rows.evaluateAll((els) =>
      els.map((el) => {
        const chip = el.querySelector('[data-testid="work-item-priority"]');
        const line = el.querySelector('[data-testid="work-item-line1"]');
        if (!chip || !line) return 'missing';
        const r = chip.getBoundingClientRect();
        const box = line.getBoundingClientRect();
        return r.width > 0 && r.right <= box.right + 0.5 && chip.scrollWidth <= chip.clientWidth + 1 ? 'ok' : `cut ${Math.round(r.right - box.right)}`;
      }),
    );
    expect(priorities.filter((p) => p !== 'ok'), priorities.join(', ')).toEqual([]);

    // A chip on the strip's hidden line is inert, so it holds no tab stop.
    const focusableHidden = await rows.evaluateAll((els) =>
      els.flatMap((el) => {
        const strip = el.querySelector('[data-testid="work-item-chips"]');
        if (!strip) return [];
        const clip = strip.getBoundingClientRect();
        return Array.from(strip.children)
          .filter((chip) => chip.getBoundingClientRect().top >= clip.bottom - 0.5)
          .filter((chip) => !chip.closest('[inert]'))
          .filter((chip) => chip.matches('[tabindex], a, button') || chip.querySelector('[tabindex], a, button'))
          .map((chip) => chip.textContent || chip.getAttribute('aria-label') || chip.tagName);
      }),
    );
    expect(focusableHidden, `focusable chips on the hidden line: ${focusableHidden.join(', ')}`).toEqual([]);

    // Tabbing through a row never scrolls its strip.
    const row = rows.filter({ hasText: 'A blocked row with a long' });
    await row.getByRole('button', { name: /A blocked row with a long/ }).focus();
    for (let i = 0; i < 4; i += 1) await page.keyboard.press('Tab');
    const scrolled = await rows.evaluateAll((els) =>
      els.map((el) => el.querySelector('[data-testid="work-item-chips"]')?.scrollTop ?? 0).filter((top) => top > 0),
    );
    expect(scrolled).toEqual([]);

    // The pickup label a hidden chip carries is in the hover card. The Tabs
    // above may have left another trigger's card open, or about to open (Tip
    // opens a focused trigger's card one animation frame after the focus), and
    // neither an Escape (#8743) nor a blur (#8764) closes it at a step the
    // test can order. So the step never counts every card on the page: it
    // focuses the title and reads the card that carries this row's title.
    // Both triggers of a row (the title, the "+N" token) show the same card,
    // so whichever of them is open, the facts read are this row's.
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    await row.getByRole('button', { name: /A blocked row with a long/ }).focus();
    const card = page.getByTestId('ops-hover-card').filter({ hasText: /A blocked row with a long/ }).first();
    await expect(card).toBeVisible();
    await expect(card.getByTestId('work-item-fact-priority')).toContainText('Medium');
    await expect(card.getByTestId('work-item-fact-pickup')).toContainText('No automatic pickup');
  });

  test('the hover card names every fact of a row whole', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openWorkItems(page);
    const row = page.getByTestId('work-item-row').filter({ hasText: 'A blocked row with a long' });
    await expect(row).toBeVisible({ timeout: 15_000 });
    await row.getByRole('button', { name: /A blocked row with a long/ }).focus();
    const card = page.getByTestId('ops-hover-card');
    await expect(card).toBeVisible();
    await expect(card).toContainText('dev:operations-lists-best-in-class-with-a-very-long-suffix');
    await expect(card).toContainText('[CEO Office] Orchestrator');
    await expect(card).toContainText('Last activity');
  });
});

// ---------------------------------------------------------------------------
// Shared State and Worksets on the same grid: a bare row, a heavy row (long
// label and key, stale or still collecting, notes or evictions) and a
// resolved row are added in front of the real list, nothing is written.
// ---------------------------------------------------------------------------

const entry = (id: string, overrides: Record<string, unknown>) => ({
  id: `00000000-0000-4000-8000-00000001${id}`,
  user_id: '00000000-0000-4000-8000-0000000083ff',
  is_company_managed: false,
  key: `grid:${id}`,
  label: null,
  owner_agent_id: null,
  status: 'active',
  is_stale: false,
  value: {},
  notes: [],
  resolved_at: null,
  created_at: NOW,
  updated_at: NOW,
  ...overrides,
});

const STATE_ROWS = [
  entry('8395', { label: 'A bare entry' }),
  entry('8396', {
    label: 'A heavy entry with a long label, a stale flag and notes that a row can never hold whole',
    key: 'dev:operations-lists-best-in-class-with-a-very-long-suffix',
    is_stale: true,
    value: { prs: [], gaps: [], runs: [] },
    notes: [{ at: NOW, note: 'A latest note long enough to be cut on line 2 of the row at any width the page offers' }],
  }),
  entry('8397', { label: 'A resolved entry', status: 'resolved', resolved_at: NOW }),
];

const workset = (id: string, overrides: Record<string, unknown>) => ({
  id: `00000000-0000-4000-8000-00000002${id}`,
  key: `grid:${id}`,
  label: null,
  scope: 'global',
  scope_ref: null,
  status: 'active',
  processing: 'auto',
  workers: 1,
  item_count: 3,
  evicted_count: 0,
  content_chars: 1200,
  max_items: 100,
  max_content_chars: 100000,
  max_auto_calls: 10,
  auto_calls_used: 0,
  declared_total: null,
  declared_total_source: null,
  sealed_at: NOW,
  watermark_at: null,
  last_progress_at: null,
  expires_at: null,
  resolved_at: null,
  is_company_managed: false,
  owner_agent_id: null,
  created_at: NOW,
  updated_at: NOW,
  ...overrides,
});

const WORKSET_ROWS = [
  workset('8395', { label: 'A bare workset' }),
  workset('8396', {
    label: 'A heavy workset still collecting, with evictions and an announced total that disagrees',
    key: 'corpus:operations-lists-best-in-class-with-a-very-long-suffix',
    scope: 'flow',
    sealed_at: null,
    item_count: 1234,
    evicted_count: 12,
    declared_total: 2000,
    content_chars: 4_500_000,
  }),
  workset('8397', { label: 'A resolved workset', status: 'resolved', resolved_at: NOW }),
];

async function openList(page: Page, path: string, url: string, rows: Record<string, unknown>[]) {
  await page.route(
    (u) => u.pathname.endsWith(path),
    async (route: Route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const items = (await response.json()) as Record<string, unknown>[];
      await route.fulfill({ response, json: [...rows, ...items].slice(0, 20) });
    },
  );
  await page.goto(url);
}

const task = (id: string, overrides: Record<string, unknown>) => ({
  task_id: `00000000-0000-4000-8000-00000003${id}`,
  user_id: '00000000-0000-4000-8000-0000000083ff',
  parent_agent_id: '00000000-0000-4000-8000-000000000001',
  parent_agent_name: 'Grid parent',
  target_agent_id: '00000000-0000-4000-8000-000000000002',
  target_agent_name: 'Grid target',
  task_description: `Task ${id}`,
  status: 'completed',
  result: 'Done.',
  error: null,
  conversation_id: null,
  assignment_run_id: null,
  parent_run_id: null,
  created_at: NOW,
  started_at: NOW,
  completed_at: NOW,
  ...overrides,
});

const TASK_ROWS = [
  task('8395', { task_description: 'A bare task' }),
  task('8396', {
    task_description: 'A heavy task still in flight, with a run, an orchestration and a description no row can hold whole',
    status: 'running',
    result: null,
    completed_at: null,
    parent_agent_name: 'A delegating agent with-a-very-long-suffix',
    assignment_run_id: '00000000-0000-4000-8000-000000008396',
    execution_state: 'running',
    execution_id: '00000000-0000-4000-8000-000000018396',
    last_progress_at: NOW,
  }),
  task('8397', { task_description: 'A failed task', status: 'failed', result: null, error: 'The target agent refused the task with a reason long enough to be cut on line 2' }),
];

const workspace = (id: string, overrides: Record<string, unknown>) => ({
  id: `00000000-0000-4000-8000-00000004${id}`,
  user_id: '00000000-0000-4000-8000-0000000083ff',
  owner_name: 'Grid owner',
  name: `Workspace ${id}`,
  slug: `grid-${id}`,
  description: '',
  icon: '',
  color: '',
  is_default: false,
  is_archived: false,
  is_company_managed: false,
  shared_group_ids: null,
  shared_group_permissions: null,
  can_manage: true,
  instructions: '',
  created_at: NOW,
  updated_at: NOW,
  contents: { conversations: 0, work_items: 0, held_work_items: 0, open_work_items: 0, shared_states: 0, worksets: 0, knowledge_bases: 0 },
  hosted_fleets: [],
  hosted_fleet_count: 0,
  live_hosted_fleet_count: 0,
  ...overrides,
});

const WORKSPACE_ROWS = [
  workspace('8395', { name: 'A bare workspace' }),
  workspace('8396', {
    name: 'A heavy workspace hosting a fleet, shared, with open work and a description no row can hold whole',
    slug: 'operations-lists-best-in-class-with-a-very-long-suffix',
    description: 'Where the fleet coordinates its work items, shared state and worksets across every run it starts.',
    shared_group_ids: ['00000000-0000-4000-8000-0000000000aa'],
    contents: { conversations: 12, work_items: 340, held_work_items: 300, open_work_items: 27, shared_states: 58, worksets: 4, knowledge_bases: 2 },
    hosted_fleets: [{ id: '00000000-0000-4000-8000-0000000000bb', name: 'A hosted fleet', slug: 'a-hosted-fleet', is_archived: false }],
    hosted_fleet_count: 1,
    live_hosted_fleet_count: 1,
  }),
  workspace('8397', { name: 'An archived workspace', is_archived: true }),
];

const TABS = [
  { name: 'Shared State', path: '/api/v1/shared-state', url: '/operations/shared-state?vis=mine', rows: STATE_ROWS, prefix: 'shared-state', bare: 'A bare entry', heavy: 'A heavy entry' },
  { name: 'Worksets', path: '/api/v1/worksets', url: '/operations/worksets?vis=mine', rows: WORKSET_ROWS, prefix: 'workset', bare: 'A bare workset', heavy: 'A heavy workset' },
  { name: 'Background Tasks', path: '/api/v1/chat/sub-agent-tasks', url: '/operations/background-tasks?vis=mine', rows: TASK_ROWS, prefix: 'background-task', bare: 'A bare task', heavy: 'A heavy task', detail: 'page', whole: '00000000-0000-4000-8000-000000008396' },
  { name: 'Workspaces', path: '/api/v1/workspaces', url: '/tasks/workspaces', rows: WORKSPACE_ROWS, prefix: 'workspace', bare: 'A bare workspace', heavy: 'A heavy workspace', detail: 'page', whole: 'Where the fleet coordinates' },
];

for (const tab of TABS) {
  test.describe(`Operations ${tab.name}, one grid`, () => {
    for (const width of [1024, 1440]) {
      test(`every row the same height and the updated column aligned at ${width} px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await openList(page, tab.path, tab.url, tab.rows);
        const lines = page.getByTestId(`${tab.prefix}-line`);
        await expect(lines.filter({ hasText: tab.bare })).toBeVisible({ timeout: 15_000 });

        const heights = await lines.evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().height)));
        expect(new Set(heights).size, `row heights ${heights.join(', ')}`).toBe(1);

        const updated = await lines.evaluateAll((els, prefix) =>
          els
            .map((el) => {
              const cell = el.querySelector(`[data-testid="${prefix}-updated"]`);
              if (!cell) return null;
              const r = cell.getBoundingClientRect();
              return { right: Math.round(r.right), top: Math.round(r.top - el.getBoundingClientRect().top) };
            })
            .filter((v): v is { right: number; top: number } => v !== null),
        tab.prefix);
        expect(updated.length).toBeGreaterThan(0);
        expect(new Set(updated.map((u) => u.right)).size).toBe(1);
        expect(new Set(updated.map((u) => u.top)).size).toBe(1);

        // Every slot of the card shares the card's vertical centre within 1 px.
        const offCentre = await lines.evaluateAll(slotsOffCentre);
        expect(offCentre, offCentre.join('; ')).toEqual([]);

        const actions = await page
          .getByTestId(`${tab.prefix}-actions`)
          .evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().left)));
        expect(new Set(actions).size).toBe(1);

        const clipped = await page.evaluate(chipsPastTheirStrip);
        expect(clipped, `chips cut at the bottom: ${clipped.join('; ')}`).toEqual([]);

        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow).toBeLessThanOrEqual(0);
      });
    }

    test('the hover card names every fact of a row whole, and a click unfolds the detail under a row of unchanged height', async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 });
      await openList(page, tab.path, tab.url, tab.rows);
      const row = page.getByTestId(`${tab.prefix}-row`).filter({ hasText: tab.heavy });
      await expect(row).toBeVisible({ timeout: 15_000 });
      await row.getByTestId(`${tab.prefix}-title`).focus();
      const card = page.getByTestId('ops-hover-card');
      await expect(card).toBeVisible();
      await expect(card).toContainText('with-a-very-long-suffix');
      if (tab.detail === 'page') {
        // The detail is another page (the run, the workspace): the hover card
        // names what the row cuts whole.
        await expect(card).toContainText(tab.whole ?? '');
        return;
      }
      await expect(card).toContainText('Updated');

      const line = row.getByTestId(`${tab.prefix}-line`);
      const before = await line.evaluate((el) => Math.round(el.getBoundingClientRect().height));
      await row.getByRole('button', { name: 'Expand' }).click();
      await expect(row.getByTestId(`${tab.prefix}-detail`)).toBeVisible();
      expect(await line.evaluate((el) => Math.round(el.getBoundingClientRect().height))).toBe(before);
    });
  });
}

const run = (id: string, overrides: Record<string, unknown>) => ({
  id: `00000000-0000-4000-8000-00000005${id}`,
  assignment_id: null,
  assignment_name: null,
  agent_id: '00000000-0000-4000-8000-000000000001',
  agent_name: 'Grid agent',
  agent_icon: '',
  status: 'completed',
  outcome: 'ok',
  is_company_managed: false,
  notify_on_complete: false,
  show_in_digest: false,
  parent_run_id: null,
  child_run_count: 0,
  parent_agent_name: null,
  parent_agent_icon: null,
  trigger_payload: null,
  trigger_context: null,
  actions_taken: null,
  result_summary: 'Done.',
  rendered_prompt: null,
  agentic_steps: null,
  attachments: null,
  error: null,
  failure_cause: null,
  failure_determination: null,
  stop_reason: null,
  human_input_question: null,
  human_input_response: null,
  human_input_history: null,
  pending_approvals: null,
  langfuse_trace_id: null,
  llm_trace_id: null,
  total_input_tokens: null,
  total_output_tokens: null,
  started_at: NOW,
  completed_at: NOW,
  created_at: NOW,
  ...overrides,
});

const RUN_ROWS = [
  run('8395', { result_summary: 'A bare run' }),
  run('8396', {
    agent_name: 'A delegating agent with-a-very-long-suffix',
    assignment_name: 'A heavy assignment',
    result_summary: 'A heavy run whose summary no row can hold whole, with every fact a run can carry',
    child_run_count: 3,
    total_input_tokens: 120000,
    total_output_tokens: 4000,
  }),
];

// Operations > Executions in card view (Sam, 12 Nov 2026): the relative date
// and the red trash sat at the top of a card whose agent line and "# run id +
// copy + +N" key line formed a two-line stack. The date, the trash and the
// owner/key stack share the card's vertical centre within 1 px.
test.describe('Operations Executions, one grid', () => {
  for (const width of [1024, 1440]) {
    test(`every slot of an execution card on the card's vertical centre at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openList(page, '/api/v1/assignments/runs/all', '/operations/executions?vis=mine', RUN_ROWS);
      const lines = page.getByTestId('execution-line');
      await expect(lines.filter({ hasText: 'A bare run' })).toBeVisible({ timeout: 15_000 });

      const offCentre = await lines.evaluateAll(slotsOffCentre);
      expect(offCentre, offCentre.join('; ')).toEqual([]);

      const heavy = lines.filter({ hasText: 'A heavy run' });
      const deltas = await heavy.evaluate((el) => {
        const row = el.getBoundingClientRect();
        const centre = row.top + row.height / 2;
        const off = (node: Element | null) => {
          if (!node) return -1;
          const r = node.getBoundingClientRect();
          return Math.abs(r.top + r.height / 2 - centre);
        };
        return {
          date: off(el.querySelector('[data-testid="execution-updated"]')),
          trash: off(el.querySelector('[aria-label="Delete run"]')),
          stack: off(el.querySelector('[data-testid="ops-meta"]')),
        };
      });
      for (const [slot, delta] of Object.entries(deltas)) {
        expect(delta, `${slot} missing or ${delta.toFixed(1)} px off the card centre`).toBeGreaterThanOrEqual(0);
        expect(delta, `${slot} ${delta.toFixed(1)} px off the card centre`).toBeLessThanOrEqual(1);
      }
    });
  }
});

// Line 2 of an in-flight task holds several parts (state, last progress, run,
// orchestration). On a narrow row a part that does not fit is hidden whole,
// never shrunk under its label so that it paints over its neighbour.
test.describe('Operations Background Tasks, execution line', () => {
  for (const width of [1024, 1440]) {
    test(`no two parts of line 2 overlap and none runs past the line at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openList(page, '/api/v1/chat/sub-agent-tasks', '/operations/background-tasks?vis=mine', TASK_ROWS);
      const row = page.getByTestId('background-task-row').filter({ hasText: 'A heavy task' });
      await expect(row).toBeVisible({ timeout: 15_000 });
      const line = row.getByTestId('task-execution-line');
      await expect(line).toBeVisible();

      const problems = await line.evaluate((el) => {
        const box = el.getBoundingClientRect();
        const strip = el.querySelector('[data-testid="task-execution-parts"]');
        if (!strip) return ['no parts strip'];
        const shown = Array.from(strip.children)
          .filter((c): c is HTMLElement => c instanceof HTMLElement && c.dataset.opsStripStart === undefined)
          .filter((c) => c.dataset.opsHidden !== 'true' && c.getBoundingClientRect().width > 0);
        const out: string[] = [];
        if (shown.length === 0) out.push('nothing shown');
        const rects = shown.map((c) => ({ text: c.textContent ?? '', r: c.getBoundingClientRect() }));
        rects.forEach(({ text, r }, i) => {
          if (r.right > box.right + 0.5) out.push(`${text} past the line by ${Math.round(r.right - box.right)} px`);
          // A shown part keeps its own content inside its box.
          const node = shown[i];
          if (node.scrollWidth > node.clientWidth + 1) out.push(`${text} cut inside its box`);
          const next = rects[i + 1];
          if (next && next.r.left < r.right - 0.5) out.push(`${text} over ${next.text} by ${Math.round(r.right - next.r.left)} px`);
        });
        return out;
      });
      expect(problems, problems.join('; ')).toEqual([]);

      // The execution state is the part kept first.
      await expect(line).toContainText('Execution running');
    });
  }
});
