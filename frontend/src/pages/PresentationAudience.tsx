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
 * Audience window - `/presentations/:id/audience` (xtm-one #10552, slice A2).
 *
 * "Present to a meeting" from a single laptop screen: this is the window the
 * presenter shares in Teams, Zoom or Google Meet. It shows the slides and
 * nothing else - no toolbar in any state, no cursor, no hover affordance, no
 * presence or comment chrome - letterboxed in black at any window size, and
 * it works as a plain browser window (no fullscreen needed), so it can be
 * shared while the speaker view covers it.
 *
 * It is a STAGE of the speaker bridge (`speaker-sync.ts`, role `audience`):
 * the speaker view drives it, it mirrors every move of the deck's other
 * stages (the viewer the presenter started from) and they mirror its moves.
 * It also takes the deck keys itself from the first render (a meeting tool's
 * remote control sends keys to the focused window, with no prior click), and
 * a left click on a slide advances (Shift + click goes back) unless it lands
 * on interactive content. `Esc` does nothing here: the window is closed from
 * its own close button.
 */
import { useCallback, useEffect, type MouseEvent as ReactMouseEvent } from "react";
import { useParams } from "react-router-dom";

import { useDeckController } from "@/components/presentation/deck-controller";
import { clickAdvanceStep, type DeckShortcutAction } from "@/components/presentation/deck-keys";
import { deckAspectRatio } from "@/components/presentation/geometry";
import { PresentationDeck } from "@/components/presentation/PresentationDeck";
import { useSpeakerStage } from "@/components/presentation/speaker-sync";
import { openSpeakerView } from "@/components/presentation/speaker-url";
import { usePresentation } from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import "@/components/presentation/presentation.css";

export default function PresentationAudience() {
  const { id } = useParams<{ id: string }>();
  const { t_i18n } = useFormatter();
  const { data, isError } = usePresentation(id, { live: true });
  const controller = useDeckController();

  useSpeakerStage({
    presentationId: data?.id,
    controller,
    revision: data?.revision ?? 0,
    enabled: !!data,
    role: "audience",
  });

  // The window title is what a share picker lists: the deck's own title.
  useEffect(() => {
    if (!data?.title) return;
    const previous = document.title;
    document.title = data.title;
    return () => {
      document.title = previous;
    };
  }, [data?.title]);

  const { next, prev } = controller;
  const onClick = useCallback(
    (event: ReactMouseEvent<HTMLDivElement>) => {
      const step = clickAdvanceStep(event.nativeEvent);
      if (step === "next") next();
      else if (step === "prev") prev();
    },
    [next, prev],
  );

  // Only `S` acts here (bring the speaker view back); fullscreen, the jump
  // menu, the sheet and Esc have no chrome to show in a slides-only window.
  const onShortcut = useCallback(
    (action: DeckShortcutAction) => {
      if (action === "speaker" && data) openSpeakerView(data.id);
    },
    [data],
  );

  return (
    <div
      className="deck-stage deck-audience dark"
      data-deck-viewport=""
      data-deck-fullscreen="viewport"
      data-mode="dark"
      data-testid="deck-audience"
    >
      {data ? (
        <div className="deck-stage-surface" data-aspect={deckAspectRatio(data.spec.aspect_ratio)} onClick={onClick}>
          <PresentationDeck
            presentation={data}
            variant="standalone"
            keyboardActive
            onDeck={controller.attach}
            onShortcut={onShortcut}
          />
        </div>
      ) : isError ? (
        <p className="m-auto text-sm text-default-secondary">{t_i18n("This presentation is not available.")}</p>
      ) : null}
    </div>
  );
}
