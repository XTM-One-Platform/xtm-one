import { test, expect, type Page } from '@playwright/test';
import { AgentPage } from './model/agent.page';
import { AssignmentPage } from './model/assignment.page';
import { createAdminApiContext } from './helpers';
import { REGULAR_USER_PASSWORD, regularUserEmail } from './constants';
import { LoginPage } from './model/login.page';

// ---------------------------------------------------------------------------
// Agents — full CRUD lifecycle (no LLM calls required).
// ---------------------------------------------------------------------------

test.describe.serial('Agents — CRUD lifecycle', () => {
  const AGENT_NAME = `E2E Agent ${Date.now()}`;

  test('should display the agents list page', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await expect(page).toHaveURL(/\/agents(\?vis=[a-z]+)?$/);
    // The "New Agent" button confirms we're on the right page.
    await expect(page.getByRole('button', { name: 'New Agent' })).toBeVisible();
  });

  test('should offer the three creation modes', async ({ page }) => {
    await page.goto('/agents/new');
    await expect(page.getByRole('heading', { name: 'Create New Agent' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Wizard' })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Advanced' })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Import an agent from a file' })).toBeEnabled();
  });

  test('should walk the wizard steps to the review without creating', async ({ page }) => {
    await page.goto('/agents/new/wizard');
    // Step 0: the template gallery; start from scratch.
    await expect(page.getByRole('heading', { name: 'Create New Agent' })).toBeVisible();
    await page.getByText('From Scratch', { exact: true }).click();

    // Step 1 — identity is mandatory: Next arms only once a name is typed.
    await expect(page.getByRole('heading', { name: 'Identity' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Next' })).toBeDisabled();
    await page.getByPlaceholder('e.g. Email Assistant').fill('E2E Wizard Draft');
    await page.getByRole('button', { name: 'Next' }).click();

    // Step 2 — persona is mandatory too.
    await expect(page.getByRole('heading', { name: 'Persona' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Next' })).toBeDisabled();
    await page.getByPlaceholder('Describe who you are').fill('You are a wizard test draft.');
    await page.getByRole('button', { name: 'Next' }).click();

    // Steps 3-5 are optional from scratch.
    await expect(page.getByRole('heading', { name: 'Tools' })).toBeVisible();
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('heading', { name: 'Integrations' })).toBeVisible();
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('heading', { name: 'Knowledge bases' })).toBeVisible();
    await page.getByRole('button', { name: 'Next' }).click();

    // Review is a hub: Create first, then the advanced options the
    // walkthrough skipped. Stop before creating — nothing is persisted.
    await expect(page.getByRole('heading', { name: 'Review & create' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Create the agent' })).toBeEnabled();
    await expect(page.getByText('Advanced options')).toBeVisible();

    // An advanced option opens its own guided step; the value set there shows
    // in the hub summary once Done returns to it.
    await page.getByRole('button', { name: /Guardrails/ }).click();
    await expect(page.getByRole('heading', { name: 'Guardrails' })).toBeVisible();
    await page.getByPlaceholder('e.g. Never disclose internal hostnames.').fill('Never do X.');
    await page.getByRole('button', { name: 'Done' }).click();
    await expect(page.getByRole('button', { name: /Guardrails/ })).toContainText('Defined');

    // Hub mode unlocks free navigation, so creation must re-check the
    // mandatory gates: clearing the persona disarms Create.
    await page.getByRole('button', { name: 'Step 2 of 6: Persona' }).click();
    await page.getByPlaceholder('Describe who you are').fill('');
    await page.getByRole('button', { name: 'Done' }).click();
    await expect(page.getByRole('button', { name: 'Create the agent' })).toBeDisabled();
    await expect(page.getByText('Complete the required steps first', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: 'Step 2 of 6: Persona' }).click();
    await page.getByPlaceholder('Describe who you are').fill('You are a wizard test draft.');
    await page.getByRole('button', { name: 'Done' }).click();

    // The advanced values reach the creation POST — asserted on an intercepted
    // request so nothing is persisted by this test.
    let payload: Record<string, unknown> | null = null;
    await page.route('**/api/v1/agents', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      payload = route.request().postDataJSON();
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: '00000000-0000-4000-8000-000000000000',
          user_id: null,
          name: 'E2E Wizard Draft',
          icon: 'Bot',
          enabled: true,
          assignment_count: 0,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        }),
      });
    });
    await page.getByRole('button', { name: 'Create the agent' }).click();
    await expect.poll(() => payload?.guardrails).toBe('Never do X.');
    await expect(page).toHaveURL(/\/agents\/00000000-0000-4000-8000-000000000000/);

    // The embedded tutorial opens over the canvas (checked on a fresh page —
    // the intercepted create navigated away).
    await page.goto('/agents/new/wizard');
    await page.getByText('From Scratch', { exact: true }).click();
    await page.getByRole('button', { name: 'Tutorial' }).click();
    await expect(page.getByText('The name and icon are how the agent appears', { exact: false })).toBeVisible();
  });

  // In-app navigation used to drop the whole wizard draft - the
  // Integrations step's "Connect one" sends the user away on purpose. The
  // draft is kept in session storage and restored on return.
  test('should keep the wizard draft across in-app navigation', async ({ page }) => {
    await page.goto('/agents/new/wizard');
    await page.getByText('From Scratch', { exact: true }).click();
    await page.getByPlaceholder('e.g. Email Assistant').fill('E2E Draft Round Trip');
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('heading', { name: 'Persona' })).toBeVisible();
    await page.getByPlaceholder('Describe who you are').fill('Kept across navigation.');

    // Leave through a client-side transition, then come back.
    await page.getByRole('button', { name: 'Back to Creation Modes' }).click();
    await expect(page.getByRole('heading', { name: 'Create New Agent' })).toBeVisible();
    await page.getByRole('button', { name: 'Wizard' }).click();

    await expect(page.getByText('Your unsaved draft was restored.')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Persona' })).toBeVisible();
    await expect(page.getByPlaceholder('Describe who you are')).toHaveValue('Kept across navigation.');
    await page.getByRole('button', { name: 'Step 1 of 6: Identity' }).click();
    await expect(page.getByPlaceholder('e.g. Email Assistant')).toHaveValue('E2E Draft Round Trip');

    // Discard returns to the gallery and forgets the draft for good.
    await page.getByRole('button', { name: 'Discard draft' }).click();
    await expect(page.getByText('From Scratch', { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByText('From Scratch', { exact: true })).toBeVisible();
    await expect(page.getByText('Your unsaved draft was restored.')).toHaveCount(0);
  });

  test('should dry-run agent files on the import page without importing', async ({ page }) => {
    await page.goto('/agents/new/import');
    await expect(page.getByRole('heading', { name: 'Import an agent' })).toBeVisible();

    // A valid native bundle passes every stage and arms the confirm button.
    // The test stops before confirming, so nothing is persisted.
    await page.locator('input[type="file"]').setInputFiles({
      name: 'e2e-import.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify({ version: 3, export_type: 'agent', agent: { name: 'E2E Import Check', icon: 'Bot' } })),
    });
    await expect(page.getByText('"E2E Import Check" is ready to import.')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Import agent' })).toBeEnabled();

    // An unsupported bundle version fails the schema stage with the reason,
    // and the confirm button stays disarmed.
    await page.getByRole('button', { name: 'Choose another file' }).click();
    await page.locator('input[type="file"]').setInputFiles({
      name: 'e2e-bad-import.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify({ version: 99, export_type: 'agent', agent: { name: 'Bad' } })),
    });
    await expect(page.getByText('This file cannot be imported.')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(/unsupported export version 99/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Import agent' })).toBeDisabled();
  });

  test('should create an agent from scratch', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await agentPage.clickNewAgent();

    // Step 1 — template picker is shown.
    await expect(page.getByRole('heading', { name: 'Create New Agent' })).toBeVisible();

    // Skip templates, create from scratch.
    await agentPage.selectScratch();

    // Step 2 — config form.
    await agentPage.fillName(AGENT_NAME);
    await agentPage.fillPersona('You are a helpful test assistant.');
    await agentPage.clickCreate();

    // Should land on the agent detail page.
    await agentPage.waitForDetail();
    await expect(page.getByText(AGENT_NAME)).toBeVisible({ timeout: 10_000 });
  });

  test('should show the agent on the list after creation', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await expect(page.getByText(AGENT_NAME)).toBeVisible({ timeout: 10_000 });
  });

  test('should open agent detail from the list', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await page.getByText(AGENT_NAME).first().click();
    await agentPage.waitForDetail();
    await expect(page.getByText(AGENT_NAME)).toBeVisible();
  });

  test('should expose trace built-in tools and supervision allow-list UI', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await page.getByText(AGENT_NAME).first().click();
    await agentPage.waitForDetail();

    await page.getByRole('tab', { name: 'Configuration', exact: true }).click();
    await page.getByRole('button', { name: 'Tooling', exact: true }).click();

    const builtinToolsCard = page.getByTestId('builtin-tools-card');
    await builtinToolsCard.getByTestId('builtin-tools-edit').click();
    await expect(builtinToolsCard.getByTestId('builtin-tools-cancel')).toBeVisible();

    // One Traces group: the former trace ids are aliases, not cards.
    await expect(builtinToolsCard.getByTestId('builtin-tool-inspect_my_traces')).toHaveCount(0);
    await expect(builtinToolsCard.getByTestId('builtin-tool-inspect_agent_traces')).toHaveCount(0);
    const tracesButton = builtinToolsCard.getByTestId('builtin-tool-observability');
    await expect(tracesButton).toBeVisible();

    // A new agent's default trace built-in is stored as the group scoped to its
    // own traces: enabled, and no allow-list, since it reads no other agent.
    await expect(tracesButton).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('trace-supervision-panel')).toHaveCount(0);

    // Widened to every function, the group reads other agents' traces: the allow-list opens.
    await builtinToolsCard.getByTestId('builtin-tool-scope-observability').click();
    const scopeDialog = page.getByRole('dialog');
    // A built-in group lists functions, not tools.
    await scopeDialog.getByRole('button', { name: 'All functions', exact: true }).click();
    await scopeDialog.getByRole('button', { name: 'Apply', exact: true }).click();
    await expect(scopeDialog).toHaveCount(0);
    await expect(page.getByTestId('trace-supervision-panel')).toBeVisible();

    await builtinToolsCard.getByTestId('builtin-tools-cancel').click();
  });

  test('should let the agent drop a built-in suite this deployment has switched off', async ({ page }) => {
    // XTM One mode disables the code interpreter unless DISABLE_CODE_INTERPRETER
    // is set to false, so `code_interpreter` is the switched-off suite here.
    const { apiCtx, token } = await createAdminApiContext();
    const headers = { Authorization: `Bearer ${token}` };
    const carriedIds = async (agentId: string): Promise<string[]> => {
      const resp = await apiCtx.get(`/api/v1/agents/${agentId}`, { headers });
      expect(resp.ok()).toBeTruthy();
      return (await resp.json()).builtin_tool_ids ?? [];
    };
    try {
      const config = await (await apiCtx.get('/api/v1/platform/config')).json();
      test.skip(config.disable_code_interpreter !== true, 'The code interpreter is enabled on this deployment');

      const agents: { id: string; name: string }[] = await (await apiCtx.get('/api/v1/agents', { headers })).json();
      const agentId = agents.find((a) => a.name === AGENT_NAME)?.id ?? '';
      expect(agentId).not.toBe('');
      // Carried from before the switch went off, or set through the API.
      const withSuite = [...new Set([...(await carriedIds(agentId)), 'code_interpreter'])];
      expect((await apiCtx.put(`/api/v1/agents/${agentId}`, { headers, data: { builtin_tool_ids: withSuite } })).ok()).toBeTruthy();

      const agentPage = new AgentPage(page);
      await page.goto(`/agents/${agentId}`);
      await agentPage.waitForDetail();
      await page.getByRole('tab', { name: 'Configuration', exact: true }).click();
      await page.getByRole('button', { name: 'Tooling', exact: true }).click();

      const builtinToolsCard = page.getByTestId('builtin-tools-card');
      await expect(builtinToolsCard.getByTestId('builtin-tool-badge-code_interpreter')).toContainText('Unavailable');

      await builtinToolsCard.getByTestId('builtin-tools-edit').click();
      const suite = builtinToolsCard.getByTestId('builtin-tool-code_interpreter');
      await expect(suite).toContainText('Unavailable');
      await expect(suite).toHaveAttribute('aria-pressed', 'true');
      await suite.hover();
      await expect(
        page.getByRole('tooltip', { name: 'Unavailable on this deployment: it stays inactive until an administrator enables it.' }),
      ).toBeVisible();

      // Removed, it stays listed until the save so the removal can be undone.
      await suite.click();
      await expect(suite).toHaveAttribute('aria-pressed', 'false');
      await builtinToolsCard.getByTestId('builtin-tools-save').click();
      await expect(builtinToolsCard.getByTestId('builtin-tools-edit')).toBeVisible();

      await expect.poll(() => carriedIds(agentId)).not.toContain('code_interpreter');
      // Only the suite that was clicked is gone.
      expect((await carriedIds(agentId)).sort()).toEqual(withSuite.filter((id) => id !== 'code_interpreter').sort());
      await expect(builtinToolsCard.getByTestId('builtin-tool-badge-code_interpreter')).toHaveCount(0);

      // No longer carried, so the grid no longer offers it at all.
      await builtinToolsCard.getByTestId('builtin-tools-edit').click();
      // `web_fetch` is one card of the `web` group.
      await expect(builtinToolsCard.getByTestId('builtin-tool-web')).toBeVisible();
      await expect(builtinToolsCard.getByTestId('builtin-tool-code_interpreter')).toHaveCount(0);
      await builtinToolsCard.getByTestId('builtin-tools-cancel').click();
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should add an assignment to the agent', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await page.getByText(AGENT_NAME).first().click();
    await agentPage.waitForDetail();

    const assignmentPage = new AssignmentPage(page);
    await assignmentPage.clickAddAssignment();
    await assignmentPage.fillName('E2E Test Assignment');
    await assignmentPage.fillPrompt('Run a test action.');
    await assignmentPage.clickCreate();

    // AddAssignmentPanel closes after creation — the assignment appears in the list.
    await expect(page.getByText('E2E Test Assignment')).toBeVisible({ timeout: 15_000 });
  });

  test('should favorite the agent and pin it to the top of the grid', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();

    const star = agentPage.favoriteStar(AGENT_NAME);
    await expect(star).toHaveAttribute('aria-label', 'Add to favorites');
    await agentPage.toggleFavoriteAndWait(star);
    // Starring must not navigate: the star is a sibling of the card's link, and
    // the card's own click handler must not fire for it either.
    await expect(page).toHaveURL(/\/agents(\?.*)?$/);
    await expect(star).toHaveAttribute('aria-label', 'Remove from favorites');

    // Favorites lead the grid, so the first star on the page is a pressed one.
    await expect(agentPage.allFavoriteStars().first()).toHaveAttribute('aria-pressed', 'true');

    // Stored on the account, not in the tab: it survives a full reload.
    await page.reload();
    await expect(agentPage.favoriteStar(AGENT_NAME)).toHaveAttribute('aria-label', 'Remove from favorites');
    await expect(agentPage.allFavoriteStars().first()).toHaveAttribute('aria-pressed', 'true');

    // Un-star so the rest of the suite (and reruns) start from a clean list.
    await agentPage.toggleFavoriteAndWait(agentPage.favoriteStar(AGENT_NAME));
    await expect(agentPage.favoriteStar(AGENT_NAME)).toHaveAttribute('aria-label', 'Add to favorites');
  });

  test('should keep a favorite pinned in list view under both sort directions', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await agentPage.setViewMode('list');

    const star = agentPage.favoriteStarInRow(AGENT_NAME);
    await agentPage.toggleFavoriteAndWait(star);
    // The star sits in a leading `interactive` column, which is why the row's
    // anchor moved off column 0 — clicking it must not open the agent.
    await expect(page).toHaveURL(/\/agents(\?.*)?$/);
    await expect(star).toHaveAttribute('aria-label', 'Remove from favorites');

    const firstRow = page.locator('tbody tr').first();
    await expect(firstRow).toContainText(AGENT_NAME);

    // The pin is applied AFTER sorting, so it survives a descending sort — the
    // case a `sortValue`-based pin would get wrong.
    await agentPage.sortByColumn('Name');
    await expect(firstRow).toContainText(AGENT_NAME);
    await agentPage.sortByColumn('Name');
    await expect(firstRow).toContainText(AGENT_NAME);

    // Sorting a different column must not dislodge it either.
    await agentPage.sortByColumn('Status');
    await expect(firstRow).toContainText(AGENT_NAME);

    // Leave the account and the view preference as we found them: the view mode
    // is persisted per page, so a stray 'list' would follow later tests/reruns.
    await agentPage.toggleFavoriteAndWait(agentPage.favoriteStarInRow(AGENT_NAME));
    await agentPage.setViewMode('grid');
    await expect(agentPage.favoriteStar(AGENT_NAME)).toHaveAttribute('aria-label', 'Add to favorites');
  });

  test('should delete the agent', async ({ page }) => {
    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await page.getByText(AGENT_NAME).first().click();
    await agentPage.waitForDetail();
    await agentPage.deleteCurrentAgent();

    await expect(page).toHaveURL(/\/agents(\?vis=[a-z]+)?$/);
    await expect(page.getByText(AGENT_NAME)).not.toBeVisible({ timeout: 10_000 });
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const resp = await apiCtx.get('/api/v1/agents', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (resp.ok()) {
        const agents = await resp.json();
        const target = agents.find((a: { name: string; id: string }) => a.name === AGENT_NAME);
        if (target) {
          await apiCtx.delete(`/api/v1/agents/${target.id}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
        }
      }
    } finally {
      await apiCtx.dispose();
    }
  });
});

test.describe.serial('Agents — Observability access (non-admin owner)', () => {
  const OWNER_AGENT_NAME = `E2E Owner Obs Agent ${Date.now()}`;
  // A trace id is a UUID: TraceDetail renders Page not found for any other segment (#822).
  const TRACE_ID = '5d1c0a7e-0000-4000-8000-0000000e2e02';
  let ownerUserId = '';
  let ownerAgentId = '';
  let ownerAgentUrl = '';
  let regularEmail = '';

  test.beforeAll(async () => {
    regularEmail = regularUserEmail('agents-observability', test.info().parallelIndex);
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const headers = { Authorization: `Bearer ${token}` };

      // Ensure deterministic setup across reruns.
      const listResp = await apiCtx.get('/api/v1/users?limit=500', { headers });
      if (listResp.ok()) {
        const users: { id: string; email: string }[] = await listResp.json();
        const leftover = users.find((u) => u.email === regularEmail);
        if (leftover) {
          await apiCtx.delete(`/api/v1/users/${leftover.id}`, { headers });
        }
      }

      const createResp = await apiCtx.post('/api/v1/users', {
        headers,
        data: {
          email: regularEmail,
          password: REGULAR_USER_PASSWORD,
          full_name: 'E2E Observability Owner',
          role: 'user',
          account_type: 'local',
        },
      });
      if (!createResp.ok()) {
        throw new Error(
          `Failed to create regular user for observability e2e: ${createResp.status()} ${await createResp.text()}`,
        );
      }
      const user = await createResp.json();
      ownerUserId = user.id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const headers = { Authorization: `Bearer ${token}` };
      if (ownerAgentId) {
        await apiCtx.delete(`/api/v1/agents/${ownerAgentId}`, { headers });
      }
      if (ownerUserId) {
        await apiCtx.delete(`/api/v1/users/${ownerUserId}`, { headers });
      }
    } finally {
      await apiCtx.dispose();
    }
  });

  test('non-admin owner should see the Observability tab on agent detail', async ({ page }) => {
    test.skip(!ownerUserId, 'Regular owner user setup failed in beforeAll');

    await page.context().clearCookies();
    await page.goto('/login');
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
    // Via LoginPage so this shares the one implementation of "get signed in",
    // including clearing the Terms of Use gate — a freshly created
    // regular user has never accepted, so they always meet it.
    await new LoginPage(page).login(regularEmail, REGULAR_USER_PASSWORD);

    const agentPage = new AgentPage(page);
    await agentPage.gotoList();
    await agentPage.clickNewAgent();
    await agentPage.selectScratch();
    await agentPage.fillName(OWNER_AGENT_NAME);
    await agentPage.fillPersona('Owner observability test persona.');
    await agentPage.clickCreate();
    await agentPage.waitForDetail();

    ownerAgentUrl = page.url();
    ownerAgentId = ownerAgentUrl.split('/').pop() ?? '';

    await expect(page.getByRole('tab', { name: 'Observability', exact: true })).toBeVisible();
  });

  test('non-admin owner should drill down to trace detail from Observability tab', async ({ page }) => {
    test.skip(!ownerAgentUrl || !ownerAgentId, 'Owner agent setup failed in previous test');

    await page.context().clearCookies();
    await page.goto('/login');
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
    // Via LoginPage so this shares the one implementation of "get signed in",
    // including clearing the Terms of Use gate — a freshly created
    // regular user has never accepted, so they always meet it.
    await new LoginPage(page).login(regularEmail, REGULAR_USER_PASSWORD);

    const summaryPayload = {
      total_cost: 0.03,
      total_tokens: 150,
      trace_count: 1,
      avg_duration_ms: 1200,
      error_rate: 0,
      avg_feedback_score: null,
      quota_consumed: 0,
      message_count: 0,
      avg_quota_per_message: null,
      previous: {
        total_cost: 0,
        total_tokens: 0,
        trace_count: 0,
        avg_duration_ms: 0,
        error_rate: 0,
        avg_feedback_score: null,
        quota_consumed: 0,
        message_count: 0,
        avg_quota_per_message: null,
      },
      last_aggregation_at: null,
      aggregation_stale: false,
      unknown_cost_trace_count: 0,
    };

    const tracesPayload = {
      items: [
        {
          id: TRACE_ID,
          agent_id: ownerAgentId,
          user_id: ownerUserId,
          conversation_id: null,
          assignment_run_id: null,
          parent_trace_id: null,
          source: 'agent-chat',
          name: 'Owner Observability Trace',
          model: 'gpt-5.3-codex',
          environment: 'test',
          metadata: {},
          input: {},
          output: {},
          tags: [],
          started_at: '2026-07-01T10:00:00.000Z',
          ended_at: '2026-07-01T10:00:01.200Z',
          duration_ms: 1200,
          input_tokens: 100,
          output_tokens: 50,
          total_tokens: 150,
          input_cost: 0.01,
          output_cost: 0.02,
          total_cost: 0.03,
          status: 'ok',
          error_message: null,
          level: 'DEFAULT',
          created_at: '2026-07-01T10:00:00.000Z',
          updated_at: '2026-07-01T10:00:01.200Z',
          agent_name: OWNER_AGENT_NAME,
          agent_icon: null,
          user_email: regularEmail,
          has_scores: false,
        },
      ],
      total: 1,
      limit: 10,
    };

    const traceDetailPayload = {
      ...tracesPayload.items[0],
    };

    const traceSpansPayload = {
      items: [
        {
          id: 'owner-span-root',
          trace_id: TRACE_ID,
          parent_span_id: null,
          type: 'GENERATION',
          name: 'Owner root span',
          model: 'gpt-5.3-codex',
          model_parameters: {},
          input: { prompt: 'hello' },
          output: { text: 'world' },
          metadata: {},
          level: 'DEFAULT',
          status_message: null,
          started_at: '2026-07-01T10:00:00.000Z',
          ended_at: '2026-07-01T10:00:01.200Z',
          duration_ms: 1200,
          time_to_first_token: 100,
          input_tokens: 100,
          output_tokens: 50,
          input_cost: 0.01,
          output_cost: 0.02,
          total_cost: 0.03,
          usage_details: null,
          tool_call_id: null,
          tool_arguments: null,
          stop_reason: 'stop',
          position: 0,
          created_at: '2026-07-01T10:00:00.000Z',
        },
      ],
      total: 1,
    };

    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      const { pathname } = url;

      if (pathname === '/api/v1/traces/analytics/summary') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(summaryPayload) });
        return;
      }
      if (pathname === '/api/v1/traces/analytics/cost-over-time') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ series: [] }) });
        return;
      }
      if (pathname === '/api/v1/traces/analytics/cost-by-model') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) });
        return;
      }
      if (pathname === '/api/v1/traces') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(tracesPayload) });
        return;
      }
      if (pathname === `/api/v1/agents/${ownerAgentId}/feedback-comments`) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [], total: 0, limit: 10, offset: 0 }),
        });
        return;
      }
      if (pathname === `/api/v1/traces/${TRACE_ID}`) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(traceDetailPayload) });
        return;
      }
      if (pathname === `/api/v1/traces/${TRACE_ID}/spans`) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(traceSpansPayload) });
        return;
      }

      await route.continue();
    });

    await page.goto(ownerAgentUrl);
    await page.getByRole('tab', { name: 'Observability', exact: true }).click();

    // The trace name is plain text; the whole row opens the trace (#12070).
    const traceRow = page.locator('tr', { hasText: 'Owner Observability Trace' });
    await expect(traceRow).toBeVisible();
    await expect(traceRow.getByRole('link', { name: 'Owner Observability Trace' })).toHaveCount(0);
    await traceRow.getByText('Owner Observability Trace', { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/observability/traces/${TRACE_ID}`));
    await expect(page.getByRole('heading', { name: 'Owner Observability Trace', level: 1 })).toBeVisible();
  });

  test('non-admin owner should see every rating of the agent work on the Feedback tab', async ({ page }) => {
    test.skip(!ownerAgentUrl || !ownerAgentId, 'Owner agent setup failed in previous test');

    await page.context().clearCookies();
    await page.goto('/login');
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
    await new LoginPage(page).login(regularEmail, REGULAR_USER_PASSWORD);

    const RUN_ID = '00000000-0000-4000-8000-000000001040';
    const summaryPayload = {
      agent_id: ownerAgentId,
      positive: 3,
      negative: 1,
      total: 4,
      positive_ratio: 75,
      by_target_type: [
        { target_type: 'message', positive: 2, negative: 0, total: 2, positive_ratio: 100 },
        { target_type: 'assignment_run', positive: 1, negative: 1, total: 2, positive_ratio: 50 },
        { target_type: 'sub_agent_task', positive: 0, negative: 0, total: 0, positive_ratio: null },
        { target_type: 'agent_delegation', positive: 0, negative: 0, total: 0, positive_ratio: null },
      ],
      // One criterion scored twice, which decides the weighted score.
      criteria: [
        {
          criterion_id: 'e2e-criterion-accuracy',
          key: 'accuracy',
          name: 'E2E Accuracy',
          type: 'scale',
          weight: 1,
          enabled: true,
          count: 2,
          mean_score: 0.625,
          histogram: [
            { key: '1', label: '1', count: 0 },
            { key: '2', label: '2', count: 1 },
            { key: '3', label: '3', count: 0 },
            { key: '4', label: '4', count: 0 },
            { key: '5', label: '5', count: 1 },
          ],
          daily: [{ date: '2026-07-01', count: 2, mean_score: 0.625 }],
        },
      ],
      weighted_score: 0.625,
    };
    const listPayload = {
      items: [
        {
          id: 'e2e-annotation-1040',
          target_type: 'assignment_run',
          target_id: RUN_ID,
          conversation_id: null,
          // The API links only the work the viewer may open.
          can_open: true,
          rating: 'negative',
          comment: 'Wrong totals in the digest',
          user_name: 'E2E Observability Owner',
          user_email: regularEmail,
          weighted_score: 0.25,
          created_at: '2026-07-01T10:00:00.000Z',
          updated_at: '2026-07-01T10:00:00.000Z',
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    };

    await page.route('**/api/v1/annotations**', async (route) => {
      const { pathname } = new URL(route.request().url());
      if (pathname === '/api/v1/annotations/summary') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(summaryPayload) });
        return;
      }
      if (pathname === '/api/v1/annotations') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(listPayload) });
        return;
      }
      await route.continue();
    });

    await page.goto(ownerAgentUrl);
    await page.getByRole('tab', { name: 'Feedback', exact: true }).click();

    await expect(page.getByText('Wrong totals in the digest')).toBeVisible();
    await expect(page.getByText('75.0%')).toBeVisible();
    await expect(page.getByRole('cell', { name: '50.0%' })).toBeVisible();
    // The criteria decide the weighted score, per criterion and overall.
    await expect(page.getByText('From the criteria scores')).toBeVisible();
    await expect(page.getByText('E2E Accuracy')).toBeVisible();
    await expect(page.getByRole('cell', { name: '63%' })).toBeVisible();
    await expect(page.getByRole('cell', { name: '25%' })).toBeVisible();
    // The rated run opens its own page from the row (#12070): the row's own link sits on the
    // first column (DataTable rowHref), the comment is plain text with no link of its own.
    const ratingRow = page.locator('tr', { hasText: 'Wrong totals in the digest' });
    await expect(ratingRow).toBeVisible();
    await expect(ratingRow.getByRole('link')).toHaveCount(1);
    await expect(ratingRow.getByRole('link')).toHaveAttribute('href', `/operations/executions/${RUN_ID}`);
    await expect(ratingRow.getByRole('link', { name: 'Wrong totals in the digest' })).toHaveCount(0);
    await ratingRow.getByText('Wrong totals in the digest', { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/operations/executions/${RUN_ID}`));
  });

  for (const decision of [
    { button: 'Approve', action: 'approve', status: 'watching', badge: 'Watching' },
    { button: 'Reject', action: 'reject', status: 'rejected', badge: 'Rejected' },
  ] as const) {
    test(`the owner decides a persona rewrite in one click from the Versioning tab: ${decision.button}`, async ({ page }) => {
      test.skip(!ownerAgentUrl || !ownerAgentId, 'Owner agent setup failed in previous test');

      await page.context().clearCookies();
      await page.goto('/login');
      await page.evaluate(() => window.localStorage.clear());
      await page.reload();
      await new LoginPage(page).login(regularEmail, REGULAR_USER_PASSWORD);

      const IMPROVEMENT_ID = '00000000-0000-4000-8000-000000001044';
      let decided = false;
      const item = (status: string) => ({
        id: IMPROVEMENT_ID,
        proposal_type: 'persona',
        target_type: 'agent',
        target_id: ownerAgentId,
        target_name: 'E2E agent',
        agent_id: ownerAgentId,
        status,
        experiment_verdict: null,
        experiment: {},
        evidence_refs: [{ kind: 'evaluation_run', id: '00000000-0000-4000-8000-00000000b044' }],
        experiment_refs: [],
        evaluation_id: '00000000-0000-4000-8000-00000000e044',
        evaluation_name: 'E2E invoices',
        baseline_run_id: '00000000-0000-4000-8000-00000000b044',
        candidate_run_id: null,
        summary: 'Quote the invoice number in every answer.',
        base_content: 'You answer questions about invoices.',
        proposed_content: 'You answer questions about invoices.\nAlways quote the invoice number.',
        diff: [
          { type: 'context', content: 'You answer questions about invoices.', old_line: 1, new_line: 1 },
          { type: 'add', content: 'Always quote the invoice number.', old_line: null, new_line: 2 },
        ],
        added: 1,
        removed: 0,
        base_version_number: 1,
        applied_version_number: status === 'watching' ? 2 : null,
        rollback_version_number: null,
        quality_metric: 'score',
        baseline_score: 70,
        candidate_score: null,
        delta: null,
        verdict: null,
        noise_margin_points: 2,
        outcome_note: null,
        author_agent_id: null,
        author_agent_name: null,
        requested_by: 'E2E Admin',
        decided_by: status === 'pending_approval' ? null : 'E2E Observability Owner',
        can_decide: status === 'pending_approval',
        created_at: '2026-09-27T10:00:00.000Z',
        decided_at: null,
        applied_at: null,
        watch_until: null,
        settled_at: null,
      });

      await page.route('**/api/v1/improvement-proposals?**', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [item(decided ? decision.status : 'pending_approval')], can_propose: false }),
        });
      });
      await page.route(`**/api/v1/improvement-proposals/${IMPROVEMENT_ID}/${decision.action}`, async (route) => {
        decided = true;
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(item(decision.status)) });
      });
      // The card shows only with evaluation on, and this stack runs XTM One
      // mode: the page is told it is on, the rewrite routes above being mocked.
      await page.route('**/api/v1/auth/config', async (route) => {
        const response = await route.fetch();
        const config = await response.json();
        await route.fulfill({ response, json: { ...config, enabled_features: [...config.enabled_features, 'EVALUATION'] } });
      });

      await page.goto(ownerAgentUrl);
      await page.getByRole('tab', { name: 'Versioning', exact: true }).click();

      const card = page.getByTestId('persona-improvements-card');
      await expect(card.getByText('Quote the invoice number in every answer.')).toBeVisible();
      await expect(card.getByText('Always quote the invoice number.')).toBeVisible();
      // An owner who is not an admin cannot ask for one, only decide.
      await expect(card.getByRole('button', { name: 'Ask for a rewrite' })).toHaveCount(0);

      // The button is the decision: no second dialog asks for it again.
      await card.getByRole('button', { name: decision.button, exact: true }).click();

      await expect(card.getByText(decision.badge, { exact: true })).toBeVisible();
      expect(decided).toBe(true);
      await expect(page.getByRole('dialog')).toHaveCount(0);
    });
  }

  test('the rating dialog scores every kind of feedback criterion', async ({ page }) => {
    test.skip(!ownerUserId, 'Regular owner user setup failed in beforeAll');

    await page.context().clearCookies();
    await page.goto('/login');
    await page.evaluate(() => window.localStorage.clear());
    await page.reload();
    await new LoginPage(page).login(regularEmail, REGULAR_USER_PASSWORD);

    const TASK_ID = '00000000-0000-4000-8000-000000001041';
    const criteria = [
      { id: 'e2e-scale', key: 'accuracy', name: 'Accuracy', type: 'scale', min_value: 1, max_value: 5, options: null },
      {
        id: 'e2e-categorical',
        key: 'tone',
        name: 'Tone',
        type: 'categorical',
        min_value: null,
        max_value: null,
        options: [
          { value: 'cold', label: 'Cold', score: 0 },
          { value: 'warm', label: 'Warm', score: 1 },
        ],
      },
      { id: 'e2e-numeric', key: 'latency', name: 'Latency', type: 'numeric', min_value: 0, max_value: 10, options: null },
    ].map((criterion, index) => ({
      ...criterion,
      description: null,
      weight: 1,
      sort_order: index,
      enabled: true,
      created_at: '2026-09-27T10:00:00.000Z',
      updated_at: '2026-09-27T10:00:00.000Z',
    }));
    const task = {
      task_id: TASK_ID,
      user_id: ownerUserId,
      parent_agent_id: ownerAgentId || TASK_ID,
      parent_agent_name: 'E2E Parent',
      target_agent_id: ownerAgentId || TASK_ID,
      target_agent_name: 'E2E Worker',
      task_description: 'Summarise the week',
      status: 'completed',
      result: 'Done.',
      error: null,
      conversation_id: null,
      assignment_run_id: null,
      parent_run_id: null,
      created_at: '2026-09-27T10:00:00.000Z',
      started_at: '2026-09-27T10:00:00.000Z',
      completed_at: '2026-09-27T10:01:00.000Z',
    };
    const posted: Record<string, unknown>[] = [];
    const pageReads: string[] = [];
    let rowReads = 0;

    // The page reads the caller's ratings of all its rows in one request.
    await page.route('**/api/v1/annotations/mine**', async (route) => {
      pageReads.push(route.request().url());
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/v1/feedback-criteria**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(criteria) });
    });
    await page.route('**/api/v1/chat/sub-agent-tasks**', async (route) => {
      const { pathname } = new URL(route.request().url());
      const body = pathname.endsWith('/count') ? { count: 1 } : [task];
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.route(`**/api/v1/annotations/sub_agent_task/${TASK_ID}`, async (route) => {
      const request = route.request();
      if (request.method() === 'GET') {
        rowReads += 1;
        await route.fulfill({ status: 200, contentType: 'application/json', body: 'null' });
        return;
      }
      const body = request.postDataJSON() as Record<string, unknown>;
      posted.push(body);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'e2e-annotation-1041',
          target_type: 'sub_agent_task',
          target_id: TASK_ID,
          rating: body.rating,
          comment: body.comment ?? null,
          scores: body.scores ?? [],
          weighted_score: 1,
          created_at: '2026-09-27T10:02:00.000Z',
          updated_at: '2026-09-27T10:02:00.000Z',
        }),
      });
    });

    // The thumb decides whether to ask for scores from the criteria it holds:
    // they must be loaded before it is clicked.
    const criteriaLoaded = page.waitForResponse((response) => response.url().includes('/api/v1/feedback-criteria'));
    await page.goto('/operations/background-tasks');
    await criteriaLoaded;
    await expect(page.getByText('Summarise the week')).toBeVisible();
    await page.getByRole('button', { name: 'Thumbs up' }).click();

    // With criteria defined, a thumbs up asks for their scores too.
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('Rate this result')).toBeVisible();
    const step = dialog.getByRole('group', { name: 'Accuracy' }).getByRole('button', { name: '4', exact: true });
    await step.click();
    await expect(step).toHaveAttribute('aria-pressed', 'true');
    const option = dialog.getByRole('group', { name: 'Tone' }).getByRole('button', { name: 'Warm' });
    await option.click();
    await expect(option).toHaveAttribute('aria-pressed', 'true');
    const latency = dialog.getByRole('spinbutton', { name: 'Latency' });
    await latency.fill('12');
    // Out of bounds: said in place, and not sent.
    await expect(dialog.getByRole('button', { name: 'Save' })).toBeDisabled();
    await latency.fill('2.5');
    await dialog.getByRole('button', { name: 'Save' }).click();
    await expect(dialog).toBeHidden();

    // One read for the page, none per row.
    expect(pageReads.length).toBeGreaterThan(0);
    expect(new URL(pageReads[0]).searchParams.getAll('target_id')).toEqual([TASK_ID]);
    expect(rowReads).toBe(0);
    // The thumb alone first, then the scores with it - in the criteria's order.
    expect(posted).toEqual([
      { rating: 'positive', comment: null },
      {
        rating: 'positive',
        comment: null,
        scores: [
          { criterion_id: 'e2e-scale', value: 4 },
          { criterion_id: 'e2e-categorical', value: 'warm' },
          { criterion_id: 'e2e-numeric', value: 2.5 },
        ],
      },
    ]);
  });
});

// ---------------------------------------------------------------------------
// Agent wizard - a knowledge base created from its dialog. Nothing is
// persisted: the knowledge-base and agent creations are answered on
// intercepted requests.
// ---------------------------------------------------------------------------

test.describe('Agent wizard - knowledge bases created inline', () => {
  const isCatalog = (url: string) => new URL(url).pathname.endsWith('/api/v1/flows/node-catalog');

  /** From scratch to the Knowledge bases step, by default once the flow node
   *  catalog - which names every selection - has loaded. */
  async function openKnowledgeStep(page: Page, { waitForCatalog = true } = {}) {
    const catalogLoaded = waitForCatalog ? page.waitForResponse((r) => isCatalog(r.url()) && r.ok()) : null;
    await page.goto('/agents/new/wizard');
    await catalogLoaded;
    await page.getByText('From Scratch', { exact: true }).first().click();
    await page.getByPlaceholder('e.g. Email Assistant').fill('E2E Wizard Knowledge');
    await page.getByRole('button', { name: 'Next' }).click();
    await page.getByPlaceholder('Describe who you are').fill('You are a wizard test draft.');
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('heading', { name: 'Tools' })).toBeVisible();
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('heading', { name: 'Integrations' })).toBeVisible();
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('heading', { name: 'Knowledge bases' })).toBeVisible();
  }

  /** Creates `kb` from the step's dialog, the create call answered here. */
  async function createKnowledgeBaseInline(page: Page, kb: { id: string; name: string }) {
    await page.route('**/api/v1/knowledge-bases', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          ...kb,
          user_id: null,
          description: null,
          tags: [],
          is_company_managed: false,
          document_count: 0,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        }),
      });
    });
    await page.getByRole('button', { name: 'Create a new knowledge base' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByRole('textbox').first().fill(kb.name);
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await expect(dialog).toBeHidden();
  }

  test('should list and name the new knowledge base at once, and keep the wizard when the catalog refetch fails', async ({ page }) => {
    const kb = { id: '00000000-0000-4000-8000-000000003194', name: `E2E Inline KB ${Date.now()}` };
    await openKnowledgeStep(page);

    // Every catalog refetch fails from here on: the one the creation
    // triggers, and its retry.
    let catalogFailures = 0;
    page.on('response', (r) => {
      if (isCatalog(r.url()) && r.status() === 500) catalogFailures += 1;
    });
    await page.route('**/api/v1/flows/node-catalog', (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'E2E: catalog down' }) }),
    );

    await createKnowledgeBaseInline(page, kb);

    // Listed by the step's own picker and named on the canvas at once; a name
    // cut at its width reads in full from the keyboard too, in the shared
    // picker row as on the canvas.
    const pickerRow = page.getByRole('button', { name: kb.name, exact: true });
    const canvasLine = page.getByText(`• ${kb.name}`, { exact: true });
    await expect(pickerRow).toBeVisible();
    await expect(canvasLine).toBeVisible();
    await pickerRow.focus();
    await expect(pickerRow).toHaveAttribute('data-state', /open/);
    await canvasLine.focus();
    await expect(canvasLine).toHaveAttribute('data-state', /open/);
    await expect(page.getByRole('tooltip', { name: kb.name }).first()).toBeAttached();

    // Both refetches failed: the wizard - draft and names - is still there,
    // not the page's error boundary, and it says its lists may be out of
    // date. Retry asks the catalog again: still failing here, so it stays.
    await expect.poll(() => catalogFailures).toBeGreaterThanOrEqual(2);
    const stale = page.getByRole('status').filter({ hasText: 'could not be refreshed' });
    await expect(stale).toBeVisible();
    const failedBeforeRetry = catalogFailures;
    await stale.getByRole('button', { name: 'Retry' }).click();
    await expect.poll(() => catalogFailures).toBeGreaterThan(failedBeforeRetry);
    await expect(stale).toBeVisible();
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByRole('heading', { name: 'Review & create' })).toBeVisible();
    await expect(canvasLine).toBeVisible();
    await expect(page.getByText('Failed to load this section')).toHaveCount(0);

    // The creation saves the knowledge base's id, never its name.
    let payload: Record<string, unknown> | null = null;
    await page.route('**/api/v1/agents', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      payload = route.request().postDataJSON();
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: '00000000-0000-4000-8000-000000000000',
          user_id: null,
          name: 'E2E Wizard Knowledge',
          icon: 'Bot',
          enabled: true,
          assignment_count: 0,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        }),
      });
    });
    await page.getByRole('button', { name: 'Create the agent' }).click();
    await expect.poll(() => payload?.knowledge_base_ids).toEqual([kb.id]);
  });

  test('should offer a selection the catalog no longer lists for removal, never as a raw id', async ({ page }) => {
    // The knowledge base is only created on the intercepted request, so the
    // real catalog the creation refetches does not list it: what the wizard
    // sees of a base deleted, or unshared, while it is open.
    const kb = { id: '00000000-0000-4000-8000-000000003195', name: `E2E Vanished KB ${Date.now()}` };
    await openKnowledgeStep(page);
    await createKnowledgeBaseInline(page, kb);

    const unavailable = page.getByRole('button', { name: /^Unavailable/ });
    await expect(unavailable).toBeVisible();
    await expect(unavailable).toContainText('No longer available to you');
    await expect(page.getByText('• Unavailable', { exact: true })).toBeVisible();
    await expect(page.getByText(kb.id)).toHaveCount(0);

    // A click removes it from the selection, and from the canvas.
    await unavailable.click();
    await expect(unavailable).toHaveCount(0);
    await expect(page.getByText('• Unavailable', { exact: true })).toHaveCount(0);
  });

  test('should not create a knowledge base before the catalog has loaded', async ({ page }) => {
    // The catalog's first answer is held until the test lets it through: a
    // base created meanwhile could not be written into a catalog not yet there.
    let releaseCatalog = () => {};
    const catalogHeld = new Promise<void>((resolve) => {
      releaseCatalog = resolve;
    });
    await page.route('**/api/v1/flows/node-catalog', async (route) => {
      await catalogHeld;
      await route.continue();
    });
    await openKnowledgeStep(page, { waitForCatalog: false });

    // Nothing is listed yet: the picker says so, rather than that nothing is
    // available.
    const knowledgeStep = page.getByText('Knowledge bases to search', { exact: true }).locator('..');
    const loading = knowledgeStep.getByText('Loading...', { exact: true });
    await expect(loading).toBeVisible();
    await expect(knowledgeStep.getByText('Nothing available to select.')).toHaveCount(0);

    const create = page.getByRole('button', { name: 'Create a new knowledge base' });
    await expect(create).toBeDisabled();
    const reason = page.locator('span[aria-disabled="true"]').filter({ has: create });
    await reason.focus();
    await expect(page.getByRole('tooltip', { name: 'Loading knowledge bases...' }).first()).toBeAttached();

    releaseCatalog();
    await expect(create).toBeEnabled();
    await expect(loading).toHaveCount(0);
  });
});
