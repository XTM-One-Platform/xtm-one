import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// "Waiting for you": one inbox for every open decision (#842, S5;
// dev-docs/human-gates.md section 4).
//
// Four decisions from four sources (a run's question, a tool approval, a flow
// and a work item) are listed in the one view of Tasks > Waiting for me, with
// the same columns. Each row opens the shared decision card in a side panel
// and is answered there through the one answer path
// (`POST /decisions/{id}/answer`, surface `inbox`); the source stays a link
// under the card, and an answered decision leaves the list.
//
// The inbox is a fixture: the spec pins what the view lists and what the panel
// sends; the backend's listing and answering are covered by
// backend/tests/test_decision_inbox_842_s5_postgres.py. Nothing is written:
// every answer is taken by the fixture.
// ---------------------------------------------------------------------------

const WAITING_PATH = '/api/v1/decisions/waiting';
const ANSWER_PATH = /^\/api\/v1\/decisions\/([^/]+)\/answer$/;

const OPTIONS = [
  { key: 'A', label: 'Go ahead', effect: 'The agent carries on.' },
  { key: 'B', label: 'Stop here', effect: 'The agent stops and reports.' },
];

interface Row {
  id: string;
  source_type: 'run' | 'flow_run' | 'work_item';
  source_id: string;
  kind: 'hitl' | 'tool_approval' | 'flow_ask' | 'work_item';
  question: string;
  ask: Record<string, unknown>;
  origin: string | null;
  agent: string | null;
  asked_at: string | null;
  deadline: string | null;
  link: string;
}

function row(n: number, overrides: Partial<Row>): Row {
  const id = `00000000-0000-4000-8000-00000000842${n}`;
  return {
    id,
    source_type: 'run',
    source_id: `00000000-0000-4000-8000-00000000843${n}`,
    kind: 'hitl',
    question: `Question ${n}`,
    ask: { options: OPTIONS, recommendation: 'A' },
    origin: null,
    agent: 'E2E Decision Desk',
    asked_at: '2026-11-22T01:00:00Z',
    deadline: null,
    link: `/operations/executions/00000000-0000-4000-8000-00000000843${n}`,
    ...overrides,
  };
}

// Ordered as the server orders them: soonest deadline first, then oldest asked.
const ROWS: Row[] = [
  row(1, {
    kind: 'work_item',
    source_type: 'work_item',
    question: 'Publish the quarterly report draft?',
    origin: 'Quarterly report',
    deadline: '2026-11-23T09:00:00Z',
    link: '/tasks/work-items/00000000-0000-4000-8000-000000008431',
  }),
  row(2, {
    kind: 'tool_approval',
    question: 'Approve generate_file for the e2e desk?',
    origin: 'Weekly digest',
  }),
  row(3, {
    kind: 'flow_ask',
    source_type: 'flow_run',
    question: 'Send the onboarding mail to the new partner?',
    origin: 'Partner onboarding',
    link: '/flows/runs/00000000-0000-4000-8000-000000008433',
  }),
  row(4, {
    kind: 'hitl',
    question: 'Which folder holds the export?',
    origin: 'Export desk',
    asked_at: '2026-11-22T02:00:00Z',
  }),
];

/** Serve the inbox from a live copy of ROWS, drop a row once answered, record every answer. */
async function serveInbox(page: Page): Promise<{ id: string; body: unknown }[]> {
  const open = [...ROWS];
  const answers: { id: string; body: unknown }[] = [];
  await page.route(
    (url: URL) => url.pathname === WAITING_PATH,
    (route: Route) => route.fulfill({ json: { enabled: true, reason: null, items: open } }),
  );
  await page.route(
    (url: URL) => ANSWER_PATH.test(url.pathname),
    (route: Route) => {
      const id = ANSWER_PATH.exec(new URL(route.request().url()).pathname)?.[1] ?? '';
      answers.push({ id, body: route.request().postDataJSON() });
      const at = open.findIndex((r) => r.id === id);
      if (at >= 0) open.splice(at, 1);
      return route.fulfill({ json: { id, status: 'answered' } });
    },
  );
  return answers;
}

const inbox = (page: Page) => page.getByTestId('waiting-for-you');
const rows = (page: Page) => inbox(page).getByTestId('waiting-for-you-row');
const panel = (page: Page) => page.getByTestId('waiting-for-you-panel');
const option = (page: Page, key: string) =>
  panel(page).locator(`[data-testid="waiting-decision-tile"][data-option="${key}"] [data-testid="waiting-decision-option"]`);

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test.describe('Waiting for you: one inbox across every source', () => {
  test('four sources are listed in one view, with the same columns', async ({ page }) => {
    await serveInbox(page);
    await page.goto('/tasks/work-items?view=waiting');

    await expect(inbox(page)).toBeVisible({ timeout: 15_000 });
    await expect(rows(page)).toHaveCount(4);
    for (const header of ['Decision', 'From', 'Asked', 'Answer by']) {
      await expect(inbox(page).getByRole('columnheader', { name: header })).toBeVisible();
    }
    // The server's order is kept, each row named by its source.
    await expect(rows(page).nth(0)).toContainText('Work item: Quarterly report');
    await expect(rows(page).nth(1)).toContainText('Tool approval: Weekly digest');
    await expect(rows(page).nth(2)).toContainText('Flow: Partner onboarding');
    await expect(rows(page).nth(3)).toContainText('Run: Export desk');
  });

  test('each decision is answered from the dialog and leaves the list', async ({ page }) => {
    const answers = await serveInbox(page);
    await page.goto('/tasks/work-items?view=waiting');
    await expect(rows(page)).toHaveCount(4, { timeout: 15_000 });

    for (const [done, decision] of ROWS.entries()) {
      await rows(page).filter({ hasText: decision.question }).click();
      await expect(panel(page)).toBeVisible();
      await expect(panel(page).getByTestId('waiting-decision-question')).toContainText(decision.question);
      // The source stays a link under the card.
      await expect(panel(page).getByTestId('waiting-decision-source-link')).toHaveAttribute('href', decision.link);

      const sent = page.waitForRequest((r) => r.method() === 'POST' && ANSWER_PATH.test(new URL(r.url()).pathname));
      await option(page, 'A').click();
      await sent;
      await expect(panel(page)).toBeHidden();
      await expect(rows(page)).toHaveCount(ROWS.length - done - 1);
    }

    expect(answers).toEqual(ROWS.map((r) => ({ id: r.id, body: { option: 'A', surface: 'inbox' } })));
    await expect(inbox(page)).toContainText('Nothing is waiting for you');
  });

  test('the inbox is not shown while the read switch is off', async ({ page }) => {
    await page.route(
      (url: URL) => url.pathname === WAITING_PATH,
      (route: Route) => route.fulfill({ json: { enabled: false, reason: 'switch off', items: [] } }),
    );
    const served = page.waitForResponse((r) => new URL(r.url()).pathname === WAITING_PATH);
    await page.goto('/tasks/work-items?view=waiting');
    await served;
    await expect(inbox(page)).toHaveCount(0);
  });
});
