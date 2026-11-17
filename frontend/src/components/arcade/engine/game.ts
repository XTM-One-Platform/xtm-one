/**
 * The waiting arcade's game: Space Invaders over the whole app.
 *
 * A fixed-timestep simulation (120 Hz) drawn on a full-viewport canvas that
 * sits above the interface. The core is the original cabinet's: a 5 x 11
 * formation marching to a four-note beat and speeding up as it thins out,
 * three invader types, three enemy shot types, one shot at a time, the
 * mystery saucer and its shot-count secret, a bonus base at 1,500. Around it:
 * a combo multiplier, capsules, missiles, a boss every fifth wave, secret
 * codes, rivals from the weekly board and achievements.
 *
 * The interface underneath is part of the battlefield through `ScreenDamage`
 * (craters painted here, crumbling through Web Animations, all reversible).
 *
 * The engine owns no React state and no network: it reports through
 * `callbacks` (a HUD snapshot ~10 times a second, discrete events, the final
 * run) and the overlay does the rest.
 */
import { CHEAT_ACHIEVEMENTS, isUntouchedRun, statAchievements, type AchievementId } from "./achievements.ts";
import { ArcadeAudio } from "./audio.ts";
import { SecretCodeListener, playTimeAchievements, type SecretCode } from "./easter-eggs.ts";
import {
  FIRE_COOLDOWN_MS,
  FORMATION_COLUMNS,
  FORMATION_ROWS,
  INVADER_POINTS,
  MAX_LIVES,
  MAX_MISSILES,
  MAX_SCORED_ELEMENTS,
  MINION_POINTS,
  MINIONS_PER_SPLIT,
  MISSILE_REGEN_MS,
  OVERFLOW_SPLIT_THRESHOLDS,
  PACIFIST_BONUS,
  PAYLOAD_INTERVAL_MS,
  PAYLOAD_POINTS,
  POWER_UP_DURATIONS_MS,
  RAPID_FIRE_COOLDOWN_MS,
  SPREAD_ANGLES,
  START_LIVES,
  START_MISSILES,
  THROTTLED_FIRE_COOLDOWN_MS,
  UFO_DUCK_POINTS,
  UFO_PI_POINTS,
  UI_CRATER_POINTS,
  UI_DESTROYED_POINTS,
  NUKE_UI_POINTS,
  bossMaxHp,
  bossPoints,
  dropChance,
  extraLivesEarned,
  isLegendaryShot,
  multiplierFor,
  pickPowerUp,
  stepIntervalMs,
  survivalBonus,
  ufoPoints,
  waveClearBonus,
  waveConfig,
  type BossKind,
  type InvaderKind,
  type PowerUpKind,
  type WaveConfig,
} from "./rules.ts";
import { ScreenDamage, type DamageResult } from "./screen-damage.ts";
import { SpriteAtlas, spriteSize, type Palette, type SpriteName } from "./sprites.ts";
import type {
  ArcadeCanvasLabels,
  ArcadeGameOptions,
  ArcadeRivalInfo,
  ArcadeTheme,
  GamePhase,
  HudSnapshot,
  RunEndReason,
} from "./types.ts";

const STEP_MS = 1000 / 120;
const MAX_FRAME_MS = 100;
const HUD_INTERVAL_MS = 100;
const ENTER_MS = 850;
const DYING_MS = 1400;
const WAVE_CLEAR_MS = 1700;
const BOSS_INTRO_MS = 2300;
const GAME_OVER_MS = 1700;
const MISSION_MS = 1600;
const RESPAWN_SHIELD_MS = 2000;
const IDLE_PAUSE_MS = 45_000;
const PACIFIST_MS = 30_000;
const NUKE_COUNTDOWN_MS = 3000;
const HUD_TOP = 48;
const MAX_PARTICLES = 700;
const MAX_POPUPS = 40;

type EnemyShotKind = "rolling" | "plunger" | "squiggly" | "boss" | "payload";
type UfoVariant = "classic" | "duck";

interface Invader {
  col: number;
  row: number;
  kind: InvaderKind;
  alive: boolean;
  hp: number;
  /** Entrance flight. */
  fromX: number;
  fromY: number;
  enterDelay: number;
  hitFlash: number;
}

interface PlayerShot {
  id: number;
  volley: number;
  x: number;
  y: number;
  vx: number;
  vy: number;
  laser: boolean;
  pierce: number;
  sudo: boolean;
  hit: boolean;
  lastProbeY: number;
  scorched: boolean;
}

interface EnemyShot {
  kind: EnemyShotKind;
  x: number;
  y: number;
  vx: number;
  vy: number;
  age: number;
  hp: number;
  scarred: boolean;
  lastProbeY: number;
}

interface Missile {
  x: number;
  y: number;
  vx: number;
  vy: number;
  speed: number;
  target: { x: number; y: number } | null;
  startY: number;
  lastProbeY: number;
  trail: number[];
}

interface Capsule {
  x: number;
  y: number;
  vy: number;
  kind: PowerUpKind;
  age: number;
}

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number;
  maxLife: number;
  color: string;
  size: number;
  gravity: number;
}

interface Popup {
  x: number;
  y: number;
  text: string;
  life: number;
  maxLife: number;
  color: string;
  size: number;
}

interface Burst {
  x: number;
  y: number;
  life: number;
  color: string;
  scale: number;
}

interface Ufo {
  x: number;
  y: number;
  dir: number;
  speed: number;
  variant: UfoVariant;
}

interface Decoy {
  x: number;
  y: number;
  life: number;
  fireTimer: number;
}

interface Minion {
  x: number;
  y: number;
  vx: number;
  vy: number;
  hp: number;
  frame: number;
  fireTimer: number;
}

interface Boss {
  kind: BossKind;
  x: number;
  y: number;
  targetY: number;
  hp: number;
  maxHp: number;
  t: number;
  flash: number;
  fireTimer: number;
  burstLeft: number;
  specialTimer: number;
  decoys: Decoy[];
  minions: Minion[];
  splits: number;
  dying: number;
}

interface InputState {
  left: boolean;
  right: boolean;
  fire: boolean;
  pointerX: number | null;
  pointerFire: boolean;
  lastPointerMove: number;
}

function hsl(h: number, s: number, l: number, a = 1): string {
  return a >= 1 ? `hsl(${h} ${s}% ${l}%)` : `hsla(${h}, ${s}%, ${l}%, ${a})`;
}

function easeOutCubic(t: number): number {
  return 1 - Math.pow(1 - t, 3);
}

function rectsOverlap(ax: number, ay: number, aw: number, ah: number, bx: number, by: number, bw: number, bh: number): boolean {
  return ax < bx + bw && ax + aw > bx && ay < by + bh && ay + ah > by;
}

const ARCADE_FONT = "'IBM Plex Mono', 'Courier New', ui-monospace, monospace";

export class ArcadeGame {
  private readonly canvas: HTMLCanvasElement;
  private readonly ctx: CanvasRenderingContext2D;
  private theme: ArcadeTheme;
  private readonly labels: ArcadeCanvasLabels;
  private readonly reducedMotion: boolean;
  private readonly opts: ArcadeGameOptions;
  private readonly atlas = new SpriteAtlas();
  private readonly damage: ScreenDamage;
  readonly audio: ArcadeAudio;
  private readonly secrets = new SecretCodeListener();

  // Geometry (recomputed on resize).
  private W = 0;
  private H = 0;
  private px = 3;
  private colSpacing = 48;
  private rowSpacing = 42;
  private bandLeft = 0;
  private bandRight = 0;
  private stepX = 8;
  private dropY = 16;
  private groundY = 0;
  private playerY = 0;
  private cellSpriteW = 36;

  // Loop.
  private raf = 0;
  private last = 0;
  private acc = 0;
  private now = 0;
  private hudTimer = 0;
  private running = false;
  private paused = false;
  private destroyed = false;
  private crashed = false;
  private ended = false;

  // Run state.
  private phase: GamePhase = "entering";
  private phaseTimer = 0;
  private wave = 0;
  private config: WaveConfig = waveConfig(1);
  private score = 0;
  private lives = START_LIVES;
  private livesAwarded = 0;
  private missiles = START_MISSILES;
  private missileRegen = 0;
  private combo = 0;
  private maxCombo = 0;
  private kills = 0;
  private shots = 0;
  private hits = 0;
  private volleys = 0;
  private uiDestroyed = 0;
  private bosses = 0;
  private ufoKills = 0;
  private playMs = 0;
  private cheat = false;
  private godMode = false;
  private rainbow = false;
  private waveStartedAt = 0;
  private livesLostThisWave = 0;
  private cleanWaves = 0;
  private lastShotAt = 0;
  private lastInputAt = 0;
  /** False until the first input: a game opened from the waiting panel does
   *  not start shooting at someone who has not touched the controls yet. */
  private engaged = false;
  private pacifistDone = false;
  private personalBestAnnounced = false;
  private rivals: ArcadeRivalInfo[];
  private readonly achievements = new Set<AchievementId>();
  private readonly unlocked: ReadonlySet<string>;

  // Entities.
  private invaders: Invader[] = [];
  private formationX = 0;
  private formationY = 0;
  private dir = 1;
  private stepTimer = 0;
  private marchIndex = 0;
  private legFrame = 0;
  private hopUntil = 0;
  private playerX = 0;
  private playerAlive = true;
  private invulnerableUntil = 0;
  private shieldUntil = 0;
  private playerShots: PlayerShot[] = [];
  private readonly volleyHits = new Set<number>();
  private nextShotId = 1;
  private lastCrumbleSound = 0;
  private enemyShots: EnemyShot[] = [];
  private enemyFireTimer = 0;
  private shotTypeIndex = 0;
  private missilesInFlight: Missile[] = [];
  private capsules: Capsule[] = [];
  private particles: Particle[] = [];
  private popups: Popup[] = [];
  private bursts: Burst[] = [];
  private ufo: Ufo | null = null;
  private ufoTimer = 0;
  private boss: Boss | null = null;
  private effects = new Map<PowerUpKind, number>();
  private throttleUntil = 0;
  private reversedUntil = 0;
  private nukeAt = 0;
  private flash = 0;
  private shakeMag = 0;
  private missionQueue: Invader[] = [];
  private missionTimer = 0;
  private readonly input: InputState = {
    left: false,
    right: false,
    fire: false,
    pointerX: null,
    pointerFire: false,
    lastPointerMove: 0,
  };

  constructor(opts: ArcadeGameOptions) {
    this.opts = opts;
    this.canvas = opts.canvas;
    const ctx = opts.canvas.getContext("2d");
    if (!ctx) throw new Error("Canvas 2D is unavailable");
    this.ctx = ctx;
    this.theme = opts.theme;
    this.labels = opts.labels;
    this.reducedMotion = opts.reducedMotion;
    this.rivals = [...opts.rivals].sort((a, b) => a.score - b.score);
    this.unlocked = opts.unlocked;
    this.audio = new ArcadeAudio(true);
    this.damage = new ScreenDamage({ overlayRoot: opts.overlayRoot, reducedMotion: opts.reducedMotion, theme: opts.theme });
    this.resize();
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  /**
   * Repaints from a new palette: the platform mode (XTM One purple, Copilot
   * blue) or the colour mode changed while the game runs.
   */
  setTheme(theme: ArcadeTheme): void {
    this.theme = theme;
  }

  start(): void {
    if (this.running || this.destroyed) return;
    this.running = true;
    this.now = 0;
    this.last = performance.now();
    this.lastInputAt = 0;
    for (const id of this.opts.initialAchievements) this.unlock(id);
    for (const id of playTimeAchievements(new Date())) this.unlock(id);
    this.startWave(1);
    this.raf = requestAnimationFrame(this.frame);
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.audio.stopUfo();
    this.audio.dispose();
    this.damage.destroy();
    this.atlas.clear();
  }

  /** Rebuild the interface - the repair crew. Resolves once everything is home. */
  repair(animated: boolean): Promise<void> {
    return this.damage.restore(animated);
  }

  setPaused(paused: boolean): void {
    if (this.paused === paused || this.ended) return;
    this.paused = paused;
    if (paused) {
      this.audio.stopUfo();
      this.input.fire = false;
      this.input.pointerFire = false;
    } else {
      this.last = performance.now();
      this.lastInputAt = this.now;
      if (this.ufo) this.audio.startUfo();
    }
    this.emitHud();
  }

  get isPaused(): boolean {
    return this.paused;
  }

  get isOver(): boolean {
    return this.ended;
  }

  /** The player has touched the controls: the invasion is on. */
  get isEngaged(): boolean {
    return this.engaged;
  }

  /** Milliseconds of play since the last player input (idle detection for the host). */
  get idleMs(): number {
    return Math.max(0, this.now - this.lastInputAt);
  }

  resize(): void {
    const rect = this.canvas.getBoundingClientRect();
    const W = Math.max(320, rect.width || window.innerWidth);
    const H = Math.max(360, rect.height || window.innerHeight);
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    this.canvas.width = Math.round(W * dpr);
    this.canvas.height = Math.round(H * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.ctx.imageSmoothingEnabled = false;
    const oldW = this.W || W;
    const oldPlayerRatio = this.W ? this.playerX / this.W : 0.5;
    this.W = W;
    this.H = H;
    this.px = Math.min(4, Math.max(2, Math.round(Math.min(W, H * 1.3) / 300)));
    const px = this.px;
    this.cellSpriteW = 12 * px;
    this.colSpacing = Math.max(16 * px, Math.min(26 * px, W * 0.052));
    this.rowSpacing = 14 * px;
    const formationW = (FORMATION_COLUMNS - 1) * this.colSpacing + this.cellSpriteW;
    const margin = Math.max(12, W * 0.04);
    const band = Math.min(W - margin * 2, Math.max(formationW, formationW * 1.6));
    this.bandLeft = (W - band) / 2;
    this.bandRight = this.bandLeft + band;
    this.stepX = Math.min(6 * px, Math.max(2 * px, Math.round((band - formationW) / 26)));
    this.dropY = 5 * px;
    this.groundY = H - Math.max(22, Math.round(H * 0.035));
    this.playerY = this.groundY - 8 * px - 6;
    if (this.W && oldW) this.formationX *= W / oldW;
    this.playerX = Math.min(W - 20, Math.max(20, oldPlayerRatio * W));
    this.atlas.clear();
  }

  // ── Input (called by the overlay) ──────────────────────────────────────────

  setKey(action: "left" | "right" | "fire", down: boolean): void {
    this.input[action] = down;
    if (down) this.noteInput();
  }

  setPointer(x: number): void {
    this.input.pointerX = x;
    this.input.lastPointerMove = this.now;
    this.noteInput();
  }

  setPointerFire(down: boolean): void {
    this.input.pointerFire = down;
    if (down) this.noteInput();
  }

  /** Right-click / X: a missile, aimed at a point when one is given. */
  launchMissile(target: { x: number; y: number } | null): void {
    this.noteInput();
    if (this.paused || !this.canAct()) return;
    if (this.missiles <= 0) {
      this.audio.play("deny");
      return;
    }
    this.missiles -= 1;
    const x = this.playerX;
    const y = this.playerY - 4;
    let vx = 0;
    let vy = -1;
    if (target && target.y < y - 10) {
      const dx = target.x - x;
      const dy = target.y - y;
      const len = Math.hypot(dx, dy) || 1;
      vx = dx / len;
      vy = dy / len;
    }
    this.missilesInFlight.push({ x, y, vx, vy, speed: this.H * 0.35, target, startY: y, lastProbeY: y, trail: [] });
    this.audio.play("missile");
  }

  /** Every key the player types, for the secret codes. */
  typeKey(key: string): void {
    const code = this.secrets.push(key);
    if (code) this.onSecret(code);
  }

  /** An achievement the host observed (waiting for the agent, overtime...). */
  unlock(id: AchievementId): void {
    if (this.achievements.has(id)) return;
    if (this.cheat && !CHEAT_ACHIEVEMENTS.has(id)) return;
    this.achievements.add(id);
    if (!this.unlocked.has(id)) {
      this.opts.callbacks.onEvent({ type: "achievement", id });
      if (this.running) this.audio.play("achievement");
    }
  }

  /** The agent's answer arrived and the player is leaving: chain-explode, bonus, done.
   *  A game nobody played just ends: there is no bonus for watching. */
  missionComplete(waitSeconds: number): void {
    if (this.ended || this.phase === "mission" || this.phase === "gameOver") return;
    if (!this.engaged) {
      this.finish("mission");
      return;
    }
    this.paused = false;
    this.phase = "mission";
    this.phaseTimer = 0;
    this.missionQueue = this.invaders.filter((inv) => inv.alive);
    this.missionTimer = 0;
    this.enemyShots = [];
    this.capsules = [];
    if (this.ufo) this.killUfo(false);
    if (this.boss && this.boss.dying <= 0) this.boss.dying = 1;
    this.addScore(survivalBonus(waitSeconds), this.W / 2, this.H * 0.45, this.theme.gold, 28);
    this.unlock("mission_complete");
    this.audio.play("waveClear");
  }

  /** The player left from the pause menu. */
  quit(): void {
    this.finish("quit");
  }

  // ── The loop ───────────────────────────────────────────────────────────────

  private readonly frame = (t: number): void => {
    if (this.destroyed || this.crashed) return;
    try {
      this.tick(t);
    } catch (error) {
      // A frame that threw would throw again on every next one: stop the loop
      // and let the host close the game (its destroy() repairs the interface).
      this.crashed = true;
      this.opts.callbacks.onCrash(error);
      return;
    }
    this.raf = requestAnimationFrame(this.frame);
  };

  private tick(t: number): void {
    const elapsed = Math.min(MAX_FRAME_MS, t - this.last);
    this.last = t;
    if (document.hidden) {
      if (!this.paused && !this.ended) {
        this.setPaused(true);
        this.opts.callbacks.onEvent({ type: "autoPaused" });
      }
      return;
    }
    if (!this.paused && !this.ended) {
      this.acc += elapsed;
      while (this.acc >= STEP_MS) {
        this.update(STEP_MS);
        this.acc -= STEP_MS;
      }
    }
    this.render(t);
    this.hudTimer += elapsed;
    if (this.hudTimer >= HUD_INTERVAL_MS) {
      this.hudTimer = 0;
      this.emitHud();
    }
  }

  private update(dt: number): void {
    this.now += dt;
    // Play time starts with the first input: a game left open is not played.
    if (this.engaged && this.phase !== "gameOver" && this.phase !== "mission") this.playMs += dt;
    const slow = this.effectActive("slow") ? 0.5 : 1;
    const enemyDt = dt * slow;

    this.updatePlayer(dt);
    this.updateEffects();

    switch (this.phase) {
      case "entering":
        this.phaseTimer += dt;
        if (this.invaders.every((inv) => !inv.alive || this.phaseTimer >= inv.enterDelay + ENTER_MS)) {
          this.phase = "playing";
          this.waveStartedAt = this.now;
        }
        break;
      case "bossIntro":
        this.phaseTimer += dt;
        if (this.boss) this.boss.y += (this.boss.targetY - this.boss.y) * Math.min(1, dt * 0.004);
        if (this.phaseTimer >= BOSS_INTRO_MS) {
          this.phase = "playing";
          this.waveStartedAt = this.now;
        }
        break;
      case "playing":
        if (this.engaged) {
          if (this.boss) this.updateBoss(enemyDt);
          else this.updateFormation(enemyDt);
          this.updateEnemyFire(enemyDt);
          this.updateUfo(enemyDt);
          this.checkPacifist();
        }
        if (!this.paused && this.now - this.lastInputAt > IDLE_PAUSE_MS) {
          this.setPaused(true);
          this.opts.callbacks.onEvent({ type: "autoPaused" });
        }
        break;
      case "dying":
        this.phaseTimer += dt;
        if (this.boss) this.updateBoss(enemyDt * 0.3);
        if (this.phaseTimer >= DYING_MS) this.afterDeath();
        break;
      case "waveClear":
        this.phaseTimer += dt;
        if (this.phaseTimer >= WAVE_CLEAR_MS) this.startWave(this.wave + 1);
        break;
      case "gameOver":
        this.phaseTimer += dt;
        if (this.phaseTimer >= GAME_OVER_MS) this.finish("dead");
        break;
      case "mission":
        this.updateMission(dt);
        break;
    }

    this.updatePlayerShots(dt);
    this.updateMissiles(dt);
    this.updateEnemyShots(enemyDt);
    this.updateCapsules(dt);
    this.updateParticles(dt);
    this.updateNuke();

    if (this.engaged && this.missiles < MAX_MISSILES) {
      this.missileRegen += dt;
      if (this.missileRegen >= MISSILE_REGEN_MS) {
        this.missileRegen = 0;
        this.missiles += 1;
      }
    }
    this.shakeMag *= Math.pow(0.9, dt / 16.67);
    this.flash = Math.max(0, this.flash - dt / 600);
  }

  // ── Waves ──────────────────────────────────────────────────────────────────

  private startWave(wave: number): void {
    this.wave = wave;
    this.config = waveConfig(wave);
    this.livesLostThisWave = 0;
    this.enemyShots = [];
    this.enemyFireTimer = this.config.fireIntervalMs;
    this.ufoTimer = 18000 + Math.random() * 10000;
    this.phaseTimer = 0;
    this.opts.callbacks.onEvent({ type: "wave", wave, boss: this.config.boss });
    if (this.config.boss) {
      this.invaders = [];
      this.spawnBoss(this.config.boss, this.config.bossCycle);
      this.phase = "bossIntro";
      this.audio.play("bossAlarm");
      return;
    }
    this.boss = null;
    this.buildFormation();
    this.phase = "entering";
  }

  private buildFormation(): void {
    const origin = this.wave === 1 && this.opts.origin ? this.opts.origin : { x: this.W / 2, y: -40 };
    const formationW = (FORMATION_COLUMNS - 1) * this.colSpacing + this.cellSpriteW;
    this.formationX = (this.W - formationW) / 2 - (this.colSpacing - this.cellSpriteW) / 2;
    this.formationY = HUD_TOP + 44 + this.config.startDrop * (this.rowSpacing / 2);
    this.dir = 1;
    this.stepTimer = 0;
    this.invaders = [];
    FORMATION_ROWS.forEach((baseKind, row) => {
      for (let col = 0; col < FORMATION_COLUMNS; col += 1) {
        let kind: InvaderKind = baseKind;
        if (Math.random() < this.config.specialChance) kind = Math.random() < 0.25 ? "golden" : "bug";
        const index = row * FORMATION_COLUMNS + col;
        this.invaders.push({
          col,
          row,
          kind,
          alive: true,
          hp: row === 0 && this.config.armoredTopRow ? 2 : 1,
          fromX: origin.x + (Math.random() - 0.5) * 40,
          fromY: origin.y + (Math.random() - 0.5) * 20,
          enterDelay: this.reducedMotion ? 0 : index * 14 + Math.random() * 120,
          hitFlash: 0,
        });
      }
    });
  }

  private invaderSprite(inv: Invader): SpriteName {
    if (inv.kind === "bug") return "bug";
    if (inv.kind === "golden") return "crab";
    if (this.opts.seasonal === "halloween" && inv.kind === "octopus") return "pumpkin";
    return inv.kind;
  }

  /** Top-left of an invader's sprite, entrance flight included. */
  private invaderPos(inv: Invader): { x: number; y: number; w: number; h: number } {
    const sprite = spriteSize(this.invaderSprite(inv));
    const w = sprite.w * this.px;
    const h = sprite.h * this.px;
    const slotX = this.formationX + inv.col * this.colSpacing + (this.colSpacing - w) / 2;
    let slotY = this.formationY + inv.row * this.rowSpacing;
    if (this.hopUntil > this.now) slotY -= Math.abs(Math.sin((this.now + inv.col * 60) / 90)) * this.px * 3;
    if (this.phase === "entering") {
      const t = Math.min(1, Math.max(0, (this.phaseTimer - inv.enterDelay) / ENTER_MS));
      if (t < 1) {
        const e = easeOutCubic(t);
        const cx = (inv.fromX + slotX) / 2 + (inv.col - 5) * this.px * 6;
        const cy = Math.min(inv.fromY, slotY) - 80;
        const x = (1 - e) * (1 - e) * inv.fromX + 2 * (1 - e) * e * cx + e * e * slotX;
        const y = (1 - e) * (1 - e) * inv.fromY + 2 * (1 - e) * e * cy + e * e * slotY;
        return { x, y, w, h };
      }
    }
    return { x: slotX, y: slotY, w, h };
  }

  private updateFormation(dt: number): void {
    const alive = this.invaders.filter((inv) => inv.alive);
    if (alive.length === 0) {
      this.clearWave();
      return;
    }
    this.stepTimer += dt;
    const interval = stepIntervalMs(alive.length, this.config.stepFactor);
    if (this.stepTimer < interval) return;
    this.stepTimer = 0;
    let minCol = FORMATION_COLUMNS;
    let maxCol = -1;
    for (const inv of alive) {
      minCol = Math.min(minCol, inv.col);
      maxCol = Math.max(maxCol, inv.col);
    }
    const inset = (this.colSpacing - this.cellSpriteW) / 2;
    const left = this.formationX + minCol * this.colSpacing + inset;
    const right = this.formationX + (maxCol + 1) * this.colSpacing - inset;
    const nextLeft = left + this.dir * this.stepX;
    const nextRight = right + this.dir * this.stepX;
    if (nextRight > this.bandRight || nextLeft < this.bandLeft) {
      this.formationY += this.dropY;
      this.dir *= -1;
    } else {
      this.formationX += this.dir * this.stepX;
    }
    this.legFrame ^= 1;
    this.audio.march(this.marchIndex);
    this.marchIndex = (this.marchIndex + 1) % 4;
    let lowest = 0;
    for (const inv of alive) {
      const pos = this.invaderPos(inv);
      lowest = Math.max(lowest, pos.y + pos.h);
    }
    if (lowest >= this.playerY - this.px * 2) this.invadersLanded();
  }

  private invadersLanded(): void {
    if (this.godMode) {
      // Even gods get the invaders pushed back.
      this.formationY = HUD_TOP + 44;
      return;
    }
    this.lives = 0;
    this.killPlayer(true);
  }

  private clearWave(): void {
    if (this.phase !== "playing") return;
    this.phase = "waveClear";
    this.phaseTimer = 0;
    const bonus = waveClearBonus(this.wave);
    this.addScore(bonus, this.W / 2, this.H * 0.42, this.theme.gold, 26);
    this.audio.play("waveClear");
    const elapsed = this.now - this.waveStartedAt;
    if (this.wave === 1 && elapsed < 25_000) this.unlock("speed_demon");
    if (this.lives === 1 && !this.godMode) this.unlock("last_stand");
    if (this.livesLostThisWave === 0) {
      this.cleanWaves += 1;
      if (this.cleanWaves >= 3) this.unlock("untouchable");
    } else {
      this.cleanWaves = 0;
    }
    this.enemyShots = [];
  }

  // ── The player ─────────────────────────────────────────────────────────────

  private canAct(): boolean {
    return this.playerAlive && (this.phase === "playing" || this.phase === "entering" || this.phase === "bossIntro" || this.phase === "waveClear");
  }

  private noteInput(): void {
    this.lastInputAt = this.now;
    if (this.engaged) return;
    this.engaged = true;
    // The first wave's clock (speed and pacifist achievements) starts here.
    this.waveStartedAt = this.now;
  }

  private updatePlayer(dt: number): void {
    if (!this.playerAlive) return;
    const speed = Math.min(1000, Math.max(380, this.W * 0.55)) / 1000;
    const reversed = this.reversedUntil > this.now ? -1 : 1;
    const keyDir = (this.input.right ? 1 : 0) - (this.input.left ? 1 : 0);
    if (keyDir !== 0) {
      this.playerX += keyDir * reversed * speed * dt;
      this.input.pointerX = null;
    } else if (this.input.pointerX !== null) {
      const target = reversed > 0 ? this.input.pointerX : this.W - this.input.pointerX;
      const delta = target - this.playerX;
      const max = speed * 1.8 * dt;
      this.playerX += Math.max(-max, Math.min(max, delta));
    }
    const half = (13 * this.px) / 2;
    this.playerX = Math.max(half + 4, Math.min(this.W - half - 4, this.playerX));
    if ((this.input.fire || this.input.pointerFire) && this.canAct()) this.tryFire();
  }

  private tryFire(): void {
    const rapid = this.effectActive("rapid");
    const throttled = this.throttleUntil > this.now;
    const maxVolleys = throttled ? 1 : rapid ? 3 : 1;
    const cooldown = throttled ? THROTTLED_FIRE_COOLDOWN_MS : rapid ? RAPID_FIRE_COOLDOWN_MS : FIRE_COOLDOWN_MS;
    if (this.now - this.lastShotAt < cooldown) return;
    const inFlight = new Set(this.playerShots.map((s) => s.volley)).size;
    if (inFlight >= maxVolleys) return;
    this.lastShotAt = this.now;
    this.volleys += 1;
    const volley = this.volleys;
    const laser = this.effectActive("laser");
    const sudo = this.effectActive("sudo");
    const vy = -(this.H * (laser ? 2.4 : 1.55)) / 1000;
    const x = this.playerX;
    const y = this.playerY - 2;
    const angles = this.effectActive("spread") ? SPREAD_ANGLES : [0];
    for (const angle of angles) {
      this.playerShots.push({
        id: this.nextShotId++,
        volley,
        x,
        y,
        vx: angle * Math.abs(vy),
        vy,
        laser,
        pierce: laser ? 3 : 1,
        sudo,
        hit: false,
        lastProbeY: y,
        scorched: false,
      });
      this.shots += 1;
    }
    this.audio.play(laser ? "laser" : "shoot");
  }

  private killPlayer(landed = false): void {
    if (!this.playerAlive || (this.godMode && !landed)) return;
    this.playerAlive = false;
    this.lives = Math.max(0, this.lives - (landed ? 0 : 1));
    this.livesLostThisWave += 1;
    this.combo = 0;
    this.phase = this.lives > 0 ? "dying" : "gameOver";
    this.phaseTimer = 0;
    this.enemyShots = [];
    this.audio.stopUfo();
    this.audio.play("playerDie");
    this.shake(14);
    this.spawnDebris(this.playerX, this.playerY + 12, this.theme.accent, 40);
    const hits = this.damage.blast(this.playerX, this.playerY + 10, 16 * this.px, { radius: 5 * this.px, damage: 2, now: this.now });
    this.countDestruction(hits, false);
    this.opts.callbacks.onEvent({ type: "lifeLost", lives: this.lives });
    if (this.lives <= 0) this.audio.play("gameOver");
  }

  private afterDeath(): void {
    this.playerAlive = true;
    this.invulnerableUntil = this.now + RESPAWN_SHIELD_MS;
    this.playerX = this.W / 2;
    this.phase = "playing";
    this.phaseTimer = 0;
  }

  // ── Shots ──────────────────────────────────────────────────────────────────

  private updatePlayerShots(dt: number): void {
    const px = this.px;
    const next: PlayerShot[] = [];
    for (const shot of this.playerShots) {
      shot.x += shot.vx * dt;
      shot.y += shot.vy * dt;
      const offScreen = shot.y <= -20 || shot.x <= -20 || shot.x >= this.W + 20;
      let alive = !offScreen;
      const h = shot.laser ? 18 * px : 4 * px;
      // Invaders.
      if (alive && !this.boss) {
        for (const inv of this.invaders) {
          if (!inv.alive || (this.phase === "entering" && this.phaseTimer < inv.enterDelay)) continue;
          const pos = this.invaderPos(inv);
          if (!rectsOverlap(shot.x - px, shot.y, px * 2, h, pos.x, pos.y, pos.w, pos.h)) continue;
          this.markHit(shot);
          this.hitInvader(inv, pos);
          shot.pierce -= 1;
          if (shot.pierce <= 0) {
            alive = false;
            break;
          }
        }
      }
      // The boss, its decoys and minions.
      if (alive && this.boss) alive = this.shotVersusBoss(shot, h);
      // The saucer.
      if (alive && this.ufo) {
        const size = this.ufoSize();
        if (rectsOverlap(shot.x - px, shot.y, px * 2, h, this.ufo.x, this.ufo.y, size.w, size.h)) {
          this.markHit(shot);
          this.killUfo(true, shot.volley);
          alive = false;
        }
      }
      // Enemy shots can be shot down.
      if (alive) {
        for (const enemy of this.enemyShots) {
          if (enemy.kind === "boss") continue;
          const ew = enemy.kind === "payload" ? this.payloadWidth() : 3 * px;
          if (!rectsOverlap(shot.x - px, shot.y, px * 2, h, enemy.x - ew / 2, enemy.y, ew, 7 * px)) continue;
          enemy.hp -= 1;
          this.sparks(enemy.x, enemy.y, this.theme.foreground, 8);
          if (enemy.hp <= 0 && enemy.kind === "payload") {
            this.addScore(PAYLOAD_POINTS, enemy.x, enemy.y, this.theme.danger, 14);
          }
          if (!shot.laser) alive = false;
          break;
        }
        this.enemyShots = this.enemyShots.filter((e) => e.hp > 0);
      }
      // The interface: sudo wipes it, a normal shot pierces a hole in the first thing it crosses.
      if (alive && Math.abs(shot.lastProbeY - shot.y) >= (shot.sudo ? 14 : 36)) {
        shot.lastProbeY = shot.y;
        if (shot.sudo) {
          const result = this.damage.scorch(shot.x, shot.y, { radius: 0, damage: Infinity, now: this.now });
          if (result) this.countDestruction([result], true);
        } else if (!shot.scorched) {
          const result = this.damage.scorch(shot.x, shot.y, { radius: 1.8 * px, damage: 0.34, now: this.now });
          if (result) {
            shot.scorched = true;
            this.countDestruction([result], true);
          }
        }
      }
      if (alive) next.push(shot);
      else if (offScreen && !this.volleyHits.has(shot.volley)) this.combo = 0;
    }
    this.playerShots = next;
    if (this.volleyHits.size > 64 && next.length === 0) this.volleyHits.clear();
  }

  private markHit(shot: PlayerShot): void {
    if (!shot.hit) this.hits += 1;
    shot.hit = true;
    this.volleyHits.add(shot.volley);
  }

  private hitInvader(inv: Invader, pos: { x: number; y: number; w: number; h: number }): void {
    inv.hp -= 1;
    inv.hitFlash = 1;
    if (inv.hp > 0) {
      this.sparks(pos.x + pos.w / 2, pos.y + pos.h / 2, this.invaderColor(inv), 6);
      this.audio.play("bossHit");
      return;
    }
    this.killInvader(inv, pos, true);
  }

  private killInvader(inv: Invader, pos: { x: number; y: number; w: number; h: number }, byShot: boolean): void {
    inv.alive = false;
    this.kills += 1;
    const cx = pos.x + pos.w / 2;
    const cy = pos.y + pos.h / 2;
    let points = INVADER_POINTS[inv.kind];
    if (byShot) {
      this.combo += 1;
      this.maxCombo = Math.max(this.maxCombo, this.combo);
      points *= multiplierFor(this.combo);
    }
    const color = this.invaderColor(inv);
    this.addScore(points, cx, cy, color, points >= 100 ? 18 : 13);
    this.bursts.push({ x: cx, y: cy, life: 1, color, scale: 1 });
    this.spawnDebris(cx, cy, color, 14);
    this.audio.play("invaderDie");
    if (Math.random() < 0.3) {
      const result = this.damage.scorch(cx, cy + pos.h, { radius: 2.4 * this.px, damage: 0.5, now: this.now });
      if (result) this.countDestruction([result], false);
    }
    if (Math.random() < dropChance(inv.kind, this.wave)) {
      this.capsules.push({ x: cx, y: cy, vy: (this.H * 0.22) / 1000, kind: pickPowerUp(Math.random()), age: 0 });
    }
  }

  private shotVersusBoss(shot: PlayerShot, h: number): boolean {
    const boss = this.boss;
    if (!boss || boss.dying > 0) return true;
    const px = this.px;
    for (let i = 0; i < boss.decoys.length; i += 1) {
      const decoy = boss.decoys[i];
      const size = this.bossSize(boss.kind);
      if (!rectsOverlap(shot.x - px, shot.y, px * 2, h, decoy.x, decoy.y, size.w, size.h)) continue;
      boss.decoys.splice(i, 1);
      this.popup(this.labels.fake, decoy.x + size.w / 2, decoy.y + size.h / 2, this.theme.violet, 16);
      this.sparks(decoy.x + size.w / 2, decoy.y + size.h / 2, this.theme.violet, 20);
      this.combo = 0;
      return shot.laser;
    }
    const minionPx = this.scaledPx(1.4);
    for (const minion of boss.minions) {
      const mw = 8 * minionPx;
      const mh = 7 * minionPx;
      if (minion.hp <= 0 || !rectsOverlap(shot.x - px, shot.y, px * 2, h, minion.x, minion.y, mw, mh)) continue;
      minion.hp -= 1;
      this.markHit(shot);
      if (minion.hp <= 0) {
        this.kills += 1;
        this.combo += 1;
        this.maxCombo = Math.max(this.maxCombo, this.combo);
        this.addScore(MINION_POINTS * multiplierFor(this.combo), minion.x + mw / 2, minion.y, this.theme.success, 13);
        this.spawnDebris(minion.x + mw / 2, minion.y + mh / 2, this.theme.success, 12);
        this.audio.play("invaderDie");
      }
      return shot.laser;
    }
    const size = this.bossSize(boss.kind);
    if (rectsOverlap(shot.x - px, shot.y, px * 2, h, boss.x, boss.y, size.w, size.h)) {
      this.markHit(shot);
      this.combo += 1;
      this.maxCombo = Math.max(this.maxCombo, this.combo);
      this.damageBoss(shot.laser ? 2 : 1);
      this.sparks(shot.x, shot.y, this.bossColor(boss.kind), 6);
      if (!shot.laser) return false;
      shot.pierce -= 1;
      return shot.pierce > 0;
    }
    return true;
  }

  /** A pixel size scaled from the base one, rounded like the sprites are drawn. */
  private scaledPx(multiplier: number): number {
    return Math.max(1, Math.round(this.px * multiplier));
  }

  private updateEnemyFire(dt: number): void {
    if (this.boss || !this.playerAlive) return;
    this.enemyFireTimer -= dt;
    if (this.enemyFireTimer > 0) return;
    this.enemyFireTimer = this.config.fireIntervalMs * (0.6 + Math.random() * 0.8);
    if (this.enemyShots.length >= this.config.maxEnemyBullets) return;
    const kinds: EnemyShotKind[] = ["rolling", "plunger", "squiggly"];
    const kind = kinds[this.shotTypeIndex++ % 3];
    const columns = new Map<number, Invader>();
    for (const inv of this.invaders) {
      if (!inv.alive) continue;
      const lowest = columns.get(inv.col);
      if (!lowest || inv.row > lowest.row) columns.set(inv.col, inv);
    }
    const shooters = [...columns.values()];
    if (shooters.length === 0) return;
    let shooter = shooters[Math.floor(Math.random() * shooters.length)];
    if (kind === "rolling") {
      // The rolling shot hunts: it comes from the column closest to the cannon.
      shooter = shooters.reduce((best, inv) => {
        const d = Math.abs(this.invaderPos(inv).x - this.playerX);
        return d < Math.abs(this.invaderPos(best).x - this.playerX) ? inv : best;
      }, shooter);
    }
    const pos = this.invaderPos(shooter);
    const speed = (this.H * (kind === "rolling" ? 0.5 : 0.42) * this.config.enemyBulletSpeed) / 1000;
    this.enemyShots.push({ kind, x: pos.x + pos.w / 2, y: pos.y + pos.h, vx: 0, vy: speed, age: 0, hp: 1, scarred: false, lastProbeY: pos.y + pos.h });
  }

  private updateEnemyShots(dt: number): void {
    const px = this.px;
    const next: EnemyShot[] = [];
    const playerW = 13 * px;
    const playerH = 8 * px;
    for (const shot of this.enemyShots) {
      shot.age += dt;
      shot.x += shot.vx * dt;
      shot.y += shot.vy * dt;
      if (shot.kind === "boss" && this.boss?.kind === "promptInjector") shot.x += Math.sin(shot.age / 90) * 0.6;
      const w = shot.kind === "payload" ? this.payloadWidth() : 3 * px;
      const h = shot.kind === "payload" ? 5 * px : 7 * px;
      let alive = shot.y < this.groundY;
      if (alive && this.playerAlive && rectsOverlap(shot.x - w / 2, shot.y, w, h, this.playerX - playerW / 2, this.playerY, playerW, playerH)) {
        alive = false;
        this.enemyHitsPlayer(shot);
      }
      if (alive && !shot.scarred && shot.kind !== "payload" && Math.abs(shot.y - shot.lastProbeY) >= 26) {
        shot.lastProbeY = shot.y;
        if (Math.random() < 0.7) {
          const result = this.damage.scorch(shot.x, shot.y + h, { radius: 2.2 * px, damage: 0.5, now: this.now });
          if (result) {
            shot.scarred = true;
            this.countDestruction([result], false);
          }
        }
      }
      if (!alive && shot.y >= this.groundY) this.sparks(shot.x, this.groundY, this.theme.foreground, 5);
      if (alive) next.push(shot);
    }
    this.enemyShots = next;
  }

  private enemyHitsPlayer(shot: EnemyShot): void {
    if (shot.kind === "payload") {
      if (this.shieldUntil > this.now) {
        this.shieldUntil = 0;
        this.audio.play("shieldHit");
        return;
      }
      this.reversedUntil = this.now + 4000;
      this.audio.play("deny");
      this.shake(6);
      return;
    }
    if (this.invulnerableUntil > this.now || this.godMode) return;
    if (this.shieldUntil > this.now) {
      this.shieldUntil = 0;
      this.invulnerableUntil = this.now + 600;
      this.audio.play("shieldHit");
      this.sparks(this.playerX, this.playerY, this.theme.accent, 24);
      return;
    }
    this.killPlayer();
  }

  // ── Missiles ───────────────────────────────────────────────────────────────

  private updateMissiles(dt: number): void {
    const next: Missile[] = [];
    for (const missile of this.missilesInFlight) {
      // Pixels per second, accelerating by 2.2 screen heights per second.
      missile.speed = Math.min(this.H * 1.15, missile.speed + this.H * 0.0022 * dt);
      if (missile.target) {
        const dx = missile.target.x - missile.x;
        const dy = missile.target.y - missile.y;
        const len = Math.hypot(dx, dy) || 1;
        missile.vx += (dx / len - missile.vx) * 0.08;
        missile.vy += (dy / len - missile.vy) * 0.08;
        const norm = Math.hypot(missile.vx, missile.vy) || 1;
        missile.vx /= norm;
        missile.vy /= norm;
      }
      const step = (missile.speed / 1000) * dt;
      missile.x += missile.vx * step;
      missile.y += missile.vy * step;
      missile.trail.push(missile.x, missile.y);
      if (missile.trail.length > 36) missile.trail.splice(0, 2);
      if (Math.random() < 0.6) {
        this.pushParticle({
          x: missile.x,
          y: missile.y + 6,
          vx: (Math.random() - 0.5) * 0.04,
          vy: 0.03 + Math.random() * 0.04,
          life: 500,
          maxLife: 500,
          color: this.theme.muted,
          size: 2 + Math.random() * 2,
          gravity: -0.00002,
        });
      }
      let explode = missile.y < -10 || missile.x < -10 || missile.x > this.W + 10;
      if (!explode && missile.target && Math.hypot(missile.target.x - missile.x, missile.target.y - missile.y) < 12) explode = true;
      if (!explode) explode = this.missileTouchesEnemy(missile);
      if (!explode && !missile.target && missile.startY - missile.y > 130 && Math.abs(missile.y - missile.lastProbeY) > 22) {
        missile.lastProbeY = missile.y;
        if (this.damage.pick(missile.x, missile.y)) explode = true;
      }
      if (explode) this.explodeMissile(missile);
      else next.push(missile);
    }
    this.missilesInFlight = next;
  }

  private missileTouchesEnemy(missile: Missile): boolean {
    const r = 3 * this.px;
    if (this.boss) {
      const size = this.bossSize(this.boss.kind);
      if (rectsOverlap(missile.x - r, missile.y - r, r * 2, r * 2, this.boss.x, this.boss.y, size.w, size.h)) return true;
    }
    if (this.ufo) {
      const size = this.ufoSize();
      if (rectsOverlap(missile.x - r, missile.y - r, r * 2, r * 2, this.ufo.x, this.ufo.y, size.w, size.h)) return true;
    }
    for (const inv of this.invaders) {
      if (!inv.alive) continue;
      const pos = this.invaderPos(inv);
      if (rectsOverlap(missile.x - r, missile.y - r, r * 2, r * 2, pos.x, pos.y, pos.w, pos.h)) return true;
    }
    return false;
  }

  private explodeMissile(missile: Missile): void {
    const radius = 12 * this.px;
    const { x, y } = missile;
    this.bursts.push({ x, y, life: 1, color: this.theme.ember, scale: 2.4 });
    this.spawnDebris(x, y, this.theme.ember, 30);
    this.audio.play("explosion");
    this.shake(8);
    for (const inv of this.invaders) {
      if (!inv.alive) continue;
      const pos = this.invaderPos(inv);
      if (Math.hypot(pos.x + pos.w / 2 - x, pos.y + pos.h / 2 - y) <= radius + pos.w / 2) this.killInvader(inv, pos, false);
    }
    if (this.ufo) {
      const size = this.ufoSize();
      if (Math.hypot(this.ufo.x + size.w / 2 - x, this.ufo.y + size.h / 2 - y) <= radius + size.w / 2) this.killUfo(true, this.volleys);
    }
    if (this.boss && this.boss.dying <= 0) {
      const size = this.bossSize(this.boss.kind);
      if (rectsOverlap(x - radius, y - radius, radius * 2, radius * 2, this.boss.x, this.boss.y, size.w, size.h)) this.damageBoss(3);
      for (const minion of this.boss.minions) {
        if (minion.hp > 0 && Math.hypot(minion.x - x, minion.y - y) <= radius * 1.2) minion.hp = 0;
      }
    }
    const hits = this.damage.blast(x, y, radius, { radius: radius * 0.42, damage: 3, now: this.now });
    this.countDestruction(hits, true);
  }

  // ── The saucer ─────────────────────────────────────────────────────────────

  private ufoSize(): { w: number; h: number } {
    const size = spriteSize(this.ufo?.variant === "duck" ? "duck" : "ufo");
    const scale = this.scaledPx(this.ufo?.variant === "duck" ? 1.1 : 1.2);
    return { w: size.w * scale, h: size.h * scale };
  }

  private updateUfo(dt: number): void {
    if (this.boss) return;
    if (!this.ufo) {
      this.ufoTimer -= dt;
      const alive = this.invaders.reduce((n, inv) => n + (inv.alive ? 1 : 0), 0);
      if (this.ufoTimer <= 0 && alive >= 8) {
        const dir = Math.random() < 0.5 ? 1 : -1;
        const variant: UfoVariant = Math.random() < 0.04 ? "duck" : "classic";
        this.ufo = { x: dir > 0 ? -60 : this.W + 10, y: HUD_TOP + 6, dir, speed: this.W / 9000, variant };
        this.audio.startUfo();
        this.ufoTimer = 18000 + Math.random() * 10000;
      }
      return;
    }
    this.ufo.x += this.ufo.dir * this.ufo.speed * dt;
    if (this.ufo.x < -80 || this.ufo.x > this.W + 80) {
      this.ufo = null;
      this.audio.stopUfo();
    }
  }

  private killUfo(scored: boolean, volley = this.volleys): void {
    const ufo = this.ufo;
    if (!ufo) return;
    const size = this.ufoSize();
    const cx = ufo.x + size.w / 2;
    const cy = ufo.y + size.h / 2;
    this.ufo = null;
    this.audio.stopUfo();
    if (!scored) {
      this.bursts.push({ x: cx, y: cy, life: 1, color: this.theme.danger, scale: 1.4 });
      return;
    }
    let points = ufoPoints(volley);
    if (ufo.variant === "duck") {
      points = UFO_DUCK_POINTS;
      this.unlock("rubber_duck");
      this.audio.play("quack");
    } else if (this.opts.seasonal === "piDay") {
      points = UFO_PI_POINTS;
      this.unlock("pi_hunter");
    } else if (isLegendaryShot(volley)) {
      this.unlock("lucky_23");
      this.popup(this.labels.legendary, cx, cy + 22, this.theme.gold, 14);
    }
    this.ufoKills += 1;
    this.addScore(points, cx, cy, this.theme.danger, 22);
    this.bursts.push({ x: cx, y: cy, life: 1, color: this.theme.danger, scale: 1.8 });
    this.spawnDebris(cx, cy, this.theme.danger, 30);
    this.audio.play("ufoHit");
    this.shake(6);
    const hits = this.damage.blast(cx, cy, 14 * this.px, { radius: 4 * this.px, damage: 2, now: this.now });
    this.countDestruction(hits, true);
  }

  // ── Bosses ─────────────────────────────────────────────────────────────────

  private bossSprite(kind: BossKind): SpriteName {
    switch (kind) {
      case "hallucination":
        return "hallucination";
      case "rateLimiter":
        return "crab";
      case "promptInjector":
        return "squid";
      case "tokenOverflow":
        return "octopus";
    }
  }

  private bossScale(kind: BossKind): number {
    return kind === "hallucination" ? 1.6 : kind === "promptInjector" ? 3.6 : 3.1;
  }

  private bossSize(kind: BossKind): { w: number; h: number } {
    const size = spriteSize(this.bossSprite(kind));
    const scale = this.scaledPx(this.bossScale(kind));
    return { w: size.w * scale, h: size.h * scale };
  }

  private bossColor(kind: BossKind): string {
    switch (kind) {
      case "hallucination":
        return this.theme.violet;
      case "rateLimiter":
        return this.theme.warning;
      case "promptInjector":
        return this.theme.danger;
      case "tokenOverflow":
        return this.theme.success;
    }
  }

  private spawnBoss(kind: BossKind, cycle: number): void {
    const size = this.bossSize(kind);
    const maxHp = bossMaxHp(kind, cycle);
    this.boss = {
      kind,
      x: this.W / 2 - size.w / 2,
      y: -size.h - 20,
      targetY: HUD_TOP + 40,
      hp: maxHp,
      maxHp,
      t: 0,
      flash: 0,
      fireTimer: 1500,
      burstLeft: 0,
      specialTimer: 4000,
      decoys: [],
      minions: [],
      splits: 0,
      dying: 0,
    };
  }

  private damageBoss(amount: number): void {
    const boss = this.boss;
    if (!boss || boss.dying > 0) return;
    boss.hp = Math.max(0, boss.hp - amount);
    boss.flash = 1;
    this.audio.play("bossHit");
    if (boss.kind === "tokenOverflow") {
      const thresholds = OVERFLOW_SPLIT_THRESHOLDS;
      while (boss.splits < thresholds.length && boss.hp <= boss.maxHp * thresholds[boss.splits]) {
        boss.splits += 1;
        this.splitOverflow(boss);
      }
    }
    if (boss.hp <= 0) boss.dying = 1;
  }

  private splitOverflow(boss: Boss): void {
    const size = this.bossSize(boss.kind);
    for (let i = 0; i < MINIONS_PER_SPLIT; i += 1) {
      boss.minions.push({
        x: boss.x + size.w / 2,
        y: boss.y + size.h / 2,
        vx: (i - 1) * 0.12 + (Math.random() - 0.5) * 0.05,
        vy: 0.06 + Math.random() * 0.05,
        hp: 3,
        frame: 0,
        fireTimer: 1500 + Math.random() * 1500,
      });
    }
    this.sparks(boss.x + size.w / 2, boss.y + size.h / 2, this.theme.success, 30);
  }

  private updateBoss(dt: number): void {
    const boss = this.boss;
    if (!boss) return;
    boss.t += dt;
    boss.flash = Math.max(0, boss.flash - dt / 180);
    const size = this.bossSize(boss.kind);
    if (boss.dying > 0) {
      this.updateBossDeath(boss, size, dt);
      return;
    }
    const center = this.W / 2 - size.w / 2;
    const amplitude = Math.max(40, (this.bandRight - this.bandLeft) / 2 - size.w / 2);
    boss.x = center + Math.sin(boss.t / 1400) * amplitude;
    boss.y = boss.targetY + Math.sin(boss.t / 700) * this.px * 3;
    if (!this.playerAlive) return;
    boss.fireTimer -= dt;
    boss.specialTimer -= dt;
    const muzzleX = boss.x + size.w / 2;
    const muzzleY = boss.y + size.h;
    const speed = (this.H * 0.4) / 1000;
    switch (boss.kind) {
      case "hallucination": {
        if (boss.fireTimer <= 0) {
          boss.fireTimer = 1300;
          for (const angle of [-0.18, 0, 0.18]) this.bossShot(muzzleX, muzzleY, speed, angle);
        }
        if (boss.specialTimer <= 0) {
          boss.specialTimer = 3400;
          if (boss.decoys.length > 0 && Math.random() < 0.5) {
            // Swap places with a decoy: which one is real?
            const decoy = boss.decoys[Math.floor(Math.random() * boss.decoys.length)];
            this.sparks(boss.x + size.w / 2, boss.y + size.h / 2, this.theme.violet, 18);
            const dx = decoy.x;
            decoy.x = boss.x;
            boss.t = Math.asin(Math.max(-1, Math.min(1, (dx - center) / amplitude))) * 1400;
          }
          while (boss.decoys.length < 2 + boss.splits) {
            boss.decoys.push({
              x: this.bandLeft + Math.random() * Math.max(10, this.bandRight - this.bandLeft - size.w),
              y: boss.targetY + (Math.random() - 0.5) * this.px * 10,
              life: 4200,
              fireTimer: 900 + Math.random() * 1600,
            });
          }
        }
        boss.decoys = boss.decoys.filter((decoy) => {
          decoy.life -= dt;
          decoy.fireTimer -= dt;
          if (decoy.fireTimer <= 0) {
            decoy.fireTimer = 2400;
            this.bossShot(decoy.x + size.w / 2, decoy.y + size.h, speed * 0.8, 0);
          }
          return decoy.life > 0;
        });
        break;
      }
      case "rateLimiter": {
        if (boss.fireTimer <= 0) {
          if (boss.burstLeft <= 0) boss.burstLeft = 5;
          this.bossShot(muzzleX + (Math.random() - 0.5) * size.w * 0.6, muzzleY, speed * 1.1, (Math.random() - 0.5) * 0.25);
          boss.burstLeft -= 1;
          boss.fireTimer = boss.burstLeft > 0 ? 95 : 1500;
        }
        if (boss.specialTimer <= 0) {
          boss.specialTimer = 7000;
          this.throttleUntil = this.now + 3000;
          this.audio.play("deny");
          this.popup("429", this.playerX, this.playerY - 24, this.theme.warning, 22);
        }
        break;
      }
      case "promptInjector": {
        if (boss.fireTimer <= 0) {
          boss.fireTimer = 900;
          this.bossShot(muzzleX, muzzleY, speed, (Math.random() - 0.5) * 0.2);
        }
        if (boss.specialTimer <= 0) {
          boss.specialTimer = PAYLOAD_INTERVAL_MS;
          this.enemyShots.push({
            kind: "payload",
            x: muzzleX,
            y: muzzleY,
            vx: 0,
            vy: (this.H * 0.18) / 1000,
            age: 0,
            hp: 2,
            scarred: true,
            lastProbeY: muzzleY,
          });
        }
        break;
      }
      case "tokenOverflow": {
        if (boss.fireTimer <= 0) {
          boss.fireTimer = 1100;
          for (const angle of [-0.12, 0.12]) this.bossShot(muzzleX, muzzleY, speed, angle);
        }
        break;
      }
    }
    this.updateMinions(boss, dt, speed);
  }

  private updateMinions(boss: Boss, dt: number, speed: number): void {
    const mw = 8 * this.scaledPx(1.4);
    for (const minion of boss.minions) {
      if (minion.hp <= 0) continue;
      minion.x += minion.vx * dt;
      minion.y += minion.vy * dt;
      if (minion.x < this.bandLeft || minion.x + mw > this.bandRight) minion.vx *= -1;
      if (minion.y < HUD_TOP + 20 || minion.y > this.H * 0.62) minion.vy *= -1;
      minion.frame = Math.floor(boss.t / 300) % 2;
      minion.fireTimer -= dt;
      if (minion.fireTimer <= 0) {
        minion.fireTimer = 2200 + Math.random() * 1500;
        this.bossShot(minion.x + mw / 2, minion.y + mw, speed * 0.9, 0);
      }
    }
    boss.minions = boss.minions.filter((m) => m.hp > 0);
  }

  private bossShot(x: number, y: number, speed: number, angle: number): void {
    if (this.enemyShots.length > 24) return;
    this.enemyShots.push({ kind: "boss", x, y, vx: Math.sin(angle) * speed, vy: Math.cos(angle) * speed, age: 0, hp: 1, scarred: false, lastProbeY: y });
  }

  private updateBossDeath(boss: Boss, size: { w: number; h: number }, dt: number): void {
    boss.dying += dt;
    if (Math.random() < dt / 70) {
      const x = boss.x + Math.random() * size.w;
      const y = boss.y + Math.random() * size.h;
      this.bursts.push({ x, y, life: 1, color: Math.random() < 0.5 ? this.bossColor(boss.kind) : this.theme.ember, scale: 1.6 });
      this.audio.play("invaderDie");
    }
    if (boss.dying < 1400) return;
    const cx = boss.x + size.w / 2;
    const cy = boss.y + size.h / 2;
    this.bursts.push({ x: cx, y: cy, life: 1, color: this.theme.gold, scale: 4 });
    this.spawnDebris(cx, cy, this.bossColor(boss.kind), 90);
    this.flash = 0.6;
    this.shake(28);
    this.shakeApp();
    this.audio.play("bossDie");
    const hits = this.damage.blast(cx, cy, 36 * this.px, { radius: 7 * this.px, damage: 3, now: this.now });
    this.countDestruction(hits, true);
    if (this.phase !== "mission") {
      this.bosses += 1;
      this.addScore(bossPoints(this.wave), cx, cy, this.theme.gold, 30);
      this.unlock(BOSS_ACHIEVEMENTS[boss.kind]);
      this.opts.callbacks.onEvent({ type: "bossDefeated", kind: boss.kind });
    }
    for (const minion of boss.minions) this.spawnDebris(minion.x, minion.y, this.theme.success, 10);
    this.boss = null;
    this.enemyShots = [];
    if (this.phase === "playing") {
      this.phase = "waveClear";
      this.phaseTimer = 0;
      if (this.livesLostThisWave === 0) {
        this.cleanWaves += 1;
        if (this.cleanWaves >= 3) this.unlock("untouchable");
      } else {
        this.cleanWaves = 0;
      }
    }
  }

  // ── Capsules and effects ───────────────────────────────────────────────────

  private effectActive(kind: PowerUpKind): boolean {
    return (this.effects.get(kind) ?? 0) > this.now;
  }

  private updateEffects(): void {
    for (const [kind, until] of this.effects) if (until <= this.now) this.effects.delete(kind);
  }

  private updateCapsules(dt: number): void {
    const playerW = 13 * this.px;
    const size = 9 * this.px;
    this.capsules = this.capsules.filter((capsule) => {
      capsule.age += dt;
      capsule.y += capsule.vy * dt;
      if (this.playerAlive && rectsOverlap(capsule.x - size / 2, capsule.y - size / 2, size, size, this.playerX - playerW / 2, this.playerY, playerW, 8 * this.px)) {
        this.collect(capsule.kind, capsule.x, capsule.y);
        return false;
      }
      return capsule.y < this.groundY;
    });
  }

  private collect(kind: PowerUpKind, x: number, y: number): void {
    this.audio.play("powerUp");
    this.sparks(x, y, this.powerUpColor(kind), 20);
    this.opts.callbacks.onEvent({ type: "powerUp", kind });
    const duration = POWER_UP_DURATIONS_MS[kind];
    switch (kind) {
      case "shield":
        this.shieldUntil = this.now + (duration ?? 25000);
        break;
      case "missiles":
        this.missiles = Math.min(MAX_MISSILES + 4, this.missiles + 3);
        break;
      case "oneUp":
        this.lives = Math.min(MAX_LIVES, this.lives + 1);
        this.audio.play("extraLife");
        break;
      case "nuke":
        if (!this.nukeAt) {
          this.nukeAt = this.now + NUKE_COUNTDOWN_MS;
          this.opts.callbacks.onEvent({ type: "nukeIncoming" });
          this.audio.play("nukeSiren");
        }
        break;
      case "sudo":
        this.effects.set(kind, this.now + (duration ?? 8000));
        this.unlock("sudo");
        this.popup(this.labels.sudo, this.playerX, this.playerY - 30, this.theme.success, 16);
        break;
      default:
        this.effects.set(kind, this.now + (duration ?? 8000));
    }
  }

  private updateNuke(): void {
    if (!this.nukeAt || this.now < this.nukeAt) return;
    this.nukeAt = 0;
    this.flash = 1;
    this.shake(34);
    this.shakeApp();
    this.audio.play("explosion");
    this.audio.play("bossDie");
    for (const inv of this.invaders) {
      if (inv.alive) this.killInvader(inv, this.invaderPos(inv), false);
    }
    if (this.ufo) this.killUfo(true);
    if (this.boss && this.boss.dying <= 0) {
      this.damageBoss(Math.ceil(this.boss.maxHp * 0.3));
      for (const minion of this.boss.minions) minion.hp = 0;
    }
    this.enemyShots = [];
    const wiped = this.damage.crumbleAllVisible(120);
    this.countDestruction(wiped, true, NUKE_UI_POINTS);
    this.unlock("nuked");
  }

  // ── Destruction, scoring, particles ────────────────────────────────────────

  private countDestruction(results: DamageResult[], byPlayer: boolean, destroyPoints = UI_DESTROYED_POINTS): void {
    for (const result of results) {
      if (result.destroyed) {
        // Only the damage a game is paid for counts (`scored`: its first
        // MAX_SCORED_ELEMENTS elements), never past the cap: beyond, the
        // interface still falls, but the game stops counting.
        if (byPlayer && result.scored && this.uiDestroyed < MAX_SCORED_ELEMENTS) {
          this.uiDestroyed += 1;
          const label = this.labels.destroyed[result.kind];
          const cx = result.rect.x + result.rect.w / 2;
          const cy = result.rect.y + result.rect.h / 2;
          this.addScore(destroyPoints, cx, cy, this.theme.ember, 12, destroyPoints === UI_DESTROYED_POINTS ? label : undefined);
        }
        if (byPlayer && result.achievement) this.unlock(result.achievement);
        if (this.now - this.lastCrumbleSound > 70) {
          this.lastCrumbleSound = this.now;
          this.audio.play("crumble");
        }
        const colors = result.colors.length > 0 ? result.colors : [this.theme.muted, this.theme.ember];
        const count = Math.min(26, 6 + Math.round((result.rect.w * result.rect.h) / 2500));
        for (let i = 0; i < count; i += 1) {
          this.pushParticle({
            x: result.rect.x + Math.random() * result.rect.w,
            y: result.rect.y + Math.random() * result.rect.h,
            vx: (Math.random() - 0.5) * 0.25,
            vy: -Math.random() * 0.2,
            life: 1100,
            maxLife: 1100,
            color: colors[i % colors.length],
            size: 2 + Math.random() * 4,
            gravity: 0.0009,
          });
        }
      } else if (byPlayer && result.scored) {
        this.score += UI_CRATER_POINTS;
      }
    }
  }

  private addScore(points: number, x: number, y: number, color: string, size: number, label?: string): void {
    this.score += Math.round(points);
    this.popup(label ? `${label} +${points}` : `+${points}`, x, y, color, size);
    const earned = extraLivesEarned(this.score);
    while (this.livesAwarded < earned) {
      this.livesAwarded += 1;
      if (this.lives < MAX_LIVES) {
        this.lives += 1;
        this.audio.play("extraLife");
        this.opts.callbacks.onEvent({ type: "extraLife", lives: this.lives });
      }
    }
    while (this.rivals.length > 0 && this.score > this.rivals[0].score) {
      const rival = this.rivals.shift();
      if (rival && !this.cheat) {
        this.audio.play("rival");
        this.opts.callbacks.onEvent({ type: "rivalPassed", rival, rank: rival.rank });
      }
    }
    if (!this.personalBestAnnounced && !this.cheat && this.opts.personalBest > 0 && this.score > this.opts.personalBest) {
      this.personalBestAnnounced = true;
      this.opts.callbacks.onEvent({ type: "personalBest", score: this.score });
    }
  }

  private popup(text: string, x: number, y: number, color: string, size: number): void {
    if (this.popups.length >= MAX_POPUPS) this.popups.shift();
    this.popups.push({ x, y, text, life: 1000, maxLife: 1000, color, size });
  }

  private sparks(x: number, y: number, color: string, count: number): void {
    for (let i = 0; i < count; i += 1) {
      const angle = Math.random() * Math.PI * 2;
      const speed = 0.05 + Math.random() * 0.25;
      this.pushParticle({ x, y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed, life: 420, maxLife: 420, color, size: 2, gravity: 0 });
    }
  }

  private spawnDebris(x: number, y: number, color: string, count: number): void {
    const n = this.reducedMotion ? Math.ceil(count / 3) : count;
    for (let i = 0; i < n; i += 1) {
      const angle = Math.random() * Math.PI * 2;
      const speed = 0.04 + Math.random() * 0.3;
      this.pushParticle({
        x,
        y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed - 0.08,
        life: 700 + Math.random() * 600,
        maxLife: 1300,
        color: Math.random() < 0.3 ? this.theme.ember : color,
        size: this.px * (0.6 + Math.random()),
        gravity: 0.0006,
      });
    }
  }

  private pushParticle(particle: Particle): void {
    if (this.particles.length >= MAX_PARTICLES) this.particles.shift();
    this.particles.push(particle);
  }

  private updateParticles(dt: number): void {
    for (const p of this.particles) {
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      p.vy += p.gravity * dt;
      p.life -= dt;
    }
    this.particles = this.particles.filter((p) => p.life > 0);
    for (const popup of this.popups) {
      popup.y -= 0.03 * dt;
      popup.life -= dt;
    }
    this.popups = this.popups.filter((p) => p.life > 0);
    for (const burst of this.bursts) burst.life -= dt / 320;
    this.bursts = this.bursts.filter((b) => b.life > 0);
    for (const inv of this.invaders) inv.hitFlash = Math.max(0, inv.hitFlash - dt / 150);
  }

  private shake(amount: number): void {
    if (this.reducedMotion) return;
    this.shakeMag = Math.max(this.shakeMag, amount);
  }

  /** The whole app trembles for a moment (boss down, nuke) - through `ScreenDamage`, which every end of the game cancels. */
  private shakeApp(): void {
    this.damage.quake();
  }

  private checkPacifist(): void {
    if (this.pacifistDone || this.boss) return;
    const since = Math.max(this.lastShotAt, this.waveStartedAt);
    if (this.invaders.some((inv) => inv.alive) && this.now - since >= PACIFIST_MS) {
      this.pacifistDone = true;
      this.unlock("pacifist");
      this.addScore(PACIFIST_BONUS, this.playerX, this.playerY - 30, this.theme.success, 16);
    }
  }

  private updateMission(dt: number): void {
    this.phaseTimer += dt;
    this.missionTimer += dt;
    while (this.missionTimer >= 28 && this.missionQueue.length > 0) {
      this.missionTimer -= 28;
      const inv = this.missionQueue.shift();
      if (inv && inv.alive) this.killInvader(inv, this.invaderPos(inv), false);
    }
    if (this.boss) this.updateBoss(dt);
    if (this.phaseTimer >= MISSION_MS && this.missionQueue.length === 0 && !this.boss) this.finish("mission");
  }

  private onSecret(code: SecretCode): void {
    switch (code) {
      case "konami":
        this.rainbow = !this.rainbow;
        this.atlas.clear();
        this.unlock("konami_kid");
        this.unlock("rainbow_road");
        this.opts.callbacks.onEvent({ type: "secret", code });
        this.audio.play("powerUp");
        break;
      case "iddqd":
        this.cheat = true;
        this.godMode = true;
        this.unlock("god_complex");
        this.opts.callbacks.onEvent({ type: "cheat", code });
        this.audio.play("extraLife");
        break;
      case "idkfa":
        this.cheat = true;
        this.missiles = 99;
        for (const kind of ["rapid", "spread", "laser"] as PowerUpKind[]) this.effects.set(kind, this.now + 10000);
        this.shieldUntil = this.now + 10000;
        this.unlock("arsenal");
        this.opts.callbacks.onEvent({ type: "cheat", code });
        this.audio.play("powerUp");
        break;
      case "hello":
        this.hopUntil = this.now + 1400;
        this.unlock("hello_there");
        this.opts.callbacks.onEvent({ type: "secret", code });
        break;
      case "answer":
        this.popup("42", this.W / 2, this.H / 2, this.theme.gold, 64);
        this.unlock("deep_thought");
        this.opts.callbacks.onEvent({ type: "secret", code });
        break;
      case "xyzzy":
        this.unlock("adventurer");
        this.opts.callbacks.onEvent({ type: "secret", code });
        break;
      case "sudo":
        this.unlock("permission_denied");
        this.opts.callbacks.onEvent({ type: "secret", code });
        this.audio.play("deny");
        break;
    }
  }

  private finish(reason: RunEndReason): void {
    if (this.ended) return;
    this.ended = true;
    this.audio.stopUfo();
    const run = {
      score: this.score,
      wave: this.wave,
      kills: this.kills,
      shots: this.shots,
      hits: Math.min(this.hits, this.shots),
      uiDestroyed: this.uiDestroyed,
      bosses: this.bosses,
      maxCombo: Math.min(this.maxCombo, this.hits, this.shots),
      ufoKills: this.ufoKills,
      durationMs: Math.round(this.playMs),
    };
    if (!this.cheat) for (const id of statAchievements(run)) this.unlock(id);
    this.emitHud();
    const untouched = isUntouchedRun(this.engaged, this.achievements, this.unlocked);
    this.opts.callbacks.onGameOver({ ...run, cheat: this.cheat, achievements: [...this.achievements], reason, untouched });
  }

  // ── HUD ────────────────────────────────────────────────────────────────────

  private emitHud(): void {
    const hud: HudSnapshot = {
      phase: this.phase,
      score: this.score,
      hiScore: Math.max(this.opts.personalBest, this.score),
      lives: this.lives,
      wave: this.wave,
      missiles: this.missiles,
      maxMissiles: MAX_MISSILES,
      multiplier: multiplierFor(this.combo),
      combo: this.combo,
      shield: this.shieldUntil > this.now,
      powerUps: [...this.effects.entries()]
        .filter(([, until]) => until > this.now)
        .map(([kind, until]) => ({ kind, remainingMs: until - this.now, totalMs: POWER_UP_DURATIONS_MS[kind] ?? 10000 })),
      rival: this.rivals[0] ?? null,
      boss: this.boss ? { kind: this.boss.kind, hp: this.boss.hp, maxHp: this.boss.maxHp } : null,
      throttled: this.throttleUntil > this.now,
      reversed: this.reversedUntil > this.now,
      godMode: this.godMode,
      rainbow: this.rainbow,
      cheat: this.cheat,
      uiDestroyed: this.uiDestroyed,
      awaitingPlayer: !this.engaged,
    };
    this.opts.callbacks.onHud(hud);
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  private invaderColor(inv: Invader): string {
    if (this.rainbow) {
      const hue = Math.round(((inv.col * 32 + inv.row * 55 + this.now / 12) % 360) / 30) * 30;
      return hsl(hue, 95, this.theme.isLight ? 45 : 62);
    }
    switch (inv.kind) {
      case "squid":
        return this.theme.violet;
      case "crab":
        return this.theme.accent;
      case "octopus":
        return this.opts.seasonal === "halloween" ? this.theme.warning : this.theme.success;
      case "bug":
        return this.theme.warning;
      case "golden":
        return this.theme.gold;
    }
  }

  private invaderPalette(inv: Invader, color: string): Palette {
    if (inv.kind === "bug") return { "1": color, "2": this.theme.background };
    if (this.opts.seasonal === "halloween" && inv.kind === "octopus") return { "1": color, "2": this.theme.background, "3": this.theme.success };
    return { "1": color };
  }

  private powerUpColor(kind: PowerUpKind): string {
    switch (kind) {
      case "rapid":
        return this.theme.accent;
      case "spread":
        return this.theme.violet;
      case "laser":
        return this.theme.danger;
      case "shield":
        return this.theme.accent;
      case "slow":
        return this.theme.muted;
      case "missiles":
        return this.theme.warning;
      case "sudo":
        return this.theme.success;
      case "oneUp":
        return this.theme.danger;
      case "nuke":
        return this.theme.gold;
    }
  }

  private render(t: number): void {
    const ctx = this.ctx;
    const { W, H, px } = this;
    ctx.setTransform(Math.min(2, window.devicePixelRatio || 1), 0, 0, Math.min(2, window.devicePixelRatio || 1), 0, 0);
    ctx.clearRect(0, 0, W, H);

    // A veil over the interface so the invaders read, the page still visible.
    ctx.fillStyle = this.theme.isLight ? "rgba(12, 16, 32, 0.12)" : "rgba(2, 6, 16, 0.34)";
    ctx.fillRect(0, 0, W, H);

    ctx.save();
    if (this.shakeMag > 0.3) ctx.translate((Math.random() - 0.5) * this.shakeMag, (Math.random() - 0.5) * this.shakeMag);

    this.damage.draw(ctx, this.now);

    // Ground line.
    ctx.globalAlpha = 0.55;
    ctx.fillStyle = this.theme.accent;
    ctx.fillRect(0, this.groundY + 2, W, Math.max(1, px / 2));
    ctx.globalAlpha = 1;

    this.drawParticles(ctx, true);
    this.drawInvaders(ctx);
    this.drawBoss(ctx, t);
    this.drawUfo(ctx);
    this.drawCapsules(ctx, t);
    this.drawEnemyShots(ctx, t);
    this.drawPlayerShots(ctx);
    this.drawMissiles(ctx);
    this.drawPlayer(ctx, t);
    this.drawBursts(ctx);
    this.drawParticles(ctx, false);
    this.drawPopups(ctx);
    ctx.restore();

    // Reduced motion: no full-screen flash, no pulsing tint.
    if (this.flash > 0 && !this.reducedMotion) {
      ctx.globalAlpha = Math.min(1, this.flash) * 0.85;
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, W, H);
      ctx.globalAlpha = 1;
    }
    if (this.nukeAt) {
      const remaining = Math.max(0, this.nukeAt - this.now);
      ctx.globalAlpha = this.reducedMotion ? 0.2 : 0.18 + 0.12 * Math.sin(t / 90);
      ctx.fillStyle = this.theme.danger;
      ctx.fillRect(0, 0, W, H);
      ctx.globalAlpha = 1;
      ctx.font = `800 ${Math.round(px * 28)}px ${ARCADE_FONT}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = this.theme.gold;
      ctx.fillText(String(Math.ceil(remaining / 1000)), W / 2, H * 0.5);
    }
  }

  private drawInvaders(ctx: CanvasRenderingContext2D): void {
    const upsideDown = this.opts.seasonal === "aprilFools";
    for (const inv of this.invaders) {
      if (!inv.alive) continue;
      if (this.phase === "entering" && this.phaseTimer < inv.enterDelay) continue;
      const pos = this.invaderPos(inv);
      const color = inv.hitFlash > 0 ? this.theme.foreground : this.invaderColor(inv);
      const glow = inv.kind === "golden" || inv.kind === "bug" ? color : null;
      const sprite = this.atlas.get(this.invaderSprite(inv), this.legFrame, this.px, this.invaderPalette(inv, color), glow);
      if (upsideDown) {
        ctx.save();
        ctx.translate(pos.x + pos.w / 2, pos.y + pos.h / 2);
        ctx.scale(1, -1);
        ctx.drawImage(sprite.canvas, -pos.w / 2 - sprite.pad, -pos.h / 2 - sprite.pad);
        ctx.restore();
      } else {
        ctx.drawImage(sprite.canvas, pos.x - sprite.pad, pos.y - sprite.pad);
      }
      if (this.opts.seasonal === "christmas" && inv.row <= 1) {
        const hat = this.atlas.get("santaHat", 0, Math.max(1, this.px - 1), { "1": this.theme.danger, "2": "#ffffff" });
        ctx.drawImage(hat.canvas, pos.x + pos.w / 2 - hat.w / 2 + this.px, pos.y - hat.h + this.px);
      }
      if (inv.hp > 1) {
        ctx.fillStyle = this.theme.foreground;
        ctx.globalAlpha = 0.6;
        ctx.fillRect(pos.x, pos.y - this.px * 2, pos.w, Math.max(1, this.px / 2));
        ctx.globalAlpha = 1;
      }
    }
  }

  private drawBoss(ctx: CanvasRenderingContext2D, t: number): void {
    const boss = this.boss;
    if (!boss) return;
    const size = this.bossSize(boss.kind);
    const scale = this.px * this.bossScale(boss.kind);
    const color = boss.flash > 0.5 ? this.theme.foreground : this.bossColor(boss.kind);
    const palette: Palette =
      boss.kind === "hallucination"
        ? { "1": color, "2": this.theme.danger, "3": this.theme.foreground, "4": this.theme.background }
        : { "1": color };
    const frame = Math.floor(boss.t / 420) % 2;
    const sprite = this.atlas.get(this.bossSprite(boss.kind), frame, Math.max(1, Math.round(scale)), palette, color);
    for (const decoy of boss.decoys) {
      ctx.globalAlpha = 0.35 + 0.25 * Math.abs(Math.sin((t + decoy.x) / 120));
      ctx.drawImage(sprite.canvas, decoy.x - sprite.pad, decoy.y - sprite.pad);
    }
    ctx.globalAlpha = boss.dying > 0 ? Math.max(0.2, 1 - boss.dying / 1400) : 1;
    ctx.drawImage(sprite.canvas, boss.x - sprite.pad, boss.y - sprite.pad);
    ctx.globalAlpha = 1;
    if (boss.kind === "rateLimiter") {
      ctx.font = `800 ${Math.round(scale * 3.2)}px ${ARCADE_FONT}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = this.theme.background;
      ctx.fillText("429", boss.x + size.w / 2, boss.y + size.h * 0.62);
    }
    if (boss.kind === "promptInjector" && Math.floor(t / 400) % 2 === 0) {
      ctx.fillStyle = this.theme.foreground;
      ctx.fillRect(boss.x + size.w * 0.44, boss.y + size.h * 0.78, size.w * 0.12, scale * 0.8);
    }
    const mini = this.atlas.get("minion", 0, Math.max(1, Math.round(this.px * 1.4)), { "1": this.theme.success }, this.theme.success);
    const mini2 = this.atlas.get("minion", 1, Math.max(1, Math.round(this.px * 1.4)), { "1": this.theme.success }, this.theme.success);
    for (const minion of boss.minions) {
      const sprite2 = minion.frame ? mini2 : mini;
      ctx.drawImage(sprite2.canvas, minion.x - sprite2.pad, minion.y - sprite2.pad);
    }
  }

  private drawUfo(ctx: CanvasRenderingContext2D): void {
    if (!this.ufo) return;
    const duck = this.ufo.variant === "duck";
    const scale = Math.max(1, Math.round(this.px * (duck ? 1.1 : 1.2)));
    const palette: Palette = duck
      ? { "1": this.theme.gold, "2": this.theme.warning, "3": this.theme.background }
      : { "1": this.theme.danger };
    const sprite = this.atlas.get(duck ? "duck" : "ufo", 0, scale, palette, duck ? this.theme.gold : this.theme.danger);
    if (duck && this.ufo.dir < 0) {
      ctx.save();
      ctx.translate(this.ufo.x + sprite.w, this.ufo.y);
      ctx.scale(-1, 1);
      ctx.drawImage(sprite.canvas, -sprite.pad, -sprite.pad);
      ctx.restore();
    } else {
      ctx.drawImage(sprite.canvas, this.ufo.x - sprite.pad, this.ufo.y - sprite.pad);
    }
  }

  private drawCapsules(ctx: CanvasRenderingContext2D, t: number): void {
    const size = 9 * this.px;
    for (const capsule of this.capsules) {
      const color = this.powerUpColor(capsule.kind);
      const bob = Math.sin((t + capsule.x) / 160) * this.px;
      const x = capsule.x - size / 2;
      const y = capsule.y - size / 2 + bob;
      ctx.globalAlpha = 0.25;
      ctx.fillStyle = color;
      ctx.fillRect(x - 2, y - 2, size + 4, size + 4);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(1, this.px / 2);
      ctx.strokeRect(x, y, size, size);
      const glyph = this.atlas.get(POWER_UP_GLYPHS[capsule.kind], 0, Math.max(1, this.px - 1), { "1": color });
      ctx.drawImage(glyph.canvas, capsule.x - glyph.w / 2, y + (size - glyph.h) / 2);
    }
  }

  private drawEnemyShots(ctx: CanvasRenderingContext2D, t: number): void {
    const color = this.theme.foreground;
    for (const shot of this.enemyShots) {
      if (shot.kind === "payload") {
        const w = this.payloadWidth();
        ctx.fillStyle = this.theme.danger;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(shot.x - w / 2, shot.y, w, 5 * this.px);
        ctx.globalAlpha = 1;
        ctx.font = `700 ${Math.round(this.px * 3)}px ${ARCADE_FONT}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = this.theme.background;
        ctx.fillText(this.labels.payload, shot.x, shot.y + 2.5 * this.px);
        continue;
      }
      if (shot.kind === "boss") {
        ctx.fillStyle = this.bossColor(this.boss?.kind ?? "hallucination");
        ctx.fillRect(shot.x - this.px, shot.y, this.px * 2, this.px * 4);
        continue;
      }
      const name: SpriteName = shot.kind === "rolling" ? "shotRolling" : shot.kind === "plunger" ? "shotPlunger" : "shotSquiggly";
      const sprite = this.atlas.get(name, Math.floor((t + shot.x) / 90), this.px, { "1": color });
      ctx.drawImage(sprite.canvas, shot.x - sprite.w / 2, shot.y);
    }
  }

  private drawPlayerShots(ctx: CanvasRenderingContext2D): void {
    for (const shot of this.playerShots) {
      if (shot.laser) {
        const gradient = ctx.createLinearGradient(shot.x, shot.y, shot.x, shot.y + 18 * this.px);
        gradient.addColorStop(0, this.theme.foreground);
        gradient.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = this.theme.danger;
        ctx.fillRect(shot.x - this.px, shot.y, this.px * 2, 18 * this.px);
        ctx.fillStyle = gradient;
        ctx.fillRect(shot.x - this.px / 2, shot.y, this.px, 18 * this.px);
        continue;
      }
      ctx.fillStyle = shot.sudo ? this.theme.success : this.theme.accent;
      ctx.fillRect(shot.x - this.px / 2, shot.y, Math.max(2, this.px), 4 * this.px);
      if (shot.sudo) {
        ctx.globalAlpha = 0.5;
        ctx.fillRect(shot.x - this.px * 1.5, shot.y + 4 * this.px, this.px * 3, this.px * 2);
        ctx.globalAlpha = 1;
      }
    }
  }

  private drawMissiles(ctx: CanvasRenderingContext2D): void {
    for (const missile of this.missilesInFlight) {
      ctx.strokeStyle = this.theme.ember;
      ctx.lineWidth = this.px;
      ctx.globalAlpha = 0.6;
      ctx.beginPath();
      for (let i = 0; i < missile.trail.length; i += 2) {
        if (i === 0) ctx.moveTo(missile.trail[i], missile.trail[i + 1]);
        else ctx.lineTo(missile.trail[i], missile.trail[i + 1]);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = this.theme.warning;
      ctx.beginPath();
      ctx.arc(missile.x, missile.y, this.px * 1.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = this.theme.foreground;
      ctx.fillRect(missile.x - this.px / 2, missile.y - this.px / 2, this.px, this.px);
    }
  }

  private drawPlayer(ctx: CanvasRenderingContext2D, t: number): void {
    const w = 13 * this.px;
    const x = this.playerX - w / 2;
    if (!this.playerAlive) {
      if (this.phase === "dying" || this.phase === "gameOver") {
        const frame = Math.floor(this.phaseTimer / 120) % 2;
        const sprite = this.atlas.get("playerExplosion", frame, this.px, { "1": this.theme.accent });
        ctx.drawImage(sprite.canvas, this.playerX - sprite.w / 2, this.playerY);
      }
      return;
    }
    if (this.invulnerableUntil > this.now && Math.floor(t / 90) % 2 === 0) return;
    const color = this.godMode ? this.theme.gold : this.reversedUntil > this.now ? this.theme.danger : this.theme.accent;
    const sprite = this.atlas.get("player", 0, this.px, { "1": color }, color);
    ctx.drawImage(sprite.canvas, x - sprite.pad, this.playerY - sprite.pad);
    if (this.shieldUntil > this.now) {
      ctx.strokeStyle = this.theme.accent;
      ctx.globalAlpha = 0.45 + 0.25 * Math.sin(t / 120);
      ctx.lineWidth = Math.max(1.5, this.px / 1.5);
      ctx.beginPath();
      ctx.ellipse(this.playerX, this.playerY + 4 * this.px, w * 0.75, 7 * this.px, 0, Math.PI, 0);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
  }

  private drawBursts(ctx: CanvasRenderingContext2D): void {
    for (const burst of this.bursts) {
      const scale = Math.max(1, Math.round(this.px * burst.scale));
      const sprite = this.atlas.get("explosion", 0, scale, { "1": burst.color });
      ctx.globalAlpha = Math.max(0, burst.life);
      ctx.drawImage(sprite.canvas, burst.x - sprite.w / 2, burst.y - sprite.h / 2);
    }
    ctx.globalAlpha = 1;
  }

  private drawParticles(ctx: CanvasRenderingContext2D, debris: boolean): void {
    for (const p of this.particles) {
      if ((p.gravity > 0) !== debris) continue;
      ctx.globalAlpha = Math.max(0, Math.min(1, p.life / p.maxLife));
      ctx.fillStyle = p.color;
      ctx.fillRect(p.x - p.size / 2, p.y - p.size / 2, p.size, p.size);
    }
    ctx.globalAlpha = 1;
  }

  private drawPopups(ctx: CanvasRenderingContext2D): void {
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (const popup of this.popups) {
      ctx.globalAlpha = Math.max(0, Math.min(1, popup.life / (popup.maxLife * 0.5)));
      ctx.font = `800 ${popup.size}px ${ARCADE_FONT}`;
      ctx.lineWidth = 3;
      ctx.strokeStyle = this.theme.isLight ? "rgba(255,255,255,0.85)" : "rgba(0,0,0,0.65)";
      ctx.strokeText(popup.text, popup.x, popup.y);
      ctx.fillStyle = popup.color;
      ctx.fillText(popup.text, popup.x, popup.y);
    }
    ctx.globalAlpha = 1;
    if (this.combo >= 5 && this.phase === "playing") {
      ctx.font = `800 ${Math.round(12 + multiplierFor(this.combo) * 2)}px ${ARCADE_FONT}`;
      ctx.fillStyle = this.theme.gold;
      ctx.globalAlpha = 0.9;
      ctx.fillText(`${this.labels.combo} x${multiplierFor(this.combo)}`, this.playerX, this.playerY - 18);
      ctx.globalAlpha = 1;
    }
  }

  private payloadWidth(): number {
    return Math.max(80, this.labels.payload.length * this.px * 2);
  }
}

const POWER_UP_GLYPHS: Record<PowerUpKind, SpriteName> = {
  rapid: "puRapid",
  spread: "puSpread",
  laser: "puLaser",
  shield: "puShield",
  slow: "puSlow",
  missiles: "puMissiles",
  sudo: "puSudo",
  oneUp: "puOneUp",
  nuke: "puNuke",
};

const BOSS_ACHIEVEMENTS: Record<BossKind, AchievementId> = {
  hallucination: "hallucination_buster",
  rateLimiter: "rate_limit_exceeded",
  promptInjector: "injection_proof",
  tokenOverflow: "overflow_handled",
};
