import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Two-way organization syncs (#1982 S4), end to end in the browser:
// - "Add a sync" on CTEM > Inventory > Sources opens the stepper, "Push to a
//   platform" offers only the sections and platforms an outbound sync takes,
//   the first run is previewed, then the sync is saved with direction outbound.
// - Edit on a saved sync shows its filters; a changed filter set is previewed
//   with POST /syncs/{id}/preview (the rows the next run removes listed) before
//   "Save the change" sends the new filters.
//
// The organization-context sync routes are answered here, so the spec needs no
// connected OpenCTI or OpenAEV and writes nothing; every other call reaches the
// stack.
// ---------------------------------------------------------------------------

const SYNC_ID = '00000000-0000-4000-8000-000000001982';

const SYNC = {
  id: SYNC_ID,
  section: 'assets',
  source: 'openaev',
  filters: {},
  batch_type: '',
  remove_gone: false,
  direction: 'outbound',
  outbound_count: 2,
  frequency: 'daily',
  hour: 6,
  minute: 0,
  weekday: 0,
  timezone: 'UTC',
  enabled: true,
  paused_reason: '',
  owner_email: 'admin@example.com',
  next_run_at: null,
  last_run_at: null,
  last_result: null,
};

const SOURCES = {
  sections: [
    {
      section: 'assets',
      field: 'assets_exposure',
      sources: [
        { source: 'openaev', available: true, reason: '', filters: ['tags', 'category'] },
        { source: 'opencti', available: true, reason: '', filters: ['labels', 'category'] },
        { source: 'intune', available: true, reason: '', filters: [] },
      ],
    },
    { section: 'primary_assets', field: 'primary_assets', sources: [{ source: 'opencrq', available: true, reason: '', filters: [] }] },
  ],
};

const DIRECTIONS = [
  { section: 'assets', source: 'openaev', directions: ['inbound', 'outbound'], reason: '' },
  { section: 'assets', source: 'opencti', directions: ['inbound', 'outbound'], reason: '' },
  { section: 'primary_assets', source: 'opencrq', directions: ['inbound'], reason: 'OpenCRQ owns its primary assets.' },
];

const FIRST_RUN = { direction: 'outbound', counts: { enter: 2, update: 0, leave: 0, kept: 0 }, enter: ['payments-api', 'billing-db'], update: [], leave: [], kept: [], error: null };
const CHANGE = { direction: 'outbound', counts: { enter: 1, update: 2, leave: 1, kept: 0 }, enter: ['web-01'], update: [], leave: ['old-db'], kept: [], error: null };

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

/** Answers the sync routes the two dialogs read and write; records every write. */
async function serveSyncs(page: Page): Promise<Sent[]> {
  const sent: Sent[] = [];
  await page.route(
    (url) => url.pathname.startsWith('/api/v1/organization-context/'),
    async (route: Route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname.replace('/api/v1/organization-context', '');
      const method = request.method();
      if (method !== 'GET') sent.push({ method, path, body: request.postDataJSON() });
      if (method === 'GET' && path === '/syncs') return route.fulfill({ json: [SYNC] });
      if (method === 'GET' && path === '/sources') return route.fulfill({ json: [] });
      if (method === 'GET' && path === '/syncs/directions') return route.fulfill({ json: DIRECTIONS });
      if (method === 'GET' && path === '/import/sources') return route.fulfill({ json: SOURCES });
      if (method === 'POST' && path === '/syncs/outbound-preview') return route.fulfill({ json: FIRST_RUN });
      if (method === 'POST' && path === '/syncs') return route.fulfill({ json: { ...SYNC, id: 'new-sync', source: 'opencti' } });
      if (method === 'POST' && path === `/syncs/${SYNC_ID}/preview`) return route.fulfill({ json: CHANGE });
      if (method === 'PUT' && path === `/syncs/${SYNC_ID}`) return route.fulfill({ json: { ...SYNC, filters: { search: 'prod' } } });
      return route.fallback();
    },
  );
  return sent;
}

test.describe('Organization syncs: push to a platform and edit the filters (#1982)', () => {
  test('Add a sync pushes the assets to OpenCTI after a preview of the first run', async ({ page }) => {
    const sent = await serveSyncs(page);
    await page.goto('/ctem/inventory/sources');
    await page.getByTestId('organization-sources.add').click();

    const dialog = page.getByRole('dialog');
    await dialog.locator('#org-import-choice-outbound').click();
    await expect(dialog.getByRole('heading', { name: 'Push to a platform' })).toBeVisible();
    // Only the sections some platform takes: OpenCRQ owns its primary assets.
    await expect(dialog.locator('#org-import-choice-primary_assets')).toHaveCount(0);
    await dialog.locator('#org-import-choice-assets').click();
    await dialog.getByRole('button', { name: 'Next' }).click();

    await expect(dialog.getByTestId('org-import.step')).toHaveText('Step 2 of 4: Target platform');
    // Intune is read, never written.
    await expect(dialog.locator('#org-import-choice-intune')).toHaveCount(0);
    await dialog.locator('#org-import-choice-opencti').click();
    await dialog.getByRole('button', { name: 'Next' }).click();
    await dialog.getByRole('button', { name: 'Next' }).click();

    await expect(dialog.getByTestId('org-import.outbound-counts')).toContainText('To create');
    await expect(dialog.getByTestId('org-import.sample')).toContainText('payments-api');
    expect(sent.find((call) => call.path === '/syncs/outbound-preview')?.body).toEqual({ section: 'assets', source: 'opencti', filters: {} });

    await dialog.getByRole('button', { name: 'Save the sync' }).click();
    await expect(dialog).toBeHidden();
    expect(sent.find((call) => call.method === 'POST' && call.path === '/syncs')?.body).toMatchObject({
      section: 'assets',
      source: 'opencti',
      direction: 'outbound',
      remove_gone: false,
    });
  });

  test('Edit previews a filter change and lists the rows the next run deletes before saving', async ({ page }) => {
    const sent = await serveSyncs(page);
    await page.goto('/ctem/inventory/sources');
    await page.getByRole('button', { name: 'Actions on OpenAEV' }).click();
    await page.getByRole('menuitem', { name: 'Edit' }).click();

    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('heading', { name: 'Edit the sync' })).toBeVisible();
    const filters = dialog.getByTestId('org-sync-edit-filters');
    await filters.getByRole('textbox').fill('prod');

    // A changed filter set is previewed first: no save before the preview.
    await expect(dialog.getByRole('button', { name: 'Save the change' })).toHaveCount(0);
    await dialog.getByRole('button', { name: 'Preview the change' }).click();
    await expect(dialog.getByTestId('org-sync-edit-change.counts')).toContainText('To remove');
    await expect(dialog.getByTestId('org-sync-edit-change.leave')).toContainText('old-db');
    expect(sent.find((call) => call.path === `/syncs/${SYNC_ID}/preview`)?.body).toEqual({ filters: { search: 'prod' }, batch_type: '', remove_gone: false });
    expect(sent.some((call) => call.method === 'PUT')).toBe(false);

    await dialog.getByRole('button', { name: 'Save the change' }).click();
    await expect(dialog).toBeHidden();
    expect(sent.find((call) => call.method === 'PUT')?.body).toMatchObject({ direction: 'outbound', filters: { search: 'prod' } });
  });
});
