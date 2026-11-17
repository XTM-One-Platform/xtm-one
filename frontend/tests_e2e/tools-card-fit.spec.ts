import { test, expect } from '@playwright/test';

// ---------------------------------------------------------------------------
// Tools page, Built-in tab, grid view.
//
// At 1024 px the "Company-managed" text chip ran past its card's right edge
// (chip right at 1003 px, card right at about 992 px) beside the approval and
// Enabled chips. The card now carries the company-managed mark as an icon
// (tooltip and aria-label keep the wording), and every element drawn in a card
// must sit inside the card box at every supported width.
// ---------------------------------------------------------------------------

test.describe('Tools page, Built-in grid, card fit', () => {
  for (const width of [1024, 1440]) {
    test(`every card keeps its content inside its box at ${width} px`, async ({ page }) => {
      await page.addInitScript(() => {
        window.localStorage.setItem('copilot-view:tools-builtin', 'grid');
      });
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/tools');

      const cards = page.getByTestId('tool-card');
      await expect(cards.first()).toBeVisible({ timeout: 15_000 });

      // The company-managed mark is still announced, by its accessible name.
      await expect(cards.first().getByLabel('Company-managed')).toBeVisible();

      // Every descendant's right edge stays within its card's right edge.
      const overflows = await cards.evaluateAll((els) =>
        els.flatMap((card) => {
          const box = card.getBoundingClientRect();
          return Array.from(card.querySelectorAll('*'))
            .map((el) => el.getBoundingClientRect())
            .filter((r) => r.width > 0 && r.right > box.right + 0.5)
            .map((r) => ({ card: Math.round(box.right), child: Math.round(r.right) }));
        }),
      );
      expect(overflows).toEqual([]);

      // No horizontal scroll on the page.
      const scroll = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(scroll).toBeLessThanOrEqual(0);
    });
  }
});
