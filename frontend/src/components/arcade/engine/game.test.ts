/**
 * The waiting arcade's engine on a stub canvas - `yarn test:unit`.
 *
 * Two promises the overlay relies on: a game nobody played ends untouched
 * (no bonus, nothing the overlay records), and an exception in the loop
 * reaches the host - which closes the game - instead of freezing it over the app.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  BOSS_WAVE_EVERY,
  MAX_SCORED_ELEMENTS,
  NUKE_UI_POINTS,
  UI_CRATER_POINTS,
  UI_DESTROYED_POINTS,
  bossPoints,
  survivalBonus,
  waveClearBonus,
} from "./rules.ts";
import type { DamageResult, ScreenDamage } from "./screen-damage.ts";
import type { ArcadeCanvasLabels, ArcadeTheme, RunResult } from "./types.ts";

let pendingFrame: FrameRequestCallback | null = null;
let drawError: Error | null = null;

/** A 2D context that accepts every call; `clearRect` throws on demand. */
function stubContext(): unknown {
  const assigned: Record<string | symbol, unknown> = {};
  return new Proxy(assigned, {
    get(target, prop) {
      if (prop in target) return target[prop];
      if (prop === "measureText") return () => ({ width: 8 });
      return () => {
        if (prop === "clearRect" && drawError) throw drawError;
        return { addColorStop: () => undefined };
      };
    },
    set(target, prop, value) {
      target[prop] = value;
      return true;
    },
  });
}

function stubCanvas() {
  const context = stubContext();
  return {
    width: 0,
    height: 0,
    getContext: () => context,
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 1280, bottom: 800, width: 1280, height: 800, x: 0, y: 0 }),
  };
}

Object.assign(globalThis, {
  window: { devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800, scrollY: 0, setTimeout, clearTimeout },
  document: {
    hidden: false,
    createElement: () => stubCanvas(),
    getElementById: () => null,
    elementsFromPoint: () => [],
    body: { querySelectorAll: () => [] },
  },
  requestAnimationFrame: (callback: FrameRequestCallback) => {
    pendingFrame = callback;
    return 1;
  },
  cancelAnimationFrame: () => {
    pendingFrame = null;
  },
});

const { ArcadeGame } = await import("./game.ts");

const THEME: ArcadeTheme = {
  isLight: false,
  accent: "var(--color-accent)",
  foreground: "hsl(0 0% 100%)",
  background: "hsl(220 56% 6%)",
  muted: "hsl(220 10% 60%)",
  success: "hsl(150 70% 45%)",
  warning: "hsl(40 90% 55%)",
  danger: "hsl(0 80% 60%)",
  violet: "hsl(270 70% 65%)",
  gold: "hsl(45 95% 55%)",
  ember: "hsl(20 90% 55%)",
  space: "hsl(220 60% 4%)",
};

/** Already unlocked, so a run on a night, a Friday or a holiday unlocks nothing new. */
const CALENDAR_ACHIEVEMENTS = ["night_owl", "early_bird", "friday_deploy", "trick_or_treat", "ho_ho_ho", "april_fool"];

const LABELS: ArcadeCanvasLabels = {
  destroyed: {
    button: "B", link: "L", icon: "I", image: "M", text: "T", input: "N",
    message: "S", navigation: "V", panel: "P", logo: "O", composer: "C",
  },
  fake: "F", payload: "Y", sudo: "U", combo: "X", legendary: "G",
};

function newGame() {
  pendingFrame = null;
  drawError = null;
  const runs: RunResult[] = [];
  const crashes: unknown[] = [];
  const game = new ArcadeGame({
    canvas: stubCanvas() as unknown as HTMLCanvasElement,
    overlayRoot: { contains: () => false } as unknown as HTMLElement,
    theme: THEME,
    labels: LABELS,
    reducedMotion: false,
    seasonal: null,
    origin: null,
    personalBest: 0,
    rivals: [],
    unlocked: new Set(CALENDAR_ACHIEVEMENTS),
    initialAchievements: [],
    callbacks: {
      onHud: () => undefined,
      onEvent: () => undefined,
      onGameOver: (run) => runs.push(run),
      onCrash: (error) => crashes.push(error),
    },
  });
  return { game, runs, crashes };
}

/** The last frame's time: the next `play` resumes from it, so time never runs backwards. */
let lastFrameAt = 0;

/** Run the animation frames for `ms` of wall time, 60 frames a second. */
function play(ms: number): void {
  const start = Math.max(lastFrameAt, performance.now());
  for (let t = start + 16; t <= start + ms && pendingFrame; t += 16) {
    const frame = pendingFrame;
    pendingFrame = null;
    frame(t);
    lastFrameAt = t;
  }
}

test("a game nobody played ends untouched: no bonus, no badge, nothing to record", () => {
  const { game, runs } = newGame();
  game.start();
  play(1000);
  assert.equal(game.isEngaged, false);

  game.missionComplete(120);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].untouched, true);
  assert.equal(runs[0].score, 0);
  assert.equal(runs[0].durationMs, 0);
  assert.ok(!runs[0].achievements.includes("mission_complete"));
  game.destroy();
});

test("a player who played is paid the mission bonus and leaves with a run", () => {
  const { game, runs } = newGame();
  game.start();
  play(500);
  game.setKey("left", true);
  play(50);
  game.setKey("left", false);
  assert.equal(game.isEngaged, true);

  game.missionComplete(60);
  play(6000);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].reason, "mission");
  assert.equal(runs[0].untouched, false);
  assert.ok(runs[0].score >= survivalBonus(60));
  assert.ok(runs[0].achievements.includes("mission_complete"));
  game.destroy();
});

test("a game counts and pays no more destroyed elements than the server accepts, the nuke included", () => {
  const { game, runs } = newGame();
  game.start();
  play(200);
  // What nukes on a busy page hand the game: elements crumbled at once, all of
  // them paid for - the count still stops at the cap.
  const wreck = (count: number, achievement: string | null = null) =>
    Array.from({ length: count }, () => ({
      kind: "panel",
      destroyed: true,
      scored: true,
      rect: { x: 0, y: 0, w: 10, h: 10 },
      colors: [],
      achievement,
    }));
  const count = (game as unknown as { countDestruction(results: unknown[], byPlayer: boolean, points?: number): void })
    .countDestruction.bind(game);
  count(wreck(MAX_SCORED_ELEMENTS - 20), true);
  count(wreck(120), true, NUKE_UI_POINTS);
  count(wreck(120), true, NUKE_UI_POINTS);
  // Past the cap the interface still falls and its badges still unlock.
  count(wreck(1, "brand_vandal"), true);
  game.quit();
  assert.equal(runs.length, 1);
  assert.equal(runs[0].uiDestroyed, MAX_SCORED_ELEMENTS);
  assert.equal(runs[0].score, (MAX_SCORED_ELEMENTS - 20) * UI_DESTROYED_POINTS + 20 * NUKE_UI_POINTS);
  assert.ok(runs[0].achievements.includes("brand_vandal"));
  game.destroy();
});

/** What the tests reach inside the engine: its interface damage and the one consumer of every `DamageResult`. */
type Engine = {
  damage: ScreenDamage;
  countDestruction(results: DamageResult[], byPlayer: boolean, points?: number): void;
};

function damageResult(destroyed: boolean, scored: boolean, achievement: DamageResult["achievement"] = null): DamageResult {
  return { kind: "panel", destroyed, scored, rect: { x: 0, y: 0, w: 10, h: 10 }, colors: [], achievement };
}

test("damage the game is not paid for counts for nothing - no destroyed element, no crater, no points - and its badges still unlock", () => {
  const { game, runs } = newGame();
  game.start();
  play(200);
  const engine = game as unknown as Engine;
  // Paid for: a crater and a destroyed element.
  engine.countDestruction([damageResult(false, true), damageResult(true, true)], true);
  // Recycled room, long before the count reaches its cap: nothing is counted or paid.
  engine.countDestruction([damageResult(false, false), damageResult(false, false), damageResult(true, false)], true);
  engine.countDestruction([damageResult(true, false, "brand_vandal")], true, NUKE_UI_POINTS);
  game.quit();
  assert.equal(runs.length, 1);
  assert.equal(runs[0].uiDestroyed, 1);
  assert.equal(runs[0].score, UI_CRATER_POINTS + UI_DESTROYED_POINTS);
  assert.ok(runs[0].achievements.includes("brand_vandal"), "a badge is not points: it still unlocks");
  game.destroy();
});

/** A 20 px tile of text on a 1280 x 800 page, as far as the interface damage reads one. */
function pageTile(i: number) {
  const left = (i % 60) * 21;
  const top = 60 + Math.floor(i / 60) * 21;
  return {
    tagName: "DIV",
    id: "",
    isConnected: true,
    childNodes: [{ nodeType: 3, textContent: "Damage me" }],
    center: { x: left + 10, y: top + 10 },
    getBoundingClientRect: () => ({ left, top, width: 20, height: 20, x: left, y: top, right: left + 20, bottom: top + 20 }),
    closest: () => null,
    getAttribute: () => null,
    contains: () => false,
  };
}

const TILE_STYLE = {
  visibility: "visible",
  display: "block",
  opacity: "1",
  backgroundImage: "none",
  backgroundColor: "rgba(0, 0, 0, 0)",
  borderTopWidth: "0px",
  borderLeftWidth: "0px",
  borderTopColor: "rgba(0, 0, 0, 0)",
  color: "rgb(255, 255, 255)",
};

test("the game's own damage pays for its first MAX_SCORED_ELEMENTS elements: past them the page crumbles for show, for nothing", () => {
  const tiles = Array.from({ length: MAX_SCORED_ELEMENTS + 60 }, (_, i) => pageTile(i));
  const doc = globalThis.document as unknown as { elementsFromPoint: (x: number, y: number) => unknown[] };
  const emptyPage = doc.elementsFromPoint;
  const { game, runs } = newGame();
  game.start();
  play(200);
  // The page appears once the loop stands still: no invader's shot lands on it.
  doc.elementsFromPoint = (x, y) =>
    tiles.filter((tile) => {
      const box = tile.getBoundingClientRect();
      return x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
    });
  Object.assign(globalThis, { getComputedStyle: () => TILE_STYLE });
  try {
    const engine = game as unknown as Engine;
    const shoot = (tile: ReturnType<typeof pageTile>, damage: number, radius: number): DamageResult => {
      const result = engine.damage.scorch(tile.center.x, tile.center.y, { radius, damage, now: 0 });
      assert.ok(result, "the shot went through the page");
      engine.countDestruction([result], true);
      return result;
    };
    const paid = tiles.slice(0, MAX_SCORED_ELEMENTS);
    const past = tiles.slice(MAX_SCORED_ELEMENTS);
    // A shot scars each of the first elements the game tracks: every crater is paid for.
    for (const tile of paid) assert.equal(shoot(tile, 0.34, 3).scored, true);
    // Past them the interface still takes damage - brought down or scarred - for nothing.
    for (const tile of past.slice(0, 40)) {
      const result = shoot(tile, Infinity, 0);
      assert.equal(result.destroyed, true);
      assert.equal(result.scored, false);
    }
    for (const tile of past.slice(40)) assert.equal(shoot(tile, 0.34, 3).scored, false);
    // The latest paid elements are still tracked: bringing them down counts.
    for (const tile of paid.slice(-20)) {
      const result = shoot(tile, Infinity, 0);
      assert.equal(result.destroyed, true);
      assert.equal(result.scored, true);
    }
    game.quit();
    assert.equal(runs.length, 1);
    assert.equal(runs[0].uiDestroyed, 20);
    assert.equal(runs[0].score, MAX_SCORED_ELEMENTS * UI_CRATER_POINTS + 20 * UI_DESTROYED_POINTS);
  } finally {
    doc.elementsFromPoint = emptyPage;
    delete (globalThis as { getComputedStyle?: unknown }).getComputedStyle;
    game.destroy();
  }
});

test("a boss that finishes dying while the cannon is down pays its wave's clear bonus too", () => {
  // The server's per-wave ceiling counts the clear bonus on boss waves for this.
  const { game, runs } = newGame();
  game.start();
  play(200);
  game.setKey("left", true);
  play(50);
  game.setKey("left", false);
  const engine = game as unknown as {
    phase: string;
    boss: { hp: number; dying: number } | null;
    enemyShots: unknown[];
    startWave(wave: number): void;
    damageBoss(amount: number): void;
    killPlayer(): void;
  };
  engine.startWave(BOSS_WAVE_EVERY);
  play(2600);
  assert.equal(engine.phase, "playing");
  engine.enemyShots = [];
  const boss = engine.boss;
  assert.ok(boss);
  engine.damageBoss(boss.hp);
  // Most of the way through its 1.4 s death, the cannon is hit: the boss dies
  // during the respawn, which then finds an empty formation and clears the wave.
  play(1100);
  assert.ok(engine.boss && engine.boss.dying > 0);
  engine.killPlayer();
  play(2000);
  assert.equal(engine.boss, null);
  game.quit();
  assert.equal(runs.length, 1);
  assert.equal(runs[0].wave, BOSS_WAVE_EVERY);
  assert.equal(runs[0].bosses, 1);
  assert.equal(runs[0].score, bossPoints(BOSS_WAVE_EVERY) + waveClearBonus(BOSS_WAVE_EVERY));
  game.destroy();
});

test("an exception in the loop reaches the host once and stops the loop", () => {
  const { game, crashes } = newGame();
  game.start();
  play(200);
  drawError = new Error("boom");
  play(200);
  drawError = null;
  assert.equal(crashes.length, 1);
  assert.equal((crashes[0] as Error).message, "boom");
  assert.equal(pendingFrame, null);
  game.destroy();
});
