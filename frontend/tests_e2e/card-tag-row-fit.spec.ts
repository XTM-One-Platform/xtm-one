import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Card tag rows stay inside their card.
//
// The unit tests of TagList mock clientWidth and scrollWidth, since jsdom has
// no layout; they passed while the real grid let the tag row run past the
// card, because the TagList root kept its min-content width in the footer's
// flex row and was never squeezed. This spec measures the real layout: with
// long tags and a long compatibility text, every element drawn in a card ends
// inside the card, the page does not scroll sideways, a `+N` marker folds the
// tags that do not fit, and the Skills compatibility badge keeps visible text.
// ---------------------------------------------------------------------------

const LONG_TAG = 'threat-intelligence-reporting-and-briefing-for-executive-boards';
const TAGS = [LONG_TAG, 'slug:openaev-payload-builder', 'ransomware', 'phishing', 'apt'];
const COMPATIBILITY = 'Uses OpenCTI, Google Threat Intelligence, CrowdStrike Falcon and Recorded Future';

async function me(page: Page): Promise<string> {
  return page.evaluate(async () => (await (await fetch('/api/v1/auth/me')).json()).id as string);
}

function base(id: string, userId: string) {
  return {
    id,
    description: 'A card whose footer carries long tags.',
    tags: TAGS,
    user_id: userId,
    is_company_managed: false,
    shared_group_ids: [],
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  };
}

async function servePrompts(page: Page) {
  const userId = await me(page);
  await page.route(/\/api\/v1\/prompts(\?|$)/, (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({
          json: [0, 1, 2, 3, 4, 5].map((i) => ({
            ...base(`tagfit-p${i}`, userId),
            name: `Prompt with long tags ${i}`,
            content: 'Summarise the latest intrusion set activity for the board.',
          })),
        })
      : route.fallback(),
  );
}

async function serveSkills(page: Page) {
  const userId = await me(page);
  await page.route(/\/api\/v1\/skills(\?|$)/, (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({
          json: [0, 1, 2, 3, 4, 5].map((i) => ({
            ...base(`tagfit-s${i}`, userId),
            name: `Skill with long tags ${i}`,
            enabled: true,
            content: 'Skill body.',
            url: i % 2 ? 'https://example.com/skill.md' : null,
            fetched_at: i % 2 ? '2026-11-01T00:00:00Z' : null,
            compatibility: COMPATIBILITY,
            source_integration_id: null,
            auth_failure_count: 0,
            owner_name: null,
          })),
        })
      : route.fallback(),
  );
}

async function serveTemplates(page: Page) {
  const userId = await me(page);
  await page.route(/\/api\/v1\/template-containers(\?|$)/, (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({
          json: [0, 1, 2, 3, 4, 5].map((i) => ({
            ...base(`tagfit-t${i}`, userId),
            name: `Template with long tags ${i}`,
            owner_name: null,
            kind: 'presentation',
            theme: null,
            theme_config: {},
            aspect_ratio: '16:9',
            layouts: [],
            extra_metadata: {},
            supported_schemes: ['dark'],
            lookup_count: 0,
            asset_count: 48,
            is_builtin: false,
            shared_group_permissions: null,
          })),
        })
      : route.fallback(),
  );
}

/** Right edges of card descendants that end past their card, and the page's sideways scroll. */
async function measure(page: Page, hrefPrefix: string) {
  return page.evaluate((prefix) => {
    const cards = Array.from(document.querySelectorAll<HTMLAnchorElement>(`a[href^="${prefix}"]`));
    const overflows = cards.flatMap((link) => {
      const box = link.getBoundingClientRect();
      return Array.from(link.querySelectorAll('*'))
        .map((el) => el.getBoundingClientRect())
        .filter((r) => r.width > 0 && r.right > box.right + 0.5)
        .map((r) => Math.round(r.right - box.right));
    });
    return {
      cards: cards.length,
      overflows,
      scroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  }, hrefPrefix);
}

test.describe('Card tag rows fit their card', () => {
  for (const width of [1024, 1440]) {
    test(`Prompts cards keep their tags inside at ${width} px`, async ({ page }) => {
      await page.addInitScript(() => window.localStorage.setItem('copilot-view:prompts', 'grid'));
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      await servePrompts(page);
      await page.goto('/prompts?vis=all');
      await expect(page.getByText('Prompt with long tags 0')).toBeVisible({ timeout: 15_000 });

      const result = await measure(page, '/prompts/tagfit-');
      expect(result.cards).toBe(6);
      expect(result.overflows).toEqual([]);
      expect(result.scroll).toBeLessThanOrEqual(0);
      // The tags that do not fit fold into a marker that names them.
      await expect(page.locator('a[href="/prompts/tagfit-p0"]').getByText(/^\+\d+$/)).toBeVisible();
    });

    test(`Skills cards keep their tags and compatibility text inside at ${width} px`, async ({ page }) => {
      await page.addInitScript(() => window.localStorage.setItem('copilot-view:skills', 'grid'));
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      await serveSkills(page);
      await page.goto('/skills?vis=all');
      await expect(page.getByText('Skill with long tags 0')).toBeVisible({ timeout: 15_000 });

      const result = await measure(page, '/skills/tagfit-');
      expect(result.cards).toBe(6);
      expect(result.overflows).toEqual([]);
      expect(result.scroll).toBeLessThanOrEqual(0);

      const card = page.locator('a[href="/skills/tagfit-s0"]');
      await expect(card.getByText(/^\+\d+$/)).toBeVisible();
      // The compatibility badge never collapses to an empty pill: some of its
      // text stays visible (about five characters at least).
      const compatWidth = await card
        .getByText(COMPATIBILITY, { exact: true })
        .evaluate((el) => el.getBoundingClientRect().width);
      expect(compatWidth).toBeGreaterThanOrEqual(24);
    });

    test(`Templates cards keep their tags inside and their counts on one line at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      await serveTemplates(page);
      await page.goto('/templates?vis=all');
      await expect(page.getByText('Template with long tags 0')).toBeVisible({ timeout: 15_000 });

      const result = await measure(page, '/templates/tagfit-');
      expect(result.cards).toBe(6);
      expect(result.overflows).toEqual([]);
      expect(result.scroll).toBeLessThanOrEqual(0);

      const card = page.locator('a[href="/templates/tagfit-t0"]');
      await expect(card.getByText(/^\+\d+$/)).toBeVisible();
      // The layout and asset counts never wrap: every footer child but the
      // tag list stays one line high, so the tags take the squeeze.
      const heights = await card.locator('[data-tag-list]').evaluate((list) =>
        Array.from(list.parentElement?.children ?? [])
          .filter((el) => el !== list)
          .map((el) => el.getBoundingClientRect().height),
      );
      expect(heights.length).toBeGreaterThan(0);
      for (const h of heights) expect(h).toBeLessThanOrEqual(20);
    });
  }
});
