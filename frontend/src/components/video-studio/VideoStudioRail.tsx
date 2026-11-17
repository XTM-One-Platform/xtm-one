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
 * The Video Studio editor's rail: the shared studio rail
 * (`components/studio/StudioRail`) bound to the video on the stage.
 *
 * Its conversations carry `video_project_id`; every message says which scene
 * the playhead is on (`studio_scene`), so "this scene" means the one on
 * screen; the picker offers the assistants carrying the Video Studio tool
 * (`useToolAgents`); a save the video tools report from inside their
 * execution (`video_saved`) refreshes the preview at once (`useChatApi`'s
 * `handleToolProgress`) and the host learns which revision landed.
 */
import { useCallback, useMemo, type ReactNode } from "react";

import { StudioHandle, StudioRail, type StudioSubject } from "@/components/studio/StudioRail";
import { VIDEO_SAVED_EVENT, useBindConversationVideo, type ToolProgressFrame } from "@/hooks/api/useChatApi";
import { useToolAgents } from "@/hooks/useToolAgents";
import { useFormatter } from "@/i18n";
import type { VideoProject } from "@/types";

import { VideoCardContext } from "./video-card-context";
import { VIDEO_TOOL_ID } from "./video-handoff";

export function VideoStudioHandle({ onOpen }: { onOpen: () => void }) {
  return <StudioHandle onOpen={onOpen} testId="video.studio" />;
}

interface VideoStudioRailProps {
  video: VideoProject;
  /** The scene under the playhead, its 1-based position and its kind, for "this scene". */
  focusedScene: { id: string; position: number; label: string } | null;
  /** A video tool saved a revision of THIS video. */
  onSaved?: (revision: number) => void;
  initialConversationId: string | null;
  onConversationChange: (id: string | null) => void;
  onCollapse: () => void;
  primedMessage?: string | null;
  onPrimedMessageApplied?: () => void;
  /** The "New video" dialog's request, sent as the first message. */
  autoSendMessage?: string | null;
  onAutoSendApplied?: () => void;
  initialAgentId?: string | null;
  onBusyChange?: (busy: boolean) => void;
  className?: string;
}

export function VideoStudioRail({ video, focusedScene, onSaved, ...rail }: VideoStudioRailProps) {
  const { t_i18n } = useFormatter();
  const makers = useToolAgents(VIDEO_TOOL_ID);
  const bindConv = useBindConversationVideo();

  const onToolProgress = useCallback(
    (frame: ToolProgressFrame) => {
      if (frame.event !== VIDEO_SAVED_EVENT || frame.video_id !== video.id) return;
      if (typeof frame.revision === "number") onSaved?.(frame.revision);
    },
    [onSaved, video.id],
  );
  const sendContext = useMemo(() => (focusedScene ? { studio_scene: focusedScene.id } : {}), [focusedScene]);
  const { mutateAsync: bindToVideo, isPending: binding } = bindConv;
  const origin = useMemo(
    () => ({
      conversationId: video.conversation_id ?? null,
      label: t_i18n("Continue the conversation that made this video"),
      bind: (conversationId: string) => bindToVideo({ conversationId, videoId: video.id }),
      pending: binding,
    }),
    [bindToVideo, binding, t_i18n, video.conversation_id, video.id],
  );

  const suggestions = useMemo(
    () => [
      t_i18n("Open on a statement over an AI shot"),
      t_i18n("Tighten the narration of this scene"),
      t_i18n("Add a callout on the key click"),
      t_i18n("Make a 30-second vertical teaser of this video"),
    ],
    [t_i18n],
  );

  // A card for the video on the stage is a receipt: it is already open, right here.
  const cardContext = useMemo(() => ({ studioVideoId: video.id }), [video.id]);
  const wrapTranscript = useCallback(
    (transcript: ReactNode) => <VideoCardContext.Provider value={cardContext}>{transcript}</VideoCardContext.Provider>,
    [cardContext],
  );

  const subject: StudioSubject = {
    kind: "video",
    field: "video_project_id",
    id: video.id,
    title: video.title,
    toolAgents: makers,
    sendContext,
    onToolProgress,
    wrapTranscript,
    focus: {
      label: focusedScene
        ? t_i18n("Scene {n} - {kind}", { n: focusedScene.position, kind: focusedScene.label })
        : t_i18n("No scene"),
      tip: t_i18n("The assistant is told which scene the playhead is on"),
    },
    suggestions,
    origin,
    testId: "video.studio",
    labels: {
      rail: t_i18n("Video Studio assistant"),
      history: t_i18n("Conversations about this video"),
      noAgentShort: t_i18n("No agent with the video tools"),
      noAgentTitle: t_i18n("No agent carries the video tools yet"),
      blockedHint: t_i18n(
        "The studio works with agents that have the Video Studio tool. Turn it on in one of your agents' tools and come back here.",
      ),
      unavailableHint: t_i18n("The studio opened this video's own conversation instead."),
    },
  };

  return <StudioRail subject={subject} {...rail} />;
}
