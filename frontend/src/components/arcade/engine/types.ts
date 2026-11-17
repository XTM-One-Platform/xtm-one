/**
 * Types shared by the waiting arcade's engine and the React layer around it.
 */
import type { AchievementId } from "./achievements.ts";
import type { SeasonalTheme } from "./easter-eggs.ts";
import type { BossKind, PowerUpKind } from "./rules.ts";

/** A player to beat, from the weekly board handed over when the game starts. */
export interface ArcadeRivalInfo {
  callsign: string;
  score: number;
  rank: number;
}

/** Colours resolved from the design tokens (CSS variables) when the game starts. */
export interface ArcadeTheme {
  isLight: boolean;
  accent: string;
  foreground: string;
  background: string;
  muted: string;
  success: string;
  warning: string;
  danger: string;
  violet: string;
  gold: string;
  ember: string;
  space: string;
}

/** What an interface element is, for the flavour text and the achievements. */
export type DamageKind =
  | "button"
  | "link"
  | "icon"
  | "image"
  | "text"
  | "input"
  | "message"
  | "navigation"
  | "panel"
  | "logo"
  | "composer";

/** The few strings the canvas draws itself - everything else is React. */
export interface ArcadeCanvasLabels {
  destroyed: Record<DamageKind, string>;
  fake: string;
  payload: string;
  sudo: string;
  combo: string;
  legendary: string;
}

export type GamePhase = "entering" | "playing" | "waveClear" | "bossIntro" | "dying" | "gameOver" | "mission";

interface ActivePowerUp {
  kind: PowerUpKind;
  remainingMs: number;
  totalMs: number;
}

export interface HudSnapshot {
  phase: GamePhase;
  score: number;
  hiScore: number;
  lives: number;
  wave: number;
  missiles: number;
  maxMissiles: number;
  multiplier: number;
  combo: number;
  shield: boolean;
  powerUps: ActivePowerUp[];
  rival: ArcadeRivalInfo | null;
  boss: { kind: BossKind; hp: number; maxHp: number } | null;
  throttled: boolean;
  reversed: boolean;
  godMode: boolean;
  rainbow: boolean;
  cheat: boolean;
  uiDestroyed: number;
  /** The invaders hold fire until the player's first input. */
  awaitingPlayer: boolean;
}

export type ArcadeEvent =
  | { type: "wave"; wave: number; boss: BossKind | null }
  | { type: "extraLife"; lives: number }
  | { type: "lifeLost"; lives: number }
  | { type: "powerUp"; kind: PowerUpKind }
  | { type: "achievement"; id: AchievementId }
  | { type: "rivalPassed"; rival: ArcadeRivalInfo; rank: number }
  | { type: "personalBest"; score: number }
  | { type: "cheat"; code: "iddqd" | "idkfa" }
  | { type: "secret"; code: "konami" | "hello" | "answer" | "xyzzy" | "sudo" }
  | { type: "nukeIncoming" }
  | { type: "bossDefeated"; kind: BossKind }
  | { type: "autoPaused" };

export type RunEndReason = "dead" | "quit" | "mission";

export interface RunResult {
  score: number;
  wave: number;
  kills: number;
  shots: number;
  hits: number;
  uiDestroyed: number;
  bosses: number;
  maxCombo: number;
  ufoKills: number;
  durationMs: number;
  cheat: boolean;
  achievements: AchievementId[];
  reason: RunEndReason;
  /** Left without touching the controls or unlocking anything new: nothing to record. */
  untouched: boolean;
}

interface ArcadeGameCallbacks {
  onHud: (hud: HudSnapshot) => void;
  onEvent: (event: ArcadeEvent) => void;
  onGameOver: (result: RunResult) => void;
  /** The loop threw: it has stopped, and the host must close the game. */
  onCrash: (error: unknown) => void;
}

export interface ArcadeGameOptions {
  canvas: HTMLCanvasElement;
  /** The overlay itself: never mistaken for part of the interface. */
  overlayRoot: HTMLElement;
  theme: ArcadeTheme;
  labels: ArcadeCanvasLabels;
  reducedMotion: boolean;
  seasonal: SeasonalTheme | null;
  /** Where the invaders break out from (the waiting panel), in viewport px. */
  origin: { x: number; y: number } | null;
  personalBest: number;
  /** Players right above on the weekly board, closest first. */
  rivals: ArcadeRivalInfo[];
  /** Achievements the player already has: only new ones are celebrated. */
  unlocked: ReadonlySet<string>;
  /** Unlocked before the first frame (e.g. the code that opened the arcade). */
  initialAchievements: AchievementId[];
  callbacks: ArcadeGameCallbacks;
}
