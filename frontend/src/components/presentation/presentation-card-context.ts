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
 * Where a deck card (`PresentationCard`) is being rendered.
 *
 * `conversationId` is the conversation whose transcript the card sits in — the
 * one "Open in studio" binds to the deck so the agent keeps its memory of the
 * work; `XtmChatPanel` provides it for every card in its transcript.
 * `studioPresentationId` is set inside the Presentation Studio rail: a card for
 * THAT deck collapses to a one-line receipt, because the deck is already on the
 * stage next to it. Cards for other decks (an agent that made a copy, say) keep
 * their full rendering.
 *
 * `navigateToSlide` / `registerSlideStage` are how a slide link in the
 * transcript is followed IN PLACE: the studio rail answers for the
 * stage beside it, the transcript answers for the cards mounted in it.
 */
import { createContext } from "react";

/**
 * A stage that can take a slide link for one deck: move to the slide `slideRef`
 * names (a stable slide id, or reveal's 0-based index) and return `true`, or
 * `false` when the ref names no slide of that deck.
 */
export type SlideStage = (slideRef: string) => boolean;

export interface PresentationCardContextValue {
  conversationId?: string | null;
  studioPresentationId?: string | null;
  /**
   * Follow a slide link in place: move the stage already showing deck
   * `presentationId` to the slide `slideRef` names. Returns `true` when a stage
   * took the link; `false` when nothing on screen shows that deck — the link
   * then opens the viewer on that slide, as any link would.
   */
  navigateToSlide?: (presentationId: string, slideRef: string) => boolean;
  /**
   * Announce a stage that can take slide links for a deck (an inline card's).
   * Returns the function that withdraws it. Provided by the transcript, which
   * is what knows which cards are mounted.
   */
  registerSlideStage?: (presentationId: string, stage: SlideStage) => () => void;
}

export const PresentationCardContext = createContext<PresentationCardContextValue>({});
