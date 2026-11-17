// SPDX-License-Identifier: Apache-2.0

/**
 * Web session lifetime and idle timeout: the rules the web app
 * follows, mirroring `backend/app/core/sessions.py`.
 *
 * A session ends past its absolute lifetime, or after the idle timeout
 * without activity. The server is the one judge: every surface refuses an
 * ended session the same recognisable way - a 401 whose `detail.code` is
 * `session_expired`, a chat or activity stream ending with a
 * `session_expired` event - and the web app then signs
 * out and says why on the sign-in page. Activity is an explicit signal the app
 * sends on real input (`POST /auth/session/activity`); nothing it requests on
 * its own counts. The warning before the idle deadline is timed on the
 * server's clock.
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */

/** `pin`: the companion's PIN was entered wrong too many times (`lib/app-lock.ts`). */
export type SessionEndReason = "idle" | "lifetime" | "pin";

/** The `code` of a 401 whose session ended (`SESSION_EXPIRED_CODE`). */
export const SESSION_EXPIRED_CODE = "session_expired";

/** The query parameter the sign-in page reads to say why the session ended. */
export const SESSION_ENDED_PARAM = "session_ended";

/** Where the page a session ended on is kept, to land there again after the sign-in. */
export const SESSION_RETURN_PATH_KEY = "session_return_path";

/** A reason the session-expiry signals carry (`pin` comes from the PIN lock, never from them). */
function asReason(value: unknown): SessionEndReason | null {
  return value === "idle" || value === "lifetime" ? value : null;
}

/** The reason of a 401 `detail` that says the session ended, else `null` (any other 401). */
export function sessionEndReason(detail: unknown): SessionEndReason | null {
  if (!detail || typeof detail !== "object") return null;
  const { code, reason } = detail as { code?: unknown; reason?: unknown };
  return code === SESSION_EXPIRED_CODE ? (asReason(reason) ?? "lifetime") : null;
}

/** The reason a `session_expired` event names (the chat panel's `onSessionEnded`), its lifetime when it names none. */
export function namedSessionEndReason(reason: unknown): SessionEndReason {
  return asReason(reason) ?? "lifetime";
}

/** The reason an activity stream's `session_expired` event carries. */
export function streamSessionEndReason(data: string): SessionEndReason {
  try {
    return namedSessionEndReason((JSON.parse(data) as { reason?: unknown } | null)?.reason);
  } catch {
    return "lifetime";
  }
}

/** The sign-in page, told why the session ended (a router path). */
export function sessionEndedLoginPath(reason: SessionEndReason): string {
  return `/login?${SESSION_ENDED_PARAM}=${reason}`;
}

/** The reason the sign-in page was opened with, else `null`. */
export function readSessionEndedParam(search: string): SessionEndReason | null {
  const value = new URLSearchParams(search).get(SESSION_ENDED_PARAM);
  return value === "pin" ? "pin" : asReason(value);
}

/**
 * A page to come back to after signing in again: a router path on this
 * origin, never the sign-in page itself, else `null`. Read from session
 * storage, which another script of the origin could have written.
 */
export function safeReturnPath(raw: string | null | undefined): string | null {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return null;
  if (raw.length > 2048 || [...raw].some((char) => char.charCodeAt(0) < 0x20)) return null;
  const path = raw.split(/[?#]/, 1)[0];
  if (path === "/login" || path.startsWith("/login/")) return null;
  return raw;
}

/**
 * Whether a signed-in URL (`/login#token=...`) brings another session than the
 * one this browser already holds. The profile, the role and every read of the
 * query cache belong to the stored session, so a switch is landed with the
 * cache cleared and a full page load, as the end of an impersonation is; a
 * first sign-in (nothing stored) or the same token again is not a switch.
 */
export function signedInUrlSwitchesSession(stored: string | null, incoming: string): boolean {
  return !!stored && stored !== incoming;
}

// ---------------------------------------------------------------------------
// The activity signal and the deadlines
// ---------------------------------------------------------------------------

/** The server's rate limit allows one signal every 3 seconds; one every 15 is plenty. */
export const ACTIVITY_SIGNAL_MIN_INTERVAL_MS = 15_000;
export const ACTIVITY_SIGNAL_MAX_INTERVAL_MS = 60_000;
/**
 * Without an idle timeout the signal still goes out now and then, so the
 * deadline is right the moment an administrator turns one on: the shortest
 * the server accepts is 5 minutes (`SESSION_IDLE_MINUTES_MIN`), several of
 * these intervals.
 */
export const ACTIVITY_SIGNAL_WITHOUT_IDLE_TIMEOUT_MS = ACTIVITY_SIGNAL_MAX_INTERVAL_MS;

/** How often real input is signalled: a quarter of the idle timeout, within the bounds above. */
export function activitySignalIntervalMs(idleTimeoutSeconds: number): number {
  if (!(idleTimeoutSeconds > 0)) return ACTIVITY_SIGNAL_WITHOUT_IDLE_TIMEOUT_MS;
  return Math.min(
    ACTIVITY_SIGNAL_MAX_INTERVAL_MS,
    Math.max(ACTIVITY_SIGNAL_MIN_INTERVAL_MS, (idleTimeoutSeconds * 1000) / 4),
  );
}

/** How long before the idle deadline the warning shows: a minute, or a quarter of a short timeout. */
export function idleWarningLeadMs(idleTimeoutSeconds: number): number {
  return Math.min(60_000, Math.max(0, (idleTimeoutSeconds * 1000) / 4));
}

/** `setTimeout`'s ceiling (about 24.8 days): a longer wait fires early and is scheduled again. */
export const MAX_TIMER_DELAY_MS = 2_147_483_647;

/** The delay of a timer that must fire at *at* (local clock), within what `setTimeout` takes. */
export function timerDelay(at: number, now: number): number {
  return Math.min(MAX_TIMER_DELAY_MS, Math.max(0, at - now));
}

/** What `GET /auth/session` answers (`SessionStatus` in `@/types`). */
interface SessionDeadlines {
  kind: string;
  server_time: string;
  expires_at: string | null;
  idle_timeout_seconds: number;
  idle_expires_at: string | null;
}

interface SessionTiming {
  /** When the session ends, idle or past its lifetime, on the local clock; `null` when nothing ends it. */
  endsAt: number | null;
  /** When the idle deadline falls, on the local clock; `null` without an enforced idle timeout. */
  idleEndsAt: number | null;
  /** When to warn that the idle deadline is near; `null` when the lifetime ends the session first. */
  warnAt: number | null;
}

function parseTime(value: string | null): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * The deadlines of a session on the local clock. *receivedAt* is when the
 * answer arrived: the server's clock is read off `server_time`, so a client
 * clock that is off by minutes still warns and checks on time.
 */
export function sessionTiming(status: SessionDeadlines, receivedAt: number): SessionTiming | null {
  if (status.kind !== "session") return null;
  const serverNow = parseTime(status.server_time);
  const offset = serverNow === null ? 0 : serverNow - receivedAt;
  const expiresAt = parseTime(status.expires_at);
  const idleExpiresAt = parseTime(status.idle_expires_at);
  const lifetimeEndsAt = expiresAt === null ? null : expiresAt - offset;
  const idleEndsAt = idleExpiresAt === null ? null : idleExpiresAt - offset;
  const ends = [lifetimeEndsAt, idleEndsAt].filter((value): value is number => value !== null);
  const endsAt = ends.length ? Math.min(...ends) : null;
  const warnAt =
    idleEndsAt !== null && (lifetimeEndsAt === null || idleEndsAt < lifetimeEndsAt)
      ? idleEndsAt - idleWarningLeadMs(status.idle_timeout_seconds)
      : null;
  return { endsAt, idleEndsAt, warnAt };
}

/** The whole seconds left before *deadline*, never below 0. */
export function secondsLeft(deadline: number, now: number): number {
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}
