import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Work items list view: every cell stays inside its column.
//
// Sam, 15 Nov 2026: in Operations > Work items (list view) a long owner agent
// name ran past the Owner column and was painted over the Due date of its row.
// The DataTable now clips every cell under layout="fixed" and ends a plain text
// cell with an ellipsis; WorkItemAgent keeps its link within the cell
// (max-w-full) so the name truncates and its tooltip names it whole.
//
// The real list is fetched and one row with a 60+ character owner name and a
// due date is added in front of it. Nothing is written.
// ---------------------------------------------------------------------------

const NOW = new Date().toISOString();
const LONG_OWNER = '[Example · Very Long Department Name] Orchestrator Agent for the region';

const ROW = {
  id: '00000000-0000-4000-8000-000000015110',
  user_id: '00000000-0000-4000-8000-0000000083ff',
  is_company_managed: false,
  owner_agent_id: '00000000-0000-4000-8000-0000000151a1',
  created_by_agent_id: null,
  title: 'A row owned by an agent with a very long name',
  description: 'A row of the list fit test.',
  payload: {},
  status: 'open',
  priority: 'high',
  correlation_key: null,
  due_at: NOW,
  claimed_by_agent_id: null,
  claimed_at: null,
  source: 'agent',
  result: null,
  is_stale: false,
  scheduled: null,
  last_activity_at: NOW,
  created_at: NOW,
  updated_at: NOW,
  context: {
    owner_agent: { id: '00000000-0000-4000-8000-0000000151a1', name: LONG_OWNER, icon: null, accessible: true },
    created_by_agent: null,
    claimed_by_agent: null,
    person: { is_you: true, name: null },
    visibility: { reason: 'own', workspace: null },
    origin: { kind: 'manual', source: 'agent', conversation: null, conversation_hidden: false, run: null, flow_run: null },
    pickup: { state: 'manual', inboxes: [], available_at: null, auto_cancel_at: null, purge_at: null, dispatch_attempts: 0, max_dispatch_attempts: 3 },
  },
};

async function openListView(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('copilot-view:operations-work-items', 'list');
  });
  await page.route(
    (url) => url.pathname.endsWith('/api/v1/work-items'),
    async (route: Route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const items = (await response.json()) as Record<string, unknown>[];
      await route.fulfill({ response, json: [ROW, ...items].slice(0, 20) });
    },
  );
  await page.goto('/tasks/work-items?vis=mine');
}

// The children of every cell of the matched rows whose box ends past their
// cell's padding box, as `text: N px`. Passed to evaluateAll whole: it reads
// nothing outside its body.
function contentPastItsCell(rows: Element[]): string[] {
  return rows.flatMap((row) =>
    Array.from(row.querySelectorAll(':scope > td')).flatMap((cell) => {
      const box = cell.getBoundingClientRect();
      if (box.width === 0) return [];
      return Array.from(cell.querySelectorAll('*'))
        .map((el) => ({ el, r: el.getBoundingClientRect() }))
        .filter(({ r }) => r.width > 0 && (r.right > box.right + 1 || r.left < box.left - 1))
        .map(({ el, r }) => `${el.textContent?.trim().slice(0, 30)}: ${Math.max(r.right - box.right, box.left - r.left).toFixed(1)} px`);
    }),
  );
}

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test.describe('Work items list view, every cell inside its column', () => {
  for (const width of [1440, 1920]) {
    test(`a long owner name ends with an ellipsis inside the Owner column at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openListView(page);

      const rows = page.getByTestId('work-items-table-row');
      const row = rows.filter({ hasText: 'A row owned by an agent with a very long name' });
      await expect(row).toBeVisible({ timeout: 15_000 });

      // The owner link stays inside its cell and its name is cut, never painted past it.
      const owner = row.getByTestId('work-item-agent-link');
      await expect(owner).toBeVisible();
      const cut = await owner.locator('[data-cut-text]').count();
      expect(cut, 'the long owner name is cut with an ellipsis').toBe(1);
      await expect(owner).toContainText(LONG_OWNER);

      // No content of any cell of any row reaches past its cell.
      const past = await rows.evaluateAll(contentPastItsCell);
      expect(past, past.join('; ')).toEqual([]);

      // The table fits its card.
      const scroller = page.locator('table').locator('xpath=..');
      const overflow = await scroller.evaluate((el) => el.scrollWidth - el.clientWidth);
      expect(overflow).toBeLessThanOrEqual(1);
    });
  }
});
