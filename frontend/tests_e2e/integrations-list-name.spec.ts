import { test, expect, type Page, type Route } from '@playwright/test';

// ---------------------------------------------------------------------------
// Integrations list view, Name column.
//
// A row carrying the Company-managed, Built-in and Shared badges used to cut
// its name to a few characters at 1440 px and drop it entirely at 1024 px,
// with the table scrolling inside its card. The badges now sit on a wrapping
// line under the name, and the secondary columns show from 1280 px.
//
// The integrations list is mocked so the row carries exactly the badges the
// issue names, whatever the deployment holds. A second row has a name long
// enough to hit the Name cell's cap at 1024 px: the table
// must still fit its card, and the full name stays in the title.
// ---------------------------------------------------------------------------

const NAME = 'Filigran shared mailbox';
const LONG_NAME = 'Filigran shared mailbox for the threat intelligence team in Paris';

const INTEGRATION = {
  id: '00000000-0000-4000-8000-000000007525',
  user_id: '00000000-0000-4000-8000-0000000075ff',
  owner_name: 'Another owner',
  provider: 'outlook',
  auth_type: 'api_key',
  status: 'connected',
  name: NAME,
  description: null,
  is_company_managed: true,
  is_builtin: true,
  is_custom: false,
  shared_group_ids: [],
  shared_group_permissions: {},
  shared_user_ids: [],
  shared_user_permissions: {},
  can_manage: true,
  tags: [],
  scopes: null,
  required_scopes: null,
  has_client_id: false,
  has_client_secret: false,
  has_access_token: false,
  has_api_key: true,
  has_extra_config: false,
  oauth_share_acknowledged: false,
  requires_approval: false,
  approval_managed_by_platform: false,
  call_count: 1234,
  last_called_at: null,
  relay_group_id: null,
};

async function openListView(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('copilot-view:integrations', 'list');
  });
  await page.route(
    (url) => url.pathname === '/api/v1/integrations',
    (route: Route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          INTEGRATION,
          {
            ...INTEGRATION,
            id: '00000000-0000-4000-8000-000000007526',
            name: LONG_NAME,
            is_company_managed: false,
            is_builtin: false,
          },
        ]),
      });
    },
  );
  // On All: the page lands on the first segment holding a row (Company here,
  // the first row is company-managed), which would hide the long-name row.
  await page.goto('/integrations?vis=all');
}

test.describe('Integrations list, Name column', () => {
  for (const width of [1024, 1440]) {
    test(`the name stays readable next to its badges at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openListView(page);

      // Exact match: the long row's name starts with the same words.
      const cell = page
        .getByTestId('entity-name-cell')
        .filter({ has: page.getByTestId('entity-name').getByText(NAME, { exact: true }) });
      await expect(cell).toBeVisible({ timeout: 15_000 });
      const name = cell.getByTestId('entity-name');
      await expect(name).toHaveText(NAME);
      // Line 2 never cuts a badge (UX round 2 on #10920, B1): at 1024 px the
      // badges that do not fit fold into a focusable "+N" badge, so Built-in
      // is either drawn whole or counted in that marker.
      const builtIn = cell.getByText('Built-in', { exact: true });
      const more = cell.getByTestId('entity-name-badges-more');
      await expect(builtIn.or(more).first()).toBeVisible();
      if (!(await builtIn.isVisible())) {
        await expect(more).toHaveAccessibleName(/more/);
      }
      const line2 = cell.getByTestId('entity-name-line2');
      const line2Fit = await line2.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
      expect(line2Fit.scroll).toBeLessThanOrEqual(line2Fit.client);

      // The "+N" marker's focus ring draws whole (UX round 3 on #10920, B1: the
      // line's overflow-hidden cut its top and bottom). Its box grown by the
      // 2 px ring lies inside every clipping ancestor up to the table cell.
      if (await more.isVisible()) {
        // The row opens the integration, so it is the row's one tab stop and the
        // marker adds none (#12070): its tooltip opens on hover there, and the
        // focus-ring check below applies only where the marker is a tab stop.
        const focusable = (await more.getAttribute('tabindex')) !== null;
        if (focusable) {
          await more.focus();
        } else {
          await more.hover();
        }
        const clipped = await more.evaluate((el, ringDrawn) => {
          if (!ringDrawn) return [] as string[];
          const ring = 2;
          const box = el.getBoundingClientRect();
          const out: string[] = [];
          for (let node = el.parentElement; node && node.tagName !== 'TD'; node = node.parentElement) {
            const style = window.getComputedStyle(node);
            if (style.overflowX === 'visible' && style.overflowY === 'visible') continue;
            const clip = node.getBoundingClientRect();
            if (
              box.top - ring < clip.top ||
              box.bottom + ring > clip.bottom ||
              box.left - ring < clip.left ||
              box.right + ring > clip.right
            ) {
              out.push(node.dataset.testid ?? node.tagName);
            }
          }
          return out;
        }, focusable);
        expect(clipped).toEqual([]);

        // The tooltip lists the hidden badges as plain text (UX round 4 on
        // #10920): every label reads at 4.5:1 or more on the tooltip surface
        // (B1), wraps whole, and holds no nested focusable trigger (B2).
        const list = page.getByTestId('entity-name-badges-more-list');
        await expect(list).toBeVisible();
        const audit = await list.evaluate((el) => {
          const rgb = (value: string) => (value.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
          const lum = ([r, g, b]: number[]) => {
            const c = [r, g, b].map((v) => {
              const s = v / 255;
              return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
          };
          let surface: Element | null = el;
          while (surface && window.getComputedStyle(surface).backgroundColor.match(/rgba\(.*,\s*0\)$|transparent/)) {
            surface = surface.parentElement;
          }
          const bg = lum(rgb(window.getComputedStyle(surface ?? document.body).backgroundColor));
          return Array.from(el.querySelectorAll('li')).map((li) => {
            const fg = lum(rgb(window.getComputedStyle(li).color));
            const ratio = (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
            return {
              text: li.textContent ?? '',
              ratio,
              cut: li.scrollWidth > li.clientWidth,
              nested: li.querySelector('[tabindex], button, a') !== null,
            };
          });
        });
        expect(audit.length).toBeGreaterThan(0);
        for (const item of audit) {
          expect(item.text.trim()).not.toBe('');
          expect(item.ratio).toBeGreaterThanOrEqual(4.5);
          expect(item.cut).toBe(false);
          expect(item.nested).toBe(false);
        }

        // The tooltip shows the same text as the hidden badges it stands for,
        // on this cold load too (UX round 5 on #10920, B1: a relay badge that
        // resolved its name after mount left the tooltip on its fallback).
        const source = cell.getByTestId('entity-name-badges-hidden-source');
        await expect
          .poll(async () => {
            const sourceLabels = await source.evaluate((el) =>
              Array.from(el.children, (child) => (child.textContent ?? '').trim()).filter(Boolean),
            );
            const tooltipLabels = await list.evaluate((el) =>
              Array.from(el.querySelectorAll('li'), (li) => (li.textContent ?? '').trim()),
            );
            return JSON.stringify(tooltipLabels) === JSON.stringify(sourceLabels) && sourceLabels.length > 0;
          })
          .toBe(true);
      }

      // Not truncated: the whole name fits the box it is drawn in.
      const fit = await name.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
      expect(fit.client).toBeGreaterThan(0);
      expect(fit.scroll).toBeLessThanOrEqual(fit.client);

      // The badges sit under the name, not beside it.
      const nameBox = await name.boundingBox();
      const badgesBox = await cell.getByTestId('entity-name-badges').boundingBox();
      expect(nameBox).not.toBeNull();
      expect(badgesBox).not.toBeNull();
      expect(badgesBox!.y).toBeGreaterThanOrEqual(nameBox!.y + nameBox!.height);

      // The long name row is drawn, keeps its full text in the title.
      const longName = page.getByTestId('entity-name').filter({ hasText: 'threat intelligence' });
      await expect(longName).toBeVisible();
      await expect(longName).toHaveAttribute('title', LONG_NAME);

      // The table fits its card: no horizontal scroll inside it.
      const scroller = page.locator('table').locator('xpath=..');
      const overflow = await scroller.evaluate((el) => el.scrollWidth - el.clientWidth);
      expect(overflow).toBeLessThanOrEqual(1);
    });
  }
});
