import { test, expect, request, type Page, type Route } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { BASE_URL, REGULAR_USER_PASSWORD, regularUserEmail } from './constants';
import { LoginPage } from './model/login.page';

// ---------------------------------------------------------------------------
// Settings > Security > Security audit.
//
// The live report is read once, to pin that the page shows what
// GET /api/v1/security-audit answers on a real stack and that Re-run asks the
// server again. The rest runs on a fixture report, so the spec pins the
// screen's contract - score, counts, pass rate, filters, grouping, and a
// category and a severity the screen has never heard of - rather than
// whatever this stack's configuration happens to report.
// ---------------------------------------------------------------------------

const AUDIT_PATH = '/api/v1/security-audit';

interface Finding {
  id?: string | null;
  category: string;
  severity: string;
  title: string;
  description: string;
  recommendation?: string | null;
  control?: string | null;
}

interface Control {
  id: string;
  category: string;
  title: string;
  setting: string;
  stored_in: string;
  where: string;
  value: string;
  default: string;
  hardened: string;
  state: string;
  opt_in: boolean;
  impact: string;
  evidence: string;
}

const FINDINGS: Finding[] = [
  {
    id: 'authentication.registration_token_default',
    category: 'authentication',
    severity: 'critical',
    title: 'Platform registration token is the published default',
    description: 'PLATFORM_REGISTRATION_TOKEN is set to the default value shipped in docker-compose.yml.',
    recommendation: 'Generate a random token and set it as PLATFORM_REGISTRATION_TOKEN on every platform.',
  },
  {
    id: 'authentication.admin_email_configured',
    category: 'authentication',
    severity: 'pass',
    title: 'Admin email configured',
    description: 'Platform admin is set to admin@example.com.',
  },
  {
    id: 'authentication.sso_not_configured',
    category: 'authentication',
    severity: 'info',
    title: 'SSO not configured',
    description: 'No SSO provider (OIDC or SAML) is enabled.',
    recommendation: 'Consider enabling SSO for stronger authentication.',
  },
  {
    id: 'infrastructure.debug_mode',
    category: 'infrastructure',
    severity: 'warning',
    title: 'Debug mode is enabled',
    description: 'The application is running in debug mode.',
    recommendation: 'Set DEBUG=false in production environments.',
    control: 'infrastructure.debug_mode',
  },
  {
    id: 'infrastructure.redis_healthy',
    category: 'infrastructure',
    severity: 'pass',
    title: 'Redis connection healthy',
    description: 'Redis is reachable and responding.',
  },
  {
    id: 'encryption.secret_key_configured',
    category: 'encryption',
    severity: 'pass',
    title: 'Secret key configured',
    description: 'SECRET_KEY is set and meets minimum length requirements.',
  },
  // A category and a severity no build of the screen knows yet.
  {
    id: 'compliance.global_key_reach',
    category: 'compliance',
    severity: 'notice',
    title: 'The global MCP key reaches 2 agent(s)',
    description: 'Two agents are exposed on the global MCP endpoint.',
  },
];

const control = (overrides: Partial<Control> & Pick<Control, 'id' | 'category' | 'title'>): Control => ({
  setting: 'SETTING',
  stored_in: 'config',
  where: '',
  value: 'false',
  default: 'false',
  hardened: 'true',
  state: 'off',
  opt_in: true,
  impact: 'Something changes for users.',
  evidence: '',
  ...overrides,
});

const CONTROLS: Control[] = [
  control({
    id: 'infrastructure.debug_mode',
    category: 'infrastructure',
    title: 'Debug mode off',
    setting: 'DEBUG',
    value: 'true',
    hardened: 'false',
    opt_in: false,
    impact: 'SQL statements are no longer echoed to the log.',
  }),
  control({
    id: 'web.public_widget_strict_rendering',
    category: 'web',
    title: 'Public chat widget strict rendering',
    setting: 'PUBLIC_WIDGET_STRICT_RENDERING',
    impact: 'Public chat widget answers show only the images the platform serves.',
  }),
  control({
    id: 'tools.tool_call_budget_strict',
    category: 'tools',
    title: 'Tool-call budget cut strictly',
    setting: 'AGENT_LOOP_MAX_TOOL_CALLS_STRICT',
    state: 'monitoring',
    evidence: "Each overshoot is logged and recorded on the run's trace.",
  }),
  control({
    id: 'authentication.min_password_length',
    category: 'authentication',
    title: 'Passwords of 12 characters or more',
    setting: 'MIN_PASSWORD_LENGTH',
    value: '14',
    default: '8',
    hardened: '>= 12',
    state: 'enforced',
  }),
  control({
    id: 'mcp.xtm_mcp_server',
    category: 'mcp',
    title: 'XTM One MCP server off',
    setting: 'xtm_mcp_enabled',
    stored_in: 'platform_settings',
    where: 'Settings > MCP Endpoint',
    hardened: 'false',
    state: 'enforced',
    opt_in: false,
  }),
];

interface Report {
  timestamp: string;
  overall_score: string;
  findings: Finding[];
  summary: Record<string, number>;
  controls?: Control[];
  controls_summary?: Record<string, number>;
}

function reportOf(findings: Finding[], overallScore = 'at_risk', controls?: Control[]): Report {
  const summary: Record<string, number> = { critical: 0, warning: 0, info: 0, pass: 0 };
  for (const finding of findings) summary[finding.severity] = (summary[finding.severity] ?? 0) + 1;
  const report: Report = { timestamp: new Date().toISOString(), overall_score: overallScore, findings, summary };
  if (controls) {
    const states: Record<string, number> = { enforced: 0, monitoring: 0, off: 0 };
    for (const item of controls) states[item.state] = (states[item.state] ?? 0) + 1;
    report.controls = controls;
    report.controls_summary = states;
  }
  return report;
}

const isAudit = (url: URL) => url.pathname === AUDIT_PATH;

async function serve(page: Page, answer: () => { status: number; body: unknown }) {
  await page.route(isAudit, (route: Route) => {
    const { status, body } = answer();
    return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

const auditResponse = (page: Page) =>
  page.waitForResponse((response) => isAudit(new URL(response.url())) && response.request().method() === 'GET');

const stat = (page: Page, severity: string) =>
  page.locator(`[data-testid="security-audit-stat"][data-severity="${severity}"]`);

const severityChip = (page: Page, severity: string) =>
  page.locator(`[data-testid="security-audit-severity-filter"][data-severity="${severity}"]`);

const categoryTile = (page: Page, category: string) =>
  page.locator(`[data-testid="security-audit-category"][data-category="${category}"]`);

const viewButton = (page: Page, view: string) => page.locator(`[data-testid="security-audit-view"][data-view="${view}"]`);

const controlRow = (page: Page, id: string) => page.locator(`[data-testid="security-audit-control"][data-control-id="${id}"]`);

const stateChip = (page: Page, state: string) =>
  page.locator(`[data-testid="security-audit-control-state-filter"][data-state="${state}"]`);

test.describe('Settings > Security > Security audit', () => {
  test('the live report is shown, and Re-run runs the audit again', async ({ page }) => {
    const first = auditResponse(page);
    await page.goto('/settings/security');
    const answer = await first;
    expect(answer.status()).toBe(200);
    const live = (await answer.json()) as Report;

    await expect(page.getByTestId('security-audit-score')).toHaveAttribute('data-score', live.overall_score);
    await expect(page.getByTestId('security-audit-finding')).toHaveCount(live.findings.length);
    for (const [severity, count] of Object.entries(live.summary)) {
      await expect(stat(page, severity)).toHaveAttribute('data-count', String(count));
    }
    // This stack's Redis answers, and the finding carries its stable id.
    await expect(
      page.locator('[data-testid="security-audit-finding"][data-finding-id="infrastructure.redis_healthy"]'),
    ).toBeVisible();

    // Every control the platform registers is listed with its state.
    const liveControls = live.controls ?? [];
    expect(liveControls.length).toBeGreaterThan(20);
    await viewButton(page, 'controls').click();
    await expect(page.getByTestId('security-audit-control')).toHaveCount(liveControls.length);
    await expect(controlRow(page, 'web.cors_origins')).toBeVisible();
    await viewButton(page, 'findings').click();

    const again = auditResponse(page);
    await page.getByTestId('security-audit-rerun').click();
    expect((await again).status()).toBe(200);
    await expect(page.getByTestId('security-audit-rerun')).toBeEnabled();
    await expect(page.getByTestId('security-audit-last-run')).toContainText('Ran just now');
  });

  test('a report reads as a score, a count per severity and a pass rate', async ({ page }) => {
    await serve(page, () => ({ status: 200, body: reportOf(FINDINGS) }));
    await page.goto('/settings/security');

    await expect(page.getByTestId('security-audit-score-label')).toHaveText('At risk');
    await expect(page.getByTestId('security-audit-headline')).toHaveText('1 critical finding needs immediate action.');
    // 3 passes out of the 6 findings that pass or fail: the info finding is
    // neither, and a severity the screen does not know is not a pass.
    await expect(page.getByTestId('security-audit-pass-rate-value')).toHaveText('50%');
    await expect(page.getByTestId('security-audit-pass-rate')).toContainText('3 of 6 checks passed');

    for (const [severity, count] of [['critical', 1], ['warning', 1], ['notice', 1], ['info', 1], ['pass', 3]] as const) {
      await expect(stat(page, severity)).toHaveAttribute('data-count', String(count));
    }
    await expect(stat(page, 'notice')).toContainText('Notice');

    // Categories, the one needing attention most first; the unknown one is
    // named from its key.
    const tiles = page.getByTestId('security-audit-category');
    await expect(tiles).toHaveCount(4);
    await expect(tiles.nth(0)).toHaveAttribute('data-category', 'authentication');
    await expect(categoryTile(page, 'compliance')).toContainText('Compliance');
    // A report without controls (an older platform) has one view.
    await expect(page.getByTestId('security-audit-views')).toHaveCount(0);
    await expect(page.getByTestId('security-audit-controls-enforced')).toHaveCount(0);
    await expect(categoryTile(page, 'encryption').getByTestId('security-audit-category-pass-rate')).toHaveText('100%');
    await expect(categoryTile(page, 'authentication').getByTestId('security-audit-category-pass-rate')).toHaveText('50%');

    // A configuration name in a recommendation reads as what to type.
    await expect(
      page.locator('[data-finding-id="infrastructure.debug_mode"] [data-testid="security-audit-recommendation"] code'),
    ).toHaveText('DEBUG=false');
  });

  test('findings filter by severity, category and text, and group by category', async ({ page }) => {
    await serve(page, () => ({ status: 200, body: reportOf(FINDINGS) }));
    await page.goto('/settings/security');
    const findings = page.getByTestId('security-audit-finding');
    await expect(findings).toHaveCount(FINDINGS.length);

    await severityChip(page, 'critical').click();
    await severityChip(page, 'warning').click();
    await expect(severityChip(page, 'critical')).toHaveAttribute('aria-pressed', 'true');
    await expect(findings).toHaveCount(2);
    await expect(page.getByTestId('security-audit-result-count')).toHaveText('Showing 2 of 7 findings');

    await categoryTile(page, 'infrastructure').click();
    await expect(categoryTile(page, 'infrastructure')).toHaveAttribute('aria-pressed', 'true');
    await expect(findings).toHaveCount(1);
    await expect(findings.first()).toHaveAttribute('data-finding-id', 'infrastructure.debug_mode');

    await page.getByTestId('security-audit-clear-filters').click();
    await expect(findings).toHaveCount(FINDINGS.length);

    await page.getByTestId('security-audit-category-filter').click();
    await page.getByRole('option', { name: 'Encryption' }).click();
    await expect(findings).toHaveCount(1);
    await expect(findings.first()).toHaveAttribute('data-finding-id', 'encryption.secret_key_configured');
    await page.getByTestId('security-audit-clear-filters').click();

    // Every word, in any order, the recommendation included.
    const search = page.getByPlaceholder('Search findings...');
    await search.fill('production debug');
    await expect(findings).toHaveCount(1);
    await search.fill('kubernetes');
    await expect(findings).toHaveCount(0);
    await expect(page.getByText('No matching findings')).toBeVisible();
    await page.getByTestId('security-audit-clear-filters').click();
    await expect(findings).toHaveCount(FINDINGS.length);

    await page.locator('[data-testid="security-audit-group-by"][data-group-by="category"]').click();
    const groups = page.getByTestId('security-audit-group');
    await expect(groups).toHaveCount(4);
    await expect(groups.nth(0)).toHaveAttribute('data-group', 'authentication');
    await expect(groups.nth(0).getByTestId('security-audit-finding').first()).toHaveAttribute('data-severity', 'critical');
  });

  test('the Controls view lists every control with its state, and the URL keeps it', async ({ page }) => {
    await serve(page, () => ({ status: 200, body: reportOf(FINDINGS, 'at_risk', CONTROLS) }));
    await page.goto('/settings/security');

    const enforced = page.getByTestId('security-audit-controls-enforced');
    await expect(enforced).toContainText('2 of 5 controls enforced');
    await expect(enforced).toContainText('1 monitoring');

    await expect(viewButton(page, 'findings')).toHaveAttribute('aria-pressed', 'true');
    await viewButton(page, 'controls').click();
    await expect(page).toHaveURL(/\/settings\/security\?view=controls$/);
    await expect(viewButton(page, 'controls')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('security-audit-finding')).toHaveCount(0);

    // Off with a warning about it first, then off, monitoring, and enforced in category order.
    const rows = page.getByTestId('security-audit-control');
    await expect(rows).toHaveCount(CONTROLS.length);
    for (const [index, id] of [
      'infrastructure.debug_mode',
      'web.public_widget_strict_rendering',
      'tools.tool_call_budget_strict',
      'authentication.min_password_length',
      'mcp.xtm_mcp_server',
    ].entries()) {
      await expect(rows.nth(index)).toHaveAttribute('data-control-id', id);
    }

    const debug = controlRow(page, 'infrastructure.debug_mode');
    await expect(debug.getByTestId('security-audit-control-state')).toHaveText('Off');
    await expect(debug.getByTestId('security-audit-control-setting')).toHaveText('DEBUG');
    await expect(debug.getByTestId('security-audit-control-finding')).toHaveAttribute('data-severity', 'warning');
    await expect(debug.getByTestId('security-audit-control-finding')).toContainText('Debug mode is enabled');
    await expect(debug.getByTestId('security-audit-control-opt-in')).toHaveCount(0);
    await expect(
      controlRow(page, 'web.public_widget_strict_rendering').getByTestId('security-audit-control-opt-in'),
    ).toHaveText('Opt-in');
    await expect(controlRow(page, 'tools.tool_call_budget_strict').getByTestId('security-audit-control-state')).toHaveText(
      'Monitoring',
    );

    // A row opens on what enforcing it changes, how, and the findings about it.
    const details = debug.getByTestId('security-audit-control-details');
    await expect(details).toBeHidden();
    await debug.getByTestId('security-audit-control-toggle').click();
    await expect(debug.getByTestId('security-audit-control-toggle')).toHaveAttribute('aria-expanded', 'true');
    await expect(details).toContainText('SQL statements are no longer echoed to the log.');
    await expect(details.getByTestId('security-audit-control-how')).toContainText('Set DEBUG=false and restart the platform.');
    const linked = details.getByTestId('security-audit-control-linked-finding');
    await expect(linked).toHaveAttribute('data-finding-id', 'infrastructure.debug_mode');
    await expect(linked).toContainText('Set DEBUG=false in production environments.');
    // DEBUG is no Security options field: nothing but its variable sets it.
    await expect(details.getByTestId('security-audit-control-source')).toHaveCount(0);

    // A threshold reads as one, evidence is named, a stored control points at its Settings page.
    const passwords = controlRow(page, 'authentication.min_password_length');
    await passwords.getByTestId('security-audit-control-toggle').click();
    await expect(passwords.getByTestId('security-audit-control-how')).toContainText(
      'Set MIN_PASSWORD_LENGTH to 12 or more and restart the platform.',
    );
    // A Security options field says where its value comes from, as the options card reads it.
    await expect(passwords.getByTestId('security-audit-control-source')).toHaveAttribute(
      'data-source',
      /^(default|environment|database)$/,
    );
    const budget = controlRow(page, 'tools.tool_call_budget_strict');
    await budget.getByTestId('security-audit-control-toggle').click();
    await expect(budget.getByTestId('security-audit-control-evidence')).toContainText('Each overshoot is logged');
    const mcp = controlRow(page, 'mcp.xtm_mcp_server');
    await expect(mcp.getByTestId('security-audit-control-setting')).toHaveText('Settings > MCP Endpoint');
    await mcp.getByTestId('security-audit-control-toggle').click();
    await expect(mcp.getByTestId('security-audit-control-how')).toContainText('Change it in Settings > MCP Endpoint.');

    // Filters: states (any of), category, every word of the search.
    const result = page.getByTestId('security-audit-control-result-count');
    await expect(result).toHaveText('Showing 5 of 5 controls');
    await stateChip(page, 'enforced').click();
    await expect(rows).toHaveCount(2);
    await stateChip(page, 'monitoring').click();
    await expect(rows).toHaveCount(3);
    await expect(result).toHaveText('Showing 3 of 5 controls');
    await page.getByTestId('security-audit-controls-clear-filters').click();
    await expect(rows).toHaveCount(CONTROLS.length);

    await page.getByTestId('security-audit-control-category-filter').click();
    await page.getByRole('option', { name: 'MCP and A2A' }).click();
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toHaveAttribute('data-control-id', 'mcp.xtm_mcp_server');
    await page.getByTestId('security-audit-controls-clear-filters').click();

    const search = page.getByPlaceholder('Search controls...');
    await search.fill('images widget');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toHaveAttribute('data-control-id', 'web.public_widget_strict_rendering');
    await search.fill('kubernetes');
    await expect(page.getByText('No matching controls')).toBeVisible();
    await page.getByTestId('security-audit-controls-clear-filters').click();
    await expect(rows).toHaveCount(CONTROLS.length);

    // The view survives a reload, and going back to Findings drops the parameter.
    await page.reload();
    await expect(rows).toHaveCount(CONTROLS.length);
    await viewButton(page, 'findings').click();
    await expect(page).toHaveURL(/\/settings\/security$/);
    await expect(page.getByTestId('security-audit-finding')).toHaveCount(FINDINGS.length);
  });

  test('a link to the Controls view of an older platform opens its findings', async ({ page }) => {
    await serve(page, () => ({ status: 200, body: reportOf(FINDINGS) }));
    await page.goto('/settings/security/audit?view=controls');
    await expect(page.getByTestId('security-audit-finding')).toHaveCount(FINDINGS.length);
    await expect(page.getByTestId('security-audit-controls')).toHaveCount(0);
  });

  test('an audit that cannot run says why, and runs again on request', async ({ page }) => {
    let failing = true;
    await serve(page, () =>
      failing
        ? { status: 500, body: { detail: 'The audit backend exploded' } }
        : { status: 200, body: reportOf(FINDINGS) },
    );
    await page.goto('/settings/security');
    const unavailable = page.getByTestId('security-audit-error');
    await expect(unavailable).toContainText('The security audit could not run');
    await expect(unavailable).toContainText('The audit backend exploded');

    failing = false;
    await unavailable.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByTestId('security-audit-score')).toBeVisible();
  });

  test('a re-run that fails keeps the last report on screen', async ({ page }) => {
    let failing = false;
    await serve(page, () =>
      failing
        ? { status: 500, body: { detail: 'The audit backend exploded' } }
        : { status: 200, body: reportOf(FINDINGS) },
    );
    await page.goto('/settings/security');
    await expect(page.getByTestId('security-audit-finding')).toHaveCount(FINDINGS.length);

    failing = true;
    await page.getByTestId('security-audit-rerun').click();
    await expect(page.getByTestId('security-audit-rerun-error')).toContainText('The audit backend exploded');
    await expect(page.getByTestId('security-audit-finding')).toHaveCount(FINDINGS.length);
  });

  test('a report without findings says so', async ({ page }) => {
    await serve(page, () => ({ status: 200, body: reportOf([], 'healthy') }));
    await page.goto('/settings/security');
    await expect(page.getByTestId('security-audit-score-label')).toHaveText('Healthy');
    await expect(page.getByTestId('security-audit-headline')).toHaveText('No critical or warning finding.');
    await expect(page.getByTestId('security-audit-pass-rate-value')).toHaveText('-');
    await expect(page.getByText('No findings reported')).toBeVisible();
  });

  test('sign-in and the memory purge keep their own sections of the Security tab', async ({ page }) => {
    await serve(page, () => ({ status: 200, body: reportOf(FINDINGS) }));
    await page.goto('/settings/security');
    await expect(page.getByTestId('security-audit')).toBeVisible();

    await page.locator('[data-testid="security-section-tab"][data-section="authentication"]').click();
    await expect(page).toHaveURL(/\/settings\/security\/authentication$/);
    await expect(page.getByText('Single Sign-On (SSO)')).toBeVisible();
    await expect(page.getByTestId('security-audit')).toHaveCount(0);

    await page.goto('/settings/security/memory');
    await expect(page.getByText('Emergency Memory Purge')).toBeVisible();

    await page.goto('/settings/security/audit');
    await expect(page.getByTestId('security-audit')).toBeVisible();
  });
});

test.describe('Settings > Security > Security audit, for a user who is not an administrator', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  let userId = '';
  let email = '';

  test.beforeAll(async () => {
    email = regularUserEmail('security-audit', test.info().parallelIndex);
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const headers = { Authorization: `Bearer ${token}` };
      const listed = await apiCtx.get('/api/v1/users?limit=500', { headers });
      if (listed.ok()) {
        const leftover = ((await listed.json()) as { id: string; email: string }[]).find((u) => u.email === email);
        if (leftover) await apiCtx.delete(`/api/v1/users/${leftover.id}`, { headers });
      }
      const created = await apiCtx.post('/api/v1/users', {
        headers,
        data: { email, password: REGULAR_USER_PASSWORD, full_name: 'E2E Security Audit User', role: 'user', account_type: 'local' },
      });
      if (!created.ok()) throw new Error(`Creating the regular user failed: ${created.status()} ${await created.text()}`);
      userId = ((await created.json()) as { id: string }).id;
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterAll(async () => {
    if (!userId) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/users/${userId}`, { headers: { Authorization: `Bearer ${token}` } });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('the endpoint refuses the audit and the page sends the user home', async ({ page }) => {
    const api = await request.newContext({ baseURL: BASE_URL });
    try {
      const login = await api.post('/api/v1/auth/login', { data: { email, password: REGULAR_USER_PASSWORD } });
      expect(login.ok()).toBe(true);
      const { access_token: token } = (await login.json()) as { access_token: string };
      const refused = await api.get(AUDIT_PATH, { headers: { Authorization: `Bearer ${token}` } });
      expect(refused.status()).toBe(403);
      expect(((await refused.json()) as { detail: string }).detail).toBe('Admin privileges required');
    } finally {
      await api.dispose();
    }

    const loginPage = new LoginPage(page);
    await loginPage.goto();
    await loginPage.login(email, REGULAR_USER_PASSWORD);
    await page.goto('/settings/security/audit');
    await expect(page).not.toHaveURL(/\/settings/);
    await expect(page.getByTestId('security-audit')).toHaveCount(0);
  });
});
