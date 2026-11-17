import { test, expect, type Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// The waiting arcade plays over the REAL interface: missiles crumble
// it, the repair flies it home. Whatever the game did, closing it gives the
// app back exactly as it was: no animation of the game left running,
// and not one style, class or attribute written on the page.
// ---------------------------------------------------------------------------

interface ElementState {
  style: string | null;
  className: string | null;
  hidden: boolean;
  inert: boolean;
  opacity: string;
  transform: string;
  /** Already animated (a pulse of its own, a transition): its opacity and transform move without the game. */
  moving: boolean;
}

declare global {
  interface Window {
    __arcadeRestore?: { before: Map<Element, ElementState>; damaged: Set<Element> };
  }
}

const arcade = (page: Page) => page.getByRole('dialog', { name: 'Space Invaders arcade' });

/** Every element of the app outside the game, as it stands before the first shot. */
function snapshotPage(): void {
  const overlay = document.querySelector('[role="dialog"][aria-label="Space Invaders arcade"]');
  const before = new Map<Element, ElementState>();
  for (const el of Array.from(document.body.querySelectorAll('*'))) {
    if (overlay?.contains(el)) continue;
    const style = getComputedStyle(el);
    before.set(el, {
      style: el.getAttribute('style'),
      className: el.getAttribute('class'),
      hidden: el.hasAttribute('hidden'),
      inert: el.hasAttribute('inert'),
      opacity: style.opacity,
      transform: style.transform,
      moving: el.getAnimations().length > 0,
    });
  }
  window.__arcadeRestore = { before, damaged: new Set() };
}

/**
 * Points of the interface a missile reaches without crossing the invaders:
 * below the formation, well above the cannon, where the game's canvas takes
 * the click (the HUD and the hint sit on top of it in places). The sidebar's
 * links first; form controls are left alone, the app sizes them itself.
 */
function aimPoints(): { x: number; y: number }[] {
  const overlay = document.querySelector('[role="dialog"][aria-label="Space Invaders arcade"]');
  if (!overlay) return [];
  const top = window.innerHeight * 0.45;
  const bottom = window.innerHeight * 0.86;
  const onCanvas = (x: number, y: number) => {
    const hit = document.elementFromPoint(x, y);
    return hit instanceof HTMLCanvasElement && overlay.contains(hit);
  };
  const reachable = (x: number, y: number) => y >= top && y <= bottom && onCanvas(x, y);
  const links: { x: number; y: number }[] = [];
  for (const el of Array.from(document.querySelectorAll('nav a, nav button'))) {
    if (overlay.contains(el)) continue;
    const box = el.getBoundingClientRect();
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    if (box.width >= 6 && box.height >= 6 && reachable(x, y)) links.push({ x, y });
  }
  if (links.length > 0) return links;
  // Otherwise whatever carries text there, clear of the form controls.
  const controls = Array.from(document.querySelectorAll('input, textarea, select')).map((el) => el.getBoundingClientRect());
  const nearControl = (x: number, y: number) =>
    controls.some((box) => x > box.left - 80 && x < box.right + 80 && y > box.top - 80 && y < box.bottom + 80);
  const points: { x: number; y: number }[] = [];
  for (let y = top; y <= bottom; y += 40) {
    for (let x = window.innerWidth * 0.05; x <= window.innerWidth * 0.95; x += 60) {
      const under = document.elementsFromPoint(x, y).find((el) => !overlay.contains(el));
      const ownText = under && Array.from(under.childNodes).some((node) => node.nodeType === 3 && (node.textContent ?? '').trim());
      if (ownText && !nearControl(x, y) && reachable(x, y)) points.push({ x, y });
    }
  }
  return points;
}

/** The elements of the app the game animates right now, added to those seen so far. */
function collectDamage(): number {
  const state = window.__arcadeRestore;
  if (!state) return 0;
  const overlay = document.querySelector('[role="dialog"][aria-label="Space Invaders arcade"]');
  for (const animation of document.getAnimations()) {
    if (animation instanceof CSSAnimation || animation instanceof CSSTransition) continue;
    const target = animation.effect instanceof KeyframeEffect ? animation.effect.target : null;
    if (target && !overlay?.contains(target)) state.damaged.add(target);
  }
  return state.damaged.size;
}

/** Once the game is gone: what it left behind. */
function leftBehind() {
  const state = window.__arcadeRestore;
  const describe = (el: Element) => `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}.${(el.getAttribute('class') ?? '').split(' ')[0]}`;
  const running = document
    .getAnimations()
    .filter((animation) => !(animation instanceof CSSAnimation) && !(animation instanceof CSSTransition))
    .map((animation) => {
      const target = animation.effect instanceof KeyframeEffect ? animation.effect.target : null;
      return target ? describe(target) : 'no target';
    });
  const changed: string[] = [];
  let compared = 0;
  for (const el of state?.damaged ?? []) {
    const before = state?.before.get(el);
    if (!before || !el.isConnected) continue;
    compared += 1;
    const attributes = [el.getAttribute('style'), el.getAttribute('class'), el.hasAttribute('hidden'), el.hasAttribute('inert')];
    if (JSON.stringify(attributes) !== JSON.stringify([before.style, before.className, before.hidden, before.inert])) {
      changed.push(`${describe(el)}: attributes ${JSON.stringify(attributes)}`);
    }
    if (!before.moving && el.getAnimations().length === 0) {
      const style = getComputedStyle(el);
      if (style.opacity !== before.opacity || style.transform !== before.transform) {
        changed.push(`${describe(el)}: opacity ${before.opacity} -> ${style.opacity}, transform ${before.transform} -> ${style.transform}`);
      }
    }
  }
  return { damaged: state?.damaged.size ?? 0, compared, running, changed };
}

test('the arcade gives the interface back exactly as it was', async ({ page }) => {
  await page.goto('/chat?arcade=play');
  await expect(page.locator('textarea').first()).toBeVisible();
  const game = arcade(page);
  await expect(game).toBeVisible();
  // The game boots after its intro: nothing has been shot yet.
  await page.evaluate(snapshotPage);
  await expect(game).toHaveClass(/cursor-crosshair/);

  // The three missiles of a new game, each aimed at a part of the interface.
  const points = await page.evaluate(aimPoints);
  expect(points.length, 'no part of the interface below the invaders to aim at').toBeGreaterThan(0);
  const step = Math.max(1, Math.floor(points.length / 3));
  for (let shot = 0; shot < 3; shot += 1) {
    const { x, y } = points[Math.min(points.length - 1, shot * step)];
    await page.mouse.click(x, y, { button: 'right' });
    await page.waitForTimeout(200);
  }
  await expect.poll(() => page.evaluate(collectDamage), { message: 'the missiles crumbled nothing', timeout: 15_000 }).toBeGreaterThan(0);
  // Let the falls land: the repair flies them home from the ground.
  await page.waitForTimeout(1_500);
  await page.evaluate(collectDamage);

  await game.getByRole('button', { name: 'Leave the arcade (Esc twice)' }).click();
  await expect(game).toHaveCount(0);

  const report = await page.evaluate(leftBehind);
  expect(report.compared, 'the damaged elements left the page').toBeGreaterThan(0);
  expect(report.running, 'an animation of the game outlived it').toEqual([]);
  expect(report.changed, 'the page was not given back as it was').toEqual([]);
});
