/**
 * Whether the waiting arcade may open at all - the one answer every
 * way in reads. `ArcadeProvider` evaluates it once and publishes it as
 * `useArcade().enabled`: the hidden code listens, the palette lists the
 * arcade commands, and the waiting panel's Take control (`useWaitingArcade`)
 * is offered only while it is open;
 * `openLeaderboard()` - a `?arcade=leaderboard` link included - and
 * `launch()` - the leaderboard's Play, a `?arcade=play` link and every other
 * way in - refuse otherwise. Never re-derive it at an entry point.
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */

/**
 * `pending`: an input it needs is still loading - closed for now.
 * `consent`: closed because the platform's Terms of Use await acceptance -
 * the one closed reason a `?arcade=play` link waits out (`arcade-link.ts`).
 * Signed out, impersonating and arcade off win over it: `closed`.
 */
export type ArcadeGate = "open" | "closed" | "pending" | "consent";

export interface ArcadeGateInputs {
  /** A session token is held. */
  signedIn: boolean;
  /** An administrator acts as another user: nothing may be recorded under that user's name (the server refuses a game session). */
  impersonating: boolean;
  /** The Terms of Use consent screen shows instead of the app (`termsOfUsePending`) and the server refuses every call; `undefined` until `/auth/me` answers. */
  termsPending: boolean | undefined;
  /** `ARCADE_ENABLED`, read from `/auth/config`; `undefined` while it loads, when it failed or on a backend that predates the flag (on by default). */
  arcadeEnabled: boolean | undefined;
  /** `/auth/config` has not answered yet. */
  configPending: boolean;
}

export function arcadeGate({
  signedIn,
  impersonating,
  termsPending,
  arcadeEnabled,
  configPending,
}: ArcadeGateInputs): ArcadeGate {
  if (!signedIn || impersonating || arcadeEnabled === false) return "closed";
  if (termsPending === true) return "consent";
  if (termsPending === undefined || configPending) return "pending";
  return "open";
}
