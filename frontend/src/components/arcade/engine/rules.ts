/**
 * Scoring and difficulty rules of the waiting arcade - pure, unit-tested.
 *
 * Everything here is a number the game and the server must agree on. The
 * plausibility bounds at the bottom mirror `backend/app/services/arcade.py`
 * (`backend/tests/test_arcade.py` reads them from this file), and
 * `maxWavePoints()` keeps the scoring honest against them: a strong,
 * legitimate player must never be refused by the server. So does the server's
 * per-wave ceiling (`max_run_score`), rebuilt from the named scoring values
 * below: a value raised here, or a new one, fails that test until the server
 * knows it.
 */

export type InvaderKind = "squid" | "crab" | "octopus" | "bug" | "golden";

/** Classic values, plus the two specials that ride in the formation. */
export const INVADER_POINTS: Record<InvaderKind, number> = {
  squid: 30,
  crab: 20,
  octopus: 10,
  bug: 50,
  golden: 100,
};

/** The formation, top row first: one squid row, two crab rows, two octopus rows. */
export const FORMATION_ROWS: readonly InvaderKind[] = ["squid", "crab", "crab", "octopus", "octopus"];
export const FORMATION_COLUMNS = 11;
const FORMATION_SIZE = FORMATION_ROWS.length * FORMATION_COLUMNS;

// ── The mystery saucer ───────────────────────────────────────────────────────

/** The saucer's regular values, walked by the shot counter. */
const UFO_REGULAR_TABLE = [100, 50, 50, 100, 150, 100, 100, 50, 100, 100, 100, 50, 150, 100] as const;
export const UFO_LEGENDARY_POINTS = 300;
/** The rare rubber duck that crosses instead of the saucer, and any saucer on Pi Day. */
export const UFO_DUCK_POINTS = 500;
export const UFO_PI_POINTS = 314;
/** The original cabinet's secret: the 23rd shot, then every 15th, is worth 300. */
const FIRST_LEGENDARY_SHOT = 23;
const LEGENDARY_SHOT_PERIOD = 15;

export function isLegendaryShot(shot: number): boolean {
  return shot >= FIRST_LEGENDARY_SHOT && (shot - FIRST_LEGENDARY_SHOT) % LEGENDARY_SHOT_PERIOD === 0;
}

/** Points for hitting the saucer with the `shot`-th trigger pull (1-based). */
export function ufoPoints(shot: number): number {
  if (isLegendaryShot(shot)) return UFO_LEGENDARY_POINTS;
  const n = UFO_REGULAR_TABLE.length;
  return UFO_REGULAR_TABLE[((Math.floor(shot) % n) + n) % n];
}

// ── Combo multiplier ─────────────────────────────────────────────────────────

/** Every COMBO_STEP consecutive hits add one to the multiplier. A miss resets it. */
const COMBO_STEP = 5;
export const MAX_MULTIPLIER = 4;

export function multiplierFor(combo: number): number {
  return Math.min(MAX_MULTIPLIER, 1 + Math.floor(Math.max(0, combo) / COMBO_STEP));
}

// ── Lives ────────────────────────────────────────────────────────────────────

export const START_LIVES = 3;
export const MAX_LIVES = 6;
/** The classic bonus base at 1,500 - then one more every 20,000 points. */
export const EXTRA_LIFE_FIRST = 1500;
export const EXTRA_LIFE_EVERY = 20000;

/** How many bonus lives `score` has earned so far. */
export function extraLivesEarned(score: number): number {
  if (score < EXTRA_LIFE_FIRST) return 0;
  return 1 + Math.floor((score - EXTRA_LIFE_FIRST) / EXTRA_LIFE_EVERY);
}

// ── Bonuses ──────────────────────────────────────────────────────────────────

/** Clearing a wave: a step more each wave, up to a cap. */
export const WAVE_CLEAR_BONUS_BASE = 500;
export const WAVE_CLEAR_BONUS_STEP = 150;
export const WAVE_CLEAR_BONUS_MAX = 2000;

export function waveClearBonus(wave: number): number {
  return Math.min(WAVE_CLEAR_BONUS_MAX, WAVE_CLEAR_BONUS_BASE + WAVE_CLEAR_BONUS_STEP * Math.max(0, wave - 1));
}

/** Every fifth wave is a boss instead of a formation. */
export const BOSS_WAVE_EVERY = 5;
/** Defeating a boss: a step more each boss, up to a cap. */
export const BOSS_POINTS_BASE = 2500;
export const BOSS_POINTS_STEP = 250;
export const BOSS_POINTS_MAX = 5000;

/** Points for defeating the boss of `wave`. */
export function bossPoints(wave: number): number {
  const nth = Math.max(1, Math.floor(wave / BOSS_WAVE_EVERY));
  return Math.min(BOSS_POINTS_MAX, BOSS_POINTS_BASE + BOSS_POINTS_STEP * (nth - 1));
}

/** The waiting bonus's cap - the only part of it the server's ceiling needs. */
export const SURVIVAL_BONUS_MAX = 3000;

/** Rewarded when the agent's answer arrives while you are still alive. */
export function survivalBonus(waitSeconds: number): number {
  return Math.min(SURVIVAL_BONUS_MAX, 500 + 10 * Math.floor(Math.max(0, waitSeconds)));
}

/** A hole punched into the interface. */
export const UI_CRATER_POINTS = 2;
/** An interface element that crumbled. */
export const UI_DESTROYED_POINTS = 25;
/** Each element a nuke wipes. */
export const NUKE_UI_POINTS = 10;
/**
 * The most elements one game is paid for: the first this many it tracks
 * (`DamageResult.scored`, screen-damage.ts), and never more destroyed ones in
 * its count. Past them - recycled room, a nuke on a busy page - the interface
 * keeps falling, for show: the server refuses a run counting more
 * (`MAX_UI_DESTROYED_PER_RUN`), and its crater cap assumes it.
 */
export const MAX_SCORED_ELEMENTS = 320;
export const PACIFIST_BONUS = 500;
/** Shooting down the Prompt Injector's payload - it drops one every interval for as long as it lives. */
export const PAYLOAD_POINTS = 50;
export const PAYLOAD_INTERVAL_MS = 6000;
/** @public */
export const DECOY_POINTS = 0;
export const MINION_POINTS = 40;
/** The Token Overflow splits at these shares of its hit points, into this many minions each time. */
export const OVERFLOW_SPLIT_THRESHOLDS: readonly number[] = [2 / 3, 1 / 3];
export const MINIONS_PER_SPLIT = 3;

// ── Waves and bosses ─────────────────────────────────────────────────────────

export type BossKind = "hallucination" | "rateLimiter" | "promptInjector" | "tokenOverflow";
export const BOSS_ROTATION: readonly BossKind[] = ["hallucination", "rateLimiter", "promptInjector", "tokenOverflow"];

const BOSS_BASE_HP: Record<BossKind, number> = {
  hallucination: 30,
  rateLimiter: 40,
  promptInjector: 50,
  tokenOverflow: 36,
};

export interface WaveConfig {
  wave: number;
  /** The boss of a boss wave, `null` for a formation wave. */
  boss: BossKind | null;
  /** How many times the boss rotation has come round (HP grows with it). */
  bossCycle: number;
  /** Half-rows the formation starts lower, like the original's descending starts. */
  startDrop: number;
  /** Multiplies the march interval: smaller is faster. */
  stepFactor: number;
  /** Base time between two enemy shots. */
  fireIntervalMs: number;
  maxEnemyBullets: number;
  /** Chance that a formation slot holds a special invader. */
  specialChance: number;
  /** From wave 6 the top row takes two hits. */
  armoredTopRow: boolean;
  /** Multiplies enemy shot speed. */
  enemyBulletSpeed: number;
}

export function waveConfig(wave: number): WaveConfig {
  const w = Math.max(1, Math.floor(wave));
  const isBoss = w % BOSS_WAVE_EVERY === 0;
  const bossIndex = Math.floor(w / BOSS_WAVE_EVERY) - 1;
  return {
    wave: w,
    boss: isBoss ? BOSS_ROTATION[bossIndex % BOSS_ROTATION.length] : null,
    bossCycle: isBoss ? Math.floor(bossIndex / BOSS_ROTATION.length) : 0,
    startDrop: Math.min(4, Math.floor((w - 1) * 0.6)),
    stepFactor: Math.max(0.45, 1 - 0.06 * (w - 1)),
    fireIntervalMs: Math.max(350, 1300 - 90 * (w - 1)),
    maxEnemyBullets: Math.min(6, 3 + Math.floor(w / 3)),
    specialChance: Math.min(0.12, 0.03 + 0.01 * w),
    armoredTopRow: w >= 6,
    enemyBulletSpeed: Math.min(1.4, 1 + 0.03 * (w - 1)),
  };
}

export function bossMaxHp(kind: BossKind, cycle: number): number {
  return Math.round(BOSS_BASE_HP[kind] * (1 + 0.5 * Math.max(0, cycle)));
}

/** The march: one step per interval, faster as the formation thins out. */
export function stepIntervalMs(alive: number, stepFactor: number): number {
  return Math.min(900, Math.max(18, alive * 16 * stepFactor));
}

// ── Power-ups ────────────────────────────────────────────────────────────────

export type PowerUpKind =
  | "rapid"
  | "spread"
  | "laser"
  | "shield"
  | "slow"
  | "missiles"
  | "sudo"
  | "oneUp"
  | "nuke";

export const POWER_UP_WEIGHTS: Record<PowerUpKind, number> = {
  rapid: 20,
  spread: 18,
  laser: 12,
  shield: 16,
  slow: 10,
  missiles: 14,
  sudo: 5,
  oneUp: 3,
  nuke: 2,
};

/** Timed effects; the others act once. */
export const POWER_UP_DURATIONS_MS: Partial<Record<PowerUpKind, number>> = {
  rapid: 10000,
  spread: 10000,
  laser: 7000,
  shield: 25000,
  slow: 7000,
  sudo: 8000,
};

const POWER_UP_ORDER = Object.keys(POWER_UP_WEIGHTS) as PowerUpKind[];
const POWER_UP_TOTAL = POWER_UP_ORDER.reduce((sum, kind) => sum + POWER_UP_WEIGHTS[kind], 0);

/** Weighted pick from a uniform `roll` in [0, 1). */
export function pickPowerUp(roll: number): PowerUpKind {
  let threshold = Math.min(0.999999, Math.max(0, roll)) * POWER_UP_TOTAL;
  for (const kind of POWER_UP_ORDER) {
    threshold -= POWER_UP_WEIGHTS[kind];
    if (threshold < 0) return kind;
  }
  return POWER_UP_ORDER[POWER_UP_ORDER.length - 1];
}

/** Chance that destroying an invader of `kind` drops a capsule. */
export function dropChance(kind: InvaderKind, wave: number): number {
  if (kind === "bug") return 1;
  if (kind === "golden") return 0.5;
  return Math.min(0.07, 0.035 * (1 + Math.max(0, wave - 1) / 10));
}

// ── The cannon ───────────────────────────────────────────────────────────────

/** Milliseconds between two volleys: plain, under rapid fire, throttled (429). */
export const FIRE_COOLDOWN_MS = 180;
export const RAPID_FIRE_COOLDOWN_MS = 110;
export const THROTTLED_FIRE_COOLDOWN_MS = 520;
/** The spread power-up fires one bullet per angle. */
export const SPREAD_ANGLES: readonly number[] = [-0.2, 0, 0.2];

// ── Missiles ─────────────────────────────────────────────────────────────────

export const START_MISSILES = 3;
export const MAX_MISSILES = 5;
/** One missile comes back every MISSILE_REGEN_MS, up to MAX_MISSILES. */
export const MISSILE_REGEN_MS = 7000;

// ── Plausibility (mirrors backend/app/services/arcade.py) ────────────────────

export const MAX_POINTS_PER_SECOND = 1500;
export const SCORE_ALLOWANCE = 10_000;
/** @public */
export const MIN_SECONDS_PER_WAVE = 5;
/** Above the cannon's fastest cadence (rapid fire with the spread). */
export const MAX_SHOTS_PER_SECOND = 30;
export const SHOT_ALLOWANCE = 30;

export function maxPlausibleScore(seconds: number): number {
  return Math.floor(SCORE_ALLOWANCE + MAX_POINTS_PER_SECOND * Math.max(0, seconds));
}

/**
 * The fastest a wave can be cleared: 55 invaders at the cannon's best rate
 * (rapid fire, three shots in flight) is about five kills a second.
 */
export const FASTEST_WAVE_SECONDS = 12;

/** Most boss minions and payloads a boss wave can put in front of the cannon. */
const MAX_BOSS_EXTRAS = 12;

/**
 * A dense wave, for the rate bound: a formation wave with its expected specials
 * rounded up (all of them golden), every kill at the top multiplier, its clear
 * bonus and the legendary saucer; or a boss with every minion and payload shot
 * down. Beyond any real wave - it exists to keep the rules under the server's
 * points-per-second bound. (The per-wave ceiling takes the true worst case
 * instead: every formation slot golden.)
 */
export function maxWavePoints(wave: number): number {
  const config = waveConfig(wave);
  if (config.boss) {
    const extras = MAX_BOSS_EXTRAS * Math.max(MINION_POINTS, PAYLOAD_POINTS) * MAX_MULTIPLIER;
    return bossPoints(wave) + extras + UFO_LEGENDARY_POINTS;
  }
  const base = FORMATION_ROWS.reduce((sum, kind) => sum + INVADER_POINTS[kind] * FORMATION_COLUMNS, 0);
  const specials = Math.ceil(FORMATION_SIZE * config.specialChance);
  const specialsExtra = specials * (INVADER_POINTS.golden - INVADER_POINTS.octopus);
  return (base + specialsExtra) * MAX_MULTIPLIER + waveClearBonus(wave) + UFO_LEGENDARY_POINTS;
}
