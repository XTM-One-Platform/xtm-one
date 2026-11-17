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
 * A turn the platform cut, and the replies that resumed it, drawn as ONE card
 * in the panel's transcript (`InterruptedReplyCard`), as the old page drew
 * them: the panel's `replaceMessage` draws the card on the group's first
 * interrupted reply and leaves the others out, and the reply that resumed them
 * no longer says it was resumed, which the card above says.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChatMessageSlotContext, ChatMessage as PanelMessage } from "@filigran/chatbot";
import { HeldCardSlot } from "@/components/chat-panel/held-card-slot";
import { InterruptedReplyCard } from "@/pages/chat/InterruptedReplyCard";
import { cutGroupViews, groupCutReplies, type CutGroupView } from "@/lib/cut-turn-group";
import type { ChatMessage, InterruptedTurn, ResumedFromNote } from "@/types";

/**
 * How long a turn in flight still counts as the resume of the trailing cut
 * once it ended, until the stored transcript holds the reply that answered:
 * the card goes from resuming to resumed, never through "not resumed".
 */
export const RESUME_SETTLE_MS = 5_000;

/**
 * How long after the stored transcript names a cut, or the reply that resumed
 * it, the panel may take to draw it before its thread is read back: a cut
 * stored after the panel read the thread (its stream closed on the cut) is in
 * the panel only once read back, and the panel draws nothing for it until then.
 */
export const PANEL_CATCH_UP_MS = 1_500;

/**
 * How long the card of a resumed cut stays under the transcript after the
 * turn ended, waiting for the panel to hold the cut reply again: past it the
 * card is dropped, never left there.
 */
export const SETTLED_CARD_HOLD_MS = 15_000;

interface CutTurnCardsOptions {
  /** The turn in flight is one the platform resumed by itself (`turn_resuming`). */
  resumingTurn: ResumedFromNote | null;
  /** The conversation's interrupted turn: queued, still offered, or gone once dismissed. */
  interruptedTurn: InterruptedTurn | null | undefined;
  /** A turn runs: the trailing card holds no Resume or Dismiss meanwhile. */
  working: boolean;
  /**
   * The conversation says a turn is in flight (`is_processing`), read with the
   * transcript: true on a page reloaded during a resume, before the panel or
   * the stream says so.
   */
  processing?: boolean;
  /**
   * Read the panel's transcript back (`ChatPanelHandle.reloadConversation`):
   * a cut reply stored mid-turn is in the panel's thread only once it is read
   * back, so the card of a resume that answered goes in place then.
   */
  reloadTranscript?: () => void;
  onResume?: () => void;
  onDismiss?: () => void;
  resumeDisabled?: boolean;
}

export function useCutTurnCards(
  storedMessages: ChatMessage[] | undefined,
  {
    resumingTurn,
    interruptedTurn,
    working,
    processing = false,
    reloadTranscript,
    onResume,
    onDismiss,
    resumeDisabled = false,
  }: CutTurnCardsOptions,
) {
  const groups = useMemo(() => groupCutReplies((storedMessages ?? []).filter((m) => !m.hidden)), [storedMessages]);
  const trailingId = groups.trailing?.replies[0]?.id ?? null;

  // A turn in flight after the trailing cut is its resume: the stream's word
  // (`working`) or the conversation's (`processing`). It stays so for a short
  // bound after it ended, until the stored transcript holds the reply that
  // answered, so the card never says "not resumed" between the two (#10910).
  const inFlight = working || processing;
  const [settlingId, setSettlingId] = useState<string | null>(null);
  const wasInFlight = useRef(false);
  useEffect(() => {
    if (inFlight) {
      wasInFlight.current = true;
      setSettlingId(null);
      return undefined;
    }
    if (!wasInFlight.current || !trailingId) return undefined;
    wasInFlight.current = false;
    setSettlingId(trailingId);
    const timer = window.setTimeout(() => setSettlingId(null), RESUME_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [inFlight, trailingId]);
  const resuming = inFlight || (settlingId !== null && settlingId === trailingId);

  const views = useMemo(
    () =>
      cutGroupViews(groups, {
        live: working ? resumingTurn : null,
        queued: !!interruptedTurn?.auto_resume,
        served: !!interruptedTurn,
        processing: resuming,
      }),
    [groups, working, resumingTurn, interruptedTurn, resuming],
  );

  // The replies the panel holds, as it asks for their replacement. A cut stored
  // after the panel read the thread, or the reply that resumed it, is not in
  // it: the panel draws nothing for it (no working bubble, no card, no answer)
  // until its thread is read back, which is done once per cut and answer.
  // Only the cut the transcript ends with (or ends with its resumed reply):
  // an older one may sit above the page the panel loaded.
  const panelIds = useRef(new Set<string>());
  const lastId = (storedMessages ?? []).filter((m) => !m.hidden).pop()?.id ?? null;
  const latest = [...groups.byFirstId.values()].pop() ?? null;
  const ending = latest && (latest === groups.trailing || latest.resumedBy?.id === lastId) ? latest : null;
  const latestIds = ending ? [ending.replies[0].id, ...(ending.resumedBy ? [ending.resumedBy.id] : [])] : [];
  const catchUpKey = !working && latestIds.length > 0 ? latestIds.join("/") : null;
  const reloadRef = useRef(reloadTranscript);
  reloadRef.current = reloadTranscript;
  const reloadedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!catchUpKey || reloadedFor.current === catchUpKey) return undefined;
    const timer = window.setTimeout(() => {
      if (catchUpKey.split("/").every((id) => panelIds.current.has(id))) return;
      reloadedFor.current = catchUpKey;
      reloadRef.current?.();
    }, PANEL_CATCH_UP_MS);
    return () => window.clearTimeout(timer);
  }, [catchUpKey]);

  // A turn the platform is resuming now: its cut reply was stored mid-turn,
  // after the panel read the thread, and the panel reads it back only at the
  // turn's end. The card is drawn under the transcript until then, its state
  // live (#10910), and the panel's copy of the reply, if any, is left out.
  const liveView = working && resumingTurn && trailingId ? (views.get(trailingId) ?? null) : null;
  const liveIds = useMemo(
    () => new Set(liveView ? liveView.replies.map((reply) => reply.id) : []),
    [liveView],
  );

  // The card of the resume that just ended stays under the transcript until
  // the panel holds the cut reply again (its thread read back), so it goes
  // from `resuming` to `resumed` with no reload and never vanishes between
  // the two (#10910). Until the stored transcript says the resume answered,
  // it keeps its last live state.
  const [held, setHeld] = useState<CutGroupView | null>(null);
  useEffect(() => {
    if (liveView) setHeld(liveView);
  }, [liveView]);
  const heldId = held?.replies[0]?.id ?? null;
  const stored = !liveView && heldId ? views.get(heldId) : undefined;
  // Another conversation, or a transcript without the cut: nothing to hold.
  const heldGone = !liveView && heldId !== null && stored === undefined;
  const settled = !liveView && !working && stored?.resumedBy ? stored : null;
  const settledView = liveView ? null : heldGone ? null : (settled ?? held);

  useEffect(() => {
    if (heldGone) setHeld(null);
  }, [heldGone]);
  const settledReloadedFor = useRef<string | null>(null);
  const settledId = settled ? heldId : null;
  useEffect(() => {
    if (!settledId || settledReloadedFor.current === settledId) return;
    settledReloadedFor.current = settledId;
    reloadRef.current?.();
  }, [settledId]);
  // Held past the turn's end, answered or not (a resume cut again): dropped
  // after a bound, so a card that never goes in place is never left there.
  const holdingId = !liveView && !working && !heldGone ? heldId : null;
  useEffect(() => {
    if (!holdingId) return undefined;
    const timer = window.setTimeout(
      () => setHeld((prev) => (prev?.replies[0]?.id === holdingId ? null : prev)),
      SETTLED_CARD_HOLD_MS,
    );
    return () => window.clearTimeout(timer);
  }, [holdingId]);

  const replaceMessage = useCallback(
    (message: PanelMessage) => {
      const id = message.serverId;
      if (!id) return undefined;
      panelIds.current.add(id);
      if (groups.folded.has(id) || liveIds.has(id)) return null;
      const view = views.get(id);
      // A reply the panel already knows was cut (its history came first) is
      // drawn by its card alone: until the stored transcript names its group,
      // nothing, never the panel's plain reply with its tool list for a moment.
      if (!view) return message.interrupted ? null : undefined;
      // The panel holds the cut reply again: its card goes in place, and from
      // this very render the held copy is no longer drawn (`heldInPlace`, read
      // by `renderAboveMessage` and `liveCard`); the held state itself is
      // cleared after this render (UX round 2 B2: never two cards at once).
      if (id === heldId && !liveView) {
        window.setTimeout(() => setHeld((prev) => (prev?.replies[0]?.id === id ? null : prev)), 0);
      }
      // The trailing card holds the Resume and Dismiss of a turn that could
      // not resume by itself: no notice under the transcript says the same cut.
      const actions = id === trailingId && !inFlight;
      return (
        <InterruptedReplyCard
          view={view}
          onResume={actions ? onResume : undefined}
          onDismiss={actions && interruptedTurn ? onDismiss : undefined}
          resumeDisabled={resumeDisabled}
        />
      );
    },
    [groups, views, liveIds, heldId, liveView, trailingId, inFlight, onResume, onDismiss, interruptedTurn, resumeDisabled],
  );
  const footerView = liveView ?? settledView;
  const card = useMemo(
    () => (footerView ? <InterruptedReplyCard view={footerView} resumeDisabled={resumeDisabled} /> : null),
    [footerView, resumeDisabled],
  );

  // Where the card of the resume goes once the resumed reply exists: just
  // above that reply's row (`renderAboveMessage`), the panel's streaming
  // placeholder first, then the same row until the panel reads the thread
  // back and the cut reply's own card goes in place, right above it. Under
  // the transcript (`liveCard`) only before the reply exists, never both, so
  // the card never jumps past the answer (#10910, UX round 1 B1).
  const [anchorId, setAnchorId] = useState<string | null>(null);
  // The anchor as the rows see it: set by `renderAboveMessage` in the very
  // render that first draws the resumed reply, the state following one tick
  // later (UX round 3 B3: the card never drawn in the footer for that frame).
  const anchorRef = useRef<string | null>(null);
  const hasCard = footerView !== null;
  if (!hasCard) anchorRef.current = null;
  else if (anchorId !== null) anchorRef.current = anchorId;
  useEffect(() => {
    if (!hasCard) setAnchorId(null);
  }, [hasCard]);
  const liveRef = useRef(false);
  liveRef.current = liveView !== null;
  const heldIdRef = useRef<string | null>(null);
  heldIdRef.current = heldId;
  // The panel draws the cut reply's own card in place (it asked for it in
  // `replaceMessage`, rows above first in the same render): the held copy is
  // not drawn any more, decided now rather than one commit later.
  const heldInPlace = useCallback(
    () => !liveRef.current && heldIdRef.current !== null && panelIds.current.has(heldIdRef.current),
    [],
  );
  const renderAboveMessage = useCallback(
    (message: PanelMessage, { streaming }: ChatMessageSlotContext) => {
      if (!card || heldInPlace()) return undefined;
      if (message.id === anchorRef.current) return card;
      // The reply the resume streams: the card goes above it from this render
      // on, and the footer slot (drawn after the rows) draws nothing in it.
      if (streaming && message.role === "assistant" && liveRef.current && anchorRef.current === null) {
        const id = message.id;
        anchorRef.current = id;
        window.setTimeout(() => setAnchorId((prev) => prev ?? id), 0);
        return card;
      }
      return undefined;
    },
    [card, heldInPlace],
  );
  // The footer copy is drawn neither once the cut reply's card is in place
  // nor once a row holds the card above the resumed reply.
  const footerHidden = useCallback(() => heldInPlace() || anchorRef.current !== null, [heldInPlace]);
  // A fresh slot each render, so the panel's footer re-renders with the
  // transcript and asks `footerHidden` after its rows asked `replaceMessage`
  // and `renderAboveMessage`.
  const liveCard = anchorId === null && card ? <HeldCardSlot card={card} inPlace={footerHidden} /> : null;

  return {
    replaceMessage,
    /** Resumed replies whose own "resumed" line the card above already says. */
    resumedIds: groups.resumedIds,
    /** The transcript ends with a cut: its card says it, not a notice under it. */
    endsWithCut: groups.trailing !== null,
    /**
     * The card of the cut being resumed now, drawn under the transcript while
     * the turn runs and its reply does not exist yet, and after it until the
     * panel draws the cut reply in place.
     */
    liveCard,
    /**
     * The panel's `renderAboveMessage`: the same card above the resumed reply
     * once it exists, streaming then read back, instead of under the transcript.
     */
    renderAboveMessage,
  };
}
