/**
 * The waiting arcade's context: who can play, whether a game is on,
 * and the channel through which chat surfaces say they are waiting on their
 * agent (what a running game reads is the provider's, see `WaitSources`).
 *
 * The default value is inert, so a surface rendered without the provider
 * (tests, a stand-alone page) simply keeps the self-playing waiting panel.
 */
import { createContext, useContext, useEffect, useId } from "react";
import type { SurfaceWait } from "./wait-sources";

type ArcadeSource = "waiting" | "palette" | "menu" | "konami" | "leaderboard" | "replay" | "link";

export interface ArcadeLaunchOptions {
  source: ArcadeSource;
  /** Where the invaders break out from (the waiting panel), in viewport px. */
  origin?: { x: number; y: number } | null;
  /** The chat surface whose answer the game waits for (see `WaitSources`). */
  waitSource?: string | null;
}

export interface ArcadeContextValue {
  /** The arcade may open (`arcadeGate`): signed in, Terms of Use accepted,
   *  not impersonating, and on for this deployment. */
  enabled: boolean;
  /** The full-screen game is on. */
  active: boolean;
  launch: (options: ArcadeLaunchOptions) => void;
  openLeaderboard: () => void;
  /** Report what the chat surface `id` shows and whether its agent is
   *  working there; `null` when the surface goes away (it will not answer). */
  setWaitSource: (id: string, wait: SurfaceWait | null) => void;
  /** Warm the game's lazy chunk so taking control is instant. */
  prefetch: () => void;
}

const noop = () => {};

export const ArcadeContext = createContext<ArcadeContextValue>({
  enabled: false,
  active: false,
  launch: noop,
  openLeaderboard: noop,
  setWaitSource: noop,
  prefetch: noop,
});

export function useArcade(): ArcadeContextValue {
  return useContext(ArcadeContext);
}

/**
 * Tell the arcade which conversation this chat surface shows and whether its
 * agent is working on a turn there (the chat's working state, see
 * `SurfaceWait`) - what lets a game know its answer has arrived. Cleared when
 * the surface unmounts. Returns the surface's id, which a game launched from
 * it waits on.
 */
export function useArcadeWaitSignal(conversationId: string | null, working: boolean): string {
  const { setWaitSource } = useArcade();
  const id = useId();
  useEffect(() => {
    setWaitSource(id, { conversationId, working });
  }, [id, conversationId, working, setWaitSource]);
  useEffect(() => () => setWaitSource(id, null), [id, setWaitSource]);
  return id;
}
