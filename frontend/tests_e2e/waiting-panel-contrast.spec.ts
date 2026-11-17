import { test, expect, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';
import { ChatPage } from './model/chat.page';

// ---------------------------------------------------------------------------
// The waiting panel's controls meet the contrast minimum, measured on
// the colours the browser actually paints: the compiled classes, the theme
// tokens and every background under the control, in both modes of both
// themes, at rest and on hover. 4.5:1 for the Take control label (10 px
// text, WCAG 1.4.3), 3:1 for the icon-only leaderboard trophy (1.4.11).
// ---------------------------------------------------------------------------

type RGB = [number, number, number];

/** The message sent is never answered, so the turn stays open with nothing to show. */
async function holdTurnOpen(page: Page): Promise<void> {
  await page.route(
    (url) => url.pathname === '/api/v1/web/chat/messages',
    (route) => (route.request().method() === 'POST' ? new Promise<void>(() => {}) : route.fallback()),
  );
}

/**
 * The control's text colour and the colour behind it, as the screen shows
 * them: every background from the root down to the control composited on a
 * 1x1 canvas (the browser's own sRGB blending), then the text colour on top.
 */
async function paintedColours(control: Locator): Promise<{ ink: RGB; behind: RGB }> {
  return control.evaluate((el) => {
    const canvas = document.createElement('canvas');
    canvas.width = 1;
    canvas.height = 1;
    const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
    const paint = (color: string) => {
      ctx.fillStyle = '#010203';
      ctx.fillStyle = color;
      if (ctx.fillStyle === '#010203' && color !== 'rgb(1, 2, 3)') throw new Error(`canvas cannot paint ${color}`);
      ctx.fillRect(0, 0, 1, 1);
    };
    const chain: Element[] = [];
    for (let node: Element | null = el; node; node = node.parentElement) chain.unshift(node);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, 1, 1);
    for (const node of chain) paint(getComputedStyle(node).backgroundColor);
    const behind = Array.from(ctx.getImageData(0, 0, 1, 1).data.slice(0, 3));
    paint(getComputedStyle(el).color);
    const ink = Array.from(ctx.getImageData(0, 0, 1, 1).data.slice(0, 3));
    return { ink: ink as RGB, behind: behind as RGB };
  });
}

function contrast(a: RGB, b: RGB): number {
  const luminance = (rgb: RGB) => {
    const [r, g, b] = rgb.map((v) => {
      const n = v / 255;
      return n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const THEMES = [
  { name: 'dark', theme: null, mode: null },
  { name: 'light', theme: null, mode: 'light' },
  { name: 'XTM One dark', theme: 'xtm-one', mode: null },
  { name: 'XTM One light', theme: 'xtm-one', mode: 'light' },
] as const;

async function applyTheme(page: Page, theme: string | null, mode: string | null): Promise<void> {
  await page.evaluate(
    ([t, m]) => {
      const root = document.documentElement;
      if (t) root.dataset.theme = t;
      else delete root.dataset.theme;
      if (m) root.dataset.mode = m;
      else delete root.dataset.mode;
    },
    [theme, mode] as const,
  );
}

test.describe('Waiting panel - contrast of its controls', () => {
  const conversationIds: string[] = [];

  test.afterAll(async () => {
    if (conversationIds.length === 0) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      for (const id of conversationIds) {
        await apiCtx.delete(`/api/v1/chat/conversations/${id}`, { headers: { Authorization: `Bearer ${token}` } });
      }
    } finally {
      await apiCtx.dispose();
    }
  });

  test('Take control and the trophy meet their minimum at rest and on hover', async ({ page }) => {
    await holdTurnOpen(page);
    const chat = new ChatPage(page);
    await chat.goto();
    await chat.waitForComposer();
    await chat.send('Take your time.');

    // The waiting game fills a working turn with no reasoning after 5 s.
    const takeControl = page.getByRole('button', { name: 'Take control of the waiting game' });
    const trophy = page.getByRole('button', { name: 'Arcade leaderboard' });
    await expect(takeControl).toBeVisible({ timeout: 30_000 });
    const conversation = new URL(page.url()).searchParams.get('c');
    if (conversation) conversationIds.push(conversation);

    const original = await page.evaluate(() => ({
      theme: document.documentElement.dataset.theme ?? null,
      mode: document.documentElement.dataset.mode ?? null,
    }));
    const failures: string[] = [];
    try {
      for (const { name, theme, mode } of THEMES) {
        await applyTheme(page, theme, mode);
        for (const [label, control, minimum] of [
          ['Take control', takeControl, 4.5],
          ['trophy', trophy, 3],
        ] as const) {
          for (const state of ['rest', 'hover'] as const) {
            if (state === 'hover') await control.hover();
            else await page.mouse.move(0, 0);
            // Past the colour transition.
            await page.waitForTimeout(300);
            const { ink, behind } = await paintedColours(control);
            const ratio = contrast(ink, behind);
            if (ratio < minimum) {
              failures.push(`${label} ${state}, ${name}: ${ratio.toFixed(2)}:1 (ink ${ink}, behind ${behind}), needs ${minimum}:1`);
            }
          }
        }
      }
    } finally {
      await applyTheme(page, original.theme, original.mode);
    }
    expect(failures).toEqual([]);
  });
});
