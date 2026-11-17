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
 * The editor's timeline: the video as the render plans it.
 *
 * Every block sits where the render's planner put its scene (`start` /
 * `end` of the preview document), so a transition shows as the overlap it
 * is and a narration longer than its scene shows the scene stretched to it.
 * Lanes under the scenes: the overlays at their time, the narration (solid
 * once spoken, at its measured length; dashed and paced at the planner's speed
 * until then, pulsing while it is being spoken), the music bed; a clip
 * whose own sound plays carries a speaker. The playhead is dragged on the ruler;
 * a scene is selected by a click, reordered by a drag, cut at the playhead,
 * duplicated and removed from the toolbar (or the keys the page binds). The
 * template's intro and outro are drawn too, locked: they are not part of the
 * spec the author edits. A reader keeps the tracks, the playhead and
 * the zoom; the editing tools are not drawn. The pointer over a figure whose
 * build plays skims it: the stage draws the build under the pointer,
 * and selecting the figure lands on it built in whole.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Captions,
  Copy,
  Film,
  ImageIcon,
  Lock,
  Mic,
  MicOff,
  MonitorPlay,
  Music,
  Plus,
  Presentation,
  Quote,
  Scissors,
  Sparkles,
  Trash2,
  Type,
  Volume2,
  Wand2,
  Workflow,
  ZoomIn,
  ZoomOut,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { VideoPreview, VideoPreviewInsert, VideoPreviewScene } from "@/types";

import { OVERLAY_TYPE_LABELS, SCENE_TYPE_LABELS } from "../video-format";
import {
  coverRect,
  formatTimecode,
  holdMedia,
  insertPlane,
  plannedType,
  playsBuild,
  sceneLanding,
  type EditorScene,
  type EditorSpec,
  type HeldMedia,
  type SceneType,
} from "./video-editor-model";
import { playheadMarks, useDevicePixelRatio, usePlayheadTime, useSkimTime, type Playhead, type Skim } from "./playhead";

/** Pixels per second, at the narrowest and the widest. */
const MIN_PPS = 6;
const MAX_PPS = 320;
const SCENE_ICONS: Record<SceneType, React.ElementType> = {
  title: Type,
  statement: Quote,
  outro: Type,
  recording: MonitorPlay,
  clip: Wand2,
  image: ImageIcon,
  slide: Presentation,
  figure: Workflow,
  motion: Sparkles,
};
const SCENE_TINTS: Record<SceneType, string> = {
  title: "border-filigran-ia-primary/40 bg-filigran-ia-primary/15",
  statement: "border-filigran-ia-primary/50 bg-filigran-ia-primary/25",
  outro: "border-filigran-ia-primary/40 bg-filigran-ia-primary/15",
  recording: "border-accent/40 bg-accent/10",
  clip: "border-feedback-alert-primary/40 bg-feedback-alert-primary/10",
  image: "border-feedback-success-primary/40 bg-feedback-success-primary/10",
  slide: "border-feedback-info-primary/40 bg-feedback-info-primary/10",
  figure: "border-feedback-info-primary/50 bg-feedback-info-primary/20",
  motion: "border-filigran-ia-primary/60 bg-filigran-ia-primary/30",
};

export type AddSceneKind = "title" | "statement" | "outro" | "motion" | "image" | "video_file" | "recording" | "ai_shot";

export interface TimelineActions {
  split: () => void;
  duplicate: () => void;
  remove: () => void;
  move: (delta: -1 | 1) => void;
  reorder: (sceneId: string, toIndex: number) => void;
  add: (kind: AddSceneKind) => void;
}

/** The tick spacing (s) that keeps labels about 80 px apart. */
function tickStep(pps: number): number {
  for (const step of [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300]) if (step * pps >= 80) return step;
  return 600;
}

/** The plan's media URLs as the thumbnails show them: each one held while it plays the same file (`holdMedia`). */
function useHeldMedia(preview: VideoPreview | null) {
  const held = useRef<Record<string, HeldMedia>>({});
  const [lost, setLost] = useState<ReadonlySet<string>>(() => new Set());
  const media = preview?.document.preview_media;
  const ids = preview?.document.preview_media_ids;
  const urls = useMemo(() => {
    held.current = holdMedia(held.current, media ?? {}, ids ?? {}, lost);
    return Object.fromEntries(Object.entries(held.current).map(([key, entry]) => [key, entry.url]));
  }, [media, ids, lost]);
  const lose = (url: string) => setLost((current) => (current.has(url) ? current : new Set(current).add(url)));
  return { urls, lose };
}

/** A screen insert on a thumbnail: its source at the moment it comes in, laid on its quad of the footage. */
function InsertPlane({
  insert,
  url,
  width,
  height,
  onLost,
}: {
  insert: VideoPreviewInsert;
  url: string;
  width: number;
  height: number;
  onLost: (url: string) => void;
}) {
  const plane = insertPlane(insert.quad, width, height);
  if (!plane) return null;
  const fit = insert.fit === "contain" ? "object-contain" : "object-cover";
  const at = insert.time_map?.[0]?.[1] ?? 0;
  return (
    <span
      // `contain` keeps the footage around the source, as the render does: never a black card.
      className="absolute left-0 top-0 origin-top-left overflow-hidden"
      // Data-driven geometry: the insert's quad on the footage the poster shows.
      style={{ width: plane.width, height: plane.height, transform: plane.transform }}
    >
      {insert.kind === "video" ? (
        <video
          src={url}
          muted
          playsInline
          preload="metadata"
          className={cn("absolute inset-0 h-full w-full", fit)}
          onLoadedMetadata={(event) => {
            event.currentTarget.currentTime = at;
          }}
          onError={() => onLost(url)}
        />
      ) : (
        <img src={url} alt="" className={cn("absolute inset-0 h-full w-full", fit)} onError={() => onLost(url)} />
      )}
    </span>
  );
}

/**
 * A scene's thumbnail, from the preview: the poster of the recording or AI shot it plays, else the
 * picture it draws (an image, a figure's still) - nothing for media still being made. A poster is the
 * footage as it was shot, so the scene's screen inserts are laid on it where the stage maps them: on
 * the rect the poster covers in the block, read once the block and the poster have a size.
 */
function ScenePoster({
  still,
  inserts,
  media,
  onLost,
}: {
  still: string | undefined;
  inserts: VideoPreviewInsert[] | undefined;
  media: Record<string, string>;
  onLost: (url: string) => void;
}) {
  const boxRef = useRef<HTMLSpanElement>(null);
  const [box, setBox] = useState<[number, number] | null>(null);
  const [footage, setFootage] = useState<[number, number] | null>(null);
  const drawn = (inserts ?? []).filter((insert) => media[insert.src]);
  const measured = !!still && drawn.length > 0;

  useEffect(() => {
    const el = boxRef.current;
    if (!measured || !el) return;
    const read = () => setBox((prev) => (prev && prev[0] === el.clientWidth && prev[1] === el.clientHeight ? prev : [el.clientWidth, el.clientHeight]));
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, [measured]);

  if (!still) return null;
  const rect = measured && box && footage ? coverRect(box[0], box[1], footage[0], footage[1]) : null;
  return (
    <span ref={boxRef} aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden opacity-35">
      <img
        src={still}
        alt=""
        className="absolute inset-0 h-full w-full object-cover"
        onLoad={(event) => {
          const { naturalWidth, naturalHeight } = event.currentTarget;
          if (naturalWidth && naturalHeight) setFootage([naturalWidth, naturalHeight]);
        }}
        onError={() => onLost(still)}
      />
      {rect && (
        <span className="absolute" style={{ left: rect[0], top: rect[1], width: rect[2], height: rect[3] }}>
          {drawn.map((insert, index) => (
            <InsertPlane key={`${insert.src}-${index}`} insert={insert} url={media[insert.src]} width={rect[2]} height={rect[3]} onLost={onLost} />
          ))}
        </span>
      )}
    </span>
  );
}

function SceneBlock({
  planned,
  scene,
  still,
  media,
  onMediaLost,
  position,
  pps,
  selected,
  fromTemplate,
  canDrag,
  onSelect,
  onDragStart,
  onDrop,
  onSkim,
}: {
  planned: VideoPreviewScene;
  scene: EditorScene | undefined;
  still: string | undefined;
  /** The preview's media URLs, by key: what the scene's screen inserts show. */
  media: Record<string, string>;
  /** One of them stopped answering: the thumbnail takes the plan's newest. */
  onMediaLost: (url: string) => void;
  position: number;
  pps: number;
  selected: boolean;
  fromTemplate: boolean;
  canDrag: boolean;
  onSelect: () => void;
  onDragStart: () => void;
  onDrop: (after: boolean) => void;
  /** A figure whose build plays: the time under the pointer, `null` once it leaves. */
  onSkim?: (seconds: number | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const type = plannedType(planned) as SceneType;
  const Icon = SCENE_ICONS[type] ?? Film;
  const length = planned.end - planned.start;
  const transition = planned.transition_in?.duration ?? 0;
  const firstLine = Array.isArray(scene?.lines) && typeof scene.lines[0] === "string" ? scene.lines[0].replace(/\*/g, "") : "";
  const label =
    (typeof scene?.title === "string" && scene.title) ||
    (typeof planned.title === "string" && planned.title) ||
    firstLine ||
    planned.preview_name ||
    t_i18n(SCENE_TYPE_LABELS[type] ?? type);
  const state = planned.preview_state;
  const [over, setOver] = useState<"before" | "after" | null>(null);
  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={t_i18n("Scene {n}: {label}", { n: position, label })}
      draggable={canDrag}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === "Enter") onSelect();
      }}
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "move";
        onSkim?.(null);
        onDragStart();
      }}
      onPointerMove={(event) => {
        if (!onSkim || event.buttons !== 0) return;
        const box = event.currentTarget.getBoundingClientRect();
        onSkim(planned.start + Math.min(1, Math.max(0, (event.clientX - box.left) / Math.max(1, box.width))) * length);
      }}
      onPointerLeave={() => onSkim?.(null)}
      onDragOver={(event) => {
        if (!canDrag) return;
        event.preventDefault();
        const box = event.currentTarget.getBoundingClientRect();
        setOver(event.clientX - box.left > box.width / 2 ? "after" : "before");
      }}
      onDragLeave={() => setOver(null)}
      onDrop={(event) => {
        event.preventDefault();
        const after = over === "after";
        setOver(null);
        onDrop(after);
      }}
      className={cn(
        "group absolute top-1 bottom-1 overflow-hidden rounded-sm border text-left transition-shadow",
        SCENE_TINTS[type] ?? "border-elevation-subtle layer-1 bg-elevation-default",
        selected && "z-10 ring-2 ring-accent shadow-[0_0_0_1px_color-mix(in_srgb,_var(--color-accent)_calc(0.4_*_100%),_transparent)]",
        fromTemplate && "border-dashed opacity-80",
        canDrag && "cursor-grab active:cursor-grabbing",
        over === "before" && "border-l-4 border-l-accent",
        over === "after" && "border-r-4 border-r-accent",
      )}
      // Data-driven geometry: the scene's planned time on the zoomed ruler.
      style={{ left: planned.start * pps, width: Math.max(length * pps, 6) }}
    >
      <ScenePoster still={still} inserts={planned.inserts} media={media} onLost={onMediaLost} />
      {transition > 0 && (
        <span
          aria-hidden
          className="absolute inset-y-0 left-0 bg-gradient-to-r from-[var(--text-default-primary)]/25 to-transparent"
          style={{ width: transition * pps }}
        />
      )}
      <span className="relative flex h-full flex-col justify-between gap-0.5 px-1.5 py-1">
        <span className="flex min-w-0 items-center gap-1 text-[11px] font-medium">
          <Icon className="h-3 w-3 shrink-0" />
          <span className="truncate">{label}</span>
          {planned.preview_sound?.plays && <Volume2 className="h-2.5 w-2.5 shrink-0 text-default-secondary" aria-label={t_i18n("Its own sound plays")} />}
          {fromTemplate && <Lock className="h-2.5 w-2.5 shrink-0 text-default-secondary" aria-label={t_i18n("Template")} />}
        </span>
        <span className="flex items-center gap-1 content-caption tabular-nums text-default-secondary">
          <span>{position}</span>
          <span>-</span>
          <span>{length.toFixed(1)} s</span>
          {state?.state === "failed" ? (
            <span className="inline-flex min-w-0 items-center gap-0.5 truncate text-feedback-warning-primary" title={state.error}>
              <AlertTriangle className="h-2.5 w-2.5 shrink-0" /> {t_i18n("Failed")}
            </span>
          ) : (
            // A slide is drawn by the render, a figure too until its still is drawn: neither is in progress.
            state &&
            state.state !== "slide" &&
            (state.state !== "figure" || state.drawing) && (
              <span className="truncate text-feedback-warning-primary">{t_i18n("in progress")}</span>
            )
          )}
        </span>
      </span>
    </div>
  );
}

/** The playhead: its line and, centred on it, its head, in whole device pixels (`playheadMarks`). */
function Playhead({ playhead, pps }: { playhead: Playhead; pps: number }) {
  const time = usePlayheadTime(playhead);
  const marks = playheadMarks(useDevicePixelRatio());
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-y-0 z-20 bg-accent shadow-[0_0_8px_color-mix(in_srgb,_var(--color-accent)_calc(0.8_*_100%),_transparent)]"
      // Data-driven geometry: the time on the zoomed ruler, the line's width in device pixels.
      style={{ left: time * pps, width: marks.line }}
      data-testid="video.editor.playhead"
    >
      <span
        className="absolute top-0 box-content h-0 border-x-transparent border-t-accent"
        // The head is the line's width plus a side on each hand: its middle column is the line's.
        style={{ left: -marks.side, width: marks.line, borderLeftWidth: marks.side, borderRightWidth: marks.side, borderTopWidth: marks.depth }}
      />
    </div>
  );
}

/** Where the pointer skims a figure's build: what the stage shows meanwhile. */
function SkimLine({ skim, pps }: { skim: Skim; pps: number }) {
  const time = useSkimTime(skim);
  const marks = playheadMarks(useDevicePixelRatio());
  if (time === null) return null;
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-y-0 z-20 bg-accent/50"
      // Data-driven geometry: the skimmed time on the zoomed ruler, the line's width in device pixels.
      style={{ left: time * pps, width: marks.line }}
    />
  );
}

export function Timeline({
  preview,
  draft,
  selectedId,
  onSelect,
  playhead,
  skim,
  onSeek,
  templateSceneIds,
  canEdit,
  canSplit,
  actions,
}: {
  preview: VideoPreview | null;
  draft: EditorSpec | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  playhead: Playhead;
  /** Set while the pointer skims a figure's build. */
  skim?: Skim;
  onSeek: (seconds: number) => void;
  templateSceneIds: string[];
  canEdit: boolean;
  canSplit: boolean;
  actions: TimelineActions;
}) {
  const { t_i18n } = useFormatter();
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(800);
  const [zoom, setZoom] = useState(1);
  const dragging = useRef<string | null>(null);
  const duration = preview?.document.duration ?? 0;

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setWidth(el.clientWidth));
    observer.observe(el);
    setWidth(el.clientWidth);
    return () => observer.disconnect();
  }, []);

  // Zoom 1 fits the whole video in the view; the buttons multiply it.
  const fitPps = duration > 0 ? (width - 24) / duration : 40;
  const pps = Math.min(MAX_PPS, Math.max(MIN_PPS, fitPps * zoom));
  const contentWidth = Math.max(width, duration * pps + 24);
  const step = tickStep(pps);
  const ticks = useMemo(() => {
    const out: number[] = [];
    for (let t = 0; t <= duration + 1e-6; t += step) out.push(Math.round(t * 100) / 100);
    return out;
  }, [duration, step]);

  const scenesById = useMemo(() => new Map((draft?.scenes ?? []).map((s) => [s.id, s])), [draft]);
  const specIndex = useMemo(() => new Map((draft?.scenes ?? []).map((s, i) => [s.id, i])), [draft]);
  const planned = useMemo(() => preview?.document.scenes ?? [], [preview]);
  const { urls: previewMedia, lose: loseMedia } = useHeldMedia(preview);
  const overlays = useMemo(
    () =>
      planned.flatMap((scene) =>
        (scene.overlays ?? []).map((overlay, index) => ({
          key: `${scene.id}-${index}`,
          sceneId: scene.id,
          start: scene.start + overlay.t0,
          end: scene.start + overlay.t1,
          text: overlay.text || t_i18n(OVERLAY_TYPE_LABELS[overlay.type as keyof typeof OVERLAY_TYPE_LABELS] ?? overlay.type),
        })),
      ),
    [planned, t_i18n],
  );

  // Keep the playhead in view while it plays past the edge.
  useEffect(
    () =>
      playhead.subscribe(() => {
        const el = scrollerRef.current;
        if (!el) return;
        const x = playhead.get() * pps;
        if (x < el.scrollLeft || x > el.scrollLeft + el.clientWidth - 40) el.scrollLeft = Math.max(0, x - 80);
      }),
    [playhead, pps],
  );

  const seekFromPointer = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const box = event.currentTarget.getBoundingClientRect();
      onSeek(Math.min(duration, Math.max(0, (event.clientX - box.left) / pps)));
    },
    [duration, onSeek, pps],
  );

  const addItems: OverflowMenuItem[] = [
    { id: "statement", label: t_i18n("Statement"), icon: Quote, onSelect: () => actions.add("statement") },
    { id: "motion", label: t_i18n("Motion graphics"), icon: Sparkles, onSelect: () => actions.add("motion") },
    { id: "title", label: t_i18n("Title card"), icon: Type, onSelect: () => actions.add("title") },
    { id: "outro", label: t_i18n("Closing card"), icon: Type, onSelect: () => actions.add("outro") },
    { id: "recording", label: t_i18n("Recording"), icon: MonitorPlay, onSelect: () => actions.add("recording"), separatorBefore: true },
    { id: "ai_shot", label: t_i18n("AI shot"), icon: Wand2, onSelect: () => actions.add("ai_shot") },
    { id: "image", label: t_i18n("Image"), icon: ImageIcon, onSelect: () => actions.add("image"), separatorBefore: true },
    { id: "video_file", label: t_i18n("Video file"), icon: Film, onSelect: () => actions.add("video_file") },
  ];
  const selectedEditable = !!selectedId && scenesById.has(selectedId) && canEdit;
  const toolButton = (label: string, keys: string | null, Icon: React.ElementType, onClick: () => void, disabled: boolean) => (
    <Tip content={keys ? `${label} (${keys})` : label}>
      <Button variant="ghost" size="sm" className="h-7 w-7 px-0" onClick={onClick} disabled={disabled} aria-label={label}>
        <Icon className="h-3.5 w-3.5" />
      </Button>
    </Tip>
  );

  return (
    <div className="flex h-full min-h-0 flex-col border-t border-elevation-subtle-soft layer-1 bg-elevation-default" data-testid="video.editor.timeline">
      {/* A container: at 1024 with the Studio rail open the timeline is narrow, so "Add a scene" folds to its
          icon (its name stays on the button) and the zoom keeps its full width left of the inspector. */}
      <div className="@container flex h-9 shrink-0 items-center gap-0.5 border-b border-elevation-subtle-soft px-2">
        {canEdit && (
          <>
            {toolButton(t_i18n("Cut at the playhead"), "S", Scissors, actions.split, !canSplit)}
            {toolButton(t_i18n("Duplicate the scene"), "Ctrl+D", Copy, actions.duplicate, !selectedEditable)}
            {toolButton(t_i18n("Delete the scene"), "Del", Trash2, actions.remove, !selectedEditable || (draft?.scenes.length ?? 0) <= 1)}
            <span className="mx-1 h-4 w-px bg-[var(--border-elevation-subtle)]" aria-hidden />
            {toolButton(t_i18n("Move earlier"), "Alt+Left", ArrowLeft, () => actions.move(-1), !selectedEditable)}
            {toolButton(t_i18n("Move later"), "Alt+Right", ArrowRight, () => actions.move(1), !selectedEditable)}
            <span className="mx-1 h-4 w-px bg-[var(--border-elevation-subtle)]" aria-hidden />
            {/* Folded to its icon below @lg, the button is a bare +: the Tip names it on hover and focus,
                as its icon siblings are named. OverflowMenu forwards no ref, so the Tip holds a span. */}
            <Tip content={t_i18n("Add a scene")}>
              <span className="inline-flex">
                <OverflowMenu
                  items={addItems}
                  label={t_i18n("Add a scene")}
                  side="auto"
                  className="h-7 gap-1 px-2 content-compact"
                  trigger={
                    <span className="flex items-center gap-1">
                      <Plus className="h-3.5 w-3.5" />
                      <span className="hidden @lg:inline">{t_i18n("Add a scene")}</span>
                    </span>
                  }
                />
              </span>
            </Tip>
          </>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-0.5">
          {toolButton(t_i18n("Zoom out"), "-", ZoomOut, () => setZoom((z) => Math.max(1, z / 1.6)), zoom <= 1)}
          <Tip content={t_i18n("Fit the video")}>
            <button
              type="button"
              onClick={() => setZoom(1)}
              className="min-w-[44px] rounded-sm px-1 content-caption tabular-nums text-default-secondary hover:bg-elevation-hover"
            >
              {Math.round(zoom * 100)}%
            </button>
          </Tip>
          {toolButton(t_i18n("Zoom in"), "+", ZoomIn, () => setZoom((z) => Math.min(40, z * 1.6)), pps >= MAX_PPS)}
        </div>
      </div>

      {/* The panel's height is the person's (resizable): the scene track takes what the
          others leave, and the last track keeps a margin from the bottom of the page. */}
      <div ref={scrollerRef} className="relative min-h-0 flex-1 overflow-auto">
        <div className="relative flex min-h-full flex-col pb-3" style={{ width: contentWidth }}>
          {/* Ruler: click or drag to move the playhead */}
          <div
            className="relative h-6 shrink-0 cursor-text select-none border-b border-elevation-subtle-soft bg-elevation-default"
            onPointerDown={(event) => {
              event.currentTarget.setPointerCapture(event.pointerId);
              seekFromPointer(event);
            }}
            onPointerMove={(event) => {
              if (event.buttons === 1) seekFromPointer(event);
            }}
            role="slider"
            aria-label={t_i18n("Playhead")}
            aria-valuemin={0}
            aria-valuemax={Math.round(duration * 10) / 10}
            aria-valuenow={Math.round(playhead.get() * 10) / 10}
            tabIndex={-1}
          >
            {ticks.map((t) => (
              <span key={t} className="absolute top-0 h-full border-l border-elevation-subtle-soft" style={{ left: t * pps }}>
                <span className="absolute left-1 top-0.5 text-[9px] tabular-nums text-default-secondary">
                  {formatTimecode(t).replace(/\.0$/, "")}
                </span>
              </span>
            ))}
          </div>

          {/* Scenes */}
          <div className="relative min-h-[58px] flex-1">
            {planned.length === 0 && (
              <p className="absolute inset-0 flex items-center px-3 content-compact text-default-secondary">
                {t_i18n("The timeline appears once the preview is ready.")}
              </p>
            )}
            {planned.map((scene, index) => {
              const fromTemplate = templateSceneIds.includes(scene.id);
              const own = specIndex.get(scene.id);
              return (
                <SceneBlock
                  key={scene.id ?? index}
                  planned={scene}
                  scene={scenesById.get(scene.id)}
                  still={previewMedia[`poster/${scene.id}`] ?? (scene.type === "image" || scene.type === "figure" ? previewMedia[`image/${scene.id}`] : undefined)}
                  media={previewMedia}
                  onMediaLost={loseMedia}
                  position={index + 1}
                  pps={pps}
                  selected={scene.id === selectedId}
                  fromTemplate={fromTemplate}
                  canDrag={canEdit && own !== undefined}
                  onSelect={() => {
                    onSelect(scene.id);
                    if (playhead.get() < scene.start || playhead.get() >= scene.end) onSeek(sceneLanding(planned, index));
                  }}
                  onSkim={skim && playsBuild(scene) ? skim.set : undefined}
                  onDragStart={() => {
                    dragging.current = scene.id;
                  }}
                  onDrop={(after) => {
                    const moved = dragging.current;
                    dragging.current = null;
                    if (!moved || moved === scene.id || own === undefined) return;
                    const from = specIndex.get(moved) ?? 0;
                    let to = own + (after ? 1 : 0);
                    if (from < to) to -= 1;
                    actions.reorder(moved, to);
                  }}
                />
              );
            })}
          </div>

          {/* Overlays */}
          <div className="relative h-5 shrink-0 border-t border-elevation-subtle-soft">
            <Captions className="absolute left-1 top-1 h-3 w-3 text-default-secondary" aria-label={t_i18n("Overlays")} />
            {overlays.map((overlay) => (
              <button
                type="button"
                key={overlay.key}
                onClick={() => {
                  onSelect(overlay.sceneId);
                  onSeek(overlay.start + 0.01);
                }}
                title={overlay.text}
                className="absolute top-0.5 h-4 truncate rounded-sm border border-filigran-ia-primary/40 bg-filigran-ia-primary/15 px-1 text-left text-[9px] leading-4"
                style={{ left: overlay.start * pps, width: Math.max((overlay.end - overlay.start) * pps, 4) }}
              >
                {overlay.text}
              </button>
            ))}
          </div>

          {/* Narration */}
          <div className="relative h-5 shrink-0 border-t border-elevation-subtle-soft">
            {preview?.voice?.state === "unavailable" ? (
              <Tip content={preview.voice.error ?? t_i18n("No speech model is available to you.")}>
                <MicOff className="absolute left-1 top-1 h-3 w-3 text-feedback-warning-primary" aria-label={t_i18n("Narration shown as captions only")} />
              </Tip>
            ) : preview?.voice?.state === "off" ? (
              <MicOff className="absolute left-1 top-1 h-3 w-3 text-default-secondary" aria-label={t_i18n("Narration shown as captions only")} />
            ) : (
              <Mic className="absolute left-1 top-1 h-3 w-3 text-default-secondary" aria-label={t_i18n("Narration")} />
            )}
            {(preview?.narration ?? []).map((line) => (
              <button
                type="button"
                key={line.scene_id}
                onClick={() => {
                  onSelect(line.scene_id);
                  onSeek(line.start + 0.01);
                }}
                title={
                  line.state === "failed"
                    ? t_i18n("The voice-over failed: {reason}", { reason: line.error ?? "" })
                    : line.state === "speaking"
                      ? `${t_i18n("Being spoken...")} ${line.text}`
                      : line.text
                }
                data-state={line.state ?? "none"}
                className={cn(
                  "absolute top-0.5 h-4 truncate rounded-sm border px-1 text-left text-[9px] leading-4",
                  line.state === "ready" && "border-accent/40 bg-accent/20",
                  line.state === "speaking" && "animate-pulse border-dashed border-accent/40 bg-accent/10",
                  line.state === "failed" && "border-feedback-alert-primary/40 bg-feedback-alert-primary/10 text-feedback-warning-tertiary",
                  (!line.state || line.state === "none") && "border-dashed border-accent/30 bg-accent/5 text-default-secondary",
                )}
                style={{ left: line.start * pps, width: Math.max(line.seconds * pps, 4) }}
              >
                {line.state === "failed" && <AlertTriangle className="mr-0.5 inline h-2.5 w-2.5" aria-hidden />}
                {line.text}
              </button>
            ))}
          </div>

          {preview?.music && (
            <div className="relative h-4 shrink-0 border-t border-elevation-subtle-soft">
              <div
                className="absolute inset-y-0.5 flex items-center gap-1 rounded-sm border border-feedback-success-primary/30 bg-feedback-success-primary/10 px-1 text-[9px] text-feedback-success-primary"
                style={{ left: 0, width: duration * pps }}
              >
                <Music className="h-2.5 w-2.5" />{" "}
                {preview.music.state?.state === "music"
                  ? t_i18n("Composing the music ({progress}%)", { progress: preview.music.state.progress ?? 0 })
                  : preview.music.state?.state === "failed"
                    ? t_i18n("The AI track failed: {reason}", { reason: preview.music.state.error ?? "" })
                    : preview.music.state?.state === "none"
                      ? t_i18n("No music source")
                      : t_i18n("Music")}
              </div>
            </div>
          )}

          <Playhead playhead={playhead} pps={pps} />
          {skim && <SkimLine skim={skim} pps={pps} />}
        </div>
      </div>
    </div>
  );
}
