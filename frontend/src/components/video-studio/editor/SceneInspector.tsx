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
 * The selected scene, every setting the spec has for it: what the
 * agent tools could always change, now in the person's hands - the content of
 * a card or a statement and what plays behind it, where an AI shot is
 * (being made, or failed with its reason and a way to generate it again), the
 * trim and speed of a recording or a shot, the narration and its
 * pace, the transition in, the overlays at their time, and for a recording
 * the post-production effects: idle compression, the camera, the cursor, the
 * browser frame and the highlights.
 */
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Camera,
  Captions,
  Clapperboard,
  Frame,
  Highlighter,
  Lock,
  Mic,
  MousePointer2,
  PenLine,
  Plus,
  Quote,
  RotateCcw,
  Sparkles,
  Timer,
  Trash2,
  Type,
  Workflow,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useRetryVideoClip, useVideoAssets, useVideoClip, useVideoMediaUrl, useVideoTake } from "@/hooks/api/useVideoStudioApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useDebounce } from "@/hooks/useDebounce";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { VideoClip, VideoClipStartSource, VideoPreviewScene } from "@/types";

import {
  ALIGN_LABELS,
  CAMERA_MODE_LABELS,
  CAMERA_PACE_LABELS,
  CAMERA_ZOOM_SECONDS,
  CARD_STYLE_LABELS,
  CLICK_EFFECT_LABELS,
  CURSOR_STYLE_LABELS,
  EXTEND_MODE_LABELS,
  FIGURE_BUILD_LABELS,
  FIGURE_CAMERA_LABELS,
  FIGURE_MOTION_LABELS,
  FIGURE_PERSPECTIVE_LABELS,
  FIGURE_REVEAL_LABELS,
  FIT_LABELS,
  FRAME_STYLE_LABELS,
  IDLE_MODE_LABELS,
  IMAGE_OVERLAY_WIDTH,
  INSERT_KEY_LABELS,
  MOTION_COMPOSITION_LABELS,
  MOTION_LABELS,
  OVERLAY_ASSET_LABELS,
  OVERLAY_POSITION_LABELS,
  OVERLAY_STYLE_LABELS,
  OVERLAY_TYPE_LABELS,
  REVEAL_LABELS,
  SCENE_SOUND_LABELS,
  SCENE_SOUND_REASONS,
  SCENE_TYPE_LABELS,
  TRANSITION_LABELS,
  TRANSITION_SECONDS,
  ZOOM_ON_LABELS,
  choicesOf,
  formatDuration,
  isLockedBy,
  sceneSoundReason,
} from "../video-format";
import { Field, NumberField, Section, SelectField, SwitchField, TextAreaField, TextField } from "./fields";
import {
  DEFAULT_DURATION,
  DEFAULT_FIGURE_DWELL,
  MOTION_COMPOSITIONS,
  estimateNarrationSeconds,
  motionCompositionPatch,
  statementLines,
  type EditorScene,
  type MotionComposition,
  type SceneType,
} from "./video-editor-model";
import { Spinner } from "@filigran/design-system";

type Patch = Record<string, unknown>;

/** The picker values of a statement's backdrop that are not an AI shot. */
const BRAND_BACKDROP = "__brand__";
const FILE_BACKDROP = "__file__";
/** The AI shots the backdrop picker offers at once: the newest, or the first that match the search. */
const BACKDROP_CHOICES = 20;
/** A picture or a take mapped onto a surface of the footage: its corners come from the assistant, never typed. */
const SCREEN_INSERT = "screen_insert";
type Overlay = {
  type?: keyof typeof OVERLAY_TYPE_LABELS | typeof SCREEN_INSERT;
  text?: string;
  subtext?: string;
  start?: number;
  duration?: number;
  at_step?: number;
  position?: keyof typeof OVERLAY_POSITION_LABELS;
  style?: keyof typeof OVERLAY_STYLE_LABELS;
  /** An image overlay's picture: a file, or a brand mark of the template by its role. */
  file_id?: string;
  asset?: keyof typeof OVERLAY_ASSET_LABELS;
  /** A share of the frame's width (up to 1), else design pixels. */
  width?: number;
  opacity?: number;
  /** A screen insert's source, surface, fit and glow; a brand mark of the template by its role. */
  source?: InsertSource;
  /** A wall of panes on one surface: N side by side or [rows, cols], one of `sources` each. */
  panes?: number | [number, number];
  sources?: InsertSource[];
  gutter?: number;
  /** Its corners on the footage, or `auto`: the bezel the render finds on its first footage frame. */
  quad?: [number, number][] | "auto";
  fit?: keyof typeof FIT_LABELS;
  glow?: number;
  /** How much of the footage's light and reflections play over the insert: 0-1, 0.15 when unset. */
  light?: number;
  /** Its quad followed through the footage. */
  track?: boolean;
  /** The surface's chroma key; unset, what crosses the surface stays in front of it. */
  key?: InsertKey;
  /** A camera push on what it shows (`spec.InsertZoom`): seconds after it comes in, how far, toward where. */
  zoom?: { start?: number; end: number; scale: number; focus?: [number, number] };
  /** How a keyed insert takes the key's spill off the edge of what stands in front (`spec.InsertDespill`). */
  despill?: { edge?: boolean; width_px?: number; strength?: number };
};

/** What a screen insert (or a pane of a wall) shows (`spec.InsertSource`). */
type InsertSource = { file_id?: string; take_id?: string; clip_id?: string; asset?: keyof typeof OVERLAY_ASSET_LABELS };

/** A screen insert's key (`spec.InsertKey`): a named flat colour or `#rrggbb`, how near and how soft. */
type InsertKey = { color?: string; tolerance?: number; feather?: number };

function ScreenInsertEditor({
  overlay,
  index,
  isRecording,
  disabled,
  onChange,
  onRemove,
}: {
  overlay: Overlay;
  index: number;
  isRecording: boolean;
  disabled: boolean;
  onChange: (patch: Overlay) => void;
  onRemove: () => void;
}) {
  const { t_i18n } = useFormatter();
  const source = overlay.source ?? {};
  const panes = overlay.sources?.length ?? 0;
  const what = panes
    ? t_i18n("{count} sources (one per pane)", { count: panes })
    : source.take_id
      ? t_i18n("a recording")
      : source.clip_id
        ? t_i18n("an AI shot")
        : source.asset
          ? t_i18n(OVERLAY_ASSET_LABELS[source.asset]).toLowerCase()
          : t_i18n("a file");
  const key = overlay.key;
  const named = key?.color && key.color in INSERT_KEY_LABELS ? (key.color as keyof typeof INSERT_KEY_LABELS) : undefined;
  const setKey = (patch: InsertKey | null) => onChange({ key: patch === null ? undefined : { ...key, ...patch } });
  // A static insert without corners takes the bezel its footage shows, as `quad: "auto"` does.
  const autoQuad = overlay.quad === "auto" || (overlay.quad === undefined && overlay.track !== true);
  return (
    <div className="space-y-2.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2.5" data-testid="video.editor.screen-insert">
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="content-caption">
          {index + 1}
        </Badge>
        <p className="min-w-0 flex-1 content-compact font-medium">{t_i18n("Screen insert")}</p>
        <Tip content={t_i18n("Remove the overlay")}>
          <Button variant="ghost" size="sm" className="h-7 w-7 px-0" onClick={onRemove} disabled={disabled} aria-label={t_i18n("Remove the overlay")}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </Tip>
      </div>
      <p className="text-[11px] text-default-secondary">
        {t_i18n("Maps {what} onto a surface of the footage, frame by frame: the assistant sets its corners.", { what })}
        {autoQuad && ` ${t_i18n("Its corners are the bezel the render finds on the footage's first frame (quad auto).")}`}
      </p>
      <div className="grid grid-cols-2 gap-2">
        <SelectField label={t_i18n("Fit")} value={overlay.fit} unsetLabel={t_i18n(FIT_LABELS.cover)} options={choicesOf(FIT_LABELS, t_i18n).filter((o) => o.value !== "cover")} onChange={(fit) => onChange({ fit })} disabled={disabled} />
        <NumberField
          label={t_i18n("Glow")}
          unit="%"
          min={0}
          max={100}
          step={5}
          value={overlay.glow === undefined ? undefined : Math.round(overlay.glow * 100)}
          fallback={0}
          onChange={(percent) => onChange({ glow: percent === undefined ? undefined : percent / 100 })}
          disabled={disabled}
        />
      </div>
      <NumberField
        label={t_i18n("Room light")}
        hint={t_i18n("How much of the shot's own light and reflections play over the insert, as light, never as a tint; the render fails an insert whose blacks it lifts too far.")}
        unit="%"
        min={0}
        max={100}
        step={5}
        value={overlay.light === undefined ? undefined : Math.round(overlay.light * 100)}
        fallback={15}
        onChange={(percent) => onChange({ light: percent === undefined ? undefined : percent / 100 })}
        disabled={disabled}
      />
      <SwitchField
        label={t_i18n("Follow the camera")}
        hint={
          autoQuad
            ? t_i18n("A quad found on the footage stays where it is found: give its corners to follow the camera.")
            : t_i18n("Tracks the surface through the footage; a render fails at the frame where it is lost.")
        }
        checked={overlay.track === true}
        onChange={(track) => onChange({ track: track ? true : undefined })}
        disabled={disabled || autoQuad}
      />
      <SelectField
        label={t_i18n("Chroma key")}
        value={key ? (named ?? "__custom__") : undefined}
        unsetLabel={t_i18n("None: what crosses the surface stays in front")}
        hint={t_i18n("Draws the insert only where the surface shows this colour, so what passes in front of it stays in front.")}
        options={[
          ...choicesOf(INSERT_KEY_LABELS, t_i18n),
          ...(key && !named ? [{ value: "__custom__", label: key.color ?? "" }] : []),
        ]}
        onChange={(color) => {
          if (!color) setKey(null);
          else if (color !== "__custom__") setKey({ color });
        }}
        disabled={disabled}
      />
      {key && (
        <div className="grid grid-cols-2 gap-2">
          <NumberField
            label={t_i18n("Tolerance")}
            unit="%"
            min={5}
            max={100}
            step={5}
            value={key.tolerance === undefined ? undefined : Math.round(key.tolerance * 100)}
            fallback={30}
            onChange={(percent) => setKey({ tolerance: percent === undefined ? undefined : percent / 100 })}
            disabled={disabled}
          />
          <NumberField
            label={t_i18n("Feather")}
            unit="px"
            min={0}
            max={20}
            step={0.5}
            value={key.feather}
            fallback={1.5}
            onChange={(feather) => setKey({ feather })}
            disabled={disabled}
          />
        </div>
      )}
      <div className="grid grid-cols-2 gap-2">
        <NumberField label={t_i18n("Starts at")} unit="s" min={0} max={600} step={0.1} value={overlay.start} fallback={0} onChange={(start) => onChange({ start })} disabled={disabled} />
        <NumberField label={t_i18n("Lasts")} unit="s" min={0.3} max={60} step={0.1} value={overlay.duration} fallback={3} onChange={(duration) => onChange({ duration })} disabled={disabled} />
      </div>
      {isRecording && (
        <NumberField label={t_i18n("On step")} min={0} max={199} step={1} value={overlay.at_step} onChange={(at_step) => onChange({ at_step })} disabled={disabled} />
      )}
    </div>
  );
}

/** The picker value of an image overlay that draws a file rather than a brand mark. */
const FILE_PICTURE = "__file__";

/** The fields a kind of overlay does not take: switching to it drops them (`spec.Overlay`). */
function switchKind(overlay: Overlay, type: keyof typeof OVERLAY_TYPE_LABELS, fallbackText: string): Overlay {
  if (type === "image") {
    return { type, text: undefined, subtext: undefined, style: undefined, asset: overlay.file_id ? undefined : overlay.asset ?? "logo_mark" };
  }
  return { type, file_id: undefined, asset: undefined, width: undefined, opacity: undefined, text: overlay.text || fallbackText };
}

const num = (value: unknown): number | undefined => (typeof value === "number" ? value : undefined);
const str = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined);
const obj = (value: unknown): Record<string, unknown> => (value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {});

function OverlayEditor({
  overlay,
  index,
  isRecording,
  disabled,
  onChange,
  onRemove,
}: {
  overlay: Overlay;
  index: number;
  isRecording: boolean;
  disabled: boolean;
  onChange: (patch: Overlay) => void;
  onRemove: () => void;
}) {
  const { t_i18n } = useFormatter();
  if (overlay.type === SCREEN_INSERT) {
    return <ScreenInsertEditor overlay={overlay} index={index} isRecording={isRecording} disabled={disabled} onChange={onChange} onRemove={onRemove} />;
  }
  return (
    <div className="space-y-2.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2.5">
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="content-caption">
          {index + 1}
        </Badge>
        <div className="min-w-0 flex-1">
          <SelectField
            label={t_i18n("Kind")}
            value={overlay.type ?? "callout"}
            options={choicesOf(OVERLAY_TYPE_LABELS, t_i18n)}
            onChange={(type) => onChange(switchKind(overlay, type ?? "callout", t_i18n("New callout")))}
            disabled={disabled}
          />
        </div>
        <Tip content={t_i18n("Remove the overlay")}>
          <Button variant="ghost" size="sm" className="mt-5 h-7 w-7 px-0" onClick={onRemove} disabled={disabled} aria-label={t_i18n("Remove the overlay")}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </Tip>
      </div>
      {overlay.type === "image" && (
        <>
          <SelectField
            label={t_i18n("Picture")}
            value={overlay.file_id ? FILE_PICTURE : overlay.asset ?? "logo_mark"}
            options={[
              ...choicesOf(OVERLAY_ASSET_LABELS, t_i18n),
              ...(overlay.file_id ? [{ value: FILE_PICTURE, label: t_i18n("A picture file") }] : []),
            ]}
            onChange={(picture) => {
              if (picture && picture !== FILE_PICTURE) onChange({ asset: picture as keyof typeof OVERLAY_ASSET_LABELS, file_id: undefined });
            }}
            disabled={disabled}
          />
          <div className="grid grid-cols-2 gap-2">
            <NumberField
              label={t_i18n("Width")}
              unit={t_i18n("% of the frame")}
              min={1}
              max={100}
              step={1}
              value={overlay.width !== undefined && overlay.width <= 1 ? Math.round(overlay.width * 100) : undefined}
              fallback={Math.round(IMAGE_OVERLAY_WIDTH * 100)}
              onChange={(percent) => onChange({ width: percent === undefined ? undefined : percent / 100 })}
              disabled={disabled}
            />
            <NumberField
              label={t_i18n("Opacity")}
              unit="%"
              min={5}
              max={100}
              step={5}
              value={overlay.opacity === undefined ? undefined : Math.round(overlay.opacity * 100)}
              fallback={100}
              onChange={(percent) => onChange({ opacity: percent === undefined ? undefined : percent / 100 })}
              disabled={disabled}
            />
          </div>
        </>
      )}
      {overlay.type !== "spotlight" && overlay.type !== "image" && (
        <TextField label={t_i18n("Text")} value={overlay.text} maxLength={200} onChange={(text) => onChange({ text: text ?? "" })} disabled={disabled} />
      )}
      {(overlay.type === "lower_third" || overlay.type === "callout") && (
        <TextField label={t_i18n("Second line")} value={overlay.subtext} maxLength={200} onChange={(subtext) => onChange({ subtext })} disabled={disabled} />
      )}
      <div className="grid grid-cols-2 gap-2">
        <NumberField label={t_i18n("Starts at")} unit="s" min={0} max={600} step={0.1} value={overlay.start} fallback={0} onChange={(start) => onChange({ start })} disabled={disabled} />
        <NumberField label={t_i18n("Lasts")} unit="s" min={0.3} max={60} step={0.1} value={overlay.duration} fallback={3} onChange={(duration) => onChange({ duration })} disabled={disabled} />
      </div>
      {isRecording && (
        <NumberField
          label={t_i18n("On step")}
          hint={t_i18n("A step of the recording (0 = the first): the overlay starts there and points at its element.")}
          min={0}
          max={199}
          step={1}
          value={overlay.at_step}
          onChange={(at_step) => onChange({ at_step })}
          disabled={disabled}
        />
      )}
      <div className="grid grid-cols-2 gap-2">
        <SelectField label={t_i18n("Position")} value={overlay.position} unsetLabel={t_i18n("Automatic")} options={choicesOf(OVERLAY_POSITION_LABELS, t_i18n).filter((o) => o.value !== "auto")} onChange={(position) => onChange({ position })} disabled={disabled} />
        {overlay.type !== "image" && (
          <SelectField label={t_i18n("Style")} value={overlay.style} unsetLabel={t_i18n("Glass")} options={choicesOf(OVERLAY_STYLE_LABELS, t_i18n).filter((o) => o.value !== "glass")} onChange={(style) => onChange({ style })} disabled={disabled} />
        )}
      </div>
    </div>
  );
}

/**
 * Lines of text, one per line (a statement's lines, its secondary lines, its
 * chips). The text is kept as typed - an empty line while typing stays - and
 * the spec gets the non-empty lines.
 */
function LinesField({
  label,
  lines,
  max,
  maxLength,
  hint,
  placeholder,
  disabled,
  onChange,
}: {
  label: string;
  lines: string[];
  max: number;
  maxLength: number;
  hint?: string;
  placeholder?: string;
  disabled: boolean;
  onChange: (lines: string[]) => void;
}) {
  const joined = lines.join("\n");
  const [text, setText] = useState(joined);
  // Lines changed elsewhere (the assistant, undo) replace what is typed here.
  useEffect(() => {
    setText((current) => (statementLines(current).join("\n") === joined ? current : joined));
  }, [joined]);
  return (
    <TextAreaField
      label={label}
      value={text}
      rows={Math.min(Math.max(lines.length, 2), max)}
      maxLength={max * (maxLength + 1)}
      hint={hint}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(value) => {
        setText(value ?? "");
        onChange(statementLines(value ?? "").slice(0, max));
      }}
    />
  );
}

/**
 * Where an AI shot is: being generated (or waiting in the queue for its
 * provider), or failed (with the reason) or expired (the retention deleted its
 * media), each with a way to generate it again in place - under the art
 * direction of the video being edited. A ready shot generated again
 * keeps playing its previous take meanwhile, and after a regeneration that
 * failed.
 */
function ShotStatus({
  clip,
  videoId,
  disabled,
  onRetried,
}: {
  clip: VideoClip | undefined;
  videoId: string | undefined;
  disabled: boolean;
  onRetried?: () => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const aiOff = useAiActionDisabled();
  const retry = useRetryVideoClip();
  const [prompt, setPrompt] = useState(clip?.prompt ?? "");
  useEffect(() => setPrompt(clip?.prompt ?? ""), [clip?.id, clip?.prompt]);
  if (!clip) return null;
  if (clip.status === "pending" || clip.status === "running") {
    const waitingUntil = clip.waiting_for_provider_until;
    const again = clip.regeneration?.state === "running";
    return (
      <p className="flex items-center gap-1.5 text-[11px] text-default-secondary" data-testid="video.editor.shot-generating">
        <Spinner size="sm" tone="inherit" className="shrink-0 text-accent" />
        {waitingUntil
          ? t_i18n("Waiting in the queue until {time}: its provider refused for its quota or rate limit. It starts by itself then.", {
              time: intl.formatTime(new Date(waitingUntil), { hour: "2-digit", minute: "2-digit" }),
            })
          : again
            ? t_i18n("Being generated again by {model} ({n}%): the previous take plays until the new one is ready.", { model: clip.model, n: clip.progress })
            : t_i18n("Being generated by {model} ({n}%)", { model: clip.model, n: clip.progress })}
      </p>
    );
  }
  const expired = !!clip.expired_at;
  const failedAgain = !expired && clip.status === "ready" && clip.regeneration?.state === "failed";
  if (clip.status !== "failed" && clip.status !== "cancelled" && !expired && !failedAgain) return null;
  const retryBlocked = disabled || aiOff;
  const title = expired
    ? t_i18n("This AI shot's media expired")
    : failedAgain
      ? t_i18n("The last regeneration failed: the previous take still plays")
      : t_i18n("This AI shot did not come out");
  const reason = expired
    ? t_i18n("The retention deleted it some time after a video last used it. Generate it again to use it here.")
    : (failedAgain ? clip.regeneration?.reason : clip.error) || t_i18n("No reason was given.");
  return (
    <div
      className={
        failedAgain
          ? "space-y-2 rounded-sm border border-feedback-warning-primary/40 bg-feedback-warning-primary/10 p-2.5"
          : "space-y-2 rounded-sm border border-feedback-alert-primary/40 bg-feedback-alert-primary/10 p-2.5"
      }
      data-testid={expired ? "video.editor.shot-expired" : failedAgain ? "video.editor.shot-regeneration-failed" : "video.editor.shot-failed"}
    >
      <p className="flex items-center gap-1.5 content-compact font-medium text-feedback-warning-primary">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
        {title}
      </p>
      <p className="break-words text-[11px] leading-relaxed text-default-primary">{reason}</p>
      <TextAreaField label={t_i18n("Prompt")} value={prompt} rows={3} maxLength={4000} onChange={(value) => setPrompt(value ?? "")} disabled={retryBlocked} />
      <Button
        size="sm"
        className="h-7 gap-1.5"
        disabled={retryBlocked || !prompt.trim() || retry.isPending}
        onClick={() =>
          retry.mutate(
            { clipId: clip.id, prompt: prompt.trim() !== clip.prompt ? prompt.trim() : undefined, video_id: videoId },
            {
              onSuccess: () => onRetried?.(),
              onError: (err) =>
                toast({ title: t_i18n("The shot was not started"), description: err instanceof Error ? err.message : undefined, variant: "error" }),
            },
          )
        } aria-busy={retry.isPending || undefined}>
        {retry.isPending && <Spinner size="sm" tone="inherit" />}{!retry.isPending && <RotateCcw className="h-3.5 w-3.5" />}
        {t_i18n("Generate again")}
      </Button>
      <p className="text-[11px] text-default-secondary">
        {failedAgain
          ? t_i18n("The previous take keeps playing until a new one is ready.")
          : t_i18n("The scene keeps the shot: it fills in once the new attempt is ready.")}
      </p>
    </div>
  );
}

/**
 * What an AI shot opened on: the frame kept from the recording, shot
 * or video file it started from, or its start image, with where it was taken.
 */
function ShotStartFrame({ clip }: { clip: VideoClip }) {
  const { t_i18n } = useFormatter();
  const url = useVideoMediaUrl("clip", clip.id, "start_frame", !!clip.start_frame_file_id, clip.start_frame_file_id);
  if (!clip.start_frame_file_id) return null;
  const source = obj(clip.params.reference_video) as Partial<VideoClipStartSource>;
  const name = source.name || t_i18n("its video");
  const seconds = typeof source.at_seconds === "number" ? source.at_seconds.toFixed(1) : undefined;
  const where = !source.kind
    ? t_i18n("The image the shot started from.")
    : typeof source.step === "number" && seconds !== undefined
      ? t_i18n("After step {step} ({seconds} s) of {name}.", { step: source.step, seconds, name })
      : seconds !== undefined
        ? t_i18n("At {seconds} s of {name}.", { seconds, name })
        : t_i18n("The last frame of {name}.", { name });
  const hint = source.crop ? `${where} ${t_i18n("Its middle, cut to the shot's shape.")}` : where;
  return (
    <Field label={t_i18n("First frame")} hint={hint}>
      {url ? (
        <img
          src={url}
          alt={t_i18n("The frame the shot opened on")}
          className="max-h-40 w-full rounded-sm border border-elevation-subtle-soft bg-elevation-hover object-contain"
          data-testid="video.editor.shot-start-frame"
        />
      ) : (
        <div className="h-24 w-full animate-pulse rounded-sm bg-elevation-hover" />
      )}
    </Field>
  );
}

/** One of a made shot's own frames, read through a media token. */
function ShotFrame({ clip, asset, label }: { clip: VideoClip; asset: "first_frame" | "mid_frame" | "last_frame"; label: string }) {
  const kept = { first_frame: clip.first_frame_file_id, mid_frame: clip.mid_frame_file_id, last_frame: clip.last_frame_file_id }[asset];
  const url = useVideoMediaUrl("clip", clip.id, asset, !!kept, kept);
  if (!kept) return null;
  return (
    <figure className="min-w-0 space-y-0.5">
      {url ? (
        <img src={url} alt={label} className="aspect-video w-full rounded-sm border border-elevation-subtle-soft bg-elevation-hover object-cover" data-testid={`video.editor.shot-${asset}`} />
      ) : (
        <div className="aspect-video w-full animate-pulse rounded-sm bg-elevation-hover" />
      )}
      <figcaption className="text-center text-[10px] text-default-secondary">{label}</figcaption>
    </figure>
  );
}

/**
 * A made shot's first, middle and last frames and what they measure:
 * how close its first frame stayed to the start frame (SSIM, and the share of
 * its words still read), and how far its last frame moved from its first -
 * after its camera was stabilised, and by which model when it fell back.
 */
function ShotFrames({ clip }: { clip: VideoClip }) {
  const { t_i18n } = useFormatter();
  if (!clip.first_frame_file_id) return null;
  const qc = clip.frame_qc ?? {};
  const measures: string[] = [];
  if (typeof qc.reference_ssim === "number") measures.push(t_i18n("First frame vs start frame: {value}", { value: qc.reference_ssim.toFixed(2) }));
  if (typeof qc.reference_text_match === "number")
    measures.push(t_i18n("Words still read: {percent}%", { percent: Math.round(qc.reference_text_match * 100) }));
  if (typeof qc.motion_ssim === "number") measures.push(t_i18n("Last frame vs first: {value}", { value: qc.motion_ssim.toFixed(2) }));
  const steadied = clip.stabilisation;
  if (steadied?.applied && typeof steadied.raw_motion === "number" && typeof steadied.residual_motion === "number")
    measures.push(t_i18n("Stabilised: motion {before} to {after}", { before: steadied.raw_motion.toFixed(2), after: steadied.residual_motion.toFixed(2) }));
  if (clip.fallback?.made_by && clip.fallback.fell_back) measures.push(t_i18n("Made by the fallback model {model}", { model: clip.fallback.made_by }));
  return (
    <Field label={t_i18n("The shot's frames")} hint={measures.join("; ") || undefined}>
      <div className="grid grid-cols-3 gap-1.5">
        <ShotFrame clip={clip} asset="first_frame" label={t_i18n("First")} />
        <ShotFrame clip={clip} asset="mid_frame" label={t_i18n("Middle")} />
        <ShotFrame clip={clip} asset="last_frame" label={t_i18n("Last")} />
      </div>
    </Field>
  );
}

function ToggleChips<T extends string>({
  label,
  options,
  value,
  onChange,
  disabled,
}: {
  label: string;
  options: { value: T; label: string }[];
  value: T[];
  onChange: (value: T[]) => void;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-1.5">
      <p className="content-compact font-medium">{label}</p>
      <div className="flex flex-wrap gap-1.5" role="group" aria-label={label}>
        {options.map((option) => {
          const on = value.includes(option.value);
          return (
            <button
              type="button"
              key={option.value}
              aria-pressed={on}
              disabled={disabled}
              onClick={() => onChange(on ? value.filter((v) => v !== option.value) : [...value, option.value])}
              className={cn(
                "rounded-sm border px-2 py-1 text-[11px] transition-colors disabled:opacity-50",
                on ? "border-accent/50 bg-accent/15 text-accent" : "border-elevation-subtle text-default-secondary hover:bg-elevation-hover",
              )}
            >
              {option.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function SceneInspector({
  videoId,
  scene,
  planned,
  position,
  fromTemplate,
  onCustomize,
  canEdit,
  locked,
  getLocalTime,
  speechSpeed,
  onChange,
  onMediaChanged,
}: {
  /** The video being edited: a shot generated again here takes its art direction. */
  videoId?: string;
  scene: EditorScene;
  planned: VideoPreviewScene | undefined;
  position: number;
  /** Added by the template: shown, not part of the spec - edited once the video has its own copy. */
  fromTemplate: boolean;
  /** Gives the video its own copy of the template's opening (closing) card, which replaces it. */
  onCustomize?: () => void;
  canEdit: boolean;
  locked: string[] | undefined;
  /** The playhead, in seconds into this scene: where a new overlay starts. */
  getLocalTime: () => number;
  speechSpeed: number;
  onChange: (patch: Patch, coalesce?: string) => void;
  /** A shot the scene shows started again: the preview is planned again. */
  onMediaChanged?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const type = scene.type as SceneType;
  const off = !canEdit || fromTemplate;
  const set = (key: string) => (value: unknown) => onChange({ [key]: value ?? null }, `${scene.id}.${key}`);
  const setIn = (group: string, key: string) => (value: unknown) =>
    onChange({ [group]: { [key]: value ?? null } }, `${scene.id}.${group}.${key}`);
  // Read by id, whatever their age or state: a failed shot shows here, with a way to make it again.
  const { data: take } = useVideoTake(type === "recording" && typeof scene.take_id === "string" ? scene.take_id : undefined);
  const { data: clip } = useVideoClip(type === "clip" && typeof scene.clip_id === "string" ? scene.clip_id : undefined);
  const backdrop = obj(scene.backdrop);
  const { data: backdropClip } = useVideoClip(typeof backdrop.clip_id === "string" ? backdrop.clip_id : undefined);
  const [shotSearch, setShotSearch] = useState("");
  const shotQuery = useDebounce(shotSearch, 250).trim();
  const shots = useVideoAssets(
    { kind: "clip", search: shotQuery || undefined, available: true, limit: BACKDROP_CHOICES, offset: 0 },
    type === "statement",
  );
  const backdropOptions = useMemo(() => {
    const found = (shots.data?.items ?? []).flatMap((asset) => (asset.kind === "clip" ? [asset] : []));
    const current = backdropClip && !found.some((shot) => shot.id === backdropClip.id) ? [backdropClip] : [];
    return [...current, ...found].map((shot) => ({ value: shot.id, label: shot.name || shot.prompt.slice(0, 60) }));
  }, [shots.data, backdropClip]);
  const animation = obj(scene.animate);
  const { data: animationClip } = useVideoClip(typeof animation.clip_id === "string" ? animation.clip_id : undefined);
  const strings = (value: unknown) => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : []);
  const setList = (key: string) => (lines: string[]) => onChange({ [key]: lines.length ? lines : null }, `${scene.id}.${key}`);
  const overlays = useMemo(() => (Array.isArray(scene.overlays) ? (scene.overlays as Overlay[]) : []), [scene.overlays]);
  const setOverlays = (next: Overlay[], coalesce?: string) => onChange({ overlays: next.length ? next : null }, coalesce);
  const narration = str(scene.narration);
  const length = planned ? planned.end - planned.start : undefined;
  const soundReason = sceneSoundReason(planned?.preview_sound);
  const camera = obj(scene.camera);
  const cursor = obj(scene.cursor);
  const frame = obj(scene.frame);
  const idle = obj(scene.idle);
  const highlights = Array.isArray(scene.highlights) ? (scene.highlights as { at_step: number; duration?: number }[]) : [];
  const sourceSeconds = take?.duration_ms ? take.duration_ms / 1000 : clip?.duration_ms ? clip.duration_ms / 1000 : 600;
  const lockedPath = (path: string) => isLockedBy(locked, path);
  // What a motion scene's composition writes: `null` for a logo reveal, which needs no text.
  const motionNeeds = typeof scene.composition === "string" && scene.composition in MOTION_COMPOSITIONS
    ? MOTION_COMPOSITIONS[scene.composition as MotionComposition]
    : undefined;

  return (
    <div data-testid="video.editor.scene-inspector">
      <div className="flex items-center gap-2 border-b border-elevation-subtle-soft px-3 py-2.5">
        <Clapperboard className="h-4 w-4 text-accent" />
        <p className="min-w-0 flex-1 truncate content-base font-semibold">
          {t_i18n("Scene {n}", { n: position })} - {t_i18n(SCENE_TYPE_LABELS[type] ?? type)}
        </p>
        {length !== undefined && <span className="text-[11px] tabular-nums text-default-secondary">{length.toFixed(1)} s</span>}
      </div>
      {fromTemplate && (
        <div className="space-y-2 border-b border-elevation-subtle-soft bg-elevation-hover px-3 py-2" data-testid="video.editor.template-scene">
          <p className="flex items-start gap-2 text-[11px] text-default-secondary">
            <Lock className="mt-0.5 h-3 w-3 shrink-0" />
            {t_i18n("Added by the video's template, the same in every video that wears it. Customize it to change its words, its length or its look in this video only: the template keeps its colours.")}
          </p>
          {onCustomize && (type === "title" || type === "outro") && (
            <Button size="sm" variant="outline" className="h-7 gap-1.5" onClick={onCustomize} disabled={!canEdit} data-testid="video.editor.customize-bookend">
              <PenLine className="h-3.5 w-3.5" />
              {type === "title" ? t_i18n("Customize the opening card") : t_i18n("Customize the closing card")}
            </Button>
          )}
        </div>
      )}

      {(type === "title" || type === "outro") && (
        <Section title={t_i18n("Card")} icon={Type}>
          <TextAreaField
            label={t_i18n("Title (heading)")}
            value={str(scene.title)}
            rows={2}
            maxLength={140}
            onChange={(v) => set("title")(v ?? "")}
            hint={t_i18n("Enter starts a new line; *words* between asterisks are drawn in the accent colour.")}
            disabled={off}
          />
          <TextField label={t_i18n("Subtitle")} value={str(scene.subtitle)} maxLength={240} onChange={set("subtitle")} disabled={off} />
          <TextField label={t_i18n("Kicker")} value={str(scene.kicker)} maxLength={80} onChange={set("kicker")} placeholder={t_i18n("A short line above the title")} disabled={off} />
          {type === "outro" && (
            <div className="grid grid-cols-2 gap-2">
              <TextField label={t_i18n("Call to action")} value={str(scene.cta)} maxLength={60} onChange={set("cta")} disabled={off} />
              <TextField label={t_i18n("Link")} value={str(scene.url)} maxLength={120} onChange={set("url")} placeholder="example.com/demo" disabled={off} />
            </div>
          )}
          <div className="grid grid-cols-2 gap-2">
            <SelectField label={t_i18n("Style")} value={str(scene.style) as keyof typeof CARD_STYLE_LABELS | undefined} unsetLabel={t_i18n("Template's")} options={choicesOf(CARD_STYLE_LABELS, t_i18n)} onChange={set("style")} locked={lockedPath(`scenes.${type}.style`)} disabled={off} />
            {type === "title" && (
              <SelectField label={t_i18n("Alignment")} value={str(scene.align) as "center" | "left" | undefined} unsetLabel={t_i18n("Centered")} options={[{ value: "left" as const, label: t_i18n("Left") }]} onChange={set("align")} disabled={off} />
            )}
          </div>
          <SwitchField label={t_i18n("Show the logo")} checked={scene.show_logo !== false} onChange={(v) => set("show_logo")(v ? null : false)} disabled={off} />
          <NumberField label={t_i18n("Duration")} unit="s" min={1} max={30} step={0.5} value={num(scene.duration)} fallback={DEFAULT_DURATION[type]} onChange={set("duration")} hint={t_i18n("A narration longer than this stretches the scene to it.")} disabled={off} />
        </Section>
      )}

      {type === "statement" && (
        <Section title={t_i18n("Statement")} icon={Quote}>
          <LinesField
            label={t_i18n("Lines")}
            lines={strings(scene.lines)}
            max={4}
            maxLength={90}
            hint={t_i18n("One line each, revealed in turn; *words* between asterisks are drawn in the accent colour. A figure is counted up on screen.")}
            placeholder={t_i18n("Too much *noise*.")}
            disabled={off}
            onChange={(lines) => lines.length && onChange({ lines }, `${scene.id}.lines`)}
          />
          <div className="grid grid-cols-2 gap-2">
            <TextField label={t_i18n("Kicker")} value={str(scene.kicker)} maxLength={60} onChange={set("kicker")} placeholder={t_i18n("A short line above")} disabled={off} />
            <TextField label={t_i18n("Figure")} value={str(scene.figure)} maxLength={16} onChange={set("figure")} placeholder="6,000+" disabled={off} />
          </div>
          <LinesField
            label={t_i18n("Smaller lines")}
            lines={strings(scene.secondary)}
            max={3}
            maxLength={160}
            hint={t_i18n("A proof point or a translation under the statement.")}
            disabled={off}
            onChange={setList("secondary")}
          />
          <LinesField label={t_i18n("Chips")} lines={strings(scene.chips)} max={6} maxLength={32} hint={t_i18n("One label per line, drawn as pills.")} placeholder={t_i18n("Open source")} disabled={off} onChange={setList("chips")} />
          <div className="grid grid-cols-2 gap-2">
            <SelectField label={t_i18n("Reveal")} value={str(scene.reveal) as keyof typeof REVEAL_LABELS | undefined} unsetLabel={t_i18n("Template's")} options={choicesOf(REVEAL_LABELS, t_i18n)} onChange={set("reveal")} disabled={off} />
            <SelectField label={t_i18n("Alignment")} value={str(scene.align) as keyof typeof ALIGN_LABELS | undefined} unsetLabel={t_i18n("Template's")} options={choicesOf(ALIGN_LABELS, t_i18n)} onChange={set("align")} disabled={off} />
          </div>
          <SwitchField
            label={t_i18n("Show the logo")}
            checked={scene.show_logo === true}
            onChange={(v) => set("show_logo")(v ? true : null)}
            hint={t_i18n("Not drawn while the video has a watermark: the watermark already shows the brand.")}
            disabled={off}
          />
          <NumberField label={t_i18n("Duration")} unit="s" min={1} max={30} step={0.5} value={num(scene.duration)} fallback={DEFAULT_DURATION.statement} onChange={set("duration")} hint={t_i18n("A narration longer than this stretches the scene to it.")} disabled={off} />
          <Field label={t_i18n("Behind the text")} hint={t_i18n("An AI shot plays behind the statement, darkened so the words read.")}>
            <SearchableSelect
              aria-label={t_i18n("Behind the text")}
              sort={false}
              options={backdropOptions}
              pinned={[
                { value: BRAND_BACKDROP, label: t_i18n("The brand backdrop") },
                ...(typeof backdrop.file_id === "string" ? [{ value: FILE_BACKDROP, label: t_i18n("An uploaded file") }] : []),
              ]}
              value={typeof backdrop.clip_id === "string" ? backdrop.clip_id : typeof backdrop.file_id === "string" ? FILE_BACKDROP : BRAND_BACKDROP}
              onValueChange={(value) => {
                if (value === FILE_BACKDROP) return;
                onChange({ backdrop: value && value !== BRAND_BACKDROP ? { clip_id: value } : null });
              }}
              onSearchChange={setShotSearch}
              disabled={off}
            />
          </Field>
          <ShotStatus clip={backdropClip} videoId={videoId} disabled={off} onRetried={onMediaChanged} />
          {backdropClip && <ShotFrames clip={backdropClip} />}
          {Object.keys(backdrop).length > 0 && (
            <NumberField label={t_i18n("Darken it")} min={0} max={0.9} step={0.05} value={num(scene.dim)} fallback={0.55} onChange={set("dim")} disabled={off} />
          )}
        </Section>
      )}

      {type === "recording" && (
        <Section title={t_i18n("Recording")} icon={Clapperboard}>
          <p className="content-compact">
            <span className="font-medium">{take?.name ?? t_i18n("Recording")}</span>
            {take?.duration_ms ? <span className="text-default-secondary"> - {formatDuration(take.duration_ms)}</span> : null}
            {take?.expired_at && <Badge variant="destructive" className="ml-2 content-caption">{t_i18n("Media expired")}</Badge>}
          </p>
          <div className="grid grid-cols-2 gap-2">
            <NumberField label={t_i18n("Starts at")} unit="s" min={0} max={sourceSeconds} step={0.1} value={num(scene.trim_start)} fallback={0} onChange={set("trim_start")} disabled={off} />
            <NumberField label={t_i18n("Ends at")} unit="s" min={0} max={sourceSeconds} step={0.1} value={num(scene.trim_end)} fallback={Math.round(sourceSeconds * 10) / 10} onChange={set("trim_end")} disabled={off} />
          </div>
          <NumberField label={t_i18n("Speed")} unit="x" min={0.25} max={4} step={0.05} value={num(scene.speed)} fallback={1} onChange={set("speed")} disabled={off} />
          <NumberField label={t_i18n("Hold the last frame")} unit="s" min={0} max={10} step={0.1} value={num(scene.hold_end)} fallback={0} onChange={set("hold_end")} disabled={off} />
          <SelectField
            label={t_i18n("When the narration outlasts it")}
            value={str(scene.extend_mode) as keyof typeof EXTEND_MODE_LABELS | undefined}
            unsetLabel={t_i18n(EXTEND_MODE_LABELS.zoom)}
            options={choicesOf(EXTEND_MODE_LABELS, t_i18n).filter((o) => o.value === "hold")}
            onChange={set("extend_mode")}
            hint={t_i18n("A held frame is flagged as frozen past half a second.")}
            disabled={off}
          />
        </Section>
      )}

      {type === "clip" && (
        <Section title={scene.file_id ? t_i18n("Video file") : t_i18n("AI shot")} icon={Sparkles}>
          <p className="line-clamp-3 content-compact text-default-secondary">{clip?.name || clip?.prompt || t_i18n("An uploaded video file")}</p>
          <ShotStatus clip={clip} videoId={videoId} disabled={off} onRetried={onMediaChanged} />
          {clip && <ShotStartFrame clip={clip} />}
          {clip && <ShotFrames clip={clip} />}
          <div className="grid grid-cols-2 gap-2">
            <NumberField label={t_i18n("Starts at")} unit="s" min={0} max={sourceSeconds} step={0.1} value={num(scene.trim_start)} fallback={0} onChange={set("trim_start")} disabled={off} />
            <NumberField label={t_i18n("Ends at")} unit="s" min={0} max={sourceSeconds} step={0.1} value={num(scene.trim_end)} fallback={Math.round(sourceSeconds * 10) / 10} onChange={set("trim_end")} disabled={off} />
          </div>
          <NumberField label={t_i18n("Speed")} unit="x" min={0.25} max={4} step={0.05} value={num(scene.speed)} fallback={1} onChange={set("speed")} disabled={off} />
          <SelectField label={t_i18n("Framing")} value={str(scene.fit) as keyof typeof FIT_LABELS | undefined} unsetLabel={t_i18n("Fill the frame")} options={choicesOf(FIT_LABELS, t_i18n).filter((o) => o.value !== "cover")} onChange={set("fit")} disabled={off} />
          <SelectField
            label={t_i18n("Its own sound")}
            value={scene.audio === true ? "keep" : scene.audio === false ? "off" : undefined}
            unsetLabel={t_i18n("Automatic")}
            options={choicesOf(SCENE_SOUND_LABELS, t_i18n)}
            onChange={(v) => set("audio")(v === "keep" ? true : v === "off" ? false : null)}
            hint={soundReason ? t_i18n(SCENE_SOUND_REASONS[soundReason]) : undefined}
            disabled={off}
          />
          {(scene.audio === true || (scene.audio !== false && planned?.preview_sound?.plays !== false)) && (
            <NumberField label={t_i18n("Volume")} min={0} max={2} step={0.05} value={num(scene.volume)} fallback={0.8} onChange={set("volume")} hint={t_i18n("Against the voice: 1 is as loud.")} disabled={off} />
          )}
          <NumberField label={t_i18n("Hold the last frame")} unit="s" min={0} max={10} step={0.1} value={num(scene.hold_end)} fallback={0} onChange={set("hold_end")} disabled={off} />
          <SelectField
            label={t_i18n("When the narration outlasts it")}
            value={str(scene.extend_mode) as keyof typeof EXTEND_MODE_LABELS | undefined}
            unsetLabel={t_i18n(EXTEND_MODE_LABELS.zoom)}
            options={choicesOf(EXTEND_MODE_LABELS, t_i18n).filter((o) => o.value !== "zoom")}
            onChange={set("extend_mode")}
            hint={t_i18n("A held frame is flagged as frozen past half a second.")}
            disabled={off}
          />
          {scene.clip_id ? (
            <SwitchField
              label={t_i18n("Its camera move is intended")}
              checked={scene.motion === "intended"}
              onChange={(v) => set("motion")(v ? "intended" : null)}
              hint={t_i18n("Never flagged as a camera that moved on a locked-off shot.")}
              disabled={off}
            />
          ) : null}
        </Section>
      )}

      {(type === "image" || type === "slide") && (
        <Section title={type === "image" ? t_i18n("Image") : t_i18n("Slide")} icon={Frame}>
          {type === "slide" && <NumberField label={t_i18n("Slide number")} min={1} max={500} step={1} value={num(scene.slide)} fallback={1} onChange={set("slide")} disabled={off} />}
          {type === "slide" && (
            <SwitchField
              label={t_i18n("Show the deck's footer, logo and slide number")}
              checked={scene.chrome !== false}
              onChange={(v) => set("chrome")(v ? null : false)}
              disabled={off}
            />
          )}
          <NumberField label={t_i18n("Duration")} unit="s" min={1} max={60} step={0.5} value={num(scene.duration)} fallback={DEFAULT_DURATION[type]} onChange={set("duration")} disabled={off} />
          <SelectField label={t_i18n("Motion")} value={str(scene.motion) as keyof typeof MOTION_LABELS | undefined} unsetLabel={t_i18n("Ken Burns")} options={choicesOf(MOTION_LABELS, t_i18n).filter((o) => o.value !== "ken_burns")} onChange={set("motion")} disabled={off} />
          {type === "image" && <SelectField label={t_i18n("Framing")} value={str(scene.fit) as keyof typeof FIT_LABELS | undefined} unsetLabel={t_i18n("Fit inside the frame")} options={choicesOf(FIT_LABELS, t_i18n).filter((o) => o.value !== "contain")} onChange={set("fit")} disabled={off} />}
          {type === "image" && (
            <SwitchField
              label={t_i18n("Animate with the video model")}
              checked={!!scene.animate}
              onChange={(v) => set("animate")(v ? {} : null)}
              hint={t_i18n("The video model makes a shot from this image; the scene plays it once made, the still until then.")}
              disabled={off}
            />
          )}
          {type === "image" && !!scene.animate && (
            <>
              <TextAreaField
                label={t_i18n("How it moves")}
                value={str(animation.prompt)}
                maxLength={600}
                rows={2}
                onChange={setIn("animate", "prompt")}
                placeholder={t_i18n("A slow push in, the charts light up one after the other")}
                disabled={off}
              />
              <ShotStatus clip={animationClip} videoId={videoId} disabled={off} onRetried={onMediaChanged} />
              <SelectField
                label={t_i18n("When the narration outlasts it")}
                value={str(scene.extend_mode) as keyof typeof EXTEND_MODE_LABELS | undefined}
                unsetLabel={t_i18n(EXTEND_MODE_LABELS.zoom)}
                options={choicesOf(EXTEND_MODE_LABELS, t_i18n).filter((o) => o.value !== "zoom")}
                onChange={set("extend_mode")}
                hint={t_i18n("A held frame is flagged as frozen past half a second.")}
                disabled={off}
              />
            </>
          )}
        </Section>
      )}

      {type === "figure" && (
        <Section title={t_i18n("Animated figure")} icon={Workflow}>
          {!!scene.presentation_id && (
            <>
              <NumberField label={t_i18n("Slide number")} min={1} max={500} step={1} value={num(scene.slide)} fallback={1} onChange={set("slide")} disabled={off || !!scene.slide_id} />
              <NumberField
                label={t_i18n("Block on the slide")}
                min={1}
                max={61}
                step={1}
                value={typeof scene.block === "number" ? scene.block + 1 : undefined}
                fallback={1}
                onChange={(v) => set("block")(v == null ? null : Math.max(0, Math.round(v) - 1))}
                hint={t_i18n("Counted from the top of the slide; a roadmap slide is its phases.")}
                disabled={off}
              />
            </>
          )}
          <NumberField label={t_i18n("Duration")} unit="s" min={2} max={60} step={0.5} value={num(scene.duration)} fallback={DEFAULT_DURATION.figure} onChange={set("duration")} hint={t_i18n("A narration longer than this stretches the scene to it.")} disabled={off} />
          <SelectField label={t_i18n("Build")} value={str(scene.build) as keyof typeof FIGURE_BUILD_LABELS | undefined} unsetLabel={t_i18n(FIGURE_BUILD_LABELS.sequence)} options={choicesOf(FIGURE_BUILD_LABELS, t_i18n).filter((o) => o.value !== "sequence")} onChange={set("build")} disabled={off} />
          {scene.build !== "together" && (
            <>
              <SelectField label={t_i18n("Steps come in")} value={str(scene.reveal) as keyof typeof FIGURE_REVEAL_LABELS | undefined} unsetLabel={t_i18n(FIGURE_REVEAL_LABELS.even)} options={choicesOf(FIGURE_REVEAL_LABELS, t_i18n).filter((o) => o.value !== "even")} onChange={set("reveal")} disabled={off} />
              <NumberField
                label={t_i18n("Time on each step")}
                unit="s"
                min={0}
                max={5}
                step={0.1}
                value={num(scene.dwell)}
                fallback={DEFAULT_FIGURE_DWELL}
                onChange={set("dwell")}
                hint={t_i18n("A step stays on screen this long before the next one; steps the scene has no room for come in a few at a time.")}
                disabled={off}
              />
            </>
          )}
          <SelectField label={t_i18n("Camera")} value={str(scene.camera) as keyof typeof FIGURE_CAMERA_LABELS | undefined} unsetLabel={t_i18n(FIGURE_CAMERA_LABELS.follow)} options={choicesOf(FIGURE_CAMERA_LABELS, t_i18n).filter((o) => o.value !== "follow")} onChange={set("camera")} disabled={off} />
          <SelectField
            label={t_i18n("Once built")}
            value={str(scene.motion) as keyof typeof FIGURE_MOTION_LABELS | undefined}
            unsetLabel={t_i18n(FIGURE_MOTION_LABELS.drift)}
            options={choicesOf(FIGURE_MOTION_LABELS, t_i18n).filter((o) => o.value !== "drift")}
            onChange={set("motion")}
            hint={t_i18n("Hover the scene in the timeline to watch its build on the stage.")}
            disabled={off}
          />
          <SelectField
            label={t_i18n("In space")}
            value={str(scene.perspective) as keyof typeof FIGURE_PERSPECTIVE_LABELS | undefined}
            unsetLabel={t_i18n(FIGURE_PERSPECTIVE_LABELS.flat)}
            options={choicesOf(FIGURE_PERSPECTIVE_LABELS, t_i18n).filter((o) => o.value !== "flat")}
            onChange={set("perspective")}
            disabled={off}
          />
        </Section>
      )}

      {type === "motion" && (
        <Section title={t_i18n("Motion graphics")} icon={Sparkles}>
          <SelectField
            label={t_i18n("Composition")}
            value={str(scene.composition) as MotionComposition | undefined}
            unsetLabel={t_i18n("Its own elements only")}
            options={choicesOf(MOTION_COMPOSITION_LABELS, t_i18n)}
            onChange={(value) =>
              onChange(value ? motionCompositionPatch(scene, value, t_i18n("Your *message* here")) : { composition: null }, `${scene.id}.composition`)
            }
            hint={t_i18n("Laid out and moved in the video's motion personality.")}
            disabled={off || (!scene.composition && !Array.isArray(scene.elements))}
          />
          {(motionNeeds === "title" || motionNeeds === null) && (
            <TextAreaField
              label={scene.composition === "quote" ? t_i18n("Quote") : scene.composition === "logo_reveal" ? t_i18n("Tagline") : t_i18n("Title (heading)")}
              value={str(scene.title)}
              rows={2}
              maxLength={140}
              onChange={set("title")}
              hint={t_i18n("Enter starts a new line; *words* between asterisks are drawn in the accent colour.")}
              disabled={off}
            />
          )}
          {motionNeeds === "figure" && (
            <div className="grid grid-cols-2 gap-2">
              <TextField label={t_i18n("Figure")} value={str(scene.figure)} maxLength={16} onChange={set("figure")} placeholder="6,000+" disabled={off} />
              <TextField label={t_i18n("What it counts")} value={str(scene.label)} maxLength={120} onChange={set("label")} disabled={off} />
            </div>
          )}
          {motionNeeds === "items" && (
            <LinesField label={t_i18n("Items")} lines={strings(scene.items)} max={6} maxLength={80} hint={t_i18n("One item per line, each coming in after the one before.")} disabled={off} onChange={(lines) => lines.length && onChange({ items: lines }, `${scene.id}.items`)} />
          )}
          {motionNeeds === "lines" && (
            <LinesField label={t_i18n("Lines")} lines={strings(scene.lines)} max={4} maxLength={90} hint={t_i18n("Two to four lines, each revealed through its own window.")} disabled={off} onChange={(lines) => lines.length >= 2 && onChange({ lines }, `${scene.id}.lines`)} />
          )}
          {scene.composition !== "logo_reveal" && scene.composition !== "stat_counter" && (
            <TextField label={scene.composition === "quote" ? t_i18n("Who said it") : t_i18n("Subtitle")} value={str(scene.subtitle)} maxLength={240} onChange={set("subtitle")} disabled={off} />
          )}
          <TextField label={t_i18n("Kicker")} value={str(scene.kicker)} maxLength={80} onChange={set("kicker")} placeholder={t_i18n("A short line above the title")} disabled={off} />
          <div className="grid grid-cols-2 gap-2">
            <SelectField label={t_i18n("Alignment")} value={str(scene.align) as keyof typeof ALIGN_LABELS | undefined} unsetLabel={t_i18n("Centered")} options={choicesOf(ALIGN_LABELS, t_i18n).filter((o) => o.value !== "center")} onChange={set("align")} disabled={off} />
            <SwitchField label={t_i18n("Show the logo")} checked={scene.show_logo === true} onChange={(v) => set("show_logo")(v ? true : null)} disabled={off} />
          </div>
          <NumberField label={t_i18n("Duration")} unit="s" min={1} max={30} step={0.5} value={num(scene.duration)} fallback={DEFAULT_DURATION.motion} onChange={set("duration")} hint={t_i18n("A narration longer than this stretches the scene to it.")} disabled={off} />
          {Array.isArray(scene.elements) && scene.elements.length > 0 && (
            <p className="text-[11px] text-default-secondary" data-testid="video.editor.motion-elements">
              {t_i18n("{count} elements of its own, drawn over the composition: ask the assistant to change them.", { count: scene.elements.length })}
            </p>
          )}
        </Section>
      )}

      <Section title={t_i18n("Narration")} icon={Mic}>
        <TextAreaField
          label={t_i18n("Spoken over the scene")}
          value={narration}
          rows={4}
          maxLength={4000}
          placeholder={t_i18n("What the voice-over says while this scene plays...")}
          onChange={set("narration")}
          hint={narration ? t_i18n("About {seconds} s at the voice's pace.", { seconds: estimateNarrationSeconds(narration, speechSpeed).toFixed(1) }) : undefined}
          disabled={off}
        />
        {narration && <NumberField label={t_i18n("Starts after")} unit="s" min={0} max={10} step={0.05} value={num(scene.narration_delay)} fallback={0.35} onChange={set("narration_delay")} disabled={off} />}
      </Section>

      {position > 1 && (
        <Section title={t_i18n("Transition in")} icon={Timer} defaultOpen={false}>
          <SelectField
            label={t_i18n("Transition")}
            value={str(obj(scene.transition).type) as keyof typeof TRANSITION_LABELS | undefined}
            unsetLabel={t_i18n("The video's default")}
            options={choicesOf(TRANSITION_LABELS, t_i18n)}
            onChange={(v) => onChange({ transition: v ? { ...obj(scene.transition), type: v } : null })}
            locked={lockedPath("default_transition")}
            hint={
              planned?.transition_in
                ? planned.transition_in.type === "cut"
                  ? t_i18n("Comes in on a cut.")
                  : t_i18n("Comes in with: {type}, {seconds} s.", {
                      type: t_i18n(TRANSITION_LABELS[planned.transition_in.type as keyof typeof TRANSITION_LABELS] ?? planned.transition_in.type),
                      seconds: planned.transition_in.duration.toFixed(2),
                    })
                : undefined
            }
            disabled={off}
          />
          {(() => {
            const chosen = str(obj(scene.transition).type);
            if (!chosen || chosen === "cut" || chosen === "auto") return null;
            return (
              <NumberField label={t_i18n("Duration")} unit="s" min={0.1} max={2} step={0.05} value={num(obj(scene.transition).duration)} fallback={TRANSITION_SECONDS[chosen as keyof typeof TRANSITION_SECONDS] ?? 0.6} onChange={setIn("transition", "duration")} disabled={off} />
            );
          })()}
        </Section>
      )}

      {type === "recording" && (
        <>
          <Section title={t_i18n("Idle moments")} icon={Timer} defaultOpen={false}>
            <SelectField label={t_i18n("When nothing happens")} value={str(idle.mode) as keyof typeof IDLE_MODE_LABELS | undefined} unsetLabel={t_i18n("Speed them up")} options={choicesOf(IDLE_MODE_LABELS, t_i18n).filter((o) => o.value !== "compress")} onChange={setIn("idle", "mode")} disabled={off} />
            {idle.mode !== "off" && (
              <>
                <NumberField label={t_i18n("Idle after")} unit="s" min={0.3} max={10} step={0.1} value={num(idle.threshold)} fallback={1.2} onChange={setIn("idle", "threshold")} disabled={off} />
                {idle.mode !== "cut" && <NumberField label={t_i18n("Fast-forward speed")} unit="x" min={1.5} max={20} step={0.5} value={num(idle.speed)} fallback={6} onChange={setIn("idle", "speed")} disabled={off} />}
              </>
            )}
          </Section>
          <Section title={t_i18n("Camera")} icon={Camera} defaultOpen={false}>
            <SelectField label={t_i18n("Camera")} value={str(camera.mode) as keyof typeof CAMERA_MODE_LABELS | undefined} unsetLabel={t_i18n("Follow the action")} options={choicesOf(CAMERA_MODE_LABELS, t_i18n).filter((o) => o.value !== "auto")} onChange={setIn("camera", "mode")} locked={lockedPath("scenes.recording.camera")} disabled={off} />
            {camera.mode !== "static" && (
              <NumberField label={t_i18n("Deepest zoom")} unit="x" min={1} max={3} step={0.05} value={num(camera.max_zoom)} fallback={1.8} onChange={setIn("camera", "max_zoom")} locked={lockedPath("scenes.recording.camera")} disabled={off} />
            )}
            {(camera.mode ?? "auto") === "auto" && (
              <>
                <SelectField label={t_i18n("Camera pace")} value={str(camera.pace) as keyof typeof CAMERA_PACE_LABELS | undefined} unsetLabel={t_i18n(CAMERA_PACE_LABELS.calm)} options={choicesOf(CAMERA_PACE_LABELS, t_i18n).filter((o) => o.value !== "calm")} onChange={setIn("camera", "pace")} locked={lockedPath("scenes.recording.camera")} disabled={off} />
                <NumberField label={t_i18n("Zoom length")} unit="s" min={0.5} max={3} step={0.1} value={num(camera.move_seconds)} fallback={CAMERA_ZOOM_SECONDS[camera.pace === "dynamic" ? "dynamic" : "calm"]} onChange={setIn("camera", "move_seconds")} locked={lockedPath("scenes.recording.camera")} disabled={off} />
                <ToggleChips
                  label={t_i18n("Zoom on")}
                  options={choicesOf(ZOOM_ON_LABELS, t_i18n)}
                  value={(Array.isArray(camera.zoom_on) ? camera.zoom_on : ["click", "type", "select", "highlight"]) as (keyof typeof ZOOM_ON_LABELS)[]}
                  onChange={(zoomOn) => onChange({ camera: { zoom_on: zoomOn } })}
                  disabled={off || lockedPath("scenes.recording.camera")}
                />
              </>
            )}
          </Section>
          <Section title={t_i18n("Cursor")} icon={MousePointer2} defaultOpen={false}>
            <SwitchField label={t_i18n("Show the cursor")} checked={cursor.visible !== false} onChange={(v) => setIn("cursor", "visible")(v ? null : false)} locked={lockedPath("scenes.recording.cursor")} disabled={off} />
            {cursor.visible !== false && (
              <>
                <NumberField label={t_i18n("Size")} unit="x" min={0.6} max={3} step={0.05} value={num(cursor.size)} fallback={1.35} onChange={setIn("cursor", "size")} locked={lockedPath("scenes.recording.cursor")} disabled={off} />
                <div className="grid grid-cols-2 gap-2">
                  <SelectField label={t_i18n("Style")} value={str(cursor.style) as keyof typeof CURSOR_STYLE_LABELS | undefined} unsetLabel={t_i18n("Arrow")} options={choicesOf(CURSOR_STYLE_LABELS, t_i18n).filter((o) => o.value !== "arrow")} onChange={setIn("cursor", "style")} locked={lockedPath("scenes.recording.cursor.style")} disabled={off} />
                  <SelectField label={t_i18n("Click effect")} value={str(cursor.click_effect) as keyof typeof CLICK_EFFECT_LABELS | undefined} unsetLabel={t_i18n("Ripple")} options={choicesOf(CLICK_EFFECT_LABELS, t_i18n).filter((o) => o.value !== "ripple")} onChange={setIn("cursor", "click_effect")} locked={lockedPath("scenes.recording.cursor")} disabled={off} />
                </div>
              </>
            )}
          </Section>
          <Section title={t_i18n("Browser frame")} icon={Frame} defaultOpen={false}>
            <SelectField label={t_i18n("Frame")} value={str(frame.style) as keyof typeof FRAME_STYLE_LABELS | undefined} unsetLabel={t_i18n("Browser window")} options={choicesOf(FRAME_STYLE_LABELS, t_i18n).filter((o) => o.value !== "window")} onChange={setIn("frame", "style")} hint={t_i18n("The recording in a browser window on the background, or edge to edge.")} locked={lockedPath("scenes.recording.frame")} disabled={off} />
            {frame.style !== "none" && (
              <>
                <SwitchField label={t_i18n("Show the address bar")} checked={frame.url_bar !== false} onChange={(v) => setIn("frame", "url_bar")(v ? null : false)} hint={t_i18n("The bar above the page, with its address.")} locked={lockedPath("scenes.recording.frame")} disabled={off} />
                <SwitchField label={t_i18n("Drop shadow")} checked={frame.shadow !== false} onChange={(v) => setIn("frame", "shadow")(v ? null : false)} hint={t_i18n("A soft shadow that lifts the window off the background.")} locked={lockedPath("scenes.recording.frame")} disabled={off} />
                <NumberField label={t_i18n("Margin")} min={0} max={0.2} step={0.005} value={num(frame.padding)} fallback={0.02} onChange={setIn("frame", "padding")} hint={t_i18n("Space around the window, as a share of the canvas width: 0.05 is 5%.")} locked={lockedPath("scenes.recording.frame")} disabled={off} />
                <NumberField label={t_i18n("Corner radius")} unit="px" min={0} max={48} step={1} value={num(frame.radius)} fallback={18} onChange={setIn("frame", "radius")} hint={t_i18n("How round the window's corners are.")} locked={lockedPath("scenes.recording.frame")} disabled={off} />
              </>
            )}
          </Section>
          <Section
            title={t_i18n("Highlights")}
            icon={Highlighter}
            defaultOpen={false}
            actions={
              !off && (
                <Tip content={t_i18n("Highlight a step")}>
                  <Button variant="ghost" size="sm" className="h-6 w-6 px-0" aria-label={t_i18n("Highlight a step")} onClick={() => onChange({ highlights: [...highlights, { at_step: 0, duration: 2 }] })}>
                    <Plus className="h-3.5 w-3.5" />
                  </Button>
                </Tip>
              )
            }
          >
            {highlights.length === 0 && <p className="text-[11px] text-default-secondary">{t_i18n("Ring the element a step acts on, for a moment.")}</p>}
            {highlights.map((highlight, index) => (
              <div key={index} className="flex items-end gap-2">
                <div className="grid flex-1 grid-cols-2 gap-2">
                  <NumberField label={t_i18n("Step")} min={0} max={199} step={1} value={highlight.at_step} onChange={(at) => onChange({ highlights: highlights.map((h, i) => (i === index ? { ...h, at_step: at ?? 0 } : h)) }, `${scene.id}.highlights.${index}`)} disabled={off} />
                  <NumberField label={t_i18n("Lasts")} unit="s" min={0.3} max={20} step={0.1} value={highlight.duration} fallback={2} onChange={(d) => onChange({ highlights: highlights.map((h, i) => (i === index ? { ...h, duration: d ?? 2 } : h)) }, `${scene.id}.highlights.${index}`)} disabled={off} />
                </div>
                <Button variant="ghost" size="sm" className="h-7 w-7 px-0" aria-label={t_i18n("Remove")} disabled={off} onClick={() => onChange({ highlights: highlights.length > 1 ? highlights.filter((_, i) => i !== index) : null })}>
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}
          </Section>
        </>
      )}

      <Section
        title={t_i18n("Overlays")}
        icon={Captions}
        defaultOpen={overlays.length > 0}
        actions={
          !off && (
            <Tip content={t_i18n("Add an overlay at the playhead")}>
              <Button
                variant="ghost"
                size="sm"
                className="h-6 w-6 px-0"
                aria-label={t_i18n("Add an overlay at the playhead")}
                onClick={() => setOverlays([...overlays, { type: "callout", text: t_i18n("New callout"), start: Math.max(0, Math.round(getLocalTime() * 10) / 10), duration: 3 }])}
              >
                <Plus className="h-3.5 w-3.5" />
              </Button>
            </Tip>
          )
        }
      >
        {overlays.length === 0 && (
          <p className="text-[11px] text-default-secondary">{t_i18n("Lower thirds, callouts, badges, captions and pictures (a logo) drawn over the scene, at their time.")}</p>
        )}
        {overlays.map((overlay, index) => (
          <OverlayEditor
            key={index}
            overlay={overlay}
            index={index}
            isRecording={type === "recording"}
            disabled={off}
            onChange={(patch) => {
              const next = overlays.map((o, i) => {
                if (i !== index) return o;
                const merged: Overlay = { ...o, ...patch };
                for (const key of Object.keys(patch) as (keyof Overlay)[]) if (patch[key] === undefined) delete merged[key];
                return merged;
              });
              setOverlays(next, `${scene.id}.overlays.${index}`);
            }}
            onRemove={() => setOverlays(overlays.filter((_, i) => i !== index))}
          />
        ))}
      </Section>
    </div>
  );
}
