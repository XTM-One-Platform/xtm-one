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

import { useCallback, useMemo, useRef, type ReactNode } from "react";
import type { ChatMarkerRenderer, ChatMessage as PanelMessage } from "@filigran/chatbot";
import { FileChip, WorkingFileChip } from "@/components/chat-panel/FileChips";
import { MarkerCardBoundary } from "@/components/chat-panel/MarkerCardBoundary";
import { MarkerInlineChip, type InlineMarkerKind } from "@/components/chat-panel/MarkerInlineChip";
import { chatFileDownloadPath } from "@/components/chat-panel/endpoints";
import { CustomDashboardCard } from "@/components/custom-dashboard/CustomDashboardCard";
import { InvestigationBlock } from "@/components/investigation/InvestigationBlock";
import { PresentationCard } from "@/components/presentation/PresentationCard";
import type { SlideStage } from "@/components/presentation/presentation-card-context";
import { ReportCard } from "@/components/report/ReportCard";
import { VideoCard } from "@/components/video-studio/VideoCard";
import { ScheduledWorkItemCard } from "@/components/work-items/ScheduledWorkItemCard";
import { AgentSwitchDivider } from "@/pages/chat/AgentSwitchDivider";
import { markerId, type MarkerRef } from "@/pages/chat/answerMarkers";
import { conversationFileMap, fileTypeLabel } from "@/pages/chat/attachments";
import { conversationRevisions } from "@/pages/chat/file-revisions";
import type { ChatMessage, ChatMessageAttachment } from "@/types";

/**
 * A file of the answer as the page's chips take it: the stored attachment
 * (download URL, hash, time) once the conversation is read back, else the
 * panel's own copy of a reply that has just landed.
 */
function attachmentFor(
  fileId: string,
  stored: ReadonlyMap<string, ChatMessageAttachment>,
  message: PanelMessage,
): ChatMessageAttachment | null {
  const known = stored.get(fileId.toLowerCase());
  if (known) return known;
  const live = message.attachments?.find((a) => a.fileId.toLowerCase() === fileId.toLowerCase());
  if (!live) return null;
  return {
    file_id: live.fileId,
    filename: live.filename,
    type: live.type || fileTypeLabel(live.filename, live.contentType),
    size: live.size ?? 0,
    content_type: live.contentType ?? null,
    file_tag: live.fileTag ?? null,
    download_url: chatFileDownloadPath(live.fileId),
  };
}

/** The marker a change-of-agent note reads as, by the note's message id. */
const AGENT_SWITCH_KIND = "AGENT_SWITCH";

/**
 * The text a stored change-of-agent note is shown as, `null` for any
 * other message: a marker its renderer draws as a divider, not as a reply.
 */
export function agentSwitchMarker(stored: Pick<ChatMessage, "id" | "agent_switch"> | undefined): string | null {
  return stored?.agent_switch ? `[[${AGENT_SWITCH_KIND}:${stored.id}]]` : null;
}

/** The features a marker card is behind. */
export interface MarkerCardFlags {
  deepInvestigation: boolean;
  videoStudio: boolean;
  dashboardStudio: boolean;
}

/** The kinds `markerCard` draws a card for, in the order a stored answer shows them. */
export const MARKER_CARD_KINDS = ["PRESENTATION", "REPORT", "VIDEO", "DASHBOARD", "INVESTIGATION", "WORK_ITEM"] as const;

/**
 * The card of a deck, report, video (behind `VIDEO_STUDIO`), custom dashboard
 * (behind `DASHBOARD_STUDIO`), investigation's
 * goal plan (behind `DEEP_INVESTIGATION`) or scheduled work item marker, in its
 * own `MarkerCardBoundary`; `null` for another kind or a feature turned off.
 */
export function markerCard(kind: string, { id, slideRef }: MarkerRef, flags: MarkerCardFlags): ReactNode {
  let card: ReactNode;
  switch (kind) {
    case "PRESENTATION":
      card = <PresentationCard presentationId={id} focusSlideRef={slideRef} />;
      break;
    case "REPORT":
      card = <ReportCard reportId={id} />;
      break;
    case "VIDEO":
      card = flags.videoStudio ? <VideoCard videoId={id} /> : null;
      break;
    case "DASHBOARD":
      card = flags.dashboardStudio ? <CustomDashboardCard dashboardId={id} /> : null;
      break;
    case "INVESTIGATION":
      card = flags.deepInvestigation ? <InvestigationBlock investigationId={id} /> : null;
      break;
    case "WORK_ITEM":
      card = <ScheduledWorkItemCard workItemId={id} />;
      break;
    default:
      card = null;
  }
  return card ? <MarkerCardBoundary>{card}</MarkerCardBoundary> : null;
}

/**
 * The card a marker of *kind* draws from what the panel hands it: everything
 * between `KIND:` and `]]`, a deck's slide as `#/<slide id>`. A marker that
 * names no id (`markerId`) draws nothing.
 */
function cardFromArgs(kind: string, args: string, flags: MarkerCardFlags): ReactNode {
  const [target, slideRef] = kind === "PRESENTATION" ? args.split("#/") : [args];
  const id = markerId(target);
  return id ? markerCard(kind, { id, slideRef: slideRef ?? null }, flags) : null;
}

/** Whether a kind's feature is on, as `markerCard` reads it. */
function kindEnabled(kind: InlineMarkerKind, flags: MarkerCardFlags): boolean {
  if (kind === "VIDEO") return flags.videoStudio;
  if (kind === "DASHBOARD") return flags.dashboardStudio;
  if (kind === "INVESTIGATION") return flags.deepInvestigation;
  return true;
}

/** Whether *kind* is one `MarkerInlineChip` draws. */
function isInlineMarkerKind(kind: string): kind is InlineMarkerKind {
  return (MARKER_CARD_KINDS as readonly string[]).includes(kind);
}

/**
 * The compact chip of a marker kept inside a table cell, a list item or a
 * quote outside the panel (`ChatMarkdown` `InlineMarkerContext`, #11697): a
 * file's chip from the answer's *files*, else the kind's `MarkerInlineChip`;
 * `null` for an unknown file, a kind with no chip (CHART) or a feature off.
 */
export function inlineMarkerChip(
  kind: string,
  args: string,
  files: ReadonlyMap<string, ChatMessageAttachment>,
  flags: MarkerCardFlags,
): ReactNode {
  if (kind === "FILE") {
    const att = files.get(args.toLowerCase());
    return att ? (
      <MarkerCardBoundary>
        <WorkingFileChip att={att} />
      </MarkerCardBoundary>
    ) : null;
  }
  return isInlineMarkerKind(kind) ? chipFromArgs(kind, args, flags) : null;
}

/**
 * The compact chip of a marker inside a table cell, a list item or a quote
 * (`ChatMarkerContext.inline`, #11697): the full card is for a marker on its
 * own line. `null` for a marker naming no id or a feature turned off.
 */
function chipFromArgs(kind: InlineMarkerKind, args: string, flags: MarkerCardFlags): ReactNode {
  if (!kindEnabled(kind, flags)) return null;
  const [target, slideRef] = kind === "PRESENTATION" ? args.split("#/") : [args];
  const id = markerId(target);
  if (!id) return null;
  return (
    <MarkerCardBoundary>
      <MarkerInlineChip kind={kind} id={id} slideRef={slideRef ?? null} />
    </MarkerCardBoundary>
  );
}

/** The renderer of a card kind: its card on its own line, its chip when inline. */
function cardRenderer(kind: InlineMarkerKind, flags: MarkerCardFlags, fullWidth: boolean): ChatMarkerRenderer {
  return {
    render: (args, context) =>
      (context as { inline?: boolean }).inline ? chipFromArgs(kind, args, flags) : cardFromArgs(kind, args, flags),
    ...(fullWidth ? { fullWidth: true } : {}),
  };
}

/**
 * The cards of an answer's markers, by name, as the panel draws them: the
 * `markerCard` kinds and the file chips with their revisions; a streamed chart
 * marker draws nothing, and a card that fails says so in its own place. The
 * names beyond the panel's own are `XTM_HOST_MARKER_KINDS`, which the pages
 * outside the panel read answers with.
 */
export function useMarkerRenderers(
  storedMessages: ChatMessage[] | undefined,
  { deepInvestigation, videoStudio, dashboardStudio }: MarkerCardFlags,
): Record<string, ChatMarkerRenderer> {
  const storedFiles = useMemo(() => conversationFileMap(storedMessages ?? []), [storedMessages]);
  return useMemo(() => {
    const flags = { deepInvestigation, videoStudio, dashboardStudio };
    return {
      FILE: {
        render: (args, context) => {
          const att = attachmentFor(args, storedFiles, context.message);
          if (!att) return null;
          // A marker inside a table cell, a list item or a quote (the panel
          // keeps it in place and says so with `inline`, @filigran/chatbot >=
          // the #11697 release) draws the compact chip; the card is for a
          // marker on its own line.
          if ((context as { inline?: boolean }).inline) {
            return (
              <MarkerCardBoundary>
                <WorkingFileChip att={att} />
              </MarkerCardBoundary>
            );
          }
          return (
            <MarkerCardBoundary>
              {att.file_tag === "working_file" ? <WorkingFileChip att={att} /> : <FileChip att={att} />}
            </MarkerCardBoundary>
          );
        },
      },
      PRESENTATION: cardRenderer("PRESENTATION", flags, true),
      REPORT: cardRenderer("REPORT", flags, true),
      VIDEO: cardRenderer("VIDEO", flags, false),
      DASHBOARD: cardRenderer("DASHBOARD", flags, true),
      INVESTIGATION: cardRenderer("INVESTIGATION", flags, true),
      WORK_ITEM: cardRenderer("WORK_ITEM", flags, true),
      // A chart streams as `[[CHART:<id>]]` and is stored as the chart's own
      // fenced block: the marker shows nothing until the stored answer
      // replaces it, inline or not.
      CHART: {
        render: () => null,
      },
    };
  }, [storedFiles, deepInvestigation, videoStudio, dashboardStudio]);
}

/**
 * The renderer of the {@link agentSwitchMarker} the panel shows a stored
 * change-of-agent note as: the divider, read from the stored note. No answer
 * carries this marker, so it is not one of `useMarkerRenderers`' kinds.
 */
export function useAgentSwitchRenderer(storedMessages: ChatMessage[] | undefined): Record<string, ChatMarkerRenderer> {
  return useMemo(() => {
    const notes = new Map((storedMessages ?? []).flatMap((m) => (m.agent_switch ? [[m.id, m] as const] : [])));
    return {
      [AGENT_SWITCH_KIND]: {
        render: (args) => {
          const note = notes.get(args);
          return note?.agent_switch ? <AgentSwitchDivider note={note.agent_switch} at={note.created_at} /> : null;
        },
        fullWidth: true,
        // A note between messages: no agent name, time or actions around it.
        standalone: true,
      },
    };
  }, [storedMessages]);
}

/**
 * What the transcript's cards read around them: the conversation a deck card binds to "Open in
 * studio", the deck stages a slide link moves in place, and the file
 * revisions their chips number (v1, v2).
 */
export function useTranscriptCards(conversationId: string | null, storedMessages: ChatMessage[] | undefined) {
  // The stages per deck, in mount order: the newest card of a deck is asked first.
  const stagesRef = useRef<Map<string, SlideStage[]>>(new Map());
  const registerSlideStage = useCallback((presentationId: string, stage: SlideStage) => {
    stagesRef.current.set(presentationId, [...(stagesRef.current.get(presentationId) ?? []), stage]);
    return () => {
      const remaining = (stagesRef.current.get(presentationId) ?? []).filter((s) => s !== stage);
      if (remaining.length === 0) stagesRef.current.delete(presentationId);
      else stagesRef.current.set(presentationId, remaining);
    };
  }, []);
  const navigateToSlide = useCallback((presentationId: string, slideRef: string) => {
    const stages = stagesRef.current.get(presentationId) ?? [];
    for (let i = stages.length - 1; i >= 0; i -= 1) {
      if (stages[i](slideRef)) return true;
    }
    return false;
  }, []);
  const cardContext = useMemo(
    () => ({ conversationId, navigateToSlide, registerSlideStage }),
    [conversationId, navigateToSlide, registerSlideStage],
  );
  const revisions = useMemo(() => conversationRevisions(storedMessages ?? []), [storedMessages]);
  return { cardContext, revisions, navigateToSlide };
}
