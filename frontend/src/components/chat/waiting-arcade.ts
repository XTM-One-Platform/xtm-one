/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The waiting arcade as the chat offers it while an agent works (#3798,
 * #3877): the one launch path of the waiting panel's Take control. The offer
 * is read here once - the arcade's gate (`useArcade().enabled`, `arcadeGate()`:
 * a signed-in session, the Terms of Use accepted, no impersonation, the
 * operator's `ARCADE_ENABLED`) and reduced motion - and a game taken from it
 * waits on the chat surface's own `waitSource`. There is no per-user switch to
 * hide the game: the gamepad icon means play, and only play.
 */
import { useCallback } from "react";
import { useArcade, type ArcadeLaunchOptions } from "@/components/arcade/arcade-context";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";

// The per-browser switch is gone: its stored value is dropped once.
try {
  window.localStorage.removeItem("filigranChatMiniGame");
} catch {
  /* no storage - nothing was stored */
}

export function useWaitingArcade({
  waitSource,
}: {
  /** The chat surface's wait (`useArcadeWaitSignal`), which the game waits on. */
  waitSource?: string | null;
}) {
  const arcade = useArcade();
  const reducedMotion = usePrefersReducedMotion();
  // The self-playing invader shows; taking control also needs the arcade.
  const playMode = !reducedMotion;
  const canTakeControl = playMode && arcade.enabled;

  /** Open the game, the invaders breaking out from `origin` (viewport px). */
  const takeControl = useCallback(
    (origin: ArcadeLaunchOptions["origin"]) => {
      if (!canTakeControl) return;
      arcade.launch({ source: "waiting", origin, waitSource });
    },
    [arcade, canTakeControl, waitSource],
  );

  return { reducedMotion, playMode, canTakeControl, takeControl };
}
