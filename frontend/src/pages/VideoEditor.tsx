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
 * The Video Studio editor - `/videos/:id`, the Presentation Studio's
 * counterpart for a video: the assistant on the left, the video on the stage,
 * the post-production in the person's hands.
 *
 * - Left: the studio rail, a chat bound to this video (`VideoStudioRail`):
 *   the assistant records flows, writes the narration, generates AI shots and
 *   edits the timeline; its saves land on the stage at once.
 * - Centre: the preview - the render's own compositor drawing the draft, with
 *   a transport of its own (frame stepping, scene jumps, speed, loop) - and
 *   the timeline the render plans: cut at the playhead, reorder by dragging,
 *   duplicate, delete, add cards, recordings, AI shots and uploads.
 * - Right: the inspector - the selected scene (content, trim, speed,
 *   narration, transition, overlays, and a recording's camera, cursor,
 *   browser frame, idle compression and highlights), the whole video
 *   (format, template, theme, voice-over, captions, music), the media bin
 *   and the renders.
 *
 * Every edit is a step of the undo history and saves itself once the draft
 * settles (`useVideoEditor`); the keys of an editor work on the stage.
 * Outside AppLayout, like the deck viewer: the work takes the whole screen.
 * So it applies the account theme itself (`useThemeEffect`): opened
 * directly, the editor follows the `/me` preference, not only the theme this
 * browser cached.
 *
 * A reader gets what a read-only deck gets: no rail, no
 * inspector, no quality check, no undo and no editing tool on the timeline -
 * not drawn rather than disabled. Once the video is rendered, a reader watches
 * the render it plays, like its public link, and no preview is asked
 * for; until then the preview, its transport and the timeline take the whole
 * width, under a line saying it is the edit's preview.
 *
 * The bar's primary verb is `Play`: the current render, in the
 * theater, for whoever may watch it; `Render`, outlined beside it, opens the
 * renders tab.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  Clapperboard,
  Copy,
  Globe,
  LayoutTemplate,
  Link2,
  MonitorPlay,
  Palette,
  Play,
  Redo2,
  Trash2,
  Undo2,
} from "lucide-react";

import { ToolbarVerb } from "@/components/presentation/DeckToolbar";
import {
  STUDIO_BAR_BUTTON,
  STUDIO_BAR_TITLE,
  StudioBarLeading,
  StudioLoading,
  StudioSaveState,
  StudioShareButton,
  StudioTitleField,
  StudioToggle,
  StudioUnavailable,
} from "@/components/studio/StudioChrome";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { MediaPanel } from "@/components/video-studio/editor/MediaPanel";
import { PreviewStage, Transport, type TransportState } from "@/components/video-studio/editor/PreviewStage";
import { QualityStrip } from "@/components/video-studio/editor/QualityStrip";
import { RendersPanel } from "@/components/video-studio/editor/RendersPanel";
import { SceneInspector } from "@/components/video-studio/editor/SceneInspector";
import { Timeline, type AddSceneKind } from "@/components/video-studio/editor/Timeline";
import { VideoSettings } from "@/components/video-studio/editor/VideoSettings";
import { createPlayhead, createSkim } from "@/components/video-studio/editor/playhead";
import { useVideoEditor } from "@/components/video-studio/editor/useVideoEditor";
import {
  blankScene,
  customizeBookend,
  duplicateScene,
  insertScene,
  moveScene,
  newSceneId,
  removeScene,
  plannedType,
  sceneIndex,
  sceneIndexAt,
  sceneJump,
  splitScene,
  updateScene,
  updateVideo,
  videoMedia,
  type EditorScene,
} from "@/components/video-studio/editor/video-editor-model";
import { VideoDownloadMenu } from "@/components/video-studio/VideoDownloadMenu";
import { VideoPlayer } from "@/components/video-studio/VideoPlayer";
import { VideoShareDialog } from "@/components/video-studio/VideoShareDialog";
import { VideoTheater } from "@/components/video-studio/VideoTheater";
import { videoLink } from "@/components/video-studio/video-links";
import { VideoStudioHandle, VideoStudioRail } from "@/components/video-studio/VideoStudioRail";
import { SCENE_TYPE_LABELS, formatDuration, renderCaption } from "@/components/video-studio/video-format";
import {
  useDeleteVideoProject,
  useDuplicateVideoProject,
  useVideoProject,
  useVideoQuality,
  useVideoTemplates,
} from "@/hooks/api/useVideoStudioApi";
import { useResizableWidth } from "@/hooks/useResizableWidth";
import { useThemeEffect } from "@/hooks/useThemeEffect";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { VideoPreviewState, VideoRender } from "@/types";

type Panel = "scene" | "video" | "media" | "renders";
/** The inspector's tabs. @i18n-keys */
const PANEL_LABELS = { scene: "Scene", video: "Video", media: "Media", renders: "Renders" } as const;
const STUDIO_PARAM = "studio";
const STUDIO_CONVERSATION_PARAM = "c";
/** The inspector: the width it shipped with, to a third of the screen (the deck's side panel). */
const INSPECTOR_WIDTH = { defaultWidth: 360, minWidth: 320, maxWidth: (viewport: number) => Math.floor(viewport / 3) };
/** The timeline: tall enough for every track and a margin under the last, to a little over half the screen. */
const TIMELINE_HEIGHT = { defaultWidth: 214, minWidth: 170, maxWidth: (viewport: number) => Math.floor(viewport * 0.55) };

/** What the "New video" dialog hands the editor: the request to send the assistant, and who answers. */
export interface VideoEditorLaunch {
  send?: string;
  agentId?: string | null;
}

/** What a reader watches once the video is rendered: the render it plays, like its public link, across the stage. */
function RenderedVideo({ render, title }: { render: VideoRender; title: string }) {
  const { t_i18n, intl } = useFormatter();
  const facts = [
    render.duration_ms ? formatDuration(render.duration_ms) : null,
    render.width && render.height ? `${render.width}x${render.height}` : null,
    render.finished_at ? t_i18n("Rendered on {date}", { date: intl.formatDate(render.finished_at, { dateStyle: "medium" }) }) : null,
  ].filter(Boolean);
  return (
    <section className="flex min-w-0 flex-1 flex-col" data-testid="video.editor.rendered">
      <div className="min-h-0 flex-1">
        <VideoPlayer
          kind="render"
          id={render.id}
          hasCaptions={!!render.captions_file_id && !render.captions_burned_in}
          title={title}
          layout="fill"
          durationHint={render.duration_ms ? render.duration_ms / 1000 : undefined}
          chapters={render.chapters}
          fps={render.fps}
        />
      </div>
      {facts.length > 0 && (
        <p className="shrink-0 border-t border-elevation-subtle-soft px-3 py-1.5 text-center content-compact tabular-nums text-default-secondary">
          {facts.join(" - ")}
        </p>
      )}
    </section>
  );
}

export default function VideoEditor() {
  useThemeEffect();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const { t_i18n } = useFormatter();
  const inspectorPanel = useResizableWidth({ ...INSPECTOR_WIDTH, storageKey: "xtm-video-inspector-width", edge: "start" });
  const timelinePanel = useResizableWidth({ ...TIMELINE_HEIGHT, storageKey: "xtm-video-timeline-height", edge: "start", axis: "y" });
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const { data: video, isLoading, isError } = useVideoProject(id);
  // What the video plays, once a render is ready: the bar's Play, and what a
  // reader watches like the public link - the edit's preview only until then.
  const current = video?.ready_render?.status === "ready" && video.ready_render.file_id ? video.ready_render : null;
  const rendered = video && !video.can_manage ? current : null;
  const editor = useVideoEditor(video, { preview: !rendered });
  // The saved revision's quality check, for an editor; a draft being edited waits for its save. Read again
  // when the media it shows change: a shot generated again in place saves nothing - and when the
  // check itself changed.
  const { data: quality } = useVideoQuality(
    video?.id,
    video?.revision,
    editor.preview?.media_key,
    video?.quality_version,
    editor.canEdit && editor.status === "saved",
  );
  const { draft, preview, edit, canEdit } = editor;
  const { data: templates } = useVideoTemplates();
  const duplicateVideo = useDuplicateVideoProject();
  const deleteVideo = useDeleteVideoProject();

  const playhead = useMemo(() => createPlayhead(), []);
  const skim = useMemo(() => createSkim(), []);
  const [transport, setTransport] = useState<TransportState>({ playing: false, rate: 1, loop: false, muted: false });
  const onTransport = useCallback((patch: Partial<TransportState>) => setTransport((t) => ({ ...t, ...patch })), []);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [panel, setPanel] = useState<Panel>("scene");
  const [sharing, setSharing] = useState(false);
  const [watching, setWatching] = useState(false);

  // The rail: out unless the URL folded it, on the conversation the URL names.
  const launch = (location.state ?? null) as VideoEditorLaunch | null;
  const [studioOpen, setStudioOpen] = useState(() => searchParams.get(STUDIO_PARAM) !== "0");
  const [studioConvId, setStudioConvId] = useState<string | null>(() => searchParams.get(STUDIO_CONVERSATION_PARAM));
  const [primed, setPrimed] = useState<string | null>(null);
  // The dialog's request is sent once: the launch state leaves the history
  // entry, so a reload or a return to this page never sends it again.
  const [autoSend, setAutoSend] = useState<string | null>(launch?.send ?? null);
  const [initialAgentId] = useState(() => (launch && "agentId" in launch ? launch.agentId : undefined));
  useEffect(() => {
    if (location.state) navigate(`${location.pathname}${location.search}`, { replace: true, state: null });
  }, [location.pathname, location.search, location.state, navigate]);
  const clearAutoSend = useCallback(() => setAutoSend(null), []);
  useEffect(() => {
    const url = new URL(window.location.href);
    if (studioOpen) url.searchParams.delete(STUDIO_PARAM);
    else url.searchParams.set(STUDIO_PARAM, "0");
    if (studioConvId) url.searchParams.set(STUDIO_CONVERSATION_PARAM, studioConvId);
    else url.searchParams.delete(STUDIO_CONVERSATION_PARAM);
    if (url.href !== window.location.href) window.history.replaceState(window.history.state, "", url.href);
  }, [studioConvId, studioOpen]);
  const askAssistant = useCallback((message: string) => {
    setPrimed(message);
    setStudioOpen(true);
  }, []);

  const planned = useMemo(() => preview?.document.scenes ?? [], [preview]);
  const inVideo = useMemo(() => videoMedia(draft), [draft]);
  // A video opens one second in, not on its first frame: a fade from black
  // makes frame 0 black, which reads as a stage that did not load.
  const openedAt = useRef(false);
  useEffect(() => {
    if (openedAt.current || !preview) return;
    openedAt.current = true;
    if (playhead.get() === 0) playhead.set(Math.min(1, preview.document.duration / 2));
  }, [playhead, preview]);
  const templateSceneIds = preview?.template_scenes ?? video?.template_scenes ?? [];
  const template = video?.template ? (templates?.find((t) => t.id === video.template?.id) ?? null) : null;

  // The scene under the playhead: the rail's "this scene", followed without re-rendering every frame.
  const [focusedId, setFocusedId] = useState<string | null>(null);
  useEffect(() => {
    const update = () => {
      const index = sceneIndexAt(planned, playhead.get());
      const next = planned[index]?.id ?? null;
      setFocusedId((current) => (current === next ? current : next));
    };
    update();
    return playhead.subscribe(update);
  }, [planned, playhead]);
  // A selection that no longer exists (undone, deleted by the assistant) goes
  // to the scene under the playhead.
  useEffect(() => {
    if (!draft) return;
    const exists = (id: string | null) => !!id && (draft.scenes.some((s) => s.id === id) || planned.some((s) => s.id === id));
    if (!exists(selectedId)) setSelectedId(exists(focusedId) ? focusedId : (draft.scenes[0]?.id ?? null));
  }, [draft, focusedId, planned, selectedId]);

  const focusedPosition = planned.findIndex((s) => s.id === focusedId);
  const focusedScene =
    focusedId && focusedPosition >= 0
      ? { id: focusedId, position: focusedPosition + 1, label: t_i18n(SCENE_TYPE_LABELS[plannedType(planned[focusedPosition])] ?? plannedType(planned[focusedPosition])) }
      : null;

  const selectedScene: EditorScene | undefined =
    draft?.scenes.find((s) => s.id === selectedId) ??
    (preview?.spec.scenes.find((s) => s.id === selectedId) as EditorScene | undefined);
  const selectedPlanned = planned.find((s) => s.id === selectedId);
  const selectedFromTemplate = !!selectedId && templateSceneIds.includes(selectedId) && !draft?.scenes.some((s) => s.id === selectedId);
  const selectedPosition = (planned.findIndex((s) => s.id === selectedId) + 1) || ((draft?.scenes.findIndex((s) => s.id === selectedId) ?? -1) + 1);

  /* ---------- Edits ---------- */
  const insertAfterSelected = useCallback(
    (scene: EditorScene) => {
      if (!draft) return;
      const newId = newSceneId(draft);
      const at = selectedId ? sceneIndex(draft, selectedId) + 1 : draft.scenes.length;
      edit((spec) => insertScene(spec, { ...scene, id: newId }, at > 0 ? at : spec.scenes.length));
      setSelectedId(newId);
      setPanel("scene");
    },
    [draft, edit, selectedId],
  );
  const splitAtPlayhead = useCallback(() => {
    if (!draft || !canEdit) return;
    const t = playhead.get();
    const scene = planned[sceneIndexAt(planned, t)];
    if (!scene || sceneIndex(draft, scene.id) < 0) return;
    const newId = newSceneId(draft);
    const result = splitScene(draft, scene.id, t - scene.start, scene, newId);
    if (!result) {
      toast({ title: t_i18n("Nothing to cut here"), description: t_i18n("Move the playhead inside a scene, away from its edges."), variant: "info" });
      return;
    }
    edit((spec) => splitScene(spec, scene.id, t - scene.start, scene, newId)?.spec ?? spec);
    setSelectedId(newId);
  }, [canEdit, draft, edit, planned, playhead, t_i18n, toast]);
  const customizeSelected = useCallback(() => {
    if (!draft || !selectedScene || !selectedFromTemplate) return;
    const newId = newSceneId(draft);
    if (!customizeBookend(draft, selectedScene, newId)) return;
    edit((spec) => customizeBookend(spec, selectedScene, newId) ?? spec);
    setSelectedId(newId);
  }, [draft, edit, selectedFromTemplate, selectedScene]);
  const duplicateSelected = useCallback(() => {
    if (!draft || !canEdit || !selectedId || sceneIndex(draft, selectedId) < 0) return;
    const newId = newSceneId(draft);
    edit((spec) => duplicateScene(spec, selectedId, newId).spec);
    setSelectedId(newId);
  }, [canEdit, draft, edit, selectedId]);
  const removeSelected = useCallback(() => {
    if (!draft || !canEdit || !selectedId) return;
    const index = sceneIndex(draft, selectedId);
    if (index < 0 || draft.scenes.length <= 1) return;
    edit((spec) => removeScene(spec, selectedId));
    setSelectedId(draft.scenes[index + 1]?.id ?? draft.scenes[index - 1]?.id ?? null);
  }, [canEdit, draft, edit, selectedId]);
  const moveSelected = useCallback(
    (delta: -1 | 1) => {
      if (!draft || !canEdit || !selectedId) return;
      const index = sceneIndex(draft, selectedId);
      if (index < 0) return;
      edit((spec) => moveScene(spec, selectedId, index + delta));
    },
    [canEdit, draft, edit, selectedId],
  );
  const addScene = useCallback(
    (kind: AddSceneKind) => {
      if (kind === "title") insertAfterSelected(blankScene("title", t_i18n("New title")));
      else if (kind === "statement") insertAfterSelected(blankScene("statement", t_i18n("Your *message* here")));
      else if (kind === "outro") insertAfterSelected(blankScene("outro", t_i18n("Thank you")));
      else if (kind === "motion") insertAfterSelected(blankScene("motion", t_i18n("Your *message* here")));
      else setPanel("media");
    },
    [insertAfterSelected, t_i18n],
  );
  const seek = useCallback(
    (seconds: number) => {
      playhead.set(seconds);
    },
    [playhead],
  );
  const fullscreen = useCallback(() => {
    void stageRef.current?.requestFullscreen?.().catch(() => undefined);
  }, []);

  // The editor's keys, off inside a field, a menu and the rail.
  const playsRender = !!rendered;
  const keys = useRef({ splitAtPlayhead, duplicateSelected, removeSelected, moveSelected, fullscreen, editor, transport, playsRender });
  keys.current = { splitAtPlayhead, duplicateSelected, removeSelected, moveSelected, fullscreen, editor, transport, playsRender };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable='true'], [role='listbox'], [role='menu'], [role='dialog'], [data-testid='video.studio.rail']")) return;
      const k = keys.current;
      // The render's own player keeps the keys a video player answers.
      if (k.playsRender) return;
      const mod = event.ctrlKey || event.metaKey;
      const key = event.key.toLowerCase();
      const fps = preview?.document.fps ?? 30;
      const duration = preview?.document.duration ?? 0;
      // Every modified key below edits: a reader's browser keeps its own shortcuts.
      if (mod && !k.editor.canEdit) return;
      if (mod && key === "z") {
        event.preventDefault();
        if (event.shiftKey) k.editor.redo();
        else k.editor.undo();
      } else if (mod && key === "y") {
        event.preventDefault();
        k.editor.redo();
      } else if (mod && key === "s") {
        event.preventDefault();
        void k.editor.saveNow();
      } else if (mod && key === "d") {
        event.preventDefault();
        k.duplicateSelected();
      } else if (mod) {
        return;
      } else if (event.key === " ") {
        event.preventDefault();
        onTransport({ playing: !k.transport.playing });
      } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const delta = event.key === "ArrowLeft" ? -1 : 1;
        if (event.altKey) k.moveSelected(delta);
        else if (event.shiftKey) {
          const target = sceneJump(planned, playhead.get(), delta);
          if (planned[target]) playhead.set(planned[target].start + (target === 0 ? 0 : 0.01));
        } else {
          onTransport({ playing: false });
          playhead.set(Math.min(duration, playhead.get() + delta / fps));
        }
      } else if (event.key === "Home") playhead.set(0);
      else if (event.key === "End") playhead.set(duration);
      else if (key === "s") k.splitAtPlayhead();
      else if (event.key === "Delete" || event.key === "Backspace") k.removeSelected();
      else if (key === "l") onTransport({ loop: !k.transport.loop });
      else if (key === "m") onTransport({ muted: !k.transport.muted });
      else if (key === "f") k.fullscreen();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onTransport, planned, playhead, preview?.document.duration, preview?.document.fps]);

  const stateLabel = useCallback(
    (state: VideoPreviewState) => {
      if (state.state === "slide") return t_i18n("Slide {n} - drawn by the render", { n: state.slide ?? 1 });
      if (state.state === "figure") {
        if (state.drawing) return t_i18n("Figure being drawn");
        return state.block
          ? t_i18n("Figure ({block}) - drawn and built in by the render", { block: state.block })
          : t_i18n("Figure from slide {n} - drawn and built in by the render", { n: state.slide ?? 1 });
      }
      if (state.state === "recording") return t_i18n("Recording in progress");
      if (state.state === "failed") {
        if (state.missing) {
          return state.kind === "recording"
            ? t_i18n("This recording was deleted: record it again, or give the scene another one in the inspector.")
            : t_i18n("This AI shot was deleted: give the scene another one in the inspector.");
        }
        const reason = state.error ?? "";
        return state.kind === "recording"
          ? t_i18n("The recording failed: {reason}", { reason })
          : t_i18n("The AI shot failed: {reason}. Generate it again from the inspector.", { reason });
      }
      return state.progress
        ? t_i18n("AI shot being generated ({n}%)", { n: state.progress })
        : t_i18n("AI shot being generated");
    },
    [t_i18n],
  );

  if (isLoading) return <StudioLoading />;
  if (isError || !video) {
    return (
      <StudioUnavailable
        title={t_i18n("This video is not available.")}
        description={t_i18n(
          "It may have been deleted, or it has not been shared with you - the link alone does not grant access. Ask whoever sent it to share the video with you.",
        )}
        backLabel={t_i18n("Back to Videos")}
        onBack={() => navigate("/videos")}
      />
    );
  }

  const menu: OverflowMenuItem[] = [
    {
      id: "copy-link",
      label: t_i18n("Copy link"),
      icon: Link2,
      onSelect: () => {
        void navigator.clipboard
          ?.writeText(videoLink(video.id))
          .then(() => toast({ title: t_i18n("Link copied"), description: t_i18n("It opens the video for the people it is shared with."), variant: "success" }))
          .catch(() => toast({ title: t_i18n("The link was not copied"), variant: "error" }));
      },
    },
    {
      id: "duplicate",
      label: t_i18n("Duplicate the video"),
      icon: Copy,
      onSelect: async () => {
        try {
          const copy = await duplicateVideo.mutateAsync(video.id);
          navigate(`/videos/${copy.id}`);
        } catch (err) {
          toast({ title: t_i18n("The video was not duplicated"), description: err instanceof Error ? err.message : undefined, variant: "error" });
        }
      },
    },
  ];
  if (video.can_manage) {
    menu.push({
      id: "delete",
      label: t_i18n("Delete"),
      icon: Trash2,
      destructive: true,
      separatorBefore: true,
      onSelect: async () => {
        const ok = await confirm({
          title: t_i18n("Delete this video?"),
          description: t_i18n('"{title}" and all its renders will be removed. Its recordings and clips are kept.', { title: video.title }),
          confirmLabel: t_i18n("Delete"),
          variant: "danger",
        });
        if (!ok) return;
        await deleteVideo.mutateAsync(video.id);
        navigate("/videos");
      },
    });
  }

  const title = draft?.title ?? video.title;
  const problems = editor.status === "error" ? editor.problems : editor.previewProblems;
  const voiceover = draft?.voiceover as { speed?: unknown } | undefined;
  const speechSpeed = typeof voiceover?.speed === "number" ? voiceover.speed : 1;
  const aspectRatio = String((draft?.format as { aspect_ratio?: unknown } | undefined)?.aspect_ratio ?? "16:9");

  return (
    <div className="flex h-dvh w-screen overflow-hidden bg-elevation-default text-default-primary" data-testid="video.editor">
      {video.can_manage &&
        (studioOpen ? (
          <VideoStudioRail
            video={video}
            focusedScene={focusedScene}
            initialConversationId={studioConvId}
            onConversationChange={setStudioConvId}
            onCollapse={() => setStudioOpen(false)}
            primedMessage={primed}
            onPrimedMessageApplied={() => setPrimed(null)}
            autoSendMessage={autoSend}
            onAutoSendApplied={clearAutoSend}
            initialAgentId={initialAgentId}
          />
        ) : (
          <VideoStudioHandle onOpen={() => setStudioOpen(true)} />
        ))}

      <main className="flex min-w-0 flex-1 flex-col">
        {/* A container: under 56rem of bar the verbs drop their text, so the title keeps its room. */}
        <header className="@container flex min-h-12 shrink-0 items-center gap-2 border-b border-elevation-subtle-soft layer-1 bg-elevation-default px-3 py-1.5">
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <StudioBarLeading backLabel={t_i18n("Back to Videos")} onBack={() => navigate("/videos")} icon={Clapperboard}>
              {canEdit ? (
                <StudioTitleField value={title} onChange={(next) => edit((spec) => updateVideo(spec, { title: next }), "video.title")} />
              ) : (
                <h1 className={STUDIO_BAR_TITLE}>{video.title}</h1>
              )}
            </StudioBarLeading>
            {/* The template chip leaves the bar below 64rem, and truncates above it, so it never runs under Play. */}
            <Badge variant="outline" className="hidden min-w-0 max-w-48 shrink gap-1 content-caption @5xl:inline-flex" data-visible-from="5xl" data-testid="video.editor.templateChip">
              {video.template ? <LayoutTemplate className="h-2.5 w-2.5 shrink-0" /> : <Palette className="h-2.5 w-2.5 shrink-0" />}
              <span className="truncate">{video.template?.name ?? t_i18n("Free style")}</span>
            </Badge>
            {canEdit && (
              <StudioSaveState
                status={editor.status === "dirty" ? "unsaved" : editor.status === "saving" || editor.status === "error" ? editor.status : "saved"}
                problems={editor.problems}
                testId="video.editor.saveState"
              />
            )}
          </div>
          <div className="flex shrink-0 items-center gap-0.5">
            {(canEdit || current) && (
              <ToolbarVerb
                icon={Play}
                label={t_i18n("Play")}
                tooltip={current ? t_i18n("Play the current render, full screen") : t_i18n("Not rendered yet: render the video to play it.")}
                onClick={() => {
                  onTransport({ playing: false });
                  setWatching(true);
                }}
                primary
                disabled={!current}
                labelClassName="@max-4xl:hidden"
                testId="video.editor.play"
              />
            )}
            {canEdit && (
              <>
                <ToolbarVerb
                  icon={MonitorPlay}
                  label={t_i18n("Render")}
                  // It opens the Renders tab, where the quality is picked and the render started (the quality gate's
                  // refused-render dialog comes from there): the tooltip says so, since the click itself starts nothing.
                  tooltip={t_i18n("Render: open the Renders tab to pick the quality and start the render")}
                  onClick={() => setPanel("renders")}
                  outline
                  labelClassName="@max-4xl:hidden"
                  testId="video.editor.render"
                />
                <Tip content={`${t_i18n("Undo")} (Ctrl+Z)`}>
                  <Button variant="ghost" size="sm" className={cn(STUDIO_BAR_BUTTON, "ml-1")} onClick={editor.undo} disabled={!editor.canUndo} aria-label={t_i18n("Undo")}>
                    <Undo2 className="h-4 w-4" />
                  </Button>
                </Tip>
                <Tip content={`${t_i18n("Redo")} (Ctrl+Shift+Z)`}>
                  <Button variant="ghost" size="sm" className={STUDIO_BAR_BUTTON} onClick={editor.redo} disabled={!editor.canRedo} aria-label={t_i18n("Redo")}>
                    <Redo2 className="h-4 w-4" />
                  </Button>
                </Tip>
                <StudioToggle open={studioOpen} onToggle={() => setStudioOpen((open) => !open)} testId="video.studio" />
                {video.has_public_link && (
                  <Tip content={t_i18n("Public link: anyone with the link can watch it")}>
                    <Globe className="mx-1 h-4 w-4 text-accent" aria-label={t_i18n("Public link: anyone with the link can watch it")} data-testid="video.editor.publicLink" />
                  </Tip>
                )}
                <StudioShareButton onClick={() => setSharing(true)} testId="video.editor.share" />
              </>
            )}
            {(canEdit || video.ready_render?.status === "ready") && <VideoDownloadMenu video={video} compact />}
            <OverflowMenu items={menu} label={t_i18n("More actions")} />
          </div>
        </header>
        {canEdit && (
          <QualityStrip
            report={quality}
            onOpenScene={(sceneId) => {
              setSelectedId(sceneId);
              setPanel("scene");
              const scene = preview?.document.scenes.find((s) => s.id === sceneId);
              if (scene) seek(scene.start + 0.01);
            }}
            onFixWithAssistant={video.can_manage ? askAssistant : undefined}
          />
        )}
        {problems.length > 0 && (
          <div className="flex items-start gap-2 border-b border-feedback-alert-primary/30 bg-feedback-alert-primary/10 px-3 py-1.5 content-caption text-feedback-warning-tertiary">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <ul className="min-w-0 flex-1 space-y-0.5">
              {problems.slice(0, 4).map((problem) => (
                <li key={`${problem.path}-${problem.message}`} className="truncate">
                  {problem.path ? <span className="font-mono-plex">{problem.path}</span> : null} {problem.message}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="flex min-h-0 flex-1">
          {rendered ? (
            <RenderedVideo render={rendered} title={video.title} />
          ) : (
            <section className="flex min-w-0 flex-1 flex-col">
              {!canEdit && (
                <p className="shrink-0 border-b border-elevation-subtle-soft px-3 py-1.5 text-[11px] text-default-secondary" data-testid="video.editor.notRendered">
                  {t_i18n("Not rendered yet: this is a preview of the edit. Once the video is rendered, it plays here.")}
                </p>
              )}
              <PreviewStage
                preview={preview}
                playhead={playhead}
                skim={skim}
                transport={transport}
                onTransport={onTransport}
                stateLabel={stateLabel}
                previewing={editor.previewing}
                stageRef={stageRef}
                onSourceLost={editor.refreshPreview}
              />
              <Transport preview={preview} playhead={playhead} transport={transport} onTransport={onTransport} onFullscreen={fullscreen} />
              <div className={cn("relative shrink-0", timelinePanel.isResizing && "select-none")} style={{ height: timelinePanel.width }}>
                <PanelResizeHandle
                  panel={timelinePanel}
                  edge="top"
                  label={t_i18n("Resize the timeline")}
                  tip={t_i18n("Drag to resize the timeline - double-click to reset")}
                  testId="video.editor.timeline.resize"
                />
                <Timeline
                  preview={preview}
                  draft={draft}
                  selectedId={selectedId}
                  onSelect={(sceneId) => {
                    setSelectedId(sceneId);
                    setPanel("scene");
                  }}
                  playhead={playhead}
                  skim={skim}
                  onSeek={seek}
                  templateSceneIds={templateSceneIds}
                  canEdit={canEdit}
                  canSplit={canEdit && !!focusedId && !!draft?.scenes.some((s) => s.id === focusedId)}
                  actions={{
                    split: splitAtPlayhead,
                    duplicate: duplicateSelected,
                    remove: removeSelected,
                    move: moveSelected,
                    reorder: (sceneId, to) => edit((spec) => moveScene(spec, sceneId, to)),
                    add: addScene,
                  }}
                />
              </div>
            </section>
          )}

          {canEdit && (
            <aside
              className={cn("relative flex shrink-0 flex-col border-l border-elevation-subtle-soft layer-1 bg-elevation-default", inspectorPanel.isResizing && "select-none")}
              style={{ width: inspectorPanel.width }}
              aria-label={t_i18n("Inspector")}
            >
              <PanelResizeHandle
                panel={inspectorPanel}
                edge="left"
                label={t_i18n("Resize the inspector")}
                tip={t_i18n("Drag to resize the panel - double-click to reset")}
                testId="video.editor.inspector.resize"
              />
              <div className="shrink-0 border-b border-elevation-subtle-soft p-2">
                <SegmentedFilter
                  options={(Object.keys(PANEL_LABELS) as Panel[]).map((key) => ({ key, label: PANEL_LABELS[key] }))}
                  value={panel}
                  onChange={setPanel}
                  ariaLabel={t_i18n("Inspector")}
                  className="w-full justify-between"
                />
              </div>
              <div className={cn("min-h-0 flex-1 overflow-y-auto")}>
                {panel === "scene" &&
                  (selectedScene ? (
                    <SceneInspector
                      key={selectedScene.id}
                      videoId={id}
                      scene={selectedScene}
                      planned={selectedPlanned}
                      position={selectedPosition}
                      fromTemplate={selectedFromTemplate}
                      onCustomize={customizeSelected}
                      canEdit={canEdit}
                      locked={template?.locked}
                      getLocalTime={() => playhead.get() - (selectedPlanned?.start ?? 0)}
                      speechSpeed={speechSpeed}
                      onChange={(patch, coalesce) => selectedId && edit((spec) => updateScene(spec, selectedId, patch), coalesce)}
                      onMediaChanged={editor.refreshPreview}
                    />
                  ) : (
                    <p className="p-4 content-compact text-default-secondary">{t_i18n("Select a scene on the timeline to edit it.")}</p>
                  ))}
                {panel === "video" && draft && (
                  <VideoSettings
                    draft={draft}
                    template={template}
                    canEdit={canEdit}
                    plannedSeconds={preview?.duration}
                    onChange={(patch, coalesce) => edit((spec) => updateVideo(spec, patch), coalesce)}
                  />
                )}
                {panel === "media" && (
                  <MediaPanel canEdit={canEdit} aspectRatio={aspectRatio} inVideo={inVideo} onInsert={insertAfterSelected} onAskAssistant={askAssistant} />
                )}
                {panel === "renders" && (
                  <RendersPanel
                    video={video}
                    onBeforeRender={() => (editor.status === "saved" ? Promise.resolve(true) : editor.saveNow())}
                  />
                )}
              </div>
            </aside>
          )}
        </div>
      </main>
      {video.can_manage && <VideoShareDialog video={video} open={sharing} onOpenChange={setSharing} />}
      {watching && current && (
        <VideoTheater
          media={{ kind: "render", render: current }}
          title={video.title}
          subtitle={renderCaption(current, t_i18n)}
          onClose={() => setWatching(false)}
        />
      )}
      {ConfirmEl}
    </div>
  );
}
