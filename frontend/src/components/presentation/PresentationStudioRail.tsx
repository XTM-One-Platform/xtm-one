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
 * The Presentation Studio rail: the shared studio rail
 * (`components/studio/StudioRail`) bound to the deck beside it.
 *
 * What makes it the deck's: its conversations carry `presentation_id`, every
 * message says which slide is on the stage (`studio_slide`), the agent picker
 * offers the agents that carry the deck tool (`useDeckAgents()`), and a save
 * the deck tool reports from inside its execution (`presentation_saved`,
 * #3428) moves the stage to the slide it wrote once the new revision is on
 * screen - the stage follows the agent's pen. Deck cards inside the
 * rail collapse to a line (`studioCards`): the deck itself is on
 * the right, and a slide link for THIS deck moves the stage in place.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { StudioHandle, StudioRail, type StudioSubject } from "@/components/studio/StudioRail";
import {
  PRESENTATION_SAVED_EVENT,
  useBindConversationPresentation,
  type ToolProgressFrame,
} from "@/hooks/api/useChatApi";
import { useFormatter } from "@/i18n";
import type { Presentation } from "@/types";

import { useDeckAgents } from "./deck-agents";
import { DECK_KEYS_OFF_ATTR } from "./deck-keys";
import { presentedElement } from "./fullscreen";
import { resolveSlideRef } from "./slide-links";

/**
 * The collapsed studio: a handle along the viewer's left edge that brings the
 * rail out. It is what "entering the presentation" shows first, so the chat is
 * one click away without taking a third of the screen from a deck the user
 * only came to read.
 */
export function PresentationStudioHandle({ onOpen }: { onOpen: () => void }) {
  return <StudioHandle onOpen={onOpen} testId="presentation.studio" />;
}

interface PresentationStudioRailProps {
  presentation: Presentation;
  /** 0-based index of the slide / page on the stage. Sent with every message
   *  (1-based) so "this slide" means the one the user is looking at. */
  activeIndex: number;
  /**
   * Move the stage to a slide / page (0-based): a slide link in the
   * transcript followed in place, the receipt's "Go to slide N", and the slide
   * an edit just wrote (the stage follows the agent's pen). Without it the
   * links open the viewer in a new tab, as any link does.
   */
  onSelectSlide?: (index: number) => void;
  /** The conversation to open with (`?c=`), or `null` for the most recent
   *  studio conversation of this deck, or a fresh one when there is none. */
  initialConversationId: string | null;
  /** The rail's current conversation, for the URL. */
  onConversationChange: (id: string | null) => void;
  onCollapse: () => void;
  /**
   * Text to put in the composer, ready to send (the layout panel's `Fix with
   * my assistant`). Appended to whatever the user already typed, never
   * sent on their behalf; `onPrimedMessageApplied` is called once it is in
   * the box so the host clears it and a later remount does not paste it again.
   */
  primedMessage?: string | null;
  onPrimedMessageApplied?: () => void;
  /**
   * Whether an agent turn is in flight in the rail's conversation.
   * The host forwards it to the presence heartbeat, which announces the flip
   * to collaborators without waiting for the next scheduled beat. Reports
   * `false` on unmount, so a collapsed rail never leaves a stale `true`.
   */
  onBusyChange?: (busy: boolean) => void;
  className?: string;
}

export function PresentationStudioRail({
  presentation,
  activeIndex,
  onSelectSlide,
  ...rail
}: PresentationStudioRailProps) {
  const { t_i18n } = useFormatter();
  const deckAgents = useDeckAgents();
  const bindConv = useBindConversationPresentation();

  // The stage follows the agent's pen: a save that names the slide it
  // wrote moves the stage there — once the new revision is on screen, because a
  // slide the edit INSERTED has no position until the refetched deck arrives.
  // Not while presenting: a projected deck must not jump under the audience
  // because the studio is open beside it.
  const [followTarget, setFollowTarget] = useState<{ revision: number; slideId: string } | null>(
    null,
  );
  const onToolProgress = useCallback(
    (frame: ToolProgressFrame) => {
      if (frame.event !== PRESENTATION_SAVED_EVENT || frame.presentation_id !== presentation.id) return;
      const { revision, focus_slide_id: slideId } = frame;
      if (typeof revision !== "number" || typeof slideId !== "string") return;
      setFollowTarget({ revision, slideId });
    },
    [presentation.id],
  );
  useEffect(() => {
    if (!followTarget || presentation.revision < followTarget.revision) return;
    setFollowTarget(null);
    if (!onSelectSlide || presentedElement()) return;
    const index = resolveSlideRef(presentation.spec, followTarget.slideId);
    if (index !== null) onSelectSlide(index);
  }, [followTarget, onSelectSlide, presentation.revision, presentation.spec]);

  // Deck cards in the transcript: the one for THIS deck collapses to a receipt
  // (the deck is on the stage); `XtmChatPanel` adds the conversation id. A
  // slide link for THIS deck — in a reply, or the receipt's "Go to slide N" —
  // moves the stage beside the rail; links to other decks fall through
  // to the transcript's own cards, then to the viewer.
  const navigateToSlide = useCallback(
    (presentationId: string, slideRef: string) => {
      if (!onSelectSlide || presentationId !== presentation.id) return false;
      const index = resolveSlideRef(presentation.spec, slideRef);
      if (index === null) return false;
      onSelectSlide(index);
      return true;
    },
    [onSelectSlide, presentation.id, presentation.spec],
  );
  const studioCards = useMemo(
    () => ({ studioPresentationId: presentation.id, navigateToSlide }),
    [navigateToSlide, presentation.id],
  );

  // The theme suggestion names the OTHER theme, and only when the deck's
  // template supports it — a suggestion the tool would refuse is noise.
  const otherTheme = presentation.spec.theme === "light" ? "dark" : "light";
  const otherThemeOffered = (presentation.supported_schemes ?? ["dark", "light"]).includes(otherTheme);
  const suggestions = useMemo(() => {
    const themeSuggestion = otherThemeOffered
      ? [otherTheme === "light" ? t_i18n("Switch to the Light theme") : t_i18n("Switch to the Dark theme")]
      : [];
    return presentation.kind === "page"
      ? [
          t_i18n("Tighten the wording on this page"),
          t_i18n("Add an executive summary up front"),
          ...themeSuggestion,
        ]
      : [
          t_i18n("Tighten the wording on this slide"),
          t_i18n("Add a section divider before this slide"),
          t_i18n("Turn the takeaways into three punchy cards"),
          ...themeSuggestion,
        ];
  }, [presentation.kind, otherTheme, otherThemeOffered, t_i18n]);

  const unitLabel =
    presentation.kind === "page"
      ? t_i18n("Page {n}", { n: activeIndex + 1 })
      : t_i18n("Slide {n}", { n: activeIndex + 1 });
  const sendContext = useMemo(() => ({ studio_slide: activeIndex + 1 }), [activeIndex]);

  // "Continue where this deck was made": the conversation that generated the
  // deck, when it is the user's own — binding it brings the agent's memory of
  // the work along. Offered only while the deck has no studio history yet.
  const { mutateAsync: bindToDeck, isPending: binding } = bindConv;
  const origin = useMemo(
    () => ({
      conversationId: presentation.conversation_id ?? null,
      label: t_i18n("Continue the conversation that created this presentation"),
      bind: (conversationId: string) => bindToDeck({ conversationId, presentationId: presentation.id }),
      pending: binding,
    }),
    [bindToDeck, binding, presentation.conversation_id, presentation.id, t_i18n],
  );

  const subject: StudioSubject = {
    kind: "presentation",
    field: "presentation_id",
    id: presentation.id,
    title: presentation.title,
    toolAgents: deckAgents,
    sendContext,
    onToolProgress,
    studioCards,
    focus: { label: unitLabel, tip: t_i18n("The assistant is told which slide you are looking at") },
    suggestions,
    origin,
    railAttrs: DECK_KEYS_OFF_ATTR,
    testId: "presentation.studio",
    labels: {
      rail: t_i18n("Presentation Studio"),
      history: t_i18n("Conversations about this presentation"),
      noAgentShort: t_i18n("No agent with the deck tools"),
      noAgentTitle: t_i18n("No agent carries the deck tools yet"),
      blockedHint: t_i18n(
        "The studio works with agents that have the Presentation Generation tool. Turn it on in one of your agents' tools and come back here.",
      ),
      unavailableHint: t_i18n("The studio opened this presentation's own conversation instead."),
    },
  };

  return <StudioRail subject={subject} {...rail} />;
}
