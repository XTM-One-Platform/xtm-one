/**
 * Secrets of the waiting arcade - pure, unit-tested.
 *
 * Secret codes, calendar skins and the texts the waiting panel and the pause
 * screen draw from. Every string here is an English i18n id, translated where
 * it is displayed. Written without DOM types so `node --test` runs it as is.
 */
import type { AchievementId } from "./achievements.ts";

// ── Secret codes ─────────────────────────────────────────────────────────────

export type SecretCode = "konami" | "iddqd" | "idkfa" | "hello" | "answer" | "xyzzy" | "sudo";

export const KONAMI_CODE: readonly string[] = [
  "arrowup",
  "arrowup",
  "arrowdown",
  "arrowdown",
  "arrowleft",
  "arrowright",
  "arrowleft",
  "arrowright",
  "b",
  "a",
];

const typed = (word: string): readonly string[] => word.toLowerCase().split("");

const SECRET_CODES: Readonly<Record<SecretCode, readonly string[]>> = {
  konami: KONAMI_CODE,
  iddqd: typed("iddqd"),
  idkfa: typed("idkfa"),
  hello: typed("hello"),
  answer: typed("42"),
  xyzzy: typed("xyzzy"),
  sudo: typed("sudo"),
};

/** A `KeyboardEvent.key` as the listener compares it, or `null` for keys it ignores. */
export function normalizeKey(key: string): string | null {
  if (!key) return null;
  if (key.length === 1) return key.toLowerCase();
  const lower = key.toLowerCase();
  return lower.startsWith("arrow") ? lower : null;
}

function endsWith(buffer: readonly string[], sequence: readonly string[]): boolean {
  if (sequence.length === 0 || buffer.length < sequence.length) return false;
  const offset = buffer.length - sequence.length;
  for (let i = 0; i < sequence.length; i += 1) {
    if (buffer[offset + i] !== sequence[i]) return false;
  }
  return true;
}

/** Watches a stream of keys and reports a secret code the moment it completes. */
export class SecretCodeListener {
  private buffer: string[] = [];
  private readonly codes: Readonly<Partial<Record<SecretCode, readonly string[]>>>;
  private readonly maxLength: number;

  constructor(codes: Readonly<Partial<Record<SecretCode, readonly string[]>>> = SECRET_CODES) {
    this.codes = codes;
    this.maxLength = Math.max(1, ...Object.values(codes).map((sequence) => sequence?.length ?? 0));
  }

  push(key: string): SecretCode | null {
    const normalized = normalizeKey(key);
    if (normalized === null) return null;
    this.buffer.push(normalized);
    if (this.buffer.length > this.maxLength) this.buffer.shift();
    for (const [name, sequence] of Object.entries(this.codes)) {
      if (sequence && endsWith(this.buffer, sequence)) {
        this.buffer = [];
        return name as SecretCode;
      }
    }
    return null;
  }

  reset(): void {
    this.buffer = [];
  }
}

/** Minimal shape of an event target, so this stays DOM-free for the unit tests. */
interface TargetLike {
  tagName?: string;
  type?: string;
  isContentEditable?: boolean;
  getAttribute?: (name: string) => string | null;
}

const NON_TEXT_INPUTS = new Set([
  "button", "checkbox", "color", "file", "image", "radio", "range", "reset", "submit",
]);

/** Is the user typing into this target? Global shortcuts must never steal those keys. */
export function isEditableTarget(target: unknown): boolean {
  if (!target || typeof target !== "object") return false;
  const el = target as TargetLike;
  if (el.isContentEditable) return true;
  const tag = (el.tagName ?? "").toUpperCase();
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") return !NON_TEXT_INPUTS.has((el.type ?? "text").toLowerCase());
  const role = el.getAttribute?.("role");
  return role === "textbox" || role === "searchbox" || role === "combobox";
}

// ── The calendar ─────────────────────────────────────────────────────────────

export type SeasonalTheme = "halloween" | "christmas" | "aprilFools" | "piDay" | "mayFourth";

/** The skin of the day, in the player's local time. */
export function seasonalTheme(date: Date): SeasonalTheme | null {
  const month = date.getMonth(); // 0-based
  const day = date.getDate();
  if (month === 9 && day >= 24) return "halloween";
  if (month === 11 && day >= 12) return "christmas";
  if (month === 3 && day === 1) return "aprilFools";
  if (month === 2 && day === 14) return "piDay";
  if (month === 4 && day === 4) return "mayFourth";
  return null;
}

/** What playing at this very moment unlocks. */
export function playTimeAchievements(date: Date): AchievementId[] {
  const earned: AchievementId[] = [];
  const hour = date.getHours();
  if (hour < 5) earned.push("night_owl");
  else if (hour < 7) earned.push("early_bird");
  if (date.getDay() === 5 && hour >= 17) earned.push("friday_deploy");
  const theme = seasonalTheme(date);
  if (theme === "halloween") earned.push("trick_or_treat");
  if (theme === "christmas") earned.push("ho_ho_ho");
  if (theme === "aprilFools") earned.push("april_fool");
  return earned;
}

// ── Texts ────────────────────────────────────────────────────────────────────

/** What the waiting panel says while the agent works (the invader's targets). */
export const WAITING_MESSAGES: readonly string[] = [
  "Crunching the data",
  "Connecting the dots",
  "Consulting the sources",
  "Thinking it through",
  "Reticulating splines",
  "Analyzing the details",
  "Almost there",
  "Putting it together",
  "Polishing the answer",
  "Wrapping things up",
];

/** Now and then, one of these slips in instead. */
export const RARE_WAITING_MESSAGES: readonly string[] = [
  "Asking the rubber duck",
  "Negotiating with the tokens",
  "Warming up the neurons",
  "Dividing by zero, carefully",
  "Herding the invaders",
  "Defragmenting the thoughts",
  "Consulting the ancient scrolls",
  "Untangling the context window",
  "Bribing the rate limiter",
  "Counting to infinity, twice",
];

export const RARE_MESSAGE_CHANCE = 0.06;

/** The `index`-th waiting message, or a rare one when `roll` says so. */
export function waitingMessage(index: number, roll: number): string {
  if (roll < RARE_MESSAGE_CHANCE) {
    const pick = Math.floor((roll / RARE_MESSAGE_CHANCE) * RARE_WAITING_MESSAGES.length);
    return RARE_WAITING_MESSAGES[Math.min(RARE_WAITING_MESSAGES.length - 1, pick)];
  }
  const n = WAITING_MESSAGES.length;
  return WAITING_MESSAGES[((Math.floor(index) % n) + n) % n];
}

/** One of these shows on the pause screen. */
export const PAUSE_TIPS: readonly string[] = [
  "Legend says the 23rd shot is special.",
  "Right-click anywhere to send a missile there.",
  "Everything you break is rebuilt when you leave.",
  "Catch the falling capsules. Some of them are very rare.",
  "Hit without missing to raise your multiplier.",
  "Your agent keeps working while you play.",
  "Some saucers are rarer than others.",
  "Every fifth wave, something big shows up.",
  "Old cheat codes still work here. Some of them.",
  "Hold Space for autofire.",
  "A shield absorbs one hit. Use it wisely.",
  "Invaders shoot holes in the interface too.",
];
