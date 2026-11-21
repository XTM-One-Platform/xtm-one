import { test, expect } from '@playwright/test';
import { AssignmentPage } from './model/assignment.page';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Assignments — create an assignment and verify the trigger configuration UI.
// No LLM calls are triggered — the assignment is created but never run.
// ---------------------------------------------------------------------------

test.describe.serial('Assignments — create and configure trigger', () => {
  const AGENT_NAME = `E2E Assignment Agent ${Date.now()}`;
  let agentId = '';
  let agentDetailUrl = '';

  test.beforeAll(async () => {
    // Create a throw-away agent via the API so we don't rely on browser UI setup.
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.post('/api/v1/agents', {
        headers: { Authorization: `Bearer ${token}` },
        data: { name: AGENT_NAME, persona: 'Temp agent for assignment tests.' },
      });
      if (!resp.ok()) {
        const body = await resp.text();
        throw new Error(`Failed to create test agent in beforeAll: ${resp.status()} ${resp.statusText()} - ${body}`);
      }
      const agent = await resp.json();
      agentId = agent.id;
      agentDetailUrl = `/agents/${agentId}`;
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should show the Add Assignment button on the agent overview', async ({ page }) => {
    await page.goto(agentDetailUrl);
    await expect(page.getByRole('button', { name: 'Add Assignment' })).toBeVisible({ timeout: 10_000 });
  });

  test('should create an assignment with a manual trigger', async ({ page }) => {
    await page.goto(agentDetailUrl);
    const assignmentPage = new AssignmentPage(page);
    await assignmentPage.clickAddAssignment();
    await assignmentPage.fillName('Daily Summary');
    await assignmentPage.fillPrompt('Summarise today\'s activity.');
    await assignmentPage.clickCreate();

    // Panel closes, agent detail updates — wait for the assignment to appear in the list.
    await expect(page.getByText('Daily Summary')).toBeVisible({ timeout: 15_000 });
  });

  test('should display assignment detail with trigger section', async ({ page }) => {
    await page.goto(agentDetailUrl);
    const assignmentPage = new AssignmentPage(page);

    // The assignment created in the previous test should now appear in the list.
    await expect(page.getByText('Daily Summary')).toBeVisible({ timeout: 10_000 });

    // Click through to the assignment detail.
    await page.getByText('Daily Summary').first().click();
    await assignmentPage.waitForDetail();

    // Trigger configuration card should be on the page.
    await expect(page.getByText(/Trigger type|Schedule|Manual/i).first()).toBeVisible({ timeout: 10_000 });
  });

  test('should surface an enabled dry run with the amber header badge', async ({ page }) => {
    // Create the assignment via the API so the test exercises exactly what the
    // UI must make impossible to miss: an assignment already running
    // in dry-run mode, where every tool call is stubbed.
    const { apiCtx, token } = await createAdminApiContext();
    let assignmentId: string;
    try {
      const resp = await apiCtx.post('/api/v1/assignments', {
        headers: { Authorization: `Bearer ${token}` },
        data: {
          agent_id: agentId,
          name: 'Dry Run Assignment',
          trigger_type: 'manual',
          prompt: 'Simulated work only.',
          dry_run: true,
        },
      });
      if (!resp.ok()) {
        const body = await resp.text();
        throw new Error(`Failed to create dry-run assignment: ${resp.status()} ${resp.statusText()} - ${body}`);
      }
      assignmentId = (await resp.json()).id;
    } finally {
      await apiCtx.dispose();
    }

    await page.goto(`/agents/assignments/${assignmentId}`);

    // Wait for the detail page to render (header title).
    await expect(page.getByRole('heading', { name: 'Dry Run Assignment' })).toBeVisible({ timeout: 15_000 });

    // "Dry run" must appear twice: the always-visible header badge AND the
    // read-only Parameters card under "Safety & reliability".
    await expect(page.getByText('Dry run', { exact: true })).toHaveCount(2, { timeout: 10_000 });
    await expect(page.getByText('Safety & reliability')).toBeVisible();
  });

  test.afterAll(async () => {
    if (!agentId) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/agents/${agentId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    } finally {
      await apiCtx.dispose();
    }
  });
});
