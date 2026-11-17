import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Settings > Security > Security options.
//
// The options are one section of the Security tab, next to the security
// audit, and an option the audit checks links to the finding it decides: the
// audit opens on that finding. The options come from the live stack; the
// audit report is a fixture, so the spec pins the link rather than whatever
// this stack's configuration happens to report.
// ---------------------------------------------------------------------------

const AUDIT_PATH = '/api/v1/security-audit';
const WIDGET_FINDING = 'web.public_widget_remote_images';

function reportOf(findings: { id: string; category: string; severity: string; title: string }[]) {
  const summary: Record<string, number> = { critical: 0, warning: 0, info: 0, pass: 0 };
  for (const finding of findings) summary[finding.severity] = (summary[finding.severity] ?? 0) + 1;
  return {
    timestamp: new Date().toISOString(),
    overall_score: 'needs_attention',
    findings: findings.map((finding) => ({ description: 'Fixture.', recommendation: null, ...finding })),
    summary,
  };
}

async function serveAudit(page: Page, findings: Parameters<typeof reportOf>[0]) {
  await page.route(
    (url: URL) => url.pathname === AUDIT_PATH,
    (route: Route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(reportOf(findings)) }),
  );
}

const auditLink = (page: Page, option: string) =>
  page.locator(`[data-testid="security-option-audit-link"][data-option="${option}"]`);

test.describe('Settings > Security > Security options', () => {
  test('the options are a section of the Security tab', async ({ page }) => {
    await page.goto('/settings/security');
    await page.locator('[data-testid="security-section-tab"][data-section="options"]').click();
    await expect(page).toHaveURL(/\/settings\/security\/options$/);
    await expect(page.getByText('Platform-wide security switches.', { exact: false })).toBeVisible();
    await expect(page.getByText('Strict widget rendering', { exact: true })).toBeVisible();
    await expect(page.getByTestId('security-audit')).toHaveCount(0);
  });

  test('an option the audit checks opens the audit on its finding', async ({ page }) => {
    await serveAudit(page, [
      { id: 'infrastructure.debug_mode', category: 'infrastructure', severity: 'warning', title: 'Debug mode is enabled' },
      { id: WIDGET_FINDING, category: 'web', severity: 'warning', title: 'Public chat widget loads remote images' },
    ]);
    await page.goto('/settings/security/options');
    // An option the audit does not check has no link.
    await expect(auditLink(page, 'mcp_outbound_dcr_enabled')).toHaveCount(0);

    await auditLink(page, 'public_widget_strict_rendering').click();
    await expect(page).toHaveURL(
      /\/settings\/security\?finding=web\.public_widget_remote_images&finding=web\.public_widget_strict$/,
    );
    const finding = page.locator(`[data-testid="security-audit-finding"][data-finding-id="${WIDGET_FINDING}"]`);
    await expect(finding).toHaveAttribute('data-focused', 'true');
    await expect(
      page.locator('[data-testid="security-audit-finding"][data-finding-id="infrastructure.debug_mode"]'),
    ).not.toHaveAttribute('data-focused', 'true');
    await expect(page.getByTestId('security-audit-focus-absent')).toHaveCount(0);
  });

  test('XTM One fixes the quota fail mode closed and says why', async ({ page }) => {
    // The E2E stack runs in xtm_one mode, the compose default.
    await page.goto('/settings/security/options');
    const row = page.locator('[data-testid="security-option"][data-option-key="quota_fail_mode"]');
    await expect(row).toHaveAttribute('data-source', 'platform_mode');
    await expect(row.getByText('quotas are part of the licence', { exact: false })).toBeVisible();
    await expect(row.getByText('Closed (refuse)', { exact: true })).toBeVisible();
    await expect(row.getByText('Env', { exact: true })).toHaveCount(0);

    // Configuring the card offers no control for it.
    await page.getByTestId('security-options-configure').click();
    await expect(page.getByTestId('settings.save-bar')).toBeVisible();
    await expect(page.locator('#security-option-quota_fail_mode-control')).toHaveCount(0);
    await expect(row.getByText('Closed (refuse)', { exact: true })).toBeVisible();
  });

  test('the audit says so when this run reports nothing for the option', async ({ page }) => {
    await serveAudit(page, [
      { id: 'infrastructure.debug_mode', category: 'infrastructure', severity: 'warning', title: 'Debug mode is enabled' },
    ]);
    await page.goto(`/settings/security?finding=${WIDGET_FINDING}`);
    await expect(page.getByTestId('security-audit-focus-absent')).toBeVisible();
    await expect(page.locator('[data-testid="security-audit-finding"][data-focused="true"]')).toHaveCount(0);
  });
});
