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
 * A stored answer's text with the cards of its markers, outside the chat
 * panel (which draws its own, `markers.tsx`): a run's result (`RunDetail`).
 */
import { Fragment, useCallback, useMemo } from "react";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { InlineMarkerContext } from "@/components/chat/inline-marker-context";
import { FileChip, WorkingFileChip } from "@/components/chat-panel/FileChips";
import { MARKER_CARD_KINDS, inlineMarkerChip, markerCard } from "@/components/chat-panel/markers";
import { InvestigationMessageText } from "@/components/investigation/InvestigationMessageText";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { fileTag, type FileTag } from "@/pages/chat/file-revisions";
import { answerParts, answerProse, answerWithInlineMarkers, markerRefs } from "@/pages/chat/answerMarkers";
import type { ChatMessageAttachment } from "@/types";

/**
 * Markers leave the prose; their cards follow it in source order (decks,
 * reports, videos, investigations, work items, then files; consecutive working
 * chips share a row). The first download chip scrolls into view. A marker in a
 * table row, a list item or a quote stays in place as its compact chip, so the
 * table or the list stays whole (#11697).
 */
export function InlineFileContent({
  content,
  attachments,
}: {
  content: string;
  attachments: ChatMessageAttachment[];
}) {
  const attMap = useMemo(() => {
    const map = new Map<string, ChatMessageAttachment>();
    for (const att of attachments) {
      if (att.file_id) map.set(att.file_id, att);
    }
    return map;
  }, [attachments]);
  // The markers kept in their table cell, list item or quote leave the split:
  // only the markers on their own line get a card below the prose.
  const kept = useMemo(() => answerWithInlineMarkers(content), [content]);
  const filesById = useMemo(
    () => new Map(attachments.flatMap((att) => (att.file_id ? [[att.file_id.toLowerCase(), att] as const] : []))),
    [attachments],
  );

  // Each card's ids in marker order, once each, read as the chat panel reads
  // them (`answerMarkers.ts`); a deck keeps the slide its marker points at.
  const refs = useMemo(() => {
    const parts = answerParts(kept);
    return {
      files: markerRefs(parts, "FILE").map((ref) => ref.id),
      cards: MARKER_CARD_KINDS.map((kind) => ({ kind, refs: markerRefs(parts, kind) })),
      investigations: markerRefs(parts, "INVESTIGATION").map((ref) => ref.id),
    };
  }, [kept]);
  const markerIds = refs.files;
  // Behind `VIDEO_STUDIO` and `DEEP_INVESTIGATION`: off, their
  // routes answer 403, so a stored marker renders no card (it still leaves the text).
  const videoStudioEnabled = useFeatureFlag("VIDEO_STUDIO");
  const deepInvestigationEnabled = useFeatureFlag("DEEP_INVESTIGATION");
  const dashboardStudioEnabled = useFeatureFlag("DASHBOARD_STUDIO");
  const flags = {
    videoStudio: videoStudioEnabled,
    deepInvestigation: deepInvestigationEnabled,
    dashboardStudio: dashboardStudioEnabled,
  };
  const investigationIds = deepInvestigationEnabled ? refs.investigations : [];
  const renderInlineMarker = useCallback(
    (kind: string, args: string) =>
      inlineMarkerChip(kind, args, filesById, {
        videoStudio: videoStudioEnabled,
        deepInvestigation: deepInvestigationEnabled,
        dashboardStudio: dashboardStudioEnabled,
      }),
    [filesById, videoStudioEnabled, deepInvestigationEnabled, dashboardStudioEnabled],
  );
  // Every marker leaves the text, a chart one streamed by `render_chart`
  // included: the stored answer carries the chart's own block.
  const textContent = useMemo(() => answerProse(kept), [kept]);

  const markerAttachments = useMemo(
    () =>
      markerIds
        .map((id) => attMap.get(id))
        .filter((att): att is ChatMessageAttachment => !!att),
    [attMap, markerIds],
  );
  // Group *consecutive* same-tag attachments so chips render in the exact
  // order their ``[[FILE:…]]`` markers appeared in the source (the documented
  // contract) — interleaved download/working markers keep their relative
  // order instead of being split into a downloads-first / working-last layout.
  // Consecutive working chips still share one compact flex-wrap row.
  const markerGroups = useMemo(() => {
    const groups: { tag: FileTag; items: ChatMessageAttachment[] }[] = [];
    for (const att of markerAttachments) {
      const tag = fileTag(att);
      const last = groups[groups.length - 1];
      if (last && last.tag === tag) last.items.push(att);
      else groups.push({ tag, items: [att] });
    }
    return groups;
  }, [markerAttachments]);
  const firstDownloadId = useMemo(
    () => markerAttachments.find((att) => fileTag(att) === "download_file")?.file_id,
    [markerAttachments],
  );

  return (
    <>
      <InlineMarkerContext.Provider value={renderInlineMarker}>
        {investigationIds.length === 1 ? (
          <InvestigationMessageText text={textContent} investigationId={investigationIds[0]} />
        ) : textContent.trim() ? (
          <ChatMarkdown content={textContent} />
        ) : null}
      </InlineMarkerContext.Provider>
      {refs.cards.map(({ kind, refs: kindRefs }) =>
        kindRefs.map((ref) => <Fragment key={`${kind}:${ref.id}`}>{markerCard(kind, ref, flags)}</Fragment>),
      )}
      {markerGroups.map((group, gi) =>
        group.tag === "download_file" ? (
          group.items.map((att, i) => (
            <div key={att.file_id ?? `dl-${gi}-${i}`} className="my-2">
              <FileChip att={att} autoFocusOnMount={att.file_id === firstDownloadId} />
            </div>
          ))
        ) : (
          <div key={`wg-${gi}`} className="mt-2 flex flex-wrap gap-1.5">
            {group.items.map((att, i) => (
              <WorkingFileChip key={att.file_id ?? `working-${gi}-${i}`} att={att} />
            ))}
          </div>
        ),
      )}
    </>
  );
}
