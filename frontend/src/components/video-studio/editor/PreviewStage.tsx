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
 * The editor's stage and its transport: the video as it will render,
 * without rendering it.
 *
 * The frame is the render's own compositor page (`GET
 * /video-studio/preview-page`, the page the worker drives in headless
 * Chromium), handed the preview document (`window.__xtm.load`) and asked for
 * the frame under the playhead (`render`). In preview mode it plays the
 * recordings and clips from `<video>` elements on confined media URLs: paused,
 * every frame is exact; playing, the videos run at the time map's rate. The
 * music bed plays here, beside it: the bed the render prepares, at its
 * level, lowered under the narration by the render's own envelope. Each spoken
 * narration plays from its start, from the render's own voice cache; one
 * not spoken yet shows its captions, paced, until the preview has spoken it.
 *
 * The editor plans the preview again every few seconds while a shot is being
 * made: a plan that changes nothing is not loaded, the compositor
 * keeps what it shows across the ones that do, the playing loop runs through
 * them, and the music and the narrations keep the URL they play until it
 * stops answering - a newer token of the same sound reloads nothing. A source
 * that stops answering asks the editor for a new plan (`onSourceLost`). An edit's plan says the stage is
 * being updated only once it lasts: one that answers at once flashes nothing.
 * Paused, the stage draws the time the pointer skims in the timeline over a
 * figure's scene (`skim`): the figure's build plays under the pointer.
 * A scene whose keyed or tracked screen insert is shown without its build
 * (`preview_inserts_approximate`) is drawn unlike the render - the source mapped
 * on the quad, no key, no matte, no tracking - and the stage says so while the
 * playhead is on it, instead of passing that picture as the render's.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronFirst,
  ChevronLast,
  Maximize,
  Pause,
  Play,
  Repeat,
  SkipBack,
  SkipForward,
  StepBack,
  StepForward,
  Volume2,
  VolumeX,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { withBasePath } from "@/lib/base-path";
import { cn } from "@/lib/utils";
import type { VideoPreview, VideoPreviewDocument, VideoPreviewState } from "@/types";

import { duckGainAt, formatTimecode, sceneJump, spokenOffsets } from "./video-editor-model";
import { usePlayheadTime, type Playhead, type Skim } from "./playhead";
import { Chip, Icon, Spinner } from "@filigran/design-system";

const PREVIEW_PAGE = withBasePath("/api/v1/video-studio/preview-page");
const PLAYBACK_RATES = ["0.5", "1", "1.5", "2"] as const;

/** What the compositor page installs (`compositor_page.PAGE_JS`). */
interface Compositor {
  load(doc: VideoPreviewDocument): Promise<boolean>;
  render(index: number): Promise<boolean>;
  setPlaying(playing: boolean): void;
  setMuted(muted: boolean): void;
  onSourceLost?(listener: (() => void) | null): void;
}

interface HeldUrl {
  url: string;
  lost: boolean;
}

export interface TransportState {
  playing: boolean;
  rate: number;
  loop: boolean;
  muted: boolean;
}

export function PreviewStage({
  preview,
  playhead,
  skim,
  transport,
  onTransport,
  stateLabel,
  previewing,
  stageRef,
  onSourceLost,
}: {
  preview: VideoPreview | null;
  playhead: Playhead;
  /** While paused, a time skimmed in the timeline is drawn instead of the playhead's (a figure's build). */
  skim?: Skim;
  transport: TransportState;
  onTransport: (patch: Partial<TransportState>) => void;
  /** The words of a scene shown without its media (a slide, a recording or a shot being made). */
  stateLabel: (state: VideoPreviewState) => string;
  previewing: boolean;
  /** The element fullscreen shows. */
  stageRef: React.RefObject<HTMLDivElement | null>;
  /** A media URL stopped answering (an expired token, a replaced file): a new plan brings a fresh one. */
  onSourceLost?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const frameRef = useRef<HTMLIFrameElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const voiceRefs = useRef(new Map<string, HTMLAudioElement>());
  const lostRef = useRef(onSourceLost);
  lostRef.current = onSourceLost;

  // Each sound keeps the URL it plays until it stops answering; then the newest one plan brought, which the
  // editor keeps fresh (a quiet plan every few minutes, and one at once when a source is lost).
  const held = useRef(new Map<string, HeldUrl>());
  const [, setLostTick] = useState(0);
  const hold = (key: string | null | undefined, url: string | null | undefined): string | undefined => {
    if (!url) return undefined;
    const id = key || url;
    const current = held.current.get(id);
    if (current && (!current.lost || current.url === url)) return current.url;
    held.current.set(id, { url, lost: false });
    return url;
  };
  const lose = useCallback((url: string) => {
    let known = false;
    for (const entry of held.current.values()) {
      if (entry.url === url && !entry.lost) {
        entry.lost = true;
        known = true;
      }
    }
    if (!known) return;
    setLostTick((n) => n + 1);
    lostRef.current?.();
  }, []);

  const spoken = useMemo(() => (preview?.narration ?? []).filter((line) => line.state === "ready" && !!line.audio_url), [preview?.narration]);
  // Read by the playing loop: a preview that lands while playing (a narration just spoken) changes no clock.
  const spokenRef = useRef(spoken);
  spokenRef.current = spoken;
  const voiceLevel = Math.min(1, Math.max(0, preview?.voice?.volume ?? 1));
  const voiceLevelRef = useRef(voiceLevel);
  voiceLevelRef.current = voiceLevel;
  const music = preview?.music?.url ? preview.music : null;
  const musicUrl = hold(music?.key, music?.url);
  const musicRef = useRef(music);
  musicRef.current = music;
  const [compositor, setCompositor] = useState<Compositor | null>(null);
  const [loaded, setLoaded] = useState<VideoPreviewDocument | null>(null);
  const loadedRef = useRef(loaded);
  loadedRef.current = loaded;
  const [scale, setScale] = useState(0.4);
  const doc = preview?.document ?? null;
  const width = doc?.width ?? 1920;
  const height = doc?.height ?? 1080;
  const labelRef = useRef(stateLabel);
  labelRef.current = stateLabel;

  const attach = useCallback(() => {
    const win = frameRef.current?.contentWindow as (Window & { __xtm?: Compositor }) | null;
    if (win?.__xtm) setCompositor(win.__xtm);
  }, []);

  // The stage fits its box, whatever the format.
  useEffect(() => {
    const el = boxRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const fit = () => setScale(Math.max(0.05, Math.min((el.clientWidth - 24) / width, (el.clientHeight - 24) / height)));
    const observer = new ResizeObserver(fit);
    observer.observe(el);
    fit();
    return () => observer.disconnect();
  }, [width, height]);

  // Each new document goes into the frame, its placeholders worded here; one that changes nothing does not.
  const requested = useRef<{ compositor: Compositor | null; json: string; seq: number }>({ compositor: null, json: "", seq: 0 });
  useEffect(() => {
    if (!compositor || !doc) return;
    const worded: VideoPreviewDocument = {
      ...doc,
      scenes: doc.scenes.map((scene) =>
        scene.preview_state ? { ...scene, preview_label: labelRef.current(scene.preview_state) } : scene,
      ),
    };
    const json = JSON.stringify(worded);
    if (requested.current.compositor === compositor && requested.current.json === json) return;
    const seq = requested.current.seq + 1;
    requested.current = { compositor, json, seq };
    void compositor.load(worded).then(
      () => {
        if (requested.current.seq !== seq) return;
        if (playhead.get() > doc.duration) playhead.set(doc.duration);
        setLoaded(doc);
      },
      () => {
        // Not loaded: the same plan, asked again, is tried again.
        if (requested.current.seq === seq) requested.current = { ...requested.current, json: "" };
      },
    );
  }, [compositor, doc, playhead]);

  useEffect(() => {
    if (!compositor?.onSourceLost) return;
    compositor.onSourceLost(() => lostRef.current?.());
    return () => compositor.onSourceLost?.(null);
  }, [compositor]);

  // One frame drawn at a time; the newest asked waits, the ones in between are skipped.
  const drawing = useRef(false);
  const queued = useRef<number | null>(null);
  const draw = useCallback(
    (index: number) => {
      if (!compositor) return;
      if (drawing.current) {
        queued.current = index;
        return;
      }
      drawing.current = true;
      void compositor
        .render(index)
        .catch(() => false)
        .finally(() => {
          drawing.current = false;
          const next = queued.current;
          queued.current = null;
          if (next !== null) draw(next);
        });
    },
    [compositor],
  );
  const playingRef = useRef(transport.playing);
  playingRef.current = transport.playing;
  useEffect(() => {
    if (!loaded) return;
    const paint = () => {
      const skimmed = playingRef.current ? null : (skim?.get() ?? null);
      draw(Math.max(0, Math.min(loaded.frames - 1, Math.round((skimmed ?? playhead.get()) * loaded.fps))));
    };
    paint();
    const stop = playhead.subscribe(paint);
    const stopSkim = skim?.subscribe(paint);
    return () => {
      stop();
      stopSkim?.();
    };
  }, [draw, loaded, playhead, skim]);

  const mutedRef = useRef(transport.muted);
  mutedRef.current = transport.muted;
  useEffect(() => {
    compositor?.setMuted(transport.muted);
    if (audioRef.current) audioRef.current.muted = transport.muted;
    for (const voice of voiceRefs.current.values()) voice.muted = transport.muted;
  }, [compositor, transport.muted]);

  // Playing: the playhead follows the clock; a seek while playing rebases it. A new plan landing meanwhile
  // (a poll, a save) changes no clock: the loop reads the document, the music and the narrations as they are now.
  const { playing, rate, loop } = transport;
  const ready = !!loaded;
  useEffect(() => {
    if (!compositor || !ready || !playing) return;
    const total = () => loadedRef.current?.duration ?? 0;
    let from = playhead.get() >= total() - 0.05 ? 0 : playhead.get();
    playhead.set(from);
    let startedAt = performance.now();
    let expected = from;
    // The bed is the render's (its length, its fades): only its ducking is applied here, from the same envelope.
    const level = (t: number) => {
      const music = musicRef.current;
      return music ? Math.min(1, Math.max(0, music.volume * duckGainAt(music.duck, t))) : 0;
    };
    // Every frame: a bed whose source changed (another bed, a lost URL) picks up where the video is.
    let bed: HTMLAudioElement | null = null;
    const syncAudio = (t: number) => {
      const audio = audioRef.current;
      if (audio !== bed) bed?.pause();
      bed = audio;
      if (!audio || !musicRef.current) return;
      if (audio.playbackRate !== rate) audio.playbackRate = rate;
      audio.volume = level(t);
      if (Math.abs(audio.currentTime - t) > 0.25) audio.currentTime = t;
      if (audio.paused) void audio.play().catch(() => undefined);
    };
    // Each narration plays while the playhead is inside it, from where the playhead is.
    const voices = voiceRefs.current;
    const syncVoices = (t: number) => {
      for (const [sceneId, offset] of spokenOffsets(spokenRef.current, t)) {
        const voice = voices.get(sceneId);
        if (!voice) continue;
        if (offset === null) {
          if (!voice.paused) voice.pause();
          continue;
        }
        voice.playbackRate = rate;
        voice.volume = voiceLevelRef.current;
        if (Math.abs(voice.currentTime - offset) > 0.25) voice.currentTime = offset;
        if (voice.paused) void voice.play().catch(() => undefined);
      }
    };
    compositor.setPlaying(true);
    syncAudio(from);
    syncVoices(from);
    let frame = 0;
    const tick = (now: number) => {
      if (Math.abs(playhead.get() - expected) > 0.05) {
        from = playhead.get();
        startedAt = now;
      }
      let t = from + ((now - startedAt) / 1000) * rate;
      const end = total();
      if (t >= end) {
        if (!loop) {
          playhead.set(end);
          onTransport({ playing: false });
          return;
        }
        from = 0;
        startedAt = now;
        t = 0;
      }
      expected = t;
      playhead.set(t);
      syncAudio(t);
      syncVoices(t);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      compositor.setPlaying(false);
      bed?.pause();
      for (const voice of voices.values()) voice.pause();
    };
  }, [compositor, ready, loop, onTransport, playhead, playing, rate]);

  return (
    <div
      ref={(node) => {
        boxRef.current = node;
        stageRef.current = node;
      }}
      className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-[radial-gradient(ellipse_at_center,color-mix(in_srgb,_var(--text-default-primary)_calc(0.05_*_100%),_transparent),transparent_70%)]"
      data-testid="video.editor.stage"
    >
      <div
        className="relative overflow-hidden rounded-sm ring-1 ring-[var(--border-elevation-subtle)]"
        // Data-driven geometry: the video's own format, fitted to the box.
        style={{ width: width * scale, height: height * scale }}
        onClick={() => onTransport({ playing: !playing })}
      >
        <iframe
          ref={frameRef}
          src={PREVIEW_PAGE}
          title={t_i18n("Video preview")}
          onLoad={attach}
          sandbox="allow-scripts allow-same-origin"
          tabIndex={-1}
          className="pointer-events-none origin-top-left border-0 bg-scrim"
          style={{ width, height, transform: `scale(${scale})` }}
        />
        {!loaded && (
          <div className="absolute inset-0 flex items-center justify-center bg-elevation-default">
            <Spinner size="lg" tone="inherit" className="text-accent" label={t_i18n("Loading the preview")} />
          </div>
        )}
        {!playing && loaded && (
          <span className="pointer-events-none absolute inset-0 flex items-center justify-center opacity-0 transition-opacity hover:opacity-100">
            <span className="rounded-full bg-elevation-default p-3 backdrop-blur">
              <Play className="h-6 w-6 text-default-primary" />
            </span>
          </span>
        )}
      </div>
      {loaded && (
        // One row for the stage's chips: on a narrow stage the second wraps under the first, never over it.
        <div className="pointer-events-none absolute inset-x-3 top-3 flex flex-wrap items-start gap-2" data-testid="video.editor.stageChips">
          <ApproximateInsertsBadge plan={loaded} playhead={playhead} />
          {previewing && (
            <span className="ml-auto inline-flex animate-appear-late items-center gap-1.5 rounded-sm bg-elevation-default px-2 py-1 content-caption text-default-secondary backdrop-blur">
              <Spinner size="sm" tone="inherit" /> {t_i18n("Updating the preview")}
            </span>
          )}
        </div>
      )}
      {musicUrl && (
        <audio
          ref={(node) => {
            audioRef.current = node;
            if (node) node.muted = mutedRef.current;
          }}
          src={musicUrl}
          preload="auto"
          className="hidden"
          onError={() => lose(musicUrl)}
          data-testid="video.editor.music"
        />
      )}
      {spoken.map((line) => {
        const url = hold(line.key, line.audio_url);
        return (
          <audio
            key={line.scene_id}
            ref={(node) => {
              if (!node) {
                voiceRefs.current.delete(line.scene_id);
                return;
              }
              node.muted = mutedRef.current;
              voiceRefs.current.set(line.scene_id, node);
            }}
            src={url}
            preload="auto"
            className="hidden"
            onError={() => url && lose(url)}
            data-testid="video.editor.voice"
          />
        );
      })}
    </div>
  );
}

/** Shown while the playhead is on a scene whose screen insert the stage cannot draw as the render does. */
function ApproximateInsertsBadge({ plan, playhead }: { plan: VideoPreviewDocument; playhead: Playhead }) {
  const { t_i18n } = useFormatter();
  const time = usePlayheadTime(playhead);
  const approximate = useMemo(() => plan.scenes.filter((scene) => scene.preview_inserts_approximate), [plan]);
  if (!approximate.some((scene) => time >= scene.start && time < scene.end)) return null;
  return (
    <Chip
      label={t_i18n("Preview approximate: insert compositing")}
      severity="medium"
      startIcon={<Icon name="triangle-alert" size={16} />}
      className="min-w-0"
      data-testid="video.editor.insertsApproximate"
    />
  );
}

/**
 * The transport's width tiers. A control a key also
 * reaches leaves the bar below its tier: frame steps, start and end under 42rem,
 * loop and mute under 36rem, scene jumps (Shift+Left, Shift+Right) and the clock's
 * duration under 24rem, the width of the bar at 1024 with the Studio rail open.
 * Play, the time, the scrubber, the speed and full screen always stay. The tier is
 * also written as data-leaves-below, which the tests read (never a class name).
 */
type TransportTier = "2xl" | "xl" | "sm";
const LEAVES_BELOW: Record<TransportTier, string> = {
  "2xl": "@max-2xl:hidden",
  xl: "@max-xl:hidden",
  sm: "@max-sm:hidden",
};

function Clock({ playhead, duration }: { playhead: Playhead; duration: number }) {
  const time = usePlayheadTime(playhead);
  return (
    // One line at any width: the scrubber beside it gives way, never the time.
    <span className="shrink-0 whitespace-nowrap font-mono-plex content-compact tabular-nums" data-testid="video.editor.clock">
      <span className="text-default-primary">{formatTimecode(time)}</span>
      <span className={cn("text-default-secondary", LEAVES_BELOW.sm)} data-leaves-below="sm" data-testid="video.editor.clockDuration">
        {" "}
        / {formatTimecode(duration)}
      </span>
    </span>
  );
}

function Scrubber({ playhead, duration, starts }: { playhead: Playhead; duration: number; starts: number[] }) {
  const { t_i18n } = useFormatter();
  const time = usePlayheadTime(playhead);
  const seek = (event: React.PointerEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    playhead.set(Math.min(duration, Math.max(0, ((event.clientX - box.left) / box.width) * duration)));
  };
  const share = duration > 0 ? Math.min(1, time / duration) : 0;
  return (
    <div
      role="slider"
      tabIndex={0}
      aria-label={t_i18n("Position")}
      aria-valuemin={0}
      aria-valuemax={Math.round(duration * 10) / 10}
      aria-valuenow={Math.round(time * 10) / 10}
      onPointerDown={(event) => {
        event.currentTarget.setPointerCapture(event.pointerId);
        seek(event);
      }}
      onPointerMove={(event) => {
        if (event.buttons === 1) seek(event);
      }}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") playhead.set(time - 1);
        if (event.key === "ArrowRight") playhead.set(Math.min(duration, time + 1));
      }}
      className="group relative h-4 min-w-6 flex-1 cursor-pointer"
    >
      <span className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-elevation-hover" />
      <span className="absolute left-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-accent" style={{ width: `${share * 100}%` }} />
      {duration > 0 &&
        starts.slice(1).map((start) => (
          <span
            key={start}
            aria-hidden
            className="absolute top-1/2 h-2 w-px -translate-y-1/2 bg-elevation-hover"
            style={{ left: `${(start / duration) * 100}%` }}
          />
        ))}
      <span
        className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent shadow ring-2 ring-[var(--bg-elevation-default)] transition-transform group-hover:scale-110"
        style={{ left: `${share * 100}%` }}
      />
    </div>
  );
}

export function Transport({
  preview,
  playhead,
  transport,
  onTransport,
  onFullscreen,
}: {
  preview: VideoPreview | null;
  playhead: Playhead;
  transport: TransportState;
  onTransport: (patch: Partial<TransportState>) => void;
  onFullscreen: () => void;
}) {
  const { t_i18n } = useFormatter();
  const doc = preview?.document;
  const duration = doc?.duration ?? 0;
  const fps = doc?.fps ?? 30;
  const starts = useMemo(() => (doc?.scenes ?? []).map((s) => s.start), [doc]);
  const stepFrame = (delta: number) => {
    onTransport({ playing: false });
    playhead.set(Math.min(duration, Math.max(0, playhead.get() + delta / fps)));
  };
  const jumpScene = (delta: -1 | 1) => {
    const scenes = doc?.scenes ?? [];
    if (!scenes.length) return;
    const target = sceneJump(scenes, playhead.get(), delta);
    playhead.set(scenes[target].start + (target === 0 ? 0 : 0.01));
  };
  const icon = (label: string, keys: string, Icon: React.ElementType, onClick: () => void, active = false, leavesBelow?: TransportTier) => (
    <Tip content={`${label} (${keys})`}>
      <Button
        variant="ghost"
        size="sm"
        className={cn("h-8 w-8 shrink-0 px-0", active && "bg-accent/15 text-accent", leavesBelow && LEAVES_BELOW[leavesBelow])}
        data-leaves-below={leavesBelow}
        onClick={onClick}
        aria-label={label}
        aria-pressed={active || undefined}
        disabled={!doc}
      >
        <Icon className="h-4 w-4" />
      </Button>
    </Tip>
  );
  return (
    // A container: on a narrow bar the controls a key also
    // reaches leave by tier (LEAVES_BELOW), so Play, the time and the scrubber keep their room.
    <div
      className="@container flex h-12 shrink-0 items-center gap-1 border-t border-elevation-subtle-soft bg-elevation-default px-3"
      data-testid="video.editor.transport"
    >
      {icon(t_i18n("To the start"), "Home", ChevronFirst, () => playhead.set(0), false, "2xl")}
      {icon(t_i18n("Previous scene"), "Shift+Left", SkipBack, () => jumpScene(-1), false, "sm")}
      {icon(t_i18n("Previous frame"), "Left", StepBack, () => stepFrame(-1), false, "2xl")}
      <Tip content={`${transport.playing ? t_i18n("Pause") : t_i18n("Play")} (Space)`}>
        <Button
          size="sm"
          className="h-9 w-9 shrink-0 rounded-full px-0"
          onClick={() => onTransport({ playing: !transport.playing })}
          aria-label={transport.playing ? t_i18n("Pause") : t_i18n("Play")}
          disabled={!doc}
        >
          {transport.playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 translate-x-px" />}
        </Button>
      </Tip>
      {icon(t_i18n("Next frame"), "Right", StepForward, () => stepFrame(1), false, "2xl")}
      {icon(t_i18n("Next scene"), "Shift+Right", SkipForward, () => jumpScene(1), false, "sm")}
      {icon(t_i18n("To the end"), "End", ChevronLast, () => playhead.set(duration), false, "2xl")}
      {/* min-w-fit: the group never shrinks below the time and a 24 px scrubber; only the scrubber gives way. */}
      <div className="mx-2 flex min-w-fit flex-1 items-center gap-3" data-testid="video.editor.timeGroup">
        <Clock playhead={playhead} duration={duration} />
        <Scrubber playhead={playhead} duration={duration} starts={starts} />
      </div>
      <Select value={String(transport.rate)} onValueChange={(value) => onTransport({ rate: Number(value) })}>
        <SelectTrigger className="h-8 w-[72px] shrink-0 content-compact" aria-label={t_i18n("Playback speed")}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {PLAYBACK_RATES.map((value) => (
            <SelectItem key={value} value={value}>
              {value}x
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {icon(t_i18n("Loop"), "L", Repeat, () => onTransport({ loop: !transport.loop }), transport.loop, "xl")}
      {icon(
        transport.muted ? t_i18n("Unmute") : t_i18n("Mute"),
        "M",
        transport.muted ? VolumeX : Volume2,
        () => onTransport({ muted: !transport.muted }),
        false,
        "xl",
      )}
      {icon(t_i18n("Full screen"), "F", Maximize, onFullscreen)}
    </div>
  );
}
