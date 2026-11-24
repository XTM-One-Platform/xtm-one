import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// The run page decides a pause on the one decision card (#842, S3;
// dev-docs/human-gates.md section 3).
//
// - a tool approval: Approve, Approve always (after its confirm) and Reject
//   (with a reason) each post the decision set `POST .../respond` takes, one
//   decision per pending call, so the backend keeps the call fingerprint it
//   checks; a call that needs the person's own consent offers no Approve
//   always;
// - a question asking `APPROVE <code>` (an irreversible action escalated by
//   hitl_consent) offers one explicit consent option that replies exactly
//   that, so the server check stays the one that decides.
//
// The paused run is a fixture: the spec pins what the card sends, the
// backend's handling of each verdict is covered by its own tests. Nothing is
// written: every respond call is answered by the fixture.
// ---------------------------------------------------------------------------

const RUN_ID = '00000000-0000-4000-8000-000000008421';
const RUN_PATH = `/api/v1/assignments/runs/${RUN_ID}`;
const RESPOND_PATH = `${RUN_PATH}/respond`;
const ACTION_REQUESTS_PATH = `${RUN_PATH}/action-requests`;

interface Proposal {
  tool_call_id: string;
  tool_name: string;
  tool_description: string;
  arguments: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  source: string;
  requires_consent?: boolean;
}

function proposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    tool_call_id: 'call_e2e_842_1',
    tool_name: 'generate_file',
    tool_description: 'Generate a downloadable file.',
    arguments: { filename: 'e2e-842.txt', content: 'decision card' },
    input_schema: {},
    source: 'builtin',
    ...overrides,
  };
}

function pausedRun(overrides: Record<string, unknown> = {}) {
  return {
    id: RUN_ID,
    assignment_id: null,
    assignment_name: 'E2E decision card',
    agent_id: '00000000-0000-4000-8000-000000008422',
    agent_name: 'E2E Decision Desk',
    agent_icon: 'Bot',
    status: 'awaiting_human',
    outcome: 'active',
    is_company_managed: false,
    notify_on_complete: false,
    show_in_digest: false,
    parent_run_id: null,
    child_run_count: 0,
    parent_agent_name: null,
    parent_agent_icon: null,
    trigger_payload: null,
    trigger_context: null,
    a2a_exchange: null,
    actions_taken: null,
    result_summary: null,
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
    approval_answered: false,
    langfuse_trace_id: null,
    llm_trace_id: null,
    total_input_tokens: null,
    total_output_tokens: null,
    started_at: '2026-11-22T01:00:00Z',
    completed_at: null,
    created_at: '2026-11-22T01:00:00Z',
    ...overrides,
  };
}

/** Serve *run* on the run page, and record every body posted to respond. */
async function serveRun(page: Page, run: Record<string, unknown>): Promise<unknown[]> {
  const posted: unknown[] = [];
  await page.route(
    (url: URL) => url.pathname === ACTION_REQUESTS_PATH,
    (route: Route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );
  await page.route(
    (url: URL) => url.pathname === RESPOND_PATH,
    (route: Route) => {
      posted.push(route.request().postDataJSON());
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...run, status: 'queued' }),
      });
    },
  );
  await page.route(
    (url: URL) => url.pathname === RUN_PATH,
    (route: Route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(run) }),
  );
  return posted;
}

const card = (page: Page) => page.getByTestId('run-decision');
const option = (page: Page, key: string) =>
  card(page).locator(`[data-testid="run-decision-tile"][data-option="${key}"] [data-testid="run-decision-option"]`);
const confirmSend = (page: Page) => card(page).getByTestId('run-decision-confirm-send');

test.describe('Run detail - the decision card', () => {
  test('a tool approval is read without the run page and approved once', async ({ page }) => {
    const posted = await serveRun(page, pausedRun({ pending_approvals: [proposal()] }));
    await page.goto(`/operations/executions/${RUN_ID}`);

    await expect(card(page)).toBeVisible();
    await expect(card(page).getByTestId('run-decision-question')).toContainText('?');
    await expect(card(page).getByTestId('run-decision-tile')).toHaveCount(3);
    await expect(card(page).getByTestId('run-decision-option-label')).toHaveText(['Approve', 'Approve always', 'Reject']);
    // The tool and its arguments are under Technical details, not in the way.
    await expect(card(page).getByTestId('run-decision-technical')).toContainText('generate_file');

    const sent = page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname === RESPOND_PATH);
    await option(page, 'approve').click();
    await sent;
    expect(posted).toEqual([{ decisions: [{ tool_call_id: 'call_e2e_842_1', decision: 'approve' }] }]);
  });

  test('approve always asks for a confirm, then keeps one decision per call', async ({ page }) => {
    const calls = [proposal(), proposal({ tool_call_id: 'call_e2e_842_2' })];
    const posted = await serveRun(page, pausedRun({ pending_approvals: calls }));
    await page.goto(`/operations/executions/${RUN_ID}`);

    await option(page, 'approve_always').click();
    const confirm = card(page).getByTestId('run-decision-confirm');
    await expect(confirm).toHaveAttribute('data-option', 'approve_always');
    await expect(confirm).toContainText('until you revoke it in your profile');
    // Nothing is sent before the confirm.
    expect(posted).toEqual([]);

    const sent = page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname === RESPOND_PATH);
    await confirmSend(page).click();
    await sent;
    expect(posted).toEqual([
      {
        decisions: [
          { tool_call_id: 'call_e2e_842_1', decision: 'approve_always' },
          { tool_call_id: 'call_e2e_842_2', decision: 'approve_always' },
        ],
      },
    ]);
  });

  test('reject sends the reason the agent adapts to', async ({ page }) => {
    const posted = await serveRun(page, pausedRun({ pending_approvals: [proposal()] }));
    await page.goto(`/operations/executions/${RUN_ID}`);

    await option(page, 'reject').click();
    const confirm = card(page).getByTestId('run-decision-confirm');
    await expect(confirm).toHaveAttribute('data-option', 'reject');
    await confirm.getByRole('textbox').fill('Not this file, use the shared folder.');

    const sent = page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname === RESPOND_PATH);
    await confirmSend(page).click();
    await sent;
    expect(posted).toEqual([
      {
        decisions: [
          { tool_call_id: 'call_e2e_842_1', decision: 'reject', rejection_reason: 'Not this file, use the shared folder.' },
        ],
      },
    ]);
  });

  test('a call that needs consent offers no approve always', async ({ page }) => {
    await serveRun(page, pausedRun({ pending_approvals: [proposal({ requires_consent: true })] }));
    await page.goto(`/operations/executions/${RUN_ID}`);

    await expect(card(page).getByTestId('run-decision-option-label')).toHaveText(['Approve', 'Reject']);
    await option(page, 'approve').click();
    await expect(card(page).getByTestId('run-decision-confirm')).toContainText('cannot be undone');
  });

  test('the consent option replies the code the server checks', async ({ page }) => {
    const question = 'Deleting the branch cannot be undone. Reply APPROVE DEL-1a2b3c4d to go on.';
    const posted = await serveRun(page, pausedRun({ human_input_question: question }));
    await page.goto(`/operations/executions/${RUN_ID}`);

    await expect(card(page).getByTestId('run-decision-question')).toHaveText(question);
    await option(page, 'consent').click();
    await expect(card(page).getByTestId('run-decision-confirm')).toHaveAttribute('data-option', 'consent');

    const sent = page.waitForRequest((r) => r.method() === 'POST' && new URL(r.url()).pathname === RESPOND_PATH);
    await confirmSend(page).click();
    await sent;
    expect(posted).toEqual([{ response: 'APPROVE DEL-1A2B3C4D' }]);
  });
});
