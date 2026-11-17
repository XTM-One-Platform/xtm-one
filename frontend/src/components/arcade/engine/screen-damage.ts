/**
 * The interface as a battlefield - and how it is put back.
 *
 * Projectiles are hit-tested against the REAL page under the game canvas
 * (`document.elementsFromPoint`), so a missile blows a hole in the message you
 * aimed at and the invaders' fire scars whatever it crosses. Two effects, both
 * cosmetic and both fully reversible:
 *
 *  - Craters are painted on the game canvas, anchored to the element's box
 *    (re-read every frame, so they follow a scroll): a jagged hole showing
 *    deep space through the page, with an ember rim. The DOM is never touched.
 *  - An element that takes enough damage crumbles: Web Animations shake it,
 *    throw it off the bottom of the screen and fade it out (`fill: forwards`).
 *    The movement composes onto the element's own transform (`composite:
 *    add`), the fade replaces its opacity - added, an opacity can only grow
 *    (see `crumbleEffects`). A Web Animation writes no class, no style and no
 *    attribute, so React can neither clobber it nor be confused by it, and
 *    cancelling it gives the element back exactly as it was.
 *
 * Tracking is bounded, damage is not: an element hit while `MAX_TRACKED` are
 * tracked makes room by recycling one of them (see `pickRecyclable`) - given
 * back exactly as it was, and free to be shot down again. What a game is paid
 * for is bounded apart: the damage done to the first `MAX_SCORED_ELEMENTS`
 * elements it tracks (`DamageResult.scored`), the server's own bound.
 *
 * Every animation started on the interface stays registered until it is
 * cancelled, so a game leaves nothing behind whichever way it ends: `restore()`
 * flies every fallen element home from where it is NOW, then cancels the rest;
 * `destroy()` - the unmount a crash, a navigation or the kill switch ends in,
 * mid-repair included - cancels everything at once.
 *
 * Nothing here dispatches an event, focuses, clicks, removes or edits a node.
 */
import type { AchievementId } from "./achievements.ts";
import { MAX_SCORED_ELEMENTS } from "./rules.ts";
import type { ArcadeTheme, DamageKind } from "./types.ts";

const MAX_CRATERS_PER_TARGET = 14;
const MAX_TOTAL_CRATERS = 360;
/**
 * The most elements tracked at once - hitting one more recycles a tracked one.
 * A bound on what the game holds on to (targets, craters, animations), not on
 * what it is paid for: that is `MAX_SCORED_ELEMENTS`, whatever this value.
 */
export const MAX_TRACKED = 320;
/** An element larger than this share of the viewport is a layout container, not a target. */
const MAX_AREA_SHARE = 0.42;
const CRUMBLE_MS = 1300;
/** Reduced motion: no fall, a fade. */
const FADE_MS = 280;
const CRUMBLE_EASING = "cubic-bezier(.5,0,.9,.5)";
/** Reduced motion: what is left of a destroyed element's own opacity. */
const GHOST_OPACITY = 0.08;
/** The repair plays every fall backwards, this many times faster than it fell. */
export const RESTORE_SPEED = 2.8;
const RESTORE_TIMEOUT_MS = 1600;
const CRATER_FADE_MS = 600;
const QUAKE_MS = 420;
const QUAKE_KEYFRAMES: Keyframe[] = [
  { transform: "translate(0px, 0px)" },
  { transform: "translate(-7px, 4px)" },
  { transform: "translate(6px, -5px)" },
  { transform: "translate(-4px, 3px)" },
  { transform: "translate(2px, -1px)" },
  { transform: "translate(0px, 0px)" },
];

const REPLACED_TAGS = new Set([
  "IMG", "SVG", "CANVAS", "VIDEO", "PICTURE", "INPUT", "TEXTAREA", "SELECT", "BUTTON", "HR",
]);

interface Crater {
  x: number;
  y: number;
  r: number;
  born: number;
  /** Unit polygon (x, y pairs) - a jagged circle, fixed per crater. */
  shape: number[];
  /** Star dots inside the hole (x, y pairs in -0.7..0.7). */
  stars: number[];
}

interface Target {
  el: Element;
  kind: DamageKind;
  role: string | null;
  hp: number;
  maxHp: number;
  craters: Crater[];
  crumbled: boolean;
  /** Its fall - the movement and the fade; empty while it stands. */
  effects: Animation[];
  /** When it was last tracked, hit or brought down: the recycling order. */
  seq: number;
  /** One of the first `MAX_SCORED_ELEMENTS` elements the game tracked: its damage counts. */
  scored: boolean;
}

export interface DamageResult {
  kind: DamageKind;
  destroyed: boolean;
  /**
   * Whether the hit counts toward the run (points, `uiDestroyed`): damage to the
   * first `MAX_SCORED_ELEMENTS` elements a game tracks does. Past them - recycled
   * room - the interface still crumbles, for show.
   */
  scored: boolean;
  /** Viewport box at the time of the hit (for debris). */
  rect: { x: number; y: number; w: number; h: number };
  /** Colours to throw debris in. */
  colors: string[];
  achievement: AchievementId | null;
}

interface DamageOptions {
  /** Crater radius in CSS px; 0 = no crater. */
  radius: number;
  /** Hit points taken. `Infinity` crumbles at once. */
  damage: number;
  now: number;
}

/** One Web Animation of a crumble. */
interface CrumbleEffect {
  keyframes: Keyframe[];
  options: KeyframeAnimationOptions;
}

/**
 * The animations that bring an element down. Two effects on one timing: the
 * movement composes onto the element's own transform (`add`: a centred
 * popover falls from where it is centred), the fade REPLACES its opacity.
 * A single effect carrying both used to add the opacity as well - its own
 * `1` plus the keyframe's `0` is still `1` - so fallen elements never faded,
 * and the reduced-motion fade, the only effect there, did nothing.
 */
export function crumbleEffects(opts: {
  reducedMotion: boolean;
  /** The element's box when it was destroyed, in viewport px. */
  rect: { top: number; height: number };
  viewportHeight: number;
  /** The element's own opacity: where the fade starts. */
  opacity: number;
  delay: number;
  random?: () => number;
}): CrumbleEffect[] {
  const own = Number.isFinite(opts.opacity) ? Math.min(1, Math.max(0, opts.opacity)) : 1;
  const timing: KeyframeAnimationOptions = { delay: opts.delay, easing: CRUMBLE_EASING, fill: "forwards" };
  if (opts.reducedMotion) {
    return [
      {
        keyframes: [{ opacity: own }, { opacity: own * GHOST_OPACITY }],
        options: { ...timing, duration: FADE_MS, composite: "replace" },
      },
    ];
  }
  const random = opts.random ?? Math.random;
  const drift = (random() - 0.5) * 180;
  const spin = (random() - 0.5) * 80;
  const fall = opts.viewportHeight - opts.rect.top + opts.rect.height + 120;
  return [
    {
      keyframes: [
        { transform: "translate(0px, 0px) rotate(0deg)", offset: 0 },
        { transform: "translate(-4px, 1px) rotate(-2deg)", offset: 0.06 },
        { transform: "translate(4px, -1px) rotate(2deg)", offset: 0.12 },
        { transform: "translate(-2px, 0px) rotate(-1deg)", offset: 0.18 },
        { transform: `translate(${drift * 0.25}px, ${fall * 0.06}px) rotate(${spin * 0.15}deg)`, offset: 0.32 },
        { transform: `translate(${drift}px, ${fall}px) rotate(${spin}deg)`, offset: 1 },
      ],
      options: { ...timing, duration: CRUMBLE_MS, composite: "add" },
    },
    {
      keyframes: [
        { opacity: own, offset: 0 },
        { opacity: own, offset: 0.32 },
        { opacity: 0, offset: 1 },
      ],
      options: { ...timing, duration: CRUMBLE_MS, composite: "replace" },
    },
  ];
}

/** Where a fall stands when the repair reaches it, read off its animation. */
interface FallState {
  /** `animation.currentTime`: a finished fall is held at its end, a running one is where it is. */
  currentTime: number | null;
  /** The end of the effect: its delay plus its duration. */
  endTime: number;
  /** The effect's delay: until it is over, the element has not left home. */
  delay: number;
}

type ReturnPlan = { kind: "home" } | { kind: "reverse"; from: number; rate: number };

/**
 * How a fallen element goes home: its fall played backwards at `RESTORE_SPEED`,
 * from where the element IS - so the fly-home looks the same whether it fell a
 * second or a minute ago. The repair seeks there before it reverses: on a
 * finished animation `updatePlaybackRate()` resumes from the unconstrained
 * time, i.e. from however long ago it started, and an element that fell a
 * while before the end of the game sat off-screen until the repair's timeout
 * cancelled it into place.
 */
export function returnPlan(fall: FallState): ReturnPlan {
  if (fall.currentTime === null || !Number.isFinite(fall.currentTime)) return { kind: "home" };
  const from = Math.min(Math.max(fall.currentTime, 0), fall.endTime);
  if (from <= fall.delay) return { kind: "home" };
  return { kind: "reverse", from, rate: -RESTORE_SPEED };
}

/** What recycling reads off a tracked element. */
export interface TrackedState {
  /** Still in the page: React may have removed it since it was hit. */
  connected: boolean;
  crumbled: boolean;
  /** Its fall has finished: it lies where it fell. */
  settled: boolean;
  /** Craters still drawn on it. */
  craters: number;
  /** When it was last tracked, hit or brought down: the lower, the older. */
  seq: number;
}

/** How cheaply a tracked element gives its place back, the lowest first. */
export function recycleRank(state: TrackedState): number {
  // Gone from the page: nothing to give back.
  if (!state.connected) return 0;
  // Standing: with no crater left to draw (never scarred, or every scar
  // evicted by the crater cap) nothing changes on screen; with craters, they vanish.
  if (!state.crumbled) return state.craters === 0 ? 1 : 3;
  // Down: one that finished falling is given back where it was; one still in
  // the air would be cut short, so it goes last.
  return state.settled ? 2 : 4;
}

/**
 * The tracked element that makes room for a new one when `MAX_TRACKED` are
 * tracked: the lowest `recycleRank`, the oldest first. Its animations are
 * cancelled and it is forgotten - exactly what it was, and a target again -
 * so damage never stops, while what the game holds on to (targets, craters,
 * animations) stays bounded.
 */
export function pickRecyclable<T extends TrackedState>(states: Iterable<T>): T | null {
  let best: T | null = null;
  let bestRank = Infinity;
  for (const state of states) {
    const rank = recycleRank(state);
    if (rank < bestRank || (rank === bestRank && best !== null && state.seq < best.seq)) {
      best = state;
      bestRank = rank;
    }
  }
  return best;
}

function parseAlpha(colour: string): number {
  const value = colour.trim().toLowerCase();
  if (!value || value === "transparent") return 0;
  const slash = value.match(/\/\s*([\d.]+)(%?)\s*\)$/);
  if (slash) return slash[2] ? parseFloat(slash[1]) / 100 : parseFloat(slash[1]);
  const rgba = value.match(/^rgba?\(([^)]+)\)$/);
  if (rgba) {
    const parts = rgba[1].split(",");
    return parts.length === 4 ? parseFloat(parts[3]) : 1;
  }
  return 1;
}

function hasOwnText(el: Element): boolean {
  for (const node of Array.from(el.childNodes)) {
    if (node.nodeType === 3 && (node.textContent ?? "").trim().length > 0) return true;
  }
  return false;
}

/** SVG shapes hit-test individually; the icon is their `<svg>`. */
function owner(el: Element): Element {
  const svg = (el as SVGElement).ownerSVGElement;
  if (svg) {
    let top: SVGSVGElement = svg;
    while (top.ownerSVGElement) top = top.ownerSVGElement;
    return top;
  }
  return el;
}

function classify(el: Element): { kind: DamageKind; role: string | null } {
  const role = el.closest("[data-msg-role]")?.getAttribute("data-msg-role") ?? null;
  if (el.closest("[data-arcade-target='logo']")) return { kind: "logo", role };
  if (el.closest("[data-arcade-target='composer']")) return { kind: "composer", role };
  if (role) return { kind: "message", role };
  if (el.closest("nav, [data-arcade-target='nav']")) return { kind: "navigation", role };
  const tag = el.tagName.toUpperCase();
  if (tag === "BUTTON" || el.getAttribute("role") === "button") return { kind: "button", role };
  if (tag === "A") return { kind: "link", role };
  if (tag === "SVG") return { kind: "icon", role };
  if (tag === "IMG" || tag === "PICTURE" || tag === "CANVAS" || tag === "VIDEO") return { kind: "image", role };
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return { kind: "input", role };
  if (hasOwnText(el)) return { kind: "text", role };
  return { kind: "panel", role };
}

function achievementFor(target: Target): AchievementId | null {
  switch (target.kind) {
    case "logo":
      return "brand_vandal";
    case "composer":
      return "radio_silence";
    case "navigation":
      return "navigator_down";
    case "message":
      return target.role === "user" ? "self_sabotage" : target.role === "assistant" ? "shoot_the_messenger" : null;
    default:
      return null;
  }
}

function jaggedShape(points: number): number[] {
  const shape: number[] = [];
  for (let i = 0; i < points; i += 1) {
    const angle = (Math.PI * 2 * i) / points;
    const radius = 0.72 + Math.random() * 0.38;
    shape.push(Math.cos(angle) * radius, Math.sin(angle) * radius);
  }
  return shape;
}

function starField(count: number): number[] {
  const stars: number[] = [];
  for (let i = 0; i < count; i += 1) stars.push((Math.random() - 0.5) * 1.3, (Math.random() - 0.5) * 1.3);
  return stars;
}

function safeCancel(animation: Animation): void {
  try {
    animation.cancel();
  } catch {
    /* already gone */
  }
}

function milliseconds(value: CSSNumberish | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function fallState(animation: Animation): FallState {
  const timing = animation.effect?.getComputedTiming();
  return {
    currentTime: milliseconds(animation.currentTime),
    endTime: milliseconds(timing?.endTime) ?? 0,
    delay: timing?.delay ?? 0,
  };
}

function isSettled(target: Target): boolean {
  return target.effects.every((animation) => animation.playState === "finished" || animation.playState === "idle");
}

export class ScreenDamage {
  private readonly overlayRoot: HTMLElement;
  private readonly reducedMotion: boolean;
  private readonly theme: ArcadeTheme;
  private readonly targets = new Map<Element, Target>();
  private readonly significance = new WeakMap<Element, boolean>();
  /** Every animation started on the interface and not cancelled yet. */
  private readonly animations = new Set<Animation>();
  /** The repair's returns still in flight: `destroy()` settles them at once. */
  private readonly returns = new Set<() => void>();
  private totalCraters = 0;
  private fadeStart = 0;
  private seq = 0;
  /** Elements tracked since the game started, recycled ones included: the first `MAX_SCORED_ELEMENTS` are paid for. */
  private admitted = 0;
  private repairing = false;
  private disposed = false;

  constructor(opts: { overlayRoot: HTMLElement; reducedMotion: boolean; theme: ArcadeTheme }) {
    this.overlayRoot = opts.overlayRoot;
    this.reducedMotion = opts.reducedMotion;
    this.theme = opts.theme;
  }

  /** The damageable element at a viewport point, if any. */
  pick(x: number, y: number): Target | null {
    if (this.disposed || this.repairing || x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return null;
    let stack: Element[];
    try {
      stack = document.elementsFromPoint(x, y);
    } catch {
      return null;
    }
    for (const raw of stack) {
      const el = owner(raw);
      if (this.overlayRoot.contains(el)) continue;
      const known = this.targets.get(el);
      if (known) {
        if (known.crumbled) continue;
        return known;
      }
      if (!this.isSignificant(el)) continue;
      return this.admit(el);
    }
    return null;
  }

  /** A projectile hitting the interface at (x, y). */
  scorch(x: number, y: number, opts: DamageOptions): DamageResult | null {
    const target = this.pick(x, y);
    if (!target) return null;
    return this.hit(target, x, y, opts);
  }

  /** An explosion: every element within `radius` takes the damage once. */
  blast(cx: number, cy: number, radius: number, opts: DamageOptions): DamageResult[] {
    const seen = new Set<Target>();
    const results: DamageResult[] = [];
    const samples: [number, number][] = [[cx, cy]];
    for (const ring of [0.55, 1]) {
      for (let i = 0; i < 8; i += 1) {
        const angle = (Math.PI * 2 * i) / 8 + ring;
        samples.push([cx + Math.cos(angle) * radius * ring, cy + Math.sin(angle) * radius * ring]);
      }
    }
    for (const [x, y] of samples) {
      const target = this.pick(x, y);
      if (!target || seen.has(target)) continue;
      seen.add(target);
      const result = this.hit(target, x, y, opts);
      results.push(result);
    }
    return results;
  }

  /**
   * The nuke: crumble up to `max` visible elements at once, innermost first
   * (a parent falling with its children inside would fall twice as fast).
   */
  crumbleAllVisible(max: number): DamageResult[] {
    if (this.disposed || this.repairing) return [];
    const candidates: Element[] = [];
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    for (const el of Array.from(document.body.querySelectorAll("*"))) {
      if (candidates.length > max * 6) break;
      if (this.overlayRoot.contains(el) || (el as SVGElement).ownerSVGElement) continue;
      const rect = el.getBoundingClientRect();
      if (rect.bottom <= 0 || rect.top >= vh || rect.right <= 0 || rect.left >= vw) continue;
      if (this.targets.get(el)?.crumbled) continue;
      if (this.isSignificant(el)) candidates.push(el);
    }
    const hasInnerCandidate = new Set<Element>();
    for (const el of candidates) {
      let parent = el.parentElement;
      for (let depth = 0; parent && depth < 40; depth += 1) {
        hasInnerCandidate.add(parent);
        parent = parent.parentElement;
      }
    }
    const leaves = candidates.filter((el) => !hasInnerCandidate.has(el));
    const step = Math.max(1, leaves.length / max);
    const chosen: Element[] = [];
    for (let i = 0; i < leaves.length && chosen.length < max; i += step) chosen.push(leaves[Math.floor(i)]);
    return chosen.map((el, index) => {
      const target = this.targets.get(el) ?? this.admit(el);
      const rect = el.getBoundingClientRect();
      return this.crumble(target, rect, Math.random() * 600 + index * 2);
    });
  }

  /** The whole app trembles for a moment (a boss down, the nuke). */
  quake(): void {
    if (this.disposed || this.repairing || this.reducedMotion || window.scrollY !== 0) return;
    const root = document.getElementById("root");
    if (!root || typeof root.animate !== "function") return;
    try {
      const animation = root.animate(QUAKE_KEYFRAMES, { duration: QUAKE_MS, composite: "add" });
      this.animations.add(animation);
      animation.onfinish = () => this.animations.delete(animation);
    } catch {
      /* the canvas shake is enough */
    }
  }

  /** How many elements are currently down. */
  get destroyedCount(): number {
    let count = 0;
    for (const target of this.targets.values()) if (target.crumbled) count += 1;
    return count;
  }

  /** How many elements are tracked: never more than `MAX_TRACKED`. */
  get trackedCount(): number {
    return this.targets.size;
  }

  draw(ctx: CanvasRenderingContext2D, now: number): void {
    if (this.totalCraters === 0) return;
    // The repair's fade runs on the wall clock `restore()` read: `now` is the
    // game's own, which stands still once the game has ended.
    const fade = this.fadeStart ? Math.max(0, 1 - (performance.now() - this.fadeStart) / CRATER_FADE_MS) : 1;
    if (fade <= 0) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    for (const [el, target] of this.targets) {
      if (target.crumbled || target.craters.length === 0) continue;
      if (!el.isConnected) {
        this.release(target);
        continue;
      }
      const rect = el.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw) continue;
      ctx.save();
      ctx.beginPath();
      ctx.rect(rect.left, rect.top, rect.width, rect.height);
      ctx.clip();
      for (const crater of target.craters) this.drawCrater(ctx, rect.left + crater.x, rect.top + crater.y, crater, now, fade);
      ctx.restore();
    }
  }

  /**
   * Put the interface back: every fallen element flies home (its fall played
   * backwards from where it is, then cancelled), the craters fade out, and
   * whatever else is still running is cancelled. No damage is taken meanwhile.
   * A hidden tab paints no frame, so nobody would see the flight: everything
   * is put back at once instead of waiting on timers the browser throttles.
   */
  async restore(animated: boolean): Promise<void> {
    this.repairing = true;
    this.fadeStart = performance.now();
    if (animated && !this.reducedMotion && !this.disposed && !document.hidden) {
      const fallen = [...this.targets.values()].filter((target) => target.effects.length > 0).sort((a, b) => a.seq - b.seq);
      await Promise.all(fallen.map((target, index) => this.flyHome(target, Math.min(400, index * 12))));
    }
    this.cancelAll();
    this.clear();
    this.repairing = false;
  }

  /** Immediate teardown: nothing may outlive the game. Idempotent. */
  destroy(): void {
    this.disposed = true;
    for (const settle of [...this.returns]) settle();
    this.cancelAll();
    this.clear();
  }

  private clear(): void {
    this.targets.clear();
    this.totalCraters = 0;
  }

  private cancelAll(): void {
    for (const animation of this.animations) safeCancel(animation);
    this.animations.clear();
    for (const target of this.targets.values()) target.effects = [];
  }

  private cancel(animation: Animation): void {
    safeCancel(animation);
    this.animations.delete(animation);
  }

  /** One fallen element flies home: every effect of its fall, in step. */
  private async flyHome(target: Target, stagger: number): Promise<void> {
    await Promise.all(target.effects.map((animation) => this.reverse(animation, stagger)));
  }

  private reverse(animation: Animation, stagger: number): Promise<void> {
    return new Promise<void>((resolve) => {
      let settled = false;
      const timers: number[] = [];
      const done = () => {
        if (settled) return;
        settled = true;
        for (const id of timers) window.clearTimeout(id);
        this.returns.delete(done);
        this.cancel(animation);
        resolve();
      };
      this.returns.add(done);
      try {
        animation.oncancel = done;
        timers.push(
          window.setTimeout(() => {
            try {
              const plan = returnPlan(fallState(animation));
              if (plan.kind === "home") {
                done();
                return;
              }
              // Armed only now: a fall that ends during the stagger is not home.
              animation.onfinish = done;
              animation.currentTime = plan.from;
              // Set, not `updatePlaybackRate()`: that one would resume a
              // finished fall from its unconstrained time (see `returnPlan`).
              animation.playbackRate = plan.rate;
              animation.play();
            } catch {
              done();
            }
          }, stagger),
        );
        timers.push(window.setTimeout(done, RESTORE_TIMEOUT_MS + 400));
      } catch {
        done();
      }
    });
  }

  private isSignificant(el: Element): boolean {
    const cached = this.significance.get(el);
    if (cached !== undefined) return cached;
    const result = this.computeSignificance(el);
    this.significance.set(el, result);
    return result;
  }

  private computeSignificance(el: Element): boolean {
    const tag = el.tagName.toUpperCase();
    if (tag === "HTML" || tag === "BODY" || tag === "MAIN") return false;
    if (el.id === "root" || el.id === "app-content") return false;
    if (el.contains(this.overlayRoot)) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 6 || rect.height < 6) return false;
    if (rect.width * rect.height > window.innerWidth * window.innerHeight * MAX_AREA_SHARE) return false;
    let style: CSSStyleDeclaration;
    try {
      style = getComputedStyle(el);
    } catch {
      return false;
    }
    if (style.visibility === "hidden" || style.display === "contents" || parseFloat(style.opacity) < 0.05) return false;
    if (REPLACED_TAGS.has(tag)) return true;
    if (hasOwnText(el)) return true;
    if (style.backgroundImage && style.backgroundImage !== "none") return true;
    if (parseAlpha(style.backgroundColor) > 0.04) return true;
    const border = parseFloat(style.borderTopWidth) + parseFloat(style.borderLeftWidth);
    return border > 0 && parseAlpha(style.borderTopColor) > 0.08;
  }

  /** Track a new element, recycling a tracked one first when the table is full. */
  private admit(el: Element): Target {
    if (this.targets.size >= MAX_TRACKED) this.makeRoom();
    return this.track(el);
  }

  private makeRoom(): void {
    const states: (TrackedState & { target: Target })[] = [];
    for (const target of this.targets.values()) {
      // Elements that left the page cost nothing to forget: all of them go.
      if (!target.el.isConnected) {
        this.release(target);
        continue;
      }
      states.push({
        target,
        connected: true,
        crumbled: target.crumbled,
        settled: target.crumbled && isSettled(target),
        craters: target.craters.length,
        seq: target.seq,
      });
    }
    if (this.targets.size < MAX_TRACKED) return;
    const recycled = pickRecyclable(states);
    if (recycled) this.release(recycled.target);
  }

  /** Give a tracked element back: its fall cancelled, its craters gone, forgotten. */
  private release(target: Target): void {
    for (const animation of target.effects) this.cancel(animation);
    target.effects = [];
    this.totalCraters -= target.craters.length;
    target.craters = [];
    this.targets.delete(target.el);
  }

  private track(el: Element): Target {
    const rect = el.getBoundingClientRect();
    const maxHp = Math.min(9, Math.max(2, Math.round(Math.sqrt(rect.width * rect.height) / 45)));
    const { kind, role } = classify(el);
    const scored = this.admitted < MAX_SCORED_ELEMENTS;
    this.admitted += 1;
    const target: Target = { el, kind, role, hp: maxHp, maxHp, craters: [], crumbled: false, effects: [], seq: ++this.seq, scored };
    this.targets.set(el, target);
    return target;
  }

  private hit(target: Target, x: number, y: number, opts: DamageOptions): DamageResult {
    target.seq = ++this.seq;
    const rect = target.el.getBoundingClientRect();
    if (opts.radius > 0 && Number.isFinite(opts.damage)) this.addCrater(target, x - rect.left, y - rect.top, opts.radius, opts.now);
    target.hp -= opts.damage;
    if (target.hp <= 0) return this.crumble(target, rect, 0);
    return {
      kind: target.kind,
      destroyed: false,
      scored: target.scored,
      rect: { x: rect.left, y: rect.top, w: rect.width, h: rect.height },
      colors: [],
      achievement: null,
    };
  }

  private addCrater(target: Target, x: number, y: number, r: number, now: number): void {
    if (target.craters.length >= MAX_CRATERS_PER_TARGET) {
      target.craters.shift();
      this.totalCraters -= 1;
    }
    if (this.totalCraters >= MAX_TOTAL_CRATERS) this.evictOldestCrater();
    target.craters.push({
      x,
      y,
      r,
      born: now,
      shape: jaggedShape(9 + Math.floor(Math.random() * 5)),
      stars: starField(r > 8 ? 4 : 2),
    });
    this.totalCraters += 1;
  }

  private evictOldestCrater(): void {
    let oldest: Target | null = null;
    for (const target of this.targets.values()) {
      if (target.craters.length === 0) continue;
      if (!oldest || target.craters[0].born < oldest.craters[0].born) oldest = target;
    }
    if (oldest) {
      oldest.craters.shift();
      this.totalCraters -= 1;
    }
  }

  private crumble(target: Target, rect: DOMRect, delay: number): DamageResult {
    target.crumbled = true;
    target.seq = ++this.seq;
    this.totalCraters -= target.craters.length;
    target.craters = [];
    const colors: string[] = [];
    let opacity = 1;
    try {
      const style = getComputedStyle(target.el);
      if (parseAlpha(style.backgroundColor) > 0.1) colors.push(style.backgroundColor);
      if (parseAlpha(style.color) > 0.1) colors.push(style.color);
      opacity = parseFloat(style.opacity);
    } catch {
      /* debris falls back to the theme, the fade starts from opaque */
    }
    const el = target.el as HTMLElement;
    if (!this.disposed && typeof el.animate === "function") {
      const effects = crumbleEffects({
        reducedMotion: this.reducedMotion,
        rect,
        viewportHeight: window.innerHeight,
        opacity,
        delay,
      });
      for (const effect of effects) {
        try {
          const animation = el.animate(effect.keyframes, effect.options);
          target.effects.push(animation);
          this.animations.add(animation);
        } catch {
          /* the element simply stops taking hits */
        }
      }
    }
    return {
      kind: target.kind,
      destroyed: true,
      scored: target.scored,
      rect: { x: rect.left, y: rect.top, w: rect.width, h: rect.height },
      colors,
      achievement: achievementFor(target),
    };
  }

  private drawCrater(ctx: CanvasRenderingContext2D, cx: number, cy: number, crater: Crater, now: number, fade: number): void {
    const { r, shape } = crater;
    const age = now - crater.born;
    ctx.globalAlpha = fade;
    // Soot ring.
    ctx.beginPath();
    for (let i = 0; i < shape.length; i += 2) {
      const x = cx + shape[i] * r * 1.35;
      const y = cy + shape[i + 1] * r * 1.35;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.fillStyle = this.theme.isLight ? "rgba(40, 24, 10, 0.18)" : "rgba(0, 0, 0, 0.35)";
    ctx.fill();
    // The hole: deep space through the page.
    ctx.beginPath();
    for (let i = 0; i < shape.length; i += 2) {
      const x = cx + shape[i] * r;
      const y = cy + shape[i + 1] * r;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.fillStyle = this.theme.space;
    ctx.fill();
    // Ember rim, bright when fresh, cooling to a glow.
    const heat = Math.max(0.35, 1 - age / 1600);
    ctx.lineWidth = Math.max(1.2, r * 0.2);
    ctx.strokeStyle = this.theme.ember;
    ctx.globalAlpha = fade * heat;
    ctx.stroke();
    // A few stars.
    ctx.globalAlpha = fade * 0.9;
    ctx.fillStyle = "rgba(255, 255, 255, 0.85)";
    for (let i = 0; i < crater.stars.length; i += 2) {
      ctx.fillRect(cx + crater.stars[i] * r * 0.6, cy + crater.stars[i + 1] * r * 0.6, 1.2, 1.2);
    }
    ctx.globalAlpha = 1;
  }
}
