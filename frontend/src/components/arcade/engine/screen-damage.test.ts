/**
 * The interface the waiting arcade damages, and how it is put back -
 * `yarn test:unit`.
 *
 * There is no browser here (and no Web Animations API in any DOM emulation):
 * a small fake DOM whose elements record every animation started on them -
 * keyframes, composite mode, where a reversed one started running - and every
 * write to a class, a style or an attribute.
 */
import { mock, test } from "node:test";
import assert from "node:assert/strict";

import {
  MAX_TRACKED,
  RESTORE_SPEED,
  ScreenDamage,
  crumbleEffects,
  pickRecyclable,
  recycleRank,
  returnPlan,
  type TrackedState,
} from "./screen-damage.ts";
import { MAX_SCORED_ELEMENTS } from "./rules.ts";
import type { ArcadeTheme } from "./types.ts";

/**
 * A Web Animation as far as the arcade drives one, modelled on the spec
 * procedures it relies on - the repair's bug lived in one of them: on a
 * FINISHED animation, `updatePlaybackRate()` resumes from the unconstrained
 * current time (the hold time ignored), i.e. from however long ago it started.
 */
class FakeAnimation {
  readonly target: FakeElement;
  readonly keyframes: Keyframe[];
  readonly options: KeyframeAnimationOptions;
  onfinish: (() => void) | null = null;
  oncancel: (() => void) | null = null;
  playState: AnimationPlayState = "running";
  /** Where each backwards run started. */
  readonly reversedFrom: number[] = [];
  private time: number | null = 0;
  /** (timeline time - start time) x rate: the current time while it runs; once
   *  finished, how far its clock went on past the end. */
  private clock = 0;
  private rate = 1;

  constructor(target: FakeElement, keyframes: Keyframe[], options: KeyframeAnimationOptions) {
    this.target = target;
    this.keyframes = keyframes;
    this.options = options;
  }

  get delay(): number {
    return this.options.delay ?? 0;
  }

  get endTime(): number {
    return this.delay + Number(this.options.duration ?? 0);
  }

  get composite(): CompositeOperation {
    return this.options.composite ?? "replace";
  }

  /** Spec "relevant animation" - what `getAnimations()` lists: running, or finished with a fill still applied. */
  get relevant(): boolean {
    if (this.playState === "idle") return false;
    if (this.playState !== "finished") return true;
    const fill = this.options.fill ?? "none";
    return this.rate > 0 ? fill === "forwards" || fill === "both" : fill === "backwards" || fill === "both";
  }

  get effect() {
    return {
      getComputedTiming: () => ({
        delay: this.delay,
        duration: this.options.duration,
        endTime: this.endTime,
        localTime: this.time,
      }),
    };
  }

  get currentTime(): number | null {
    return this.time;
  }

  set currentTime(value: number | null) {
    if (value !== null) this.seek(value);
  }

  get playbackRate(): number {
    return this.rate;
  }

  /** Spec "set the playback rate": the current time is kept. */
  set playbackRate(value: number) {
    const previous = this.time;
    this.rate = value;
    if (previous !== null) this.seek(previous);
  }

  /** Spec "seamlessly update the playback rate". */
  updatePlaybackRate(value: number): void {
    this.rate = value;
    if (this.playState === "finished") {
      this.seek(this.clock);
      return;
    }
    if (value < 0 && this.time !== null) this.reversedFrom.push(this.time);
  }

  play(): void {
    if (this.playState === "running") return;
    const from = this.time ?? 0;
    // Auto-rewind: played backwards from its start, it begins at its end.
    this.seek(this.rate < 0 && from <= 0 ? this.endTime : from);
  }

  cancel(): void {
    if (this.playState === "idle") return;
    this.playState = "idle";
    this.time = null;
    this.oncancel?.();
  }

  commitStyles(): void {
    this.target.mutations.push("commitStyles");
  }

  /** `ms` of wall time pass for this animation (frames, or a minute of play). */
  advance(ms: number): void {
    if (this.playState === "idle" || this.playState === "paused") return;
    this.clock += ms * this.rate;
    if (this.playState === "finished") return;
    if (this.rate > 0 ? this.clock >= this.endTime : this.clock <= 0) {
      this.time = this.rate > 0 ? this.endTime : 0;
      this.playState = "finished";
      this.onfinish?.();
      return;
    }
    this.time = this.clock;
  }

  /** Spec "set the current time", then "update the finished state" (did seek). */
  private seek(to: number): void {
    this.time = to;
    if (this.rate > 0 ? to >= this.endTime : to <= 0) {
      // Held at its end; its start time - the clock - is untouched.
      this.playState = "finished";
      return;
    }
    this.clock = to;
    this.playState = "running";
    if (this.rate < 0) this.reversedFrom.push(to);
  }
}

interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** An element of the real interface: it takes animations and records any write. */
class FakeElement {
  readonly name: string;
  tagName = "DIV";
  id = "";
  isConnected = true;
  parentElement: FakeElement | null = null;
  readonly childNodes = [{ nodeType: 3, textContent: "Damage me" }];
  readonly animations: FakeAnimation[] = [];
  readonly mutations: string[] = [];
  readonly computed = {
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
  readonly box: Box;
  readonly style = new Proxy({} as Record<string, string>, {
    set: (_target, property) => {
      this.mutations.push(`style.${String(property)}`);
      return true;
    },
  });
  readonly classList = {
    add: () => this.mutations.push("classList.add"),
    remove: () => this.mutations.push("classList.remove"),
    toggle: () => this.mutations.push("classList.toggle"),
    replace: () => this.mutations.push("classList.replace"),
  };
  private flags = { inert: false, hidden: false, className: "" };

  constructor(name: string, box: Box) {
    this.name = name;
    this.box = box;
  }

  get center(): { x: number; y: number } {
    return { x: this.box.left + this.box.width / 2, y: this.box.top + this.box.height / 2 };
  }

  get inert(): boolean {
    return this.flags.inert;
  }

  set inert(value: boolean) {
    this.mutations.push("inert");
    this.flags.inert = value;
  }

  get hidden(): boolean {
    return this.flags.hidden;
  }

  set hidden(value: boolean) {
    this.mutations.push("hidden");
    this.flags.hidden = value;
  }

  get className(): string {
    return this.flags.className;
  }

  set className(value: string) {
    this.mutations.push("className");
    this.flags.className = value;
  }

  getBoundingClientRect() {
    const { left, top, width, height } = this.box;
    return { left, top, width, height, x: left, y: top, right: left + width, bottom: top + height };
  }

  closest(): null {
    return null;
  }

  getAttribute(): null {
    return null;
  }

  setAttribute(name: string): void {
    this.mutations.push(`setAttribute ${name}`);
  }

  removeAttribute(name: string): void {
    this.mutations.push(`removeAttribute ${name}`);
  }

  toggleAttribute(name: string): void {
    this.mutations.push(`toggleAttribute ${name}`);
  }

  contains(other: unknown): boolean {
    return other === this;
  }

  animate(keyframes: Keyframe[], options: KeyframeAnimationOptions): FakeAnimation {
    const animation = new FakeAnimation(this, keyframes, options);
    this.animations.push(animation);
    return animation;
  }

  getAnimations(): FakeAnimation[] {
    return this.animations.filter((animation) => animation.relevant);
  }
}

let page: FakeElement[] = [];
const appRoot = new FakeElement("#root", { left: 0, top: 0, width: 1280, height: 800 });
const overlay = { contains: () => false };

function inside(box: Box, x: number, y: number): boolean {
  return x >= box.left && x <= box.left + box.width && y >= box.top && y <= box.top + box.height;
}

Object.assign(globalThis, {
  window: {
    innerWidth: 1280,
    innerHeight: 800,
    scrollY: 0,
    // Resolved at call time, so `mock.timers` drives the repair's clock.
    setTimeout: (callback: () => void, ms?: number) => setTimeout(callback, ms),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
  },
  document: {
    elementsFromPoint: (x: number, y: number) =>
      page.filter((el) => el.isConnected && inside(el.box, x, y)).reverse(),
    body: { querySelectorAll: () => page.filter((el) => el.isConnected) },
    getElementById: (id: string) => (id === "root" ? appRoot : null),
  },
  getComputedStyle: (el: FakeElement) => el.computed,
});

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

/** `count` 20 px tiles of text, side by side on a 1280 x 800 page. */
function layout(count: number): FakeElement[] {
  page = Array.from({ length: count }, (_, i) => {
    const left = (i % 60) * 21;
    const top = 60 + Math.floor(i / 60) * 21;
    return new FakeElement(`tile ${i}`, { left, top, width: 20, height: 20 });
  });
  return page;
}

function newDamage(reducedMotion = false): ScreenDamage {
  return new ScreenDamage({ overlayRoot: overlay as unknown as HTMLElement, reducedMotion, theme: THEME });
}

/** A shot that leaves a crater. */
function scar(damage: ScreenDamage, el: FakeElement) {
  const { x, y } = el.center;
  return damage.scorch(x, y, { radius: 3, damage: 0.34, now: 0 });
}

/** A shot that brings the element down at once. */
function shootDown(damage: ScreenDamage, el: FakeElement) {
  const { x, y } = el.center;
  return damage.scorch(x, y, { radius: 0, damage: Infinity, now: 0 });
}

/** Wall time passes for every animation on the page. */
function frames(ms: number, elements: FakeElement[] = [...page, appRoot]): void {
  for (const el of elements) for (const animation of el.animations) animation.advance(ms);
}

/** The opacity an element shows once its animations reached their end - CSS composition of a <number>. */
function settledOpacity(el: FakeElement): number {
  let value = Number(el.computed.opacity);
  for (const animation of el.getAnimations()) {
    const fades = animation.keyframes.filter((frame) => frame.opacity !== undefined);
    if (fades.length === 0) continue;
    const end = Number(fades[fades.length - 1].opacity);
    value = animation.composite === "replace" ? end : value + end;
  }
  return Math.min(1, Math.max(0, value));
}

function assertIntact(elements: FakeElement[]): void {
  for (const el of elements) {
    assert.deepEqual(
      el.getAnimations().map((animation) => animation.keyframes),
      [],
      `${el.name}: an animation outlived the game`,
    );
    assert.deepEqual(el.mutations, [], `${el.name}: the interface was written to`);
    assert.equal(el.inert, false, `${el.name} is inert`);
    assert.equal(el.hidden, false, `${el.name} is hidden`);
  }
}

// ── The decisions, without a page ────────────────────────────────────────────

const hasTransform = (effect: { keyframes: Keyframe[] }) => effect.keyframes.some((frame) => frame.transform !== undefined);
const hasOpacity = (effect: { keyframes: Keyframe[] }) => effect.keyframes.some((frame) => frame.opacity !== undefined);

test("crumbleEffects: the movement is added to the element's transform, the fade replaces its opacity down to 0", () => {
  const effects = crumbleEffects({
    reducedMotion: false,
    rect: { top: 100, height: 40 },
    viewportHeight: 800,
    opacity: 0.9,
    delay: 120,
    random: () => 0.5,
  });
  const moves = effects.filter(hasTransform);
  const fades = effects.filter(hasOpacity);
  assert.equal(moves.length, 1);
  assert.equal(fades.length, 1);
  assert.equal(effects.filter((effect) => hasTransform(effect) && hasOpacity(effect)).length, 0, "no effect carries both");
  assert.equal(moves[0].options.composite, "add");
  assert.equal(fades[0].options.composite, "replace");
  const opacities = fades[0].keyframes.map((frame) => frame.opacity);
  assert.equal(opacities[0], 0.9, "the fade starts from the element's own opacity");
  assert.equal(opacities[opacities.length - 1], 0);
  // One timing for both, so the fade stays in step with the fall.
  for (const key of ["duration", "delay", "easing", "fill"] as const) {
    assert.equal(moves[0].options[key], fades[0].options[key], key);
  }
  assert.equal(moves[0].options.fill, "forwards");
  assert.equal(moves[0].options.delay, 120);
  // It lands below the bottom of the screen: 800 - 100 + 40 + 120.
  assert.match(String(moves[0].keyframes[moves[0].keyframes.length - 1].transform), /^translate\(0px, 860px\)/);
});

test("crumbleEffects with reduced motion: no movement, a fade to a ghost of the element's own opacity", () => {
  const [fade, ...rest] = crumbleEffects({
    reducedMotion: true,
    rect: { top: 100, height: 40 },
    viewportHeight: 800,
    opacity: 0.5,
    delay: 0,
  });
  assert.equal(rest.length, 0);
  assert.equal(hasTransform(fade), false);
  assert.equal(fade.options.composite, "replace");
  assert.deepEqual(
    fade.keyframes.map((frame) => frame.opacity),
    [0.5, 0.5 * 0.08],
  );
});

test("returnPlan: back from where the element is, however long ago it fell, at the repair's speed", () => {
  // Finished a minute ago: the browser holds it at its end.
  assert.deepEqual(returnPlan({ currentTime: 1300, endTime: 1300, delay: 0 }), { kind: "reverse", from: 1300, rate: -RESTORE_SPEED });
  // An engine that reports the time past the end still reverses from the end.
  assert.deepEqual(returnPlan({ currentTime: 60_000, endTime: 1300, delay: 0 }), { kind: "reverse", from: 1300, rate: -RESTORE_SPEED });
  // Still falling: from mid-air.
  assert.deepEqual(returnPlan({ currentTime: 650, endTime: 1900, delay: 600 }), { kind: "reverse", from: 650, rate: -RESTORE_SPEED });
  // Still waiting for its delay (the nuke staggers them), never started, or already cancelled: it is home.
  assert.deepEqual(returnPlan({ currentTime: 400, endTime: 1900, delay: 600 }), { kind: "home" });
  assert.deepEqual(returnPlan({ currentTime: 0, endTime: 1300, delay: 0 }), { kind: "home" });
  assert.deepEqual(returnPlan({ currentTime: null, endTime: 1300, delay: 0 }), { kind: "home" });
});

test("recycling order: gone, then intact, then settled, then scarred, then still falling - the oldest first", () => {
  const state = (overrides: Partial<TrackedState>): TrackedState => ({
    connected: true,
    crumbled: false,
    settled: false,
    craters: 0,
    seq: 0,
    ...overrides,
  });
  const gone = state({ connected: false, craters: 3, seq: 9 });
  const intact = state({ seq: 8 });
  const settled = state({ crumbled: true, settled: true, seq: 7 });
  const scarred = state({ craters: 2, seq: 1 });
  const falling = state({ crumbled: true, seq: 0 });
  assert.deepEqual([gone, intact, settled, scarred, falling].map(recycleRank), [0, 1, 2, 3, 4]);

  const pool = [falling, scarred, settled, intact, gone];
  const order: TrackedState[] = [];
  while (pool.length > 0) {
    const next = pickRecyclable(pool);
    assert.ok(next);
    order.push(next);
    pool.splice(pool.indexOf(next), 1);
  }
  assert.deepEqual(order, [gone, intact, settled, scarred, falling]);

  const older = state({ crumbled: true, settled: true, seq: 3 });
  assert.equal(pickRecyclable([settled, older]), older, "the oldest of a rank first");
  assert.equal(pickRecyclable([]), null);
});

// ── Crumbling ────────────────────────────────────────────────────────────────

test("a fallen element fades out: its movement adds to its own transform, its fade replaces its opacity", () => {
  const [el] = layout(1);
  const damage = newDamage();
  assert.equal(shootDown(damage, el)?.destroyed, true);

  const moves = el.animations.filter((animation) => animation.keyframes.some((frame) => frame.transform !== undefined));
  const fades = el.animations.filter((animation) => animation.keyframes.some((frame) => frame.opacity !== undefined));
  assert.ok(moves.length > 0 && fades.length > 0);
  for (const animation of moves) assert.equal(animation.composite, "add", "the fall composes onto the element's own transform");
  for (const animation of fades) assert.notEqual(animation.composite, "add", "an added opacity can only grow");

  frames(5000);
  assert.equal(settledOpacity(el), 0);
  damage.destroy();
});

test("with reduced motion a destroyed element does not move: it fades to a ghost", () => {
  const [el] = layout(1);
  const damage = newDamage(true);
  assert.equal(shootDown(damage, el)?.destroyed, true);

  assert.ok(el.animations.length > 0);
  for (const animation of el.animations) {
    assert.ok(animation.keyframes.every((frame) => frame.transform === undefined), "no movement");
  }
  frames(5000);
  assert.ok(Math.abs(settledOpacity(el) - 0.08) < 1e-9, `settled at ${settledOpacity(el)}`);
  damage.destroy();
});

// ── The repair ───────────────────────────────────────────────────────────────

test("the repair flies an element home from where it lies, however long ago it fell", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const [early, late] = layout(2);
    const damage = newDamage();
    shootDown(damage, early);
    frames(60_000, [early]);
    shootDown(damage, late);
    frames(650, [late]);

    const repaired = damage.restore(true);
    mock.timers.tick(400);
    for (const animation of [...early.animations, ...late.animations]) {
      assert.equal(animation.reversedFrom.length, 1);
      assert.ok(
        animation.reversedFrom[0] <= animation.endTime,
        `reversed from ${animation.reversedFrom[0]} ms, the fall ends at ${animation.endTime} ms: it sits off-screen before it moves`,
      );
      assert.equal(animation.playbackRate, -RESTORE_SPEED, "the repair's speed is unchanged");
    }
    assert.equal(early.animations[0].reversedFrom[0], early.animations[0].endTime, "home from where it landed");
    assert.equal(late.animations[0].reversedFrom[0], 650, "home from mid-air");

    // Both land within the repair's own time, not cut short by its timeout.
    frames(600);
    assertIntact([early, late]);
    mock.timers.tick(5000);
    await repaired;
  } finally {
    mock.timers.reset();
  }
});

/** A 2D context that keeps the opacity every shape is painted at - and, like a canvas, ignores an alpha outside 0..1. */
function recordingContext() {
  let alpha = 1;
  const saved: number[] = [];
  const painted: number[] = [];
  const paint = () => {
    painted.push(alpha);
  };
  const noop = () => undefined;
  const ctx = {
    painted,
    get globalAlpha() {
      return alpha;
    },
    set globalAlpha(value: number) {
      if (Number.isFinite(value) && value >= 0 && value <= 1) alpha = value;
    },
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    save: () => {
      saved.push(alpha);
    },
    restore: () => {
      alpha = saved.pop() ?? 1;
    },
    beginPath: noop,
    rect: noop,
    clip: noop,
    moveTo: noop,
    lineTo: noop,
    closePath: noop,
    fill: paint,
    stroke: paint,
    fillRect: paint,
  };
  return ctx;
}

test("the craters fade out while the repair flies the interface home", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  // The page has been open a while; the game's own clock stands still once it has ended.
  let wall = 900_000;
  const clock = mock.method(performance, "now", () => wall);
  try {
    const [el] = layout(1);
    const damage = newDamage();
    scar(damage, el);
    const paintedAt = () => {
      const ctx = recordingContext();
      damage.draw(ctx as unknown as CanvasRenderingContext2D, 5000);
      return ctx.painted;
    };
    assert.equal(Math.max(...paintedAt()), 1, "a fresh crater is drawn at full strength");

    const repaired = damage.restore(true);
    wall += 300;
    const halfway = Math.max(...paintedAt());
    assert.ok(halfway > 0.4 && halfway < 0.6, `halfway through the fade the craters are drawn at ${halfway}`);
    wall += 400;
    assert.deepEqual(paintedAt(), [], "the craters are gone before the repair ends");

    mock.timers.tick(5000);
    await repaired;
  } finally {
    clock.mock.restore();
    mock.timers.reset();
  }
});

// ── Tracking is bounded, damage is not ───────────────────────────────────────

test("the interface keeps taking damage once the tracking table is full", () => {
  const tiles = layout(MAX_TRACKED + 40);
  const damage = newDamage();
  for (const el of tiles.slice(0, MAX_TRACKED)) assert.ok(scar(damage, el));
  assert.equal(damage.trackedCount, MAX_TRACKED);
  for (const el of tiles.slice(MAX_TRACKED)) assert.ok(scar(damage, el), `${el.name} took no damage`);
  for (const el of tiles.slice(MAX_TRACKED)) assert.equal(shootDown(damage, el)?.destroyed, true);
  // A long game: wave after wave over the same page, never a dud.
  for (let round = 0; round < 3; round += 1) {
    for (const el of tiles) assert.ok(scar(damage, el) || el.getAnimations().length > 0, `${el.name}, round ${round}`);
  }
  assert.equal(damage.trackedCount, MAX_TRACKED);
  const live = tiles.reduce((count, el) => count + el.getAnimations().length, 0);
  assert.ok(live <= 2 * MAX_TRACKED, `${live} animations alive`);
  damage.destroy();
});

test("a full table forgets what left the page, then an element with no scar left: nothing changes on screen", () => {
  const tiles = layout(MAX_TRACKED + 2);
  const damage = newDamage();
  const [unmounted, unscarred, ...rest] = tiles.slice(0, MAX_TRACKED);
  scar(damage, unmounted);
  // Hit, but no crater to draw (a radius-0 hit, or scars the crater cap evicted).
  const { x, y } = unscarred.center;
  assert.ok(damage.scorch(x, y, { radius: 0, damage: 0.5, now: 0 }));
  for (const el of rest) {
    shootDown(damage, el);
    frames(5000, [el]);
  }
  unmounted.isConnected = false;

  assert.ok(scar(damage, tiles[MAX_TRACKED]));
  assert.equal(damage.trackedCount, MAX_TRACKED, "the unmounted element made room");
  assert.ok(scar(damage, tiles[MAX_TRACKED + 1]));
  assert.equal(damage.trackedCount, MAX_TRACKED);
  assert.ok(
    rest.every((el) => el.getAnimations().length === el.animations.length),
    "every fallen element still lies where it fell: the unscarred one made room",
  );
  damage.destroy();
});

test("a full table first gives back the oldest element that finished falling, exactly as it was", () => {
  const tiles = layout(MAX_TRACKED + 1);
  const damage = newDamage();
  const [oldest, older, ...standing] = tiles.slice(0, MAX_TRACKED);
  shootDown(damage, oldest);
  shootDown(damage, older);
  frames(5000, [oldest, older]);
  for (const el of standing) scar(damage, el);

  assert.ok(scar(damage, tiles[MAX_TRACKED]));
  assert.deepEqual(oldest.getAnimations(), [], "the oldest fallen element is back");
  assert.equal(older.getAnimations().length, older.animations.length, "the next one still lies where it fell");
  assert.deepEqual(oldest.mutations, []);
  assert.equal(shootDown(damage, oldest)?.destroyed, true, "and it can be shot down again");
  assert.deepEqual(older.getAnimations(), [], "making room for it gave back the next oldest");
  damage.destroy();
});

test("a game is paid for the damage to its first MAX_SCORED_ELEMENTS elements: past them the interface crumbles for show", () => {
  const tiles = layout(MAX_SCORED_ELEMENTS + 30);
  const damage = newDamage();
  for (const el of tiles.slice(0, MAX_SCORED_ELEMENTS)) assert.equal(scar(damage, el)?.scored, true, el.name);
  for (const el of tiles.slice(MAX_SCORED_ELEMENTS)) assert.equal(scar(damage, el)?.scored, false, `${el.name} was paid for`);

  // Still tracked since the start: its destruction counts. Recycled and hit again: it does not.
  const survivor = shootDown(damage, tiles[MAX_SCORED_ELEMENTS - 1]);
  assert.equal(survivor?.destroyed, true);
  assert.equal(survivor?.scored, true);
  assert.equal(scar(damage, tiles[0])?.scored, false);

  // A long game: the interface keeps crumbling, the run never counts more
  // destroyed elements than the server accepts (MAX_UI_DESTROYED_PER_RUN).
  let destroyed = 1;
  let counted = 1;
  for (let round = 0; round < 3; round += 1) {
    for (const el of tiles) {
      const result = shootDown(damage, el);
      if (!result?.destroyed) continue;
      destroyed += 1;
      if (result.scored) counted += 1;
    }
    frames(5000);
  }
  assert.ok(destroyed > MAX_SCORED_ELEMENTS, `${destroyed} destroyed`);
  assert.ok(counted > 0 && counted <= MAX_SCORED_ELEMENTS, `${counted} counted`);
  damage.destroy();
});

test("the nuke on a full table recycles too: tracking stays bounded", () => {
  const tiles = layout(MAX_TRACKED + 150);
  const damage = newDamage();
  for (const el of tiles.slice(0, MAX_TRACKED)) scar(damage, el);
  const wiped = damage.crumbleAllVisible(120);
  assert.equal(wiped.length, 120);
  assert.ok(wiped.every((result) => result.destroyed));
  assert.equal(damage.trackedCount, MAX_TRACKED);
  damage.destroy();
});

// ── Whichever way the game ends, the interface is whole again ────────────────

/** A long game: scars, old falls, falls in the air, a nuke on a full table and the page shaking. */
function battlefield(damage: ScreenDamage): FakeElement[] {
  const tiles = layout(MAX_TRACKED + 60);
  for (const el of tiles.slice(0, 100)) scar(damage, el);
  for (const el of tiles.slice(100, 140)) shootDown(damage, el);
  frames(30_000);
  for (const el of tiles.slice(140, 160)) shootDown(damage, el);
  frames(400, tiles.slice(140, 160));
  for (const el of tiles.slice(160, MAX_TRACKED)) scar(damage, el);
  damage.crumbleAllVisible(120);
  frames(200);
  damage.quake();
  assert.ok(tiles.some((el) => el.getAnimations().length > 0));
  return tiles;
}

test("the game ends and the repair flies everything home: nothing is left behind", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const damage = newDamage();
    const tiles = battlefield(damage);
    const repaired = damage.restore(true);
    mock.timers.tick(400);
    frames(1000);
    // Every fallen element landed home: none waited for the repair's timeout.
    assertIntact(tiles);
    mock.timers.tick(5000);
    await repaired;
    assertIntact([...tiles, appRoot]);
    damage.destroy();
    assertIntact([...tiles, appRoot]);
  } finally {
    mock.timers.reset();
  }
});

test("a repair whose frames never come (the tab hidden mid-flight) still ends with everything home", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const damage = newDamage();
    const tiles = battlefield(damage);
    const repaired = damage.restore(true);
    mock.timers.tick(5000);
    await repaired;
    assertIntact([...tiles, appRoot]);
  } finally {
    mock.timers.reset();
  }
});

test("a repair in a hidden tab puts everything back at once: nobody would see the flight", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const doc = globalThis.document as { hidden?: boolean };
  doc.hidden = true;
  try {
    const damage = newDamage();
    const tiles = battlefield(damage);
    let settled = false;
    void damage.restore(true).then(() => {
      settled = true;
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(settled, true, "the repair waited on the timers a hidden tab throttles");
    assertIntact([...tiles, appRoot]);
  } finally {
    delete doc.hidden;
    mock.timers.reset();
  }
});

test("reduced motion: the repair puts everything back at once", async () => {
  const damage = newDamage(true);
  const tiles = battlefield(damage);
  await damage.restore(false);
  assertIntact([...tiles, appRoot]);
});

test("reduced motion switched on or off mid-game: the repair still leaves nothing behind", async () => {
  // Switched on: the overlay asks for no flight on a game that fell with motion.
  const moving = newDamage(false);
  const fell = battlefield(moving);
  await moving.restore(false);
  assertIntact([...fell, appRoot]);
  // Switched off: the overlay asks for the flight on a game that faded to ghosts - they come back at once.
  const still = newDamage(true);
  const faded = battlefield(still);
  await still.restore(true);
  assertIntact([...faded, appRoot]);
});

test("the app's own animations are never touched: only what the game started is cancelled", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const tiles = layout(MAX_TRACKED + 20);
    // The app animates a few elements of its own (a pulse, say) while the game plays over them.
    const pulses = tiles.slice(0, 3).map((el) => el.animate([{ opacity: 1 }, { opacity: 0.5 }], { duration: 600_000 }));
    const damage = newDamage();
    // Scarred, then recycled when the table overflows...
    for (const el of tiles) scar(damage, el);
    assert.ok(pulses.every((pulse) => pulse.playState === "running"), "recycling cancelled an animation of the app");
    // ... brought down, nuked, shaken, flown home, torn down.
    shootDown(damage, tiles[0]);
    shootDown(damage, tiles[2]);
    damage.crumbleAllVisible(120);
    damage.quake();
    frames(400);
    const repaired = damage.restore(true);
    mock.timers.tick(400);
    frames(1000);
    mock.timers.tick(5000);
    await repaired;
    damage.destroy();
    for (const [index, pulse] of pulses.entries()) {
      const el = tiles[index];
      assert.deepEqual(el.getAnimations(), [pulse], `${el.name}: only the app's own animation is left`);
      assert.equal(pulse.playState, "running");
      assert.equal(pulse.playbackRate, 1);
      assert.deepEqual(pulse.reversedFrom, [], `${el.name}: the repair reversed an animation of the app`);
      assert.deepEqual(el.mutations, []);
    }
    assertIntact([...tiles.slice(pulses.length), appRoot]);
  } finally {
    mock.timers.reset();
  }
});

test("an element React replaced or removed mid-game: its fall is cancelled, the node in its place is never touched", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const [landed, falling] = layout(2);
    const damage = newDamage();
    shootDown(damage, landed);
    frames(5000, [landed]);
    shootDown(damage, falling);
    frames(300, [falling]);
    // A re-render puts a new node where the landed one was, and drops the one still in the air.
    landed.isConnected = false;
    falling.isConnected = false;
    const fresh = new FakeElement("tile 0, rendered again", { ...landed.box });
    page = [fresh];
    const repaired = damage.restore(true);
    mock.timers.tick(400);
    frames(1000, [landed, falling, fresh]);
    mock.timers.tick(5000);
    await repaired;
    assertIntact([landed, falling, fresh]);
    assert.deepEqual(fresh.animations, [], "the new node was animated");
  } finally {
    mock.timers.reset();
  }
});

test("a crash, a navigation or the kill switch unmounts the game: destroy() alone restores everything", () => {
  const damage = newDamage();
  const tiles = battlefield(damage);
  damage.destroy();
  assertIntact([...tiles, appRoot]);
  assert.equal(shootDown(damage, tiles[0]), null, "a destroyed game damages nothing");
  assertIntact([...tiles, appRoot]);
});

test("the game unmounted in the middle of the repair: everything is home at once, the repair settles", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const damage = newDamage();
    const tiles = battlefield(damage);
    const repaired = damage.restore(true);
    let settled = false;
    void repaired.then(() => {
      settled = true;
    });
    mock.timers.tick(150);
    frames(100);
    damage.destroy();
    assertIntact([...tiles, appRoot]);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(settled, true, "the repair the overlay awaits settled with the unmount");
  } finally {
    mock.timers.reset();
  }
});
