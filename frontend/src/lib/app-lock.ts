// SPDX-License-Identifier: Apache-2.0

/**
 * The mobile companion's PIN lock: the rules the web app follows, mirroring
 * `backend/app/services/app_lock.py`.
 *
 * The server is the judge. A companion session locked behind its PIN is
 * refused everywhere the same recognisable way - a 423 whose `detail.code` is
 * `app_locked` (or `app_pin_setup_required` when the organization requires a
 * PIN the person has not chosen yet), a chat stream ending with an
 * `app_locked` frame -
 * and the app then shows its lock screen and nothing else. Too many wrong PINs
 * end the session: a 401 `app_lock_ended`, and the phone is signed out.
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */

export type AppLockCode = "app_locked" | "app_pin_setup_required";

const APP_LOCKED_CODE = "app_locked";
export const APP_PIN_SETUP_REQUIRED_CODE = "app_pin_setup_required";
/** The `code` of the 401 of a session ended by too many wrong PINs. */
const APP_LOCK_ENDED_CODE = "app_lock_ended";
/** The `code` of a wrong PIN (403), with `attempts_left`. */
const APP_PIN_WRONG_CODE = "app_pin_wrong";

/** The frame a chat stream ends with once its session locked (`APP_LOCKED_FRAME_TYPE`). */
const APP_LOCKED_FRAME_TYPE = "app_locked";

/** The window event the API client fires on a 423: the lock gate shows its screen. */
export const APP_LOCKED_EVENT = "xtm:app-locked";

/** How long the app may stay out of sight before it locks itself. */
export const RELOCK_AFTER_HIDDEN_MS = 30_000;

/** The lock code of a 423 `detail`, else `null`. */
export function appLockCode(detail: unknown): AppLockCode | null {
  if (!detail || typeof detail !== "object") return null;
  const { code } = detail as { code?: unknown };
  return code === APP_LOCKED_CODE || code === APP_PIN_SETUP_REQUIRED_CODE ? code : null;
}

/** Whether a 401 `detail` says the PIN ended the session. */
export function isAppLockEnded(detail: unknown): boolean {
  return !!detail && typeof detail === "object" && (detail as { code?: unknown }).code === APP_LOCK_ENDED_CODE;
}

/**
 * Whether a chat stream's frame says its session locked: the lock screen shows,
 * and the app's next request reads whether the PIN ended the session instead.
 */
export function isAppLockedFrame(frame: Record<string, unknown> | null | undefined): boolean {
  return frame?.type === APP_LOCKED_FRAME_TYPE;
}

/** The attempts left of a wrong-PIN 403 `detail`, else `null`. */
export function wrongPinAttemptsLeft(detail: unknown): number | null {
  if (!detail || typeof detail !== "object") return null;
  const { code, attempts_left: left } = detail as { code?: unknown; attempts_left?: unknown };
  return code === APP_PIN_WRONG_CODE && typeof left === "number" && Number.isFinite(left) ? Math.max(0, left) : null;
}

export type PinProblem = "digits" | "length" | "repeated" | "sequence";

/**
 * Why *pin* cannot be chosen as a PIN, else `null`: the same rules the server
 * checks (`pin_problem`), so the person hears it before sending.
 */
export function pinProblem(pin: string, minDigits: number, maxDigits: number): PinProblem | null {
  if (!/^[0-9]+$/.test(pin)) return "digits";
  if (pin.length < minDigits || pin.length > maxDigits) return "length";
  if (new Set(pin).size === 1) return "repeated";
  const steps = new Set<number>();
  for (let i = 1; i < pin.length; i += 1) steps.add(Number(pin[i]) - Number(pin[i - 1]));
  if (steps.size === 1 && (steps.has(1) || steps.has(-1))) return "sequence";
  return null;
}
