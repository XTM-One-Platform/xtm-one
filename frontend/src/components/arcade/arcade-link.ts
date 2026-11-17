/**
 * Opening the waiting arcade from a link: `?arcade=play` on any route.
 * The chat embedded in OpenCTI, OpenAEV and OpenCRQ (`@filigran/chatbot`)
 * invites its users to play here through `<XTM One>/chat?arcade=play`.
 * `?arcade=leaderboard` opens the leaderboard instead: what a reload offered
 * for a leaderboard the page could not load asks for.
 *
 * Such a link often reaches a browser that is not signed in to XTM One: the
 * route guard sends it to `/login` without the query string, and SSO leaves
 * the tab for the identity provider. The request is therefore kept in the
 * tab's session storage until the sign-in completes, for a few minutes only,
 * so a stale one never starts a game long after the click.
 *
 * Type imports only: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */
import type { ArcadeGate } from "./arcade-gate.ts";

const ARCADE_LINK_PARAM = "arcade";
const ARCADE_LINK_VALUE = "play";
const ARCADE_LEADERBOARD_LINK_VALUE = "leaderboard";
export const ARCADE_LINK_STORAGE_KEY = "xtm-one:arcade-link";
export const ARCADE_LINK_TTL_MS = 10 * 60_000;

/** The part of `Storage` the link needs; `null` when the browser offers none. */
export interface ArcadeLinkStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** What a request opens: a game, or the leaderboard. */
export type ArcadeRequestTarget = "game" | "leaderboard";

/** A request, dated from the click. */
export interface ArcadeRequest {
  target: ArcadeRequestTarget;
  requestedAt: number;
}

/** What the query string asks the arcade for, or `null`. */
export function arcadeRequestTarget(search: string): ArcadeRequestTarget | null {
  const value = new URLSearchParams(search).get(ARCADE_LINK_PARAM);
  if (value === ARCADE_LINK_VALUE) return "game";
  return value === ARCADE_LEADERBOARD_LINK_VALUE ? "leaderboard" : null;
}

/** True when the query string asks for the arcade. */
export function requestsArcade(search: string): boolean {
  return arcadeRequestTarget(search) !== null;
}

/** The query string without the arcade request: `?`-prefixed, or empty. */
export function withoutArcadeRequest(search: string): string {
  const params = new URLSearchParams(search);
  params.delete(ARCADE_LINK_PARAM);
  const rest = params.toString();
  return rest ? `?${rest}` : "";
}

/** The query string asking the arcade for `target`, the rest kept:
 *  `?`-prefixed. What a reload offered for a game or a leaderboard the page
 *  could not load opens. */
export function withArcadeRequest(search: string, target: ArcadeRequestTarget = "game"): string {
  const params = new URLSearchParams(search);
  params.set(ARCADE_LINK_PARAM, target === "game" ? ARCADE_LINK_VALUE : ARCADE_LEADERBOARD_LINK_VALUE);
  return `?${params.toString()}`;
}

/** The page at `href` again, asking the arcade for `target`. */
export function arcadeRequestUrl(href: string, target: ArcadeRequestTarget): string {
  const url = new URL(href);
  url.search = withArcadeRequest(url.search, target);
  return url.href;
}

/** True while a request made at `requestedAt` may still be honoured. */
export function isFreshArcadeRequest(requestedAt: number, now: number): boolean {
  return now - requestedAt >= 0 && now - requestedAt < ARCADE_LINK_TTL_MS;
}

/** How a request is remembered: its date, prefixed for the leaderboard. */
const LEADERBOARD_REMEMBERED_PREFIX = `${ARCADE_LEADERBOARD_LINK_VALUE}:`;

function rememberedValue({ target, requestedAt }: ArcadeRequest): string {
  return target === "game" ? String(requestedAt) : `${LEADERBOARD_REMEMBERED_PREFIX}${requestedAt}`;
}

function rememberedRequest(value: string): ArcadeRequest {
  return value.startsWith(LEADERBOARD_REMEMBERED_PREFIX)
    ? { target: "leaderboard", requestedAt: Number(value.slice(LEADERBOARD_REMEMBERED_PREFIX.length)) }
    : { target: "game", requestedAt: Number(value) };
}

/**
 * The request `search` makes (then remembered, so it survives the sign-in),
 * else one remembered less than `ARCADE_LINK_TTL_MS` ago, else `null`. A stale
 * or unreadable request is forgotten. Storage that throws (private mode,
 * disabled storage) only loses the survival across sign-in.
 */
export function captureArcadeRequest(
  search: string,
  storage: ArcadeLinkStorage | null,
  now: number,
): ArcadeRequest | null {
  const target = arcadeRequestTarget(search);
  try {
    if (target !== null) {
      const request = { target, requestedAt: now };
      storage?.setItem(ARCADE_LINK_STORAGE_KEY, rememberedValue(request));
      return request;
    }
    const stored = storage?.getItem(ARCADE_LINK_STORAGE_KEY);
    if (stored == null) return null;
    const request = rememberedRequest(stored);
    if (isFreshArcadeRequest(request.requestedAt, now)) return request;
    storage?.removeItem(ARCADE_LINK_STORAGE_KEY);
    return null;
  } catch {
    return target === null ? null : { target, requestedAt: now };
  }
}

type ArcadeLinkVerdict = "wait" | "play" | "drop";

/**
 * What a request made at `requestedAt` does now, decided by the arcade's one
 * gate (`arcade-gate.ts`). Signed out, it waits for the sign-in. Signed
 * in, it waits while the gate is `pending` - a token in storage can be one the
 * server refuses, whose first request sends the tab back to `/login`, where
 * the request must still be pending - and while the platform's Terms of Use
 * await acceptance (`consent`), then opens its game or leaderboard when the
 * gate opens, or is dropped when it closes (impersonating, arcade off).
 * Expired on the way (10 minutes after the click, `ARCADE_LINK_TTL_MS`), it
 * is dropped.
 */
export function arcadeLinkVerdict(state: {
  requestedAt: number;
  now: number;
  /** A session token is held. */
  signedIn: boolean;
  /** The provider's `arcadeGate()` answer. */
  gate: ArcadeGate;
}): ArcadeLinkVerdict {
  if (!isFreshArcadeRequest(state.requestedAt, state.now)) return "drop";
  if (!state.signedIn || state.gate === "pending" || state.gate === "consent") return "wait";
  return state.gate === "open" ? "play" : "drop";
}

/** Forget a remembered request, once it has been honoured or dropped. */
export function forgetArcadeRequest(storage: ArcadeLinkStorage | null): void {
  try {
    storage?.removeItem(ARCADE_LINK_STORAGE_KEY);
  } catch {
    // Nothing was stored.
  }
}
