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
 * The Video Studio player: our controls over a native `<video>`, the
 * same wherever a video plays - the theater Play opens, a reader's view in the
 * editor, the chat card and the public link page.
 *
 * The browser keeps what it does well: decoding, the byte-range seeking it
 * asks the server for by itself, and the timing of the WebVTT cues. The player
 * draws the rest: the captions in the band every scene keeps clear for them,
 * inside the picture, and its controls, on a
 * forced-dark subtree (a video plays on black whatever the app mode): play and
 * pause, 10-second skips, a scrubber with what is buffered and the time under
 * the pointer, volume, captions, speed, picture-in-picture and fullscreen.
 * While it plays, the controls and the pointer go after a moment of stillness
 * and come back on movement; paused, they stay. Its keys are a video player's
 * (`player-model.ts`), answered while the focus is in it.
 *
 * - `inline` takes its width and its shape (16:9 unless told), for a card;
 *   `fill` takes the box it is given, letterboxed: the theater, the editor's
 *   stage, the public page. `header` draws at the top, with the controls.
 * - Fullscreen is the deck's (`useElementFullscreen`): the browser's, or the
 *   viewport where the browser refuses it. A player that already fills the
 *   window (`fillsWindow`) offers only the browser's.
 * - Tooltips and menus render inside the player (`container`, `inline`): a
 *   fullscreen element paints only its own subtree, and the theater sits
 *   above every portal of the page.
 * - A render's `chapters` are marked on the scrubber, named under the
 *   pointer and listed in a menu that names the one playing
 *   (`player-chapters.ts`); the system's media keys and lock screen drive
 *   the player played last (`useMediaSession`).
 * - Where the viewer left a video (`resumeKey`) is picked up next time, said
 *   with a way to start over (`player-resume.ts`).
 * - A URL that stops answering (a confined token past its life) is minted
 *   again through `onSourceLost` and the video goes on where it was; past
 *   that, or without it, the player says it failed and offers to try again.
 * - What a key does is shown on the picture and said to a screen reader;
 *   `?` opens the key sheet (`PlayerShortcutsDialog`). On a touch screen, a
 *   double tap on a side skips back or forward.
 * - Nothing flashes: the spinner comes in only once a wait lasts, and
 *   an autoplaying player shows it loads until its first play is answered,
 *   never a Play button it is about to take away.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  Captions,
  CaptionsOff,
  Check,
  Keyboard,
  ListVideo,
  Maximize,
  Minimize,
  Pause,
  PictureInPicture2,
  Play,
  RefreshCw,
  RotateCcw,
  RotateCw,
  Volume1,
  Volume2,
  VolumeX,
  type LucideIcon,
} from "lucide-react";

import { isFullscreenSupported, presentedElement, useElementFullscreen } from "@/components/presentation/fullscreen";
import { Button } from "@/components/ui/button";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { VideoChapter } from "@/types";

import "./cinema-player.css";
import { PlayerShortcutsDialog } from "./PlayerShortcutsDialog";
import { chapterAt, chapterSeek, chapterSpans, type ChapterSpan } from "./player-chapters";
import {
  DEFAULT_FPS,
  DOUBLE_TAP_MS,
  FLASH_MS,
  IDLE_HIDE_MS,
  PLAYBACK_SPEEDS,
  RESUMED_NOTICE_MS,
  SKIP_SECONDS,
  SOURCE_RENEWAL_COOLDOWN_MS,
  SOURCE_RENEWAL_TIMEOUT_MS,
  VOLUME_STEP,
  bufferedEnd,
  captionLift,
  clamp,
  cueAtTop,
  cueText,
  formatPlayerTime,
  letterbox,
  nextSpeed,
  playerKeyAction,
  readPlayerVolume,
  shareAt,
  tapSide,
  writePlayerVolume,
  type Box,
  type PlayerAction,
  type PlayerVolume,
} from "./player-model";
import { readResumePosition, writeResumePosition } from "./player-resume";
import { useMediaSession } from "./useMediaSession";
import { SCENE_TYPE_LABELS } from "./video-format";
import { Spinner } from "@filigran/design-system";

export interface CinemaPlayerProps {
  /** The MP4; `undefined` while its URL is minted. */
  src: string | undefined;
  poster?: string;
  /** The WebVTT track, only when the captions are not burned into the frame. */
  captions?: string;
  /** The player's accessible name: the video's title. */
  title: string;
  layout?: "inline" | "fill";
  /** The player already takes the whole window (the theater, the public page): only the browser's fullscreen adds to it. */
  fillsWindow?: boolean;
  autoPlay?: boolean;
  /** Ask the browser's fullscreen at mount: the theater, opened by a click. */
  startFullscreen?: boolean;
  /** Take the focus at mount, so the player's keys answer at once. */
  autoFocus?: boolean;
  pictureInPicture?: boolean;
  /** The length shown before the metadata loads (a render knows it), in seconds. */
  durationHint?: number;
  /** `width / height` of an inline player; 16:9 when unknown. */
  aspectRatio?: number;
  /** Drawn at the top with the controls, handed the player for its own tooltips. */
  header?: (container: HTMLElement | null) => ReactNode;
  /** The render's chapters (`timeline.chapters`): marked on the scrubber, named while they play. */
  chapters?: VideoChapter[] | null;
  /** Where the viewer left the video is remembered under this key, in this browser, and picked up next time. */
  resumeKey?: string;
  /** The video's frame rate, for the frame-by-frame keys (`,` / `.`). */
  fps?: number | null;
  /** The video's URL stopped answering (a confined token past its life): mint it again; the video goes on where it was. */
  onSourceLost?: () => void;
  className?: string;
  testId?: string;
  videoTestId?: string;
}

/** A control on the scrim: light in both themes, a step lighter when hovered. */
const CONTROL = "h-9 w-9 shrink-0 px-0 text-on-scrim hover:bg-on-scrim/15 hover:text-on-scrim";

/**
 * The captions' place in the picture: a share of its height from its edge,
 * their size a share of its height (the render's band at 1080p is 26-34 px),
 * and the room the controls and the title bar take at the player's edges.
 */
const CAPTION_MARGIN = 0.05;
const CAPTION_SIZE = 0.034;
const CAPTION_SIZE_PX = { min: 13, max: 40 };
const CONTROLS_CLEARANCE_PX = 76;
const HEADER_CLEARANCE_PX = 64;

function browserStorage(): Storage | undefined {
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}

/** The playhead and what is buffered past it, read every frame while the video plays. */
function usePlaybackClock(video: HTMLVideoElement | null) {
  const [clock, setClock] = useState({ time: 0, buffered: 0 });
  useEffect(() => {
    if (!video) return;
    let frame = 0;
    const read = () => {
      const time = video.currentTime || 0;
      const buffered = video.buffered ? bufferedEnd(video.buffered, time) : 0;
      setClock((current) => (current.time === time && current.buffered === buffered ? current : { time, buffered }));
    };
    const loop = () => {
      read();
      frame = requestAnimationFrame(loop);
    };
    const start = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(loop);
    };
    const stop = () => {
      cancelAnimationFrame(frame);
      read();
    };
    const reads = ["timeupdate", "seeked", "progress", "loadedmetadata", "emptied"] as const;
    reads.forEach((name) => video.addEventListener(name, read));
    video.addEventListener("play", start);
    video.addEventListener("pause", stop);
    video.addEventListener("ended", stop);
    if (!video.paused) start();
    read();
    return () => {
      cancelAnimationFrame(frame);
      reads.forEach((name) => video.removeEventListener(name, read));
      video.removeEventListener("play", start);
      video.removeEventListener("pause", stop);
      video.removeEventListener("ended", stop);
    };
  }, [video]);
  return clock;
}

/** A track the pointer drags: the share under it on press and while it moves, captured. */
function useTrackPointer(onShare: (share: number) => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const shareOf = (event: React.PointerEvent) => {
    const box = ref.current?.getBoundingClientRect();
    return box ? shareAt(event.clientX, box.left, box.width) : 0;
  };
  const handlers = {
    onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      event.currentTarget.setPointerCapture?.(event.pointerId);
      setDragging(true);
      onShare(shareOf(event));
    },
    onPointerMove: (event: React.PointerEvent<HTMLDivElement>) => {
      if (dragging) onShare(shareOf(event));
    },
    onPointerUp: (event: React.PointerEvent<HTMLDivElement>) => {
      setDragging(false);
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    },
    onPointerCancel: () => setDragging(false),
  };
  return { ref, dragging, shareOf, handlers };
}

/** Half the gap between two chapters on the scrubber, in px. */
const CHAPTER_GAP_PX = 1;

function Scrubber({
  video,
  duration,
  chapters,
  chapterName,
}: {
  video: HTMLVideoElement | null;
  duration: number;
  chapters: ChapterSpan[];
  chapterName: (chapter: ChapterSpan) => string;
}) {
  const { t_i18n } = useFormatter();
  const { time, buffered } = usePlaybackClock(video);
  const [hover, setHover] = useState<number | null>(null);
  const track = useTrackPointer((share) => {
    if (video && duration > 0) video.currentTime = share * duration;
  });
  const played = duration > 0 ? clamp(time / duration, 0, 1) : 0;
  // Without chapters, the track is one segment from end to end.
  const segments = chapters.length ? chapters : [{ index: 0, start: 0, end: duration }];
  const hovered = hover !== null && duration > 0 ? chapterAt(chapters, hover * duration) : null;
  const playing = chapterAt(chapters, time);
  const clockText = t_i18n("{time} of {duration}", { time: formatPlayerTime(time), duration: formatPlayerTime(duration) });
  return (
    <div
      ref={track.ref}
      role="slider"
      tabIndex={0}
      aria-label={t_i18n("Seek")}
      aria-valuemin={0}
      aria-valuemax={Math.round(duration)}
      aria-valuenow={Math.round(time)}
      aria-valuetext={playing ? `${clockText} - ${chapterName(playing)}` : clockText}
      className="group/scrub relative flex h-4 cursor-pointer touch-none items-center rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      {...track.handlers}
      onPointerMove={(event) => {
        setHover(track.shareOf(event));
        track.handlers.onPointerMove(event);
      }}
      onPointerLeave={() => setHover(null)}
      data-testid="video.player.seek"
    >
      <div className="relative h-1 w-full transition-[height] group-hover/scrub:h-1.5 group-focus-visible/scrub:h-1.5">
        {segments.map((segment, index) => {
          const length = segment.end - segment.start;
          const share = (value: number) => (length > 0 ? clamp((value - segment.start) / length, 0, 1) : 0);
          const before = index > 0 ? CHAPTER_GAP_PX : 0;
          const after = index < segments.length - 1 ? CHAPTER_GAP_PX : 0;
          return (
            <div
              key={segment.index}
              className={cn(
                "absolute inset-y-0 overflow-hidden rounded-full bg-on-scrim/25 transition-[top,bottom]",
                hovered?.index === segment.index && "-inset-y-px",
              )}
              style={{
                left: `calc(${duration > 0 ? (segment.start / duration) * 100 : 0}% + ${before}px)`,
                width: `calc(${duration > 0 ? (length / duration) * 100 : 100}% - ${before + after}px)`,
              }}
              data-testid={chapters.length ? "video.player.chapterMark" : undefined}
            >
              <div className="absolute inset-y-0 left-0 bg-on-scrim/40" style={{ width: `${share(buffered) * 100}%` }} />
              <div className="absolute inset-y-0 left-0 bg-accent" style={{ width: `${share(time) * 100}%` }} />
            </div>
          );
        })}
      </div>
      <span
        className={cn(
          "pointer-events-none absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent transition-transform",
          track.dragging ? "scale-100" : "scale-0 group-hover/scrub:scale-100 group-focus-visible/scrub:scale-100",
        )}
        style={{ left: `${played * 100}%` }}
      />
      {hover !== null && duration > 0 && (
        <span
          className="pointer-events-none absolute bottom-full mb-2 flex max-w-[16rem] -translate-x-1/2 flex-col items-center rounded-sm bg-scrim-strong px-1.5 py-0.5 text-center content-caption tabular-nums text-on-scrim"
          // Kept inside the player at its edges: a chapter's name is wider than a time.
          style={{ left: `clamp(${hovered ? "8rem" : "1.5rem"}, ${hover * 100}%, calc(100% - ${hovered ? "8rem" : "1.5rem"}))` }}
          data-testid="video.player.seekPreview"
        >
          {hovered && <span className="max-w-full truncate font-medium">{chapterName(hovered)}</span>}
          {formatPlayerTime(hover * duration)}
        </span>
      )}
    </div>
  );
}

/** The chapters menu: the one playing named on its trigger, each one listed with where it starts. */
function ChapterMenu({
  video,
  chapters,
  chapterName,
  onSeek,
}: {
  video: HTMLVideoElement | null;
  chapters: ChapterSpan[];
  chapterName: (chapter: ChapterSpan) => string;
  onSeek: (seconds: number) => void;
}) {
  const { t_i18n } = useFormatter();
  const { time } = usePlaybackClock(video);
  const playing = chapterAt(chapters, time);
  const items: OverflowMenuItem[] = chapters.map((chapter) => ({
    id: `chapter-${chapter.index}`,
    label: `${chapter.index + 1}. ${chapterName(chapter)}`,
    icon: chapter.index === playing?.index ? Check : NoIcon,
    shortcut: formatPlayerTime(chapter.start),
    onSelect: () => onSeek(chapter.start),
  }));
  return (
    <OverflowMenu
      inline
      side="top"
      scroll
      items={items}
      label={t_i18n("Chapters")}
      header={t_i18n("Chapters")}
      className="ml-1 h-9 min-w-0 shrink gap-1.5 px-2 text-on-scrim hover:bg-on-scrim/15 hover:text-on-scrim"
      trigger={
        <>
          <ListVideo className="h-4 w-4 shrink-0" />
          {playing && (
            <span className="hidden min-w-0 max-w-[14rem] truncate content-compact sm:inline" data-testid="video.player.chapter">
              {chapterName(playing)}
            </span>
          )}
        </>
      }
    />
  );
}

function Clock({ video, duration }: { video: HTMLVideoElement | null; duration: number }) {
  const { time } = usePlaybackClock(video);
  return (
    <span className="ml-1.5 hidden shrink-0 content-compact tabular-nums text-on-scrim min-[360px]:inline" data-testid="video.player.clock">
      {formatPlayerTime(time)} / {formatPlayerTime(duration)}
    </span>
  );
}

function VolumeControl({
  sound,
  onSound,
  container,
}: {
  sound: PlayerVolume;
  onSound: (next: PlayerVolume) => void;
  container: HTMLElement | null;
}) {
  const { t_i18n } = useFormatter();
  const level = sound.muted ? 0 : sound.volume;
  const Icon = level === 0 ? VolumeX : level < 0.5 ? Volume1 : Volume2;
  const label = level === 0 ? t_i18n("Unmute") : t_i18n("Mute");
  const set = (volume: number) => onSound({ volume: clamp(volume, 0, 1), muted: false });
  const track = useTrackPointer(set);
  return (
    <div className="group/volume flex items-center">
      <Tip content={`${label} (M)`} container={container}>
        <Button
          variant="ghost"
          size="sm"
          className={CONTROL}
          onClick={() => onSound(level === 0 ? { volume: sound.volume || 1, muted: false } : { ...sound, muted: true })}
          aria-label={label}
        >
          <Icon className="h-5 w-5" />
        </Button>
      </Tip>
      <div className="w-0 overflow-hidden opacity-0 transition-[width,opacity] duration-200 group-focus-within/volume:w-20 group-focus-within/volume:opacity-100 group-hover/volume:w-20 group-hover/volume:opacity-100">
        <div
          ref={track.ref}
          role="slider"
          tabIndex={0}
          aria-label={t_i18n("Volume")}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(level * 100)}
          aria-valuetext={`${Math.round(level * 100)}%`}
          className="mx-2 flex h-6 cursor-pointer touch-none items-center rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          {...track.handlers}
          onKeyDown={(event) => {
            const steps: Record<string, number> = { ArrowRight: VOLUME_STEP, ArrowUp: VOLUME_STEP, ArrowLeft: -VOLUME_STEP, ArrowDown: -VOLUME_STEP };
            if (event.key in steps) set(level + steps[event.key]);
            else if (event.key === "Home") set(0);
            else if (event.key === "End") set(1);
            else return;
            event.preventDefault();
            event.stopPropagation();
          }}
        >
          <div className="relative h-1 w-full rounded-full bg-on-scrim/25">
            <div className="absolute inset-y-0 left-0 rounded-full bg-on-scrim" style={{ width: `${level * 100}%` }} />
            <span className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-on-scrim" style={{ left: `${level * 100}%` }} />
          </div>
        </div>
      </div>
    </div>
  );
}

/** The player's box and the picture letterboxed in it, kept as either changes. */
function usePictureBox(root: HTMLElement | null, video: HTMLVideoElement | null) {
  const [box, setBox] = useState<{ player: { width: number; height: number }; picture: Box } | null>(null);
  useEffect(() => {
    if (!root || !video) return;
    const measure = () => {
      const player = { width: root.clientWidth, height: root.clientHeight };
      setBox({ player, picture: letterbox(player, { width: video.videoWidth, height: video.videoHeight }) });
    };
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(root);
    video.addEventListener("loadedmetadata", measure);
    measure();
    return () => {
      observer?.disconnect();
      video.removeEventListener("loadedmetadata", measure);
    };
  }, [root, video]);
  return box;
}

/** The cues showing now, from a track the browser keeps `hidden`: it times them, the player draws them. */
function useActiveCues(video: HTMLVideoElement | null, captions: string | undefined) {
  const [cues, setCues] = useState<{ text: string; top: boolean }[]>([]);
  useEffect(() => {
    const track = video?.textTracks?.[0];
    if (!track) return;
    track.mode = captions ? "hidden" : "disabled";
    const read = () =>
      setCues(Array.from(track.activeCues ?? [], (cue) => ({ text: cueText((cue as VTTCue).text ?? ""), top: cueAtTop(cue as VTTCue) })));
    track.addEventListener("cuechange", read);
    read();
    return () => track.removeEventListener("cuechange", read);
  }, [video, captions]);
  return cues;
}

/**
 * The captions, drawn by the player in the picture's band rather than by the
 * browser at the edge of the `<video>` box, which a letterboxed or full-window
 * player puts under its own controls. They step clear of the controls and the
 * title bar while those are up (`cinema-player.css`).
 */
function CaptionLayer({
  cues,
  box,
  hasHeader,
}: {
  cues: { text: string; top: boolean }[];
  box: { player: { width: number; height: number }; picture: Box };
  hasHeader: boolean;
}) {
  const { player, picture } = box;
  if (!picture.height) return null;
  const margin = picture.height * CAPTION_MARGIN;
  const fontSize = clamp(picture.height * CAPTION_SIZE, CAPTION_SIZE_PX.min, CAPTION_SIZE_PX.max);
  const groups = [
    { edge: "top" as const, lines: cues.filter((cue) => cue.top) },
    { edge: "bottom" as const, lines: cues.filter((cue) => !cue.top) },
  ];
  return (
    <>
      {groups.map(({ edge, lines }) => {
        if (!lines.length) return null;
        const offset = edge === "top" ? picture.top + margin : player.height - (picture.top + picture.height) + margin;
        const lift = edge === "top" ? (hasHeader ? captionLift(offset, HEADER_CLEARANCE_PX) : 0) : captionLift(offset, CONTROLS_CLEARANCE_PX);
        return (
          <div
            key={edge}
            className="cinema-captions pointer-events-none absolute z-[15] flex flex-col items-center gap-1 px-[4%] text-center"
            data-edge={edge}
            style={
              {
                left: picture.left,
                width: picture.width,
                [edge]: offset,
                fontSize,
                "--cinema-caption-lift": `${lift}px`,
              } as React.CSSProperties
            }
            data-testid="video.player.captions"
          >
            {lines.map((line, index) => (
              <span key={index} className="whitespace-pre-line rounded-sm bg-scrim-strong px-[0.5em] py-[0.15em] font-sans-plex leading-snug text-on-scrim">
                {line.text}
              </span>
            ))}
          </div>
        );
      })}
    </>
  );
}

/** The menu's rows line up whether or not they carry the check. */
function NoIcon({ className }: { className?: string }) {
  return <span className={className} aria-hidden="true" />;
}

export function CinemaPlayer({
  src,
  poster,
  captions,
  title,
  layout = "inline",
  fillsWindow = false,
  autoPlay = false,
  startFullscreen = false,
  autoFocus = false,
  pictureInPicture = true,
  durationHint,
  aspectRatio,
  header,
  chapters,
  resumeKey,
  fps,
  onSourceLost,
  className,
  testId,
  videoTestId,
}: CinemaPlayerProps) {
  const { t_i18n } = useFormatter();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [root, setRoot] = useState<HTMLDivElement | null>(null);
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  const fullscreen = useElementFullscreen(rootRef);
  const [playing, setPlaying] = useState(false);
  const [ended, setEnded] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [loadedDuration, setLoadedDuration] = useState(0);
  const [sound, setSound] = useState<PlayerVolume>(() => readPlayerVolume(browserStorage()));
  const [rate, setRate] = useState(1);
  const [captionsOn, setCaptionsOn] = useState(true);
  const [loop, setLoop] = useState(false);
  const [idle, setIdle] = useState(false);
  const [inPictureInPicture, setInPictureInPicture] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  /** What the last key did, on the picture and to a screen reader. */
  const [flash, setFlash] = useState<{ id: number; text: string } | null>(null);
  /** Where the video was picked up from, while it offers to start over. */
  const [resumedAt, setResumedAt] = useState<number | null>(null);
  /** A new URL is being minted for a source that stopped answering. */
  const [renewing, setRenewing] = useState(false);
  /** An autoplaying player until its first play settles: it shows it loads, never a Play it is about to take away. */
  const [starting, setStarting] = useState(autoPlay);
  const [failed, setFailed] = useState(false);
  /** Each play takes the system's media controls back (`useMediaSession`). */
  const [plays, setPlays] = useState(0);
  const idleTimer = useRef<number | undefined>(undefined);
  const flashTimer = useRef<number | undefined>(undefined);
  const tapTimer = useRef<number | undefined>(undefined);
  const renewalTimer = useRef<number | undefined>(undefined);
  const lastTap = useRef<{ at: number; side: -1 | 0 | 1 } | null>(null);
  const lastPointer = useRef("mouse");
  /** Where to go on once the next URL has loaded: a source minted again carries on, never restarts. */
  const restore = useRef<{ time: number; playing: boolean } | null>(null);
  const renewedAt = useRef(Number.NEGATIVE_INFINITY);
  const resumeChecked = useRef(false);
  const duration = loadedDuration || durationHint || 0;
  const offersFullscreen = !fillsWindow || fullscreen.supported;
  const offersPictureInPicture = pictureInPicture && typeof document !== "undefined" && !!document.pictureInPictureEnabled;
  const spans = useMemo(() => chapterSpans(chapters, duration), [chapters, duration]);
  const chapterName = useCallback(
    (chapter: ChapterSpan) =>
      chapter.title ??
      `${t_i18n("Scene {n}", { n: chapter.index + 1 })} - ${t_i18n(SCENE_TYPE_LABELS[chapter.type as keyof typeof SCENE_TYPE_LABELS] ?? chapter.type)}`,
    [t_i18n],
  );

  const attachRoot = useCallback((node: HTMLDivElement | null) => {
    rootRef.current = node;
    setRoot(node);
  }, []);

  useEffect(() => {
    if (!video) return;
    video.volume = sound.volume;
    video.muted = sound.muted;
    writePlayerVolume(browserStorage(), sound);
  }, [video, sound]);
  useEffect(() => {
    if (video) video.playbackRate = rate;
  }, [video, rate]);
  const cues = useActiveCues(video, captions);
  const pictureBox = usePictureBox(root, video);
  useEffect(() => {
    if (!video) return;
    const enter = () => setInPictureInPicture(true);
    const leave = () => setInPictureInPicture(false);
    video.addEventListener("enterpictureinpicture", enter);
    video.addEventListener("leavepictureinpicture", leave);
    return () => {
      video.removeEventListener("enterpictureinpicture", enter);
      video.removeEventListener("leavepictureinpicture", leave);
    };
  }, [video]);

  // Asked in the commit of the click that opened the player, so the browser
  // still counts it as the person's gesture. No viewport fallback here: a
  // player opened to fill the window already does.
  useLayoutEffect(() => {
    if (!startFullscreen || !isFullscreenSupported()) return;
    rootRef.current?.requestFullscreen?.({ navigationUI: "hide" })?.catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, at mount
  }, []);
  useEffect(() => {
    if (autoFocus) rootRef.current?.focus({ preventScroll: true });
  }, [autoFocus]);

  const wake = useCallback(() => {
    setIdle(false);
    window.clearTimeout(idleTimer.current);
    idleTimer.current = window.setTimeout(() => setIdle(true), IDLE_HIDE_MS);
  }, []);
  useEffect(() => () => window.clearTimeout(idleTimer.current), []);

  const play = useCallback(() => {
    if (!video) return;
    if (video.ended) video.currentTime = 0;
    video.play()?.catch(() => undefined);
  }, [video]);
  const toggle = useCallback(() => {
    if (!video) return;
    if (video.paused || video.ended) play();
    else video.pause();
  }, [video, play]);
  const seekTo = useCallback(
    (seconds: number) => {
      if (video) video.currentTime = clamp(seconds, 0, duration || 0);
      setResumedAt(null);
    },
    [video, duration],
  );
  const togglePictureInPicture = () => {
    if (!video) return;
    if (document.pictureInPictureElement === video) document.exitPictureInPicture?.()?.catch(() => undefined);
    else video.requestPictureInPicture?.()?.catch(() => undefined);
  };

  const showFlash = useCallback((text: string) => {
    window.clearTimeout(flashTimer.current);
    setFlash((current) => ({ id: (current?.id ?? 0) + 1, text }));
    flashTimer.current = window.setTimeout(() => setFlash(null), FLASH_MS);
  }, []);
  useEffect(
    () => () => {
      window.clearTimeout(flashTimer.current);
      window.clearTimeout(tapTimer.current);
      window.clearTimeout(renewalTimer.current);
    },
    [],
  );

  // Read live, so the media keys' handler stays the same while the chapters and the clock move.
  const chapterState = useRef({ spans, seekTo });
  useLayoutEffect(() => {
    chapterState.current = { spans, seekTo };
  });
  const seekChapter = useCallback(
    (direction: 1 | -1) => {
      const { spans: current, seekTo: seek } = chapterState.current;
      const target = video ? chapterSeek(current, video.currentTime, direction) : null;
      if (target !== null) seek(target);
    },
    [video],
  );
  useMediaSession(video, {
    title,
    artwork: poster,
    claim: plays,
    skipSeconds: SKIP_SECONDS,
    onChapter: spans.length ? seekChapter : undefined,
  });

  // Where the viewer is, kept as they watch: every few seconds, on pause, when the page goes.
  useEffect(() => {
    if (!video || !resumeKey) return;
    let saved = Number.NEGATIVE_INFINITY;
    const save = () => {
      if (!(video.duration > 0)) return;
      saved = video.currentTime;
      writeResumePosition(browserStorage(), resumeKey, video.currentTime, video.duration);
    };
    const onTime = () => {
      if (Math.abs(video.currentTime - saved) >= 5) save();
    };
    const onEnded = () => writeResumePosition(browserStorage(), resumeKey, 0, video.duration);
    video.addEventListener("timeupdate", onTime);
    video.addEventListener("pause", save);
    video.addEventListener("ended", onEnded);
    window.addEventListener("pagehide", save);
    return () => {
      video.removeEventListener("timeupdate", onTime);
      video.removeEventListener("pause", save);
      video.removeEventListener("ended", onEnded);
      window.removeEventListener("pagehide", save);
      if (!video.ended) save();
    };
  }, [video, resumeKey]);
  useEffect(() => {
    if (resumedAt === null) return;
    const timer = window.setTimeout(() => setResumedAt(null), RESUMED_NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [resumedAt]);

  const run = (action: PlayerAction) => {
    switch (action.type) {
      case "toggle":
        toggle();
        break;
      case "seekBy":
        seekTo((video?.currentTime ?? 0) + action.seconds);
        showFlash(
          action.seconds > 0
            ? t_i18n("{seconds, plural, one {# second} other {# seconds}} forward", { seconds: action.seconds })
            : t_i18n("{seconds, plural, one {# second} other {# seconds}} back", { seconds: -action.seconds }),
        );
        break;
      case "seekTo":
        seekTo(action.share * duration);
        showFlash(formatPlayerTime(action.share * duration));
        break;
      case "volumeBy": {
        const volume = clamp((sound.muted ? 0 : sound.volume) + action.delta, 0, 1);
        setSound({ volume, muted: false });
        showFlash(volume === 0 ? t_i18n("Muted") : t_i18n("Volume {percent}%", { percent: Math.round(volume * 100) }));
        break;
      }
      case "mute": {
        const unmute = sound.muted || sound.volume === 0;
        setSound(unmute ? { volume: sound.volume || 1, muted: false } : { ...sound, muted: true });
        showFlash(unmute ? t_i18n("Volume {percent}%", { percent: Math.round((sound.volume || 1) * 100) }) : t_i18n("Muted"));
        break;
      }
      case "captions":
        if (!captions) break;
        setCaptionsOn(!captionsOn);
        showFlash(captionsOn ? t_i18n("Captions off") : t_i18n("Captions on"));
        break;
      case "fullscreen":
        if (offersFullscreen) fullscreen.toggle();
        break;
      case "speed": {
        const next = nextSpeed(rate, action.direction);
        setRate(next);
        showFlash(t_i18n("Speed {rate}x", { rate: next }));
        break;
      }
      case "frame":
        if (!video) break;
        // Frame by frame is a look at a still: the video stops first.
        video.pause();
        seekTo(video.currentTime + action.direction / (fps || DEFAULT_FPS));
        break;
      case "shortcuts":
        setShortcutsOpen(true);
        break;
    }
  };

  /** The URL stopped answering: minted again once in a while, else the player says it failed. */
  const sourceFailed = (element: HTMLVideoElement) => {
    restore.current = restore.current ?? { time: element.currentTime, playing };
    window.clearTimeout(renewalTimer.current);
    if (onSourceLost && Date.now() - renewedAt.current > SOURCE_RENEWAL_COOLDOWN_MS) {
      renewedAt.current = Date.now();
      setRenewing(true);
      onSourceLost();
      renewalTimer.current = window.setTimeout(() => {
        setRenewing(false);
        setFailed(true);
      }, SOURCE_RENEWAL_TIMEOUT_MS);
      return;
    }
    setRenewing(false);
    setFailed(true);
  };
  const retry = () => {
    setFailed(false);
    if (onSourceLost) {
      renewedAt.current = Date.now();
      setRenewing(true);
      onSourceLost();
      window.clearTimeout(renewalTimer.current);
      renewalTimer.current = window.setTimeout(() => {
        setRenewing(false);
        setFailed(true);
      }, SOURCE_RENEWAL_TIMEOUT_MS);
    } else {
      video?.load();
    }
  };
  const onLoadedMetadata = (element: HTMLVideoElement) => {
    window.clearTimeout(renewalTimer.current);
    setRenewing(false);
    setFailed(false);
    const resume = restore.current;
    restore.current = null;
    if (resume) {
      element.currentTime = Math.min(resume.time, element.duration || resume.time);
      if (resume.playing) element.play()?.catch(() => undefined);
    } else if (resumeKey && !resumeChecked.current) {
      resumeChecked.current = true;
      const at = readResumePosition(browserStorage(), resumeKey, element.duration);
      if (at !== null) {
        element.currentTime = at;
        setResumedAt(at);
      }
    }
    if (starting) {
      // The browser's own autoplay says nothing when it refuses: this play's answer does.
      const started = element.play();
      if (started) started.then(() => setStarting(false), () => setStarting(false));
      else setStarting(false);
    }
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.defaultPrevented) return;
    if (event.key === "Escape") {
      // A fullscreen player leaves its fullscreen first, read live (`F` then
      // `Esc` lands before the re-render); the theater around it stays.
      if (presentedElement() === rootRef.current) {
        event.preventDefault();
        fullscreen.exit();
      }
      return;
    }
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, select, [contenteditable='true'], [role='menu'], [role='listbox']")) return;
    // The key sheet keeps its own keys, inside the player or portalled out of it (React still bubbles them here).
    const dialog = target.closest("[role='dialog']");
    if (!event.currentTarget.contains(target) || (dialog && event.currentTarget.contains(dialog))) return;
    // Space and Enter on a button press it, as everywhere.
    if ((event.key === " " || event.key === "Enter") && target !== event.currentTarget && target.closest("button, a, [role='button']")) return;
    const action = playerKeyAction(event);
    if (!action) return;
    event.preventDefault();
    run(action);
    wake();
  };

  const onSurfaceClick = (event: React.MouseEvent<HTMLDivElement>) => {
    if (!src) return;
    if (lastPointer.current !== "touch") {
      toggle();
      wake();
      return;
    }
    // On a touch screen, two taps on a side skip back or forward (and more taps skip more); one tap
    // waits to be sure it is alone, then brings the controls back - or plays and pauses, as on a phone.
    const box = event.currentTarget.getBoundingClientRect();
    const side = tapSide(box.width > 0 ? (event.clientX - box.left) / box.width : 0.5);
    const now = performance.now();
    const previous = lastTap.current;
    lastTap.current = { at: now, side };
    window.clearTimeout(tapTimer.current);
    if (side !== 0 && previous?.side === side && now - previous.at < DOUBLE_TAP_MS) {
      run({ type: "seekBy", seconds: side * SKIP_SECONDS });
      wake();
      return;
    }
    const controlsAway = playing && idle;
    tapTimer.current = window.setTimeout(() => {
      if (!controlsAway) toggle();
      wake();
    }, DOUBLE_TAP_MS);
  };

  const control = (label: string, keys: string | null, Icon: LucideIcon, onClick: () => void, extra?: { pressed?: boolean; className?: string }) => (
    <Tip content={keys ? `${label} (${keys})` : label} container={root}>
      <Button
        variant="ghost"
        size="sm"
        className={cn(CONTROL, extra?.pressed && "text-accent hover:text-accent", extra?.className)}
        onClick={onClick}
        aria-label={label}
        aria-pressed={extra?.pressed}
      >
        <Icon className="h-5 w-5" />
      </Button>
    </Tip>
  );

  const settings: OverflowMenuItem[] = [
    ...PLAYBACK_SPEEDS.map((speed) => ({
      id: `speed-${speed}`,
      label: speed === 1 ? t_i18n("Normal") : `${speed}x`,
      icon: speed === rate ? Check : NoIcon,
      onSelect: () => setRate(speed),
    })),
    { id: "loop", label: t_i18n("Loop"), icon: loop ? Check : NoIcon, onSelect: () => setLoop((on) => !on), separatorBefore: true },
    { id: "shortcuts", label: t_i18n("Keyboard shortcuts"), icon: Keyboard, shortcut: "?", onSelect: () => setShortcutsOpen(true) },
  ];

  return (
    <div
      ref={attachRoot}
      role="region"
      aria-label={title}
      aria-roledescription={t_i18n("Video player")}
      tabIndex={0}
      // A video plays on black: the dark token set whatever the app mode, for
      // the controls, their tooltips and their menus (`index.css`).
      data-mode="dark"
      data-idle={playing && idle ? "true" : undefined}
      data-testid={testId}
      className={cn(
        "cinema-player dark relative isolate select-none overflow-hidden text-on-scrim outline-none",
        layout === "inline" ? "w-full rounded-sm focus-visible:ring-2 focus-visible:ring-focus" : "h-full w-full",
        className,
      )}
      style={layout === "inline" ? { aspectRatio: aspectRatio ?? 16 / 9 } : undefined}
      onKeyDown={onKeyDown}
      onPointerMove={wake}
      onPointerDown={(event) => {
        lastPointer.current = event.pointerType;
        wake();
      }}
      onPointerLeave={() => {
        if (playing) setIdle(true);
      }}
    >
      <video
        ref={setVideo}
        className="absolute inset-0 h-full w-full object-contain"
        src={src}
        poster={poster}
        preload="metadata"
        playsInline
        autoPlay={autoPlay}
        loop={loop}
        onPlay={() => {
          setPlaying(true);
          setStarting(false);
          setEnded(false);
          setPlays((count) => count + 1);
          wake();
        }}
        onLoadedMetadata={(event) => onLoadedMetadata(event.currentTarget)}
        onError={(event) => {
          if (event.currentTarget.error) sourceFailed(event.currentTarget);
        }}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          setPlaying(false);
          setEnded(true);
        }}
        onWaiting={() => setWaiting(true)}
        onPlaying={() => setWaiting(false)}
        onCanPlay={() => setWaiting(false)}
        onDurationChange={(event) => {
          const length = event.currentTarget.duration;
          if (Number.isFinite(length) && length > 0) setLoadedDuration(length);
        }}
        onRateChange={(event) => setRate(event.currentTarget.playbackRate)}
        data-testid={videoTestId}
      >
        <track kind="captions" src={captions || undefined} label={t_i18n("Captions")} />
      </video>

      <div
        className="cinema-surface absolute inset-0"
        onClick={onSurfaceClick}
        onDoubleClick={() => lastPointer.current !== "touch" && offersFullscreen && fullscreen.toggle()}
        aria-hidden="true"
        data-testid="video.player.surface"
      />

      {captions && captionsOn && cues.length > 0 && pictureBox && <CaptionLayer cues={cues} box={pictureBox} hasHeader={!!header} />}

      {flash && (
        <div
          key={flash.id}
          className="pointer-events-none absolute left-1/2 top-[18%] z-30 -translate-x-1/2 animate-fade-in rounded-md bg-scrim-strong px-3 py-1.5 content-base font-medium tabular-nums text-on-scrim shadow-overlay"
          aria-hidden="true"
          data-testid="video.player.flash"
        >
          {flash.text}
        </div>
      )}
      <span role="status" className="sr-only">
        {flash?.text ?? ""}
      </span>

      {(!src || renewing || starting || (waiting && playing)) && !failed && (
        <div className="pointer-events-none absolute inset-0 flex animate-appear-late items-center justify-center">
          <Spinner size="xl" tone="inherit" className="text-on-scrim" label={t_i18n("Loading the video")} />
        </div>
      )}
      {failed && (
        <div
          className="absolute inset-0 z-30 flex flex-col items-center justify-center gap-3 bg-scrim-strong px-6 text-center"
          role="alert"
          data-testid="video.player.failed"
        >
          <AlertTriangle className="h-7 w-7 text-feedback-warning-primary" aria-hidden="true" />
          <p className="content-base font-medium text-on-scrim">{t_i18n("The video could not be played.")}</p>
          <p className="max-w-sm content-compact text-on-scrim/70">{t_i18n("Check your connection and try again.")}</p>
          <Button variant="outline" size="sm" onClick={retry} className="gap-1.5">
            <RefreshCw className="h-3.5 w-3.5" /> {t_i18n("Try again")}
          </Button>
        </div>
      )}
      {resumedAt !== null && !failed && (
        <div
          className="absolute bottom-24 left-3 z-30 flex items-center gap-2 rounded-md bg-scrim-strong py-1 pl-3 pr-1 content-compact text-on-scrim shadow-overlay sm:left-5"
          data-testid="video.player.resumed"
        >
          <span className="tabular-nums">{t_i18n("Resumed at {time}", { time: formatPlayerTime(resumedAt) })}</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-on-scrim hover:bg-on-scrim/15 hover:text-on-scrim"
            onClick={() => {
              seekTo(0);
              rootRef.current?.focus({ preventScroll: true });
            }}
          >
            {t_i18n("Start over")}
          </Button>
        </div>
      )}
      {src && !playing && !waiting && !renewing && !starting && !failed && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <Button
            variant="ghost"
            className="pointer-events-auto h-16 w-16 rounded-full bg-scrim px-0 text-on-scrim shadow-overlay backdrop-blur-sm hover:bg-scrim-strong hover:text-accent"
            onClick={() => {
              // The button goes once the video plays: the focus stays in the player, for its keys.
              rootRef.current?.focus({ preventScroll: true });
              play();
            }}
            aria-label={ended ? t_i18n("Replay") : t_i18n("Play")}
            data-testid="video.player.bigPlay"
          >
            {ended ? <RotateCcw className="h-7 w-7" /> : <Play className="h-7 w-7 translate-x-0.5" />}
          </Button>
        </div>
      )}

      {header && (
        <div className="cinema-chrome absolute inset-x-0 top-0 z-20 bg-gradient-to-b from-scrim-strong to-transparent px-3 pb-12 pt-3 sm:px-5">
          {header(root)}
        </div>
      )}

      {src && (
        <div className="cinema-chrome absolute inset-x-0 bottom-0 z-20 bg-gradient-to-t from-scrim-strong to-transparent px-3 pb-2 pt-12 sm:px-5" data-testid="video.player.controls">
          <Scrubber video={video} duration={duration} chapters={spans} chapterName={chapterName} />
          <div className="mt-1 flex items-center gap-0.5">
            {control(playing ? t_i18n("Pause") : t_i18n("Play"), "K", playing ? Pause : Play, toggle)}
            {control(t_i18n("Back 10 seconds"), "J", RotateCcw, () => run({ type: "seekBy", seconds: -SKIP_SECONDS }), { className: "hidden sm:inline-flex" })}
            {control(t_i18n("Forward 10 seconds"), "L", RotateCw, () => run({ type: "seekBy", seconds: SKIP_SECONDS }), { className: "hidden sm:inline-flex" })}
            <VolumeControl sound={sound} onSound={setSound} container={root} />
            <Clock video={video} duration={duration} />
            {spans.length > 0 && <ChapterMenu video={video} chapters={spans} chapterName={chapterName} onSeek={seekTo} />}
            <span className="flex-1" />
            {captions &&
              control(captionsOn ? t_i18n("Hide the captions") : t_i18n("Show the captions"), "C", captionsOn ? Captions : CaptionsOff, () => run({ type: "captions" }), {
                pressed: captionsOn,
              })}
            <OverflowMenu
              inline
              side="top"
              items={settings}
              label={t_i18n("Settings")}
              header={t_i18n("Playback speed")}
              className="h-9 shrink-0 px-2 text-on-scrim hover:bg-on-scrim/15 hover:text-on-scrim"
              trigger={<span className="content-compact font-medium tabular-nums">{rate}x</span>}
            />
            {offersPictureInPicture &&
              control(inPictureInPicture ? t_i18n("Back from picture-in-picture") : t_i18n("Picture-in-picture"), null, PictureInPicture2, togglePictureInPicture, {
                pressed: inPictureInPicture,
                className: "hidden sm:inline-flex",
              })}
            {offersFullscreen &&
              control(fullscreen.isFullscreen ? t_i18n("Exit fullscreen") : t_i18n("Full screen"), "F", fullscreen.isFullscreen ? Minimize : Maximize, fullscreen.toggle)}
          </div>
        </div>
      )}

      {/* Inside the player where only it is painted (fullscreen, a window it fills); over the page otherwise. */}
      <PlayerShortcutsDialog
        open={shortcutsOpen}
        onOpenChange={setShortcutsOpen}
        container={fillsWindow || fullscreen.isFullscreen ? root : null}
        captions={!!captions}
        fullscreen={offersFullscreen}
      />
    </div>
  );
}
