import { test, expect, type Page } from '@playwright/test';
import { TRACE_ID, traceFixture, spansFixture } from './fixtures/trace-shapes';

async function mockTraceDetailApis(page: Page) {
  await page.route(`**/api/v1/traces/${TRACE_ID}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(traceFixture),
    });
  });

  await page.route(`**/api/v1/traces/${TRACE_ID}/spans`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(spansFixture),
    });
  });
}

test.describe('Trace detail explorer', () => {
  test('should switch explorer modes with mocked trace data', async ({ page }) => {
    await mockTraceDetailApis(page);
    await page.goto(`/observability/traces/${TRACE_ID}/tree`);

    await expect(page.getByRole('heading', { name: 'E2E Trace Explorer', level: 1 })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Tree' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Timeline' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Search' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Graph' })).toBeVisible();

    await expect(page.getByText('Tool Call ID:')).toBeVisible();

    await page.getByRole('button', { name: 'Timeline' }).click();
    await expect(page.getByRole('button', { name: /Root generation span/i })).toBeVisible();

    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.getByPlaceholder('Search name, input, output, arguments...')).toBeVisible();

    await page.getByRole('button', { name: 'Graph' }).click();
    await expect(page.getByLabel('Trace graph')).toBeVisible();
  });

  test('should update span inspector on span selection', async ({ page }) => {
    await mockTraceDetailApis(page);
    await page.goto(`/observability/traces/${TRACE_ID}/tree`);

    await expect(page.getByText('Root generation span').first()).toBeVisible();
    await page.getByRole('button', { name: /Tool span/i }).first().click();

    await expect(page.getByText('Tool Call ID:')).toBeVisible();
    await expect(page.getByText('call-tool-1')).toBeVisible();
  });
});
