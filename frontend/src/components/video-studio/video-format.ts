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
 * Plain helpers of the Video Studio surfaces, kept out of the
 * component files so Vite's Fast Refresh keeps working there.
 */
import { formatList } from "../../lib/format-list.ts";
import type { VideoJobStatus, VideoMotionStyle, VideoShotDirection, VideoTemplatePurpose } from "@/types";

/** `12.3 s` / `2:05` for a duration in milliseconds. */
export function formatDuration(ms: number | null | undefined): string {
  if (!ms || ms <= 0) return "0 s";
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

/** `48.2 MB` for a size in bytes. */
export function formatSize(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1_000_000) return `${Math.round(bytes / 1000)} kB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

export function isUnfinished(status: VideoJobStatus | null | undefined): boolean {
  return status === "pending" || status === "running";
}

/**
 * What opening a video offers its reader: who may change it opens the editor; a reader views it -
 * the render it plays, or the preview of the edit until there is one. @i18n-keys
 */
export const VIDEO_OPEN_LABELS = {
  edit: "Open in the editor",
  view: "View now",
} as const;

export function videoOpenAction(video: { can_manage?: boolean }): keyof typeof VIDEO_OPEN_LABELS {
  return video.can_manage ? "edit" : "view";
}

/** The English label of a job status, read through `t_i18n` by the caller. @i18n-keys */
export const JOB_STATUS_LABELS = {
  pending: "Queued",
  running: "In progress",
  ready: "Ready",
  failed: "Failed",
  cancelled: "Cancelled",
} as const satisfies Record<VideoJobStatus, string>;

/**
 * The Video Studio tabs, by key. A video is a video, whatever its scenes: the
 * recordings and the AI shots are its media, kept together. @i18n-keys
 */
export const STUDIO_TAB_LABELS = {
  videos: "Videos",
  media: "Media",
} as const;

export type StudioTab = keyof typeof STUDIO_TAB_LABELS;

/** Render quality presets, with what each produces. @i18n-keys */
export const QUALITY_LABELS = {
  draft: "Draft (720p, 30 fps)",
  standard: "Standard (1080p, 30 fps)",
  high: "High (1080p, 60 fps)",
  ultra: "Ultra (4K, 60 fps)",
} as const;

/** What a render is, under its video's title in the theater: its quality and the revision it was made from. */
export function renderCaption(
  render: { quality: keyof typeof QUALITY_LABELS; revision: number },
  t_i18n: (key: string, values?: Record<string, string | number>) => string,
): string {
  return `${t_i18n(QUALITY_LABELS[render.quality])} - ${t_i18n("revision {revision}", { revision: render.revision })}`;
}

/**
 * The size a scene preview was drawn at: a player scales it to its display, so a 720p draft read on a
 * large screen looks softer than the render will - said wherever the preview plays.
 */
export function previewSize(
  render: { width: number | null; height: number | null; fps: number | null; quality: keyof typeof QUALITY_LABELS },
  t_i18n: (key: string, values?: Record<string, string | number>) => string,
): string {
  if (!render.width || !render.height) return t_i18n(QUALITY_LABELS[render.quality]);
  // Strings, so ICU never groups the digits ("1,920").
  return t_i18n("Previewed at {width}x{height}, {fps} fps", {
    width: String(render.width),
    height: String(render.height),
    fps: String(render.fps ?? 30),
  });
}

/** A time in a render's MP4 as its checks give it (`render_checks.clock`): `1:02.35`. */
export function formatClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "?";
  const whole = Math.max(0, seconds);
  const minutes = Math.floor(whole / 60);
  return `${minutes}:${(whole - minutes * 60).toFixed(2).padStart(5, "0")}`;
}

/**
 * What a render's check found (`render_checks`): every kind it writes, `test_video_studio_render_checks.py`
 * keeps the two lists equal. @i18n-keys
 */
const FINDING_KIND_LABELS = {
  voice_segment: "Narration failed its check",
  voice_fallback: "Narration spoken by another voice",
  transcript: "Narration heard wrong",
  pronunciation: "Brand term mispronounced",
  repeated_sentence: "Sentence heard twice",
  stutter: "Stutter",
  frozen: "Frozen picture",
  brightness_dip: "Frame dims for a moment",
  double_image: "Two pictures in one frame",
  near_empty: "Canvas left near empty",
  static_flicker: "Page chrome drawn at two places",
  music_level: "Music level",
  audio_mix: "Mixed audio",
  insert_foreground: "Screen insert drawn over the foreground",
  insert_padded: "Screen insert padded",
  logo_lost: "Brand mark lost",
  surface_lost: "Tracked surface lost",
  insert_missing: "Screen insert not drawn",
  insert_colour_shift: "Screen insert colours shifted",
  logo_duplicated: "Logo shown twice",
  logo_not_integrated: "Logo drawn flat, not part of its surface",
  insert_alignment: "Screen insert off its surface",
  insert_jitter: "Screen insert jitters",
  insert_residual_key: "Key colour left visible",
  insert_flicker: "Screen insert flickers",
  insert_soft: "Screen insert soft",
  insert_halo: "Halo around the foreground",
  insert_rim: "Grey rim around the foreground",
  insert_cropped: "Screen insert cropped",
  insert_band_key_colour: "Key colour left in the contain bands",
  insert_flatness: "Surface curved",
  insert_dropped: "Screen insert dropped at a scene join",
  insert_gap: "Screen left empty between two inserts",
  insert_measurement_lost: "Screen insert not measured where it is drawn",
  insert_through_foreground: "Screen insert seen through the foreground",
  insert_ghost: "Foreground mask too unsure to trust",
  insert_matte_halo: "Surface left uncovered around the foreground",
  insert_matte_lag: "Foreground mask lagging the subject",
  insert_occluded: "Screen insert hidden by the foreground",
  key_spill_outside_quad: "Key colour spilled past the screen",
  duplicate_logo: "Brand mark seen twice in one frame",
  invented_text: "Invented lettering in an AI shot",
} as const;

/** The label of a finding's kind, or `null` for a kind this build does not know. */
export function findingKindLabel(kind: string): string | null {
  return (FINDING_KIND_LABELS as Record<string, string>)[kind] ?? null;
}

/**
 * A finding's words by its `code` (`render_checks.Finding.code`), filled with its `params` in the reader's
 * language. A code this build does not know, or a finding without one, shows the backend's English `message`.
 * `test_video_studio_render_checks.py` keeps every code the backend writes in this list. @i18n-keys
 */
const FINDING_MESSAGES = {
  frozen: "the picture is frozen for {seconds, number} s ({start} to {end})",
  brightness_dip: "the whole frame dims to {share}% of its brightness ({start} to {end}), outside any transition",
  double_image: "{frames, plural, one {# frame draws} other {# frames draw}} two pictures at once ({start} to {end})",
  near_empty: "the canvas is near empty for {seconds, number} s ({start} to {end}), the cursor left out",
  static_flicker: "{frames, plural, one {# frame draws} other {# frames draw}} the page chrome at two places ({start} to {end})",
} as const;

/** What a render's finding says, translated when its code is known, its English message otherwise. */
export function findingMessage(
  finding: { code?: string | null; params?: Record<string, string | number> | null; message: string },
  t_i18n: (key: string, values?: Record<string, string | number>) => string,
): string {
  const template = finding.code ? (FINDING_MESSAGES as Record<string, string>)[finding.code] : undefined;
  return template ? t_i18n(template, finding.params ?? {}) : finding.message;
}

/** Why the quality gate refused a render, as `render_checks.GateReason.as_dict` sends it. */
export interface RenderGateReasonLike {
  code: string;
  message: string;
  params?: {
    count?: number;
    quality?: string;
    created_at?: string | null;
    findings?: { kind: string; at: number | null }[];
  } | null;
}

/** How `renderGateReasonText` writes times and lists in the reader's language. */
interface RenderGateFormat {
  /** "3 minutes ago" (`formatTimeAgo`). */
  timeAgo: (iso: string) => string;
  /** "7 Nov, 14:22" (`formatShortDateTimeIntl`). */
  dateTime: (iso: string) => string;
  /** The app language, for `formatList`. */
  locale: string;
}

/**
 * A refused render's reason in the reader's language, built from its `params` (`render_checks.gate`): the render
 * named by its quality, its age and its time, never by its id, each finding by its label, joined the way the
 * language joins a list (`formatList`), a real plural in both sentences. A reason without params, or with a code
 * this build does not build, shows the backend's English `message`.
 */
export function renderGateReasonText(
  reason: RenderGateReasonLike,
  t_i18n: (key: string, values?: Record<string, string | number>) => string,
  format: RenderGateFormat,
): string {
  const params = reason.params;
  if (reason.code !== "open_findings" || !params || typeof params.count !== "number") return reason.message;
  const quality = params.quality && params.quality in QUALITY_LABELS
    ? t_i18n(QUALITY_LABELS[params.quality as keyof typeof QUALITY_LABELS])
    : t_i18n("another quality");
  const listed = (params.findings ?? []).map((finding) => {
    const label = findingKindLabel(finding.kind);
    const name = label ? t_i18n(label) : t_i18n("another finding");
    return finding.at === null || finding.at === undefined
      ? name
      : t_i18n("{finding} at {time}", { finding: name, time: formatClock(finding.at) });
  });
  const rest = params.count - listed.length;
  // The remainder is the last item of the list, without a conjunction of its own: the list adds it ("et 25 de plus").
  if (rest > 0) listed.push(t_i18n("{count} more", { count: rest }));
  const values = { count: params.count, quality, findings: formatList(listed, format.locale) };
  return params.created_at
    ? t_i18n(
        "A render of this revision ({quality}, {when}) left {count, plural, one {# finding} other {# findings}} open: {findings}. {count, plural, one {Change what it points at, or preview its scene again} other {Change what they point at, or preview their scenes again}}: the same revision renders the same.",
        {
          ...values,
          when: t_i18n("{relative}, {absolute}", {
            relative: format.timeAgo(params.created_at),
            absolute: format.dateTime(params.created_at),
          }),
        },
      )
    : t_i18n(
        "A render of this revision ({quality}) left {count, plural, one {# finding} other {# findings}} open: {findings}. {count, plural, one {Change what it points at, or preview its scene again} other {Change what they point at, or preview their scenes again}}: the same revision renders the same.",
        values,
      );
}

/** How a pronunciation entry finds its term in a narration (`spec.Pronunciation.match`). @i18n-keys */
export const PRONUNCIATION_MATCH_LABELS = {
  word: "Whole word, same case",
  exact: "Whole token only",
  case_insensitive: "Any case",
} as const;

/** What reads a pronunciation entry (`spec.Pronunciation.apply_to`). @i18n-keys */
export const PRONUNCIATION_APPLY_LABELS = {
  voice: "The voice",
  captions: "The captions",
  both: "Voice and captions",
} as const;

/** How a scene plays once its footage ended before its narration (`spec.EXTEND_MODES`). @i18n-keys */
export const EXTEND_MODE_LABELS = {
  zoom: "Play slower, then drift in",
  loop: "Loop its last second",
  hold: "Freeze its last frame",
} as const;

/** The scene types of a video, as the scene list names them. @i18n-keys */
export const SCENE_TYPE_LABELS = {
  title: "Title card",
  statement: "Statement",
  recording: "Recording",
  slide: "Slide",
  figure: "Animated figure",
  clip: "AI shot",
  image: "Image",
  outro: "Outro",
  motion: "Motion graphics",
} as const;

/** How the brand moves (`spec.MotionStyle.personality`): every card, statement and motion scene. @i18n-keys */
export const MOTION_PERSONALITY_LABELS = {
  precise: "Precise: crisp, measured moves",
  calm: "Calm: slow, soft reveals",
  energetic: "Energetic: snappy springs, letter by letter",
  cinematic: "Cinematic: long blurred reveals and light sweeps",
  playful: "Playful: bouncy springs",
} as const;

/** How a card's title comes in, over its personality (`spec.MotionStyle.title`). @i18n-keys */
export const TITLE_SPLIT_LABELS = {
  lines: "Lines",
  words: "Words",
  letters: "Letters",
} as const;

/** When the cuts and the builds follow the music (`spec.MotionStyle.beat_sync`). @i18n-keys */
export const BEAT_SYNC_LABELS = {
  off: "Off",
  cuts: "Cuts on the beat",
  all: "Cuts and builds on the beat",
} as const;

/** A motion scene's built-in compositions (`spec.MOTION_COMPOSITIONS`). @i18n-keys */
export const MOTION_COMPOSITION_LABELS = {
  kinetic_title: "Kinetic title",
  logo_reveal: "Logo reveal",
  stat_counter: "Counted figure",
  list_build: "List, item by item",
  split_statement: "Split statement",
  quote: "Quote",
  constellation: "Title over a constellation",
} as const;

/** How a figure stands in space (`spec.FigureScene.perspective`). @i18n-keys */
export const FIGURE_PERSPECTIVE_LABELS = {
  flat: "Flat, facing the camera",
  tilt: "Leaning back, turning towards the camera",
  orbit: "Turning slowly side to side",
} as const;

/** How a scene comes in (`spec.Transition`). @i18n-keys */
export const TRANSITION_LABELS = {
  auto: "Automatic",
  cut: "Cut",
  crossfade: "Crossfade",
  fade_black: "Fade through black",
  slide_left: "Slide left",
  slide_up: "Slide up",
  zoom: "Zoom",
  blur: "Blur",
  wipe: "Wipe",
  whip: "Whip",
  flash: "Flash",
} as const;

/** Each named transition's own length, in seconds (`timeline.TRANSITION_SECONDS`). */
export const TRANSITION_SECONDS: Record<Exclude<keyof typeof TRANSITION_LABELS, "auto">, number> = {
  cut: 0,
  crossfade: 0.6,
  fade_black: 0.8,
  slide_left: 0.55,
  slide_up: 0.55,
  zoom: 0.6,
  blur: 0.55,
  wipe: 0.6,
  whip: 0.35,
  flash: 0.4,
};

/** How the automatic transitions cut (`spec.Transition.pace`). @i18n-keys */
export const TRANSITION_PACE_LABELS = {
  calm: "Calm: dissolves",
  dynamic: "Dynamic: pushes, whips and flashes",
  classic: "Classic: cuts, crossfades and fades to black",
} as const;

/** How a figure builds in (`spec.FigureScene.build`). @i18n-keys */
export const FIGURE_BUILD_LABELS = {
  sequence: "Step by step",
  together: "All at once",
} as const;

/** When a figure's steps come in (`spec.FigureScene.reveal`). @i18n-keys */
export const FIGURE_REVEAL_LABELS = {
  even: "Spread over the scene",
  narration: "When the narration names them",
} as const;

/** How the camera moves over a figure (`spec.FigureScene.camera`). @i18n-keys */
export const FIGURE_CAMERA_LABELS = {
  follow: "Follow each step, end on the whole figure",
  still: "The whole figure, still",
} as const;

/** What a figure does once built (`spec.FigureScene.motion`). @i18n-keys */
export const FIGURE_MOTION_LABELS = {
  drift: "Keeps moving: a slow drift, pulses along its links",
  none: "Holds still",
} as const;

/** What an overlay is (`spec.Overlay`). @i18n-keys */
export const OVERLAY_TYPE_LABELS = {
  callout: "Callout",
  lower_third: "Lower third",
  badge: "Badge",
  caption: "Caption",
  spotlight: "Spotlight",
  image: "Picture",
} as const;

/** The brand marks of the template an image overlay draws by role (`spec.BRAND_ASSETS`). @i18n-keys */
export const OVERLAY_ASSET_LABELS = {
  logo_mark: "The brand's mark",
  logo: "The brand's logo",
  logo_light: "The logo for light backgrounds",
} as const;

/** An image overlay's width when the spec names none (`spec.IMAGE_OVERLAY_WIDTH`): a share of the frame. */
export const IMAGE_OVERLAY_WIDTH = 0.12;

/** @i18n-keys */
export const OVERLAY_POSITION_LABELS = {
  auto: "Automatic",
  top: "Top",
  bottom: "Bottom",
  left: "Left",
  right: "Right",
  center: "Center",
  top_left: "Top left",
  top_right: "Top right",
  bottom_left: "Bottom left",
  bottom_right: "Bottom right",
} as const;

/** @i18n-keys */
export const OVERLAY_STYLE_LABELS = {
  glass: "Glass",
  accent: "Accent",
  dark: "Dark",
  light: "Light",
} as const;

/** The look of a title or closing card. @i18n-keys */
export const CARD_STYLE_LABELS = {
  aurora: "Aurora",
  minimal: "Minimal",
  spotlight: "Spotlight",
  split: "Split",
} as const;

/** How a statement's words come in (`spec.StatementScene.reveal`). @i18n-keys */
export const REVEAL_LABELS = {
  lines: "Line by line",
  words: "Word by word",
  together: "All at once",
} as const;

/** @i18n-keys */
export const ALIGN_LABELS = {
  center: "Centered",
  left: "Left",
} as const;

/** What a video template is made for (`templates.TEMPLATE_PURPOSES`). @i18n-keys */
export const TEMPLATE_PURPOSE_LABELS = {
  product_demo: "Product demo",
  brand_film: "Brand film",
  event: "Event",
  launch: "Launch",
  explainer: "Threat explainer",
  social: "Social teaser",
} as const satisfies Record<VideoTemplatePurpose, string>;

/** The kinds of video no template is made for: a video for anything but work. */
type CreativeVideoKind = "fun" | "story" | "howto" | "greeting";
/** A kind of video "New video" offers: a template purpose, or a creative kind. */
export type VideoKind = VideoTemplatePurpose | CreativeVideoKind;

/** @i18n-keys */
const CREATIVE_KIND_LABELS = {
  fun: "Just for fun",
  story: "Short story",
  howto: "How-to",
  greeting: "Greeting",
} as const satisfies Record<CreativeVideoKind, string>;

export const VIDEO_KIND_LABELS: Record<VideoKind, string> = { ...TEMPLATE_PURPOSE_LABELS, ...CREATIVE_KIND_LABELS };

/** What each kind of video asks the assistant for, before the brief. @i18n-keys */
export const VIDEO_KIND_REQUESTS = {
  product_demo: "Make this video a product demo:",
  brand_film: "Make this video a brand film:",
  event: "Make this video for an event - a booth loop or a stage opener:",
  launch: "Make this video a launch announcement:",
  explainer: "Make this video a threat explainer built from our intelligence:",
  social: "Make this video a short social teaser:",
  fun: "Make this video just for fun - no brand, no product, whatever style suits the idea:",
  story: "Make this video a short story:",
  howto: "Make this video a how-to that teaches one thing, step by step:",
  greeting: "Make this video a greeting - a birthday, a thank-you, a farewell or a celebration:",
} as const satisfies Record<VideoKind, string>;

/** An example brief per kind of video, shown in the empty brief. @i18n-keys */
export const VIDEO_KIND_EXAMPLES = {
  product_demo: "A 60-second walkthrough of creating a case from an alert in our demo instance...",
  brand_film:
    "Two minutes introducing the company: the mission, the community, extracts of our last get-together, then the platform as proof...",
  event: "A 45-second loop for our booth: the problem, the platform, our customers, the tagline - no sound...",
  launch: "Announce the new release: why it matters, what it does, where to try it...",
  explainer: "Explain the latest ransomware campaign we track: who is behind it, how it works, what to do...",
  social: "A 20-second vertical teaser announcing our webinar, with a hook in the first two seconds...",
  fun: "A 30-second video of a cat astronaut exploring a planet made of candy, upbeat music, no narration...",
  story: "A one-minute story of a lighthouse keeper who befriends a storm, told by a warm narrator...",
  howto: "Two minutes on brewing pour-over coffee: the gear, the ratio, the pour, the taste...",
  greeting: "A birthday video for Marie from the whole team: photos of the year, funny captions, a big finish...",
} as const satisfies Record<VideoKind, string>;

/** @i18n-keys */
export const VIDEO_KIND_GROUP_LABELS = { creative: "Creative", work: "For work" } as const;

/** The kinds of video in the order the "New video" dialog offers them. */
export const VIDEO_KIND_GROUPS: readonly { id: keyof typeof VIDEO_KIND_GROUP_LABELS; kinds: readonly VideoKind[] }[] = [
  { id: "creative", kinds: ["fun", "story", "howto", "greeting"] },
  { id: "work", kinds: ["product_demo", "brand_film", "event", "launch", "explainer", "social"] },
];

/** Whether a template can be made for *kind* (the creative kinds have none). */
export function isTemplatePurpose(kind: VideoKind): kind is VideoTemplatePurpose {
  return kind in TEMPLATE_PURPOSE_LABELS;
}

/** The format a kind of video starts in, when its look makes it. */
export const VIDEO_KIND_FORMATS: Partial<Record<VideoKind, string>> = { social: "9:16" };

/** The formats a video is made in, by the frame's shape or where it plays. @i18n-keys */
export const ASPECT_RATIO_LABELS = {
  "16:9": "Landscape",
  "9:16": "Mobile",
  "1:1": "Square",
  "4:5": "Feed",
} as const;

/** A kind's example as a brief to start from: without the ellipsis that marks it as an example. */
export function exampleBrief(example: string): string {
  return example.replace(/\s*(\.\.\.|\u2026)\s*$/, "");
}

/** The templates suited to *kind*, best first: made for it, then the rest (built-ins before the person's own). */
export function templatesForKind<T extends { purposes: readonly string[]; is_builtin: boolean }>(
  templates: readonly T[],
  kind: VideoKind | null,
): T[] {
  if (!kind) return [...templates];
  const rank = (t: T) => (t.purposes.includes(kind) ? 0 : 2) + (t.is_builtin ? 0 : 1);
  return [...templates].sort((a, b) => rank(a) - rank(b));
}

/**
 * The first message the assistant receives for a new video: what kind
 * of video, then the brief in the person's words. The rail is bound to the
 * new video, so the agent fills THIS one; its template and format are set.
 */
export function newVideoRequest(kindRequest: string | null, brief: string, choices: readonly string[] = []): string {
  const text = brief.trim();
  const parts = [kindRequest, text, choices.length ? choices.join("\n") : null].filter((part): part is string => !!part);
  return parts.join("\n\n");
}

/**
 * The optional kickstart choices of the "New video" dialog. Each one starts on
 * "suggest": the assistant proposes it in its first answer, and nothing is
 * said or set for it here. A choice the person sets is one line of the request
 * (`newVideoChoiceLines`) and, where the spec holds it at creation, set on the
 * new video (`newVideoSpecChoices`).
 */
export interface NewVideoChoices {
  music: "suggest" | "none" | "track" | "ai";
  /** A built-in track's id, with `music: "track"`. */
  musicTrack: string;
  /** Its name, said in the request. */
  musicTrackName: string;
  /** The mood of a track composed for this video, with `music: "ai"`. */
  musicMood: string;
  narration: "suggest" | "none" | "on";
  narrationLanguage: string;
  narrationVoice: string;
  captions: "suggest" | "off" | "file" | "burned";
  /** Target seconds; 0 leaves the length to the assistant. */
  seconds: number;
  pace: "suggest" | "calm" | "dynamic";
}

export const NEW_VIDEO_CHOICES_UNSET: NewVideoChoices = {
  music: "suggest",
  musicTrack: "",
  musicTrackName: "",
  musicMood: "",
  narration: "suggest",
  narrationLanguage: "",
  narrationVoice: "",
  captions: "suggest",
  seconds: 0,
  pace: "suggest",
};

/** The chips of each choice. @i18n-keys */
export const NEW_VIDEO_CHOICE_LABELS = {
  suggest: "Let the assistant suggest",
  music_none: "No music",
  music_track: "Built-in track",
  music_ai: "Composed by AI",
  narration_none: "No narration",
  narration_on: "With narration",
  captions_off: "No captions",
  captions_file: "Subtitle file only",
  captions_burned: "Burned in",
  pace_calm: "Calm",
  pace_dynamic: "Dynamic",
} as const;

/** One line of the request per choice set. @i18n-keys */
const NEW_VIDEO_CHOICE_LINES = {
  music_none: "Music: none.",
  music_track: "Music: the built-in track {track}.",
  music_ai: "Music: a track composed for this video - {mood}.",
  narration_none: "Narration: none.",
  narration_on: "Narration: yes.",
  narration_language: "Narration: yes, in {language}.",
  narration_voice: "Voice: {voice}.",
  captions_off: "Captions: none.",
  captions_file: "Captions: a subtitle file only, not drawn on the video.",
  captions_burned: "Captions: burned into the video.",
  length: "Length: about {seconds, plural, one {# second} other {# seconds}}.",
  pace_calm: "Pace: calm.",
  pace_dynamic: "Pace: dynamic.",
} as const;

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** The request's lines for the choices set, in the reader's language; a choice on "suggest" adds none. */
export function newVideoChoiceLines(choices: NewVideoChoices, t: Translate): string[] {
  const L = NEW_VIDEO_CHOICE_LINES;
  const lines: string[] = [];
  const mood = choices.musicMood.trim();
  if (choices.music === "none") lines.push(t(L.music_none));
  else if (choices.music === "track" && choices.musicTrack) lines.push(t(L.music_track, { track: choices.musicTrackName || choices.musicTrack }));
  else if (choices.music === "ai" && mood) lines.push(t(L.music_ai, { mood }));
  const language = choices.narrationLanguage.trim();
  const voice = choices.narrationVoice.trim();
  if (choices.narration === "none") lines.push(t(L.narration_none));
  else if (choices.narration === "on") {
    lines.push(language ? t(L.narration_language, { language }) : t(L.narration_on));
    if (voice) lines.push(t(L.narration_voice, { voice }));
  }
  if (choices.captions === "off") lines.push(t(L.captions_off));
  else if (choices.captions === "file") lines.push(t(L.captions_file));
  else if (choices.captions === "burned") lines.push(t(L.captions_burned));
  if (choices.seconds > 0) lines.push(t(L.length, { seconds: Math.round(choices.seconds) }));
  if (choices.pace === "calm") lines.push(t(L.pace_calm));
  else if (choices.pace === "dynamic") lines.push(t(L.pace_dynamic));
  return lines;
}

/** How many of the five options the person set (narration with a voice counts once, as one choice). */
export function newVideoChoicesSet(choices: NewVideoChoices): number {
  return [
    choices.music === "none" || (choices.music === "track" && !!choices.musicTrack) || (choices.music === "ai" && !!choices.musicMood.trim()),
    choices.narration !== "suggest",
    choices.captions !== "suggest",
    choices.seconds > 0,
    choices.pace !== "suggest",
  ].filter(Boolean).length;
}

/** What the spec holds of the choices at creation (a track composed by AI is the assistant's to start). */
export function newVideoSpecChoices(choices: NewVideoChoices): Record<string, unknown> {
  const spec: Record<string, unknown> = {};
  if (choices.music === "none") spec.music = { enabled: false };
  else if (choices.music === "track" && choices.musicTrack) spec.music = { track: choices.musicTrack };
  if (choices.narration === "none") spec.voiceover = { enabled: false };
  if (choices.captions === "off") spec.captions = { enabled: false };
  else if (choices.captions === "file") spec.captions = { enabled: true, burn_in: false };
  else if (choices.captions === "burned") spec.captions = { enabled: true, burn_in: true };
  if (choices.pace !== "suggest") spec.pace = choices.pace;
  return spec;
}

/** How a still moves while it is on screen. @i18n-keys */
export const MOTION_LABELS = {
  ken_burns: "Ken Burns",
  zoom_in: "Zoom in",
  zoom_out: "Zoom out",
  pan_left: "Pan left",
  pan_right: "Pan right",
  none: "Still",
} as const;

/** @i18n-keys */
export const FIT_LABELS = {
  cover: "Fill the frame",
  contain: "Fit inside the frame",
} as const;

/** The flat colour a screen insert keys out of its surface (`spec.InsertKey.color`). @i18n-keys */
export const INSERT_KEY_LABELS = {
  green: "Green screen",
  blue: "Blue screen",
  magenta: "Magenta screen",
} as const;

/** A clip scene's own sound (`spec.ClipScene.audio`), the automatic choice unset. @i18n-keys */
export const SCENE_SOUND_LABELS = {
  keep: "Keep it",
  off: "Off",
} as const;

/** The clips' own sound where a scene does not choose (`spec.VideoSpec.clip_sound`). @i18n-keys */
export const CLIP_SOUND_LABELS = {
  keep: "Keep every clip's sound",
  off: "Off: only the voice and the music",
} as const;

/**
 * Why a clip scene's own sound plays or not in the preview (`preview_sound.reason`,
 * `timeline.TimelinePlan.clip_sound`), said under its sound choice. @i18n-keys
 */
export const SCENE_SOUND_REASONS = {
  music: "Off: the video's music plays over this AI shot.",
  auto_kept: "Its sound plays, levelled with the voice and lowered under every sentence.",
  video_off: "Off: the video keeps only the voice and the music (Music and sound).",
  video_kept: "Kept: the video keeps every clip's sound (Music and sound).",
  silent: "This clip has no sound.",
} as const;

/** The words under a clip scene's sound choice, from what the preview planned; `null` when the scene chose. */
export function sceneSoundReason(sound: { plays: boolean; reason: string } | undefined): keyof typeof SCENE_SOUND_REASONS | null {
  if (!sound) return null;
  if (sound.reason === "music" || sound.reason === "silent") return sound.reason;
  if (sound.reason === "video") return sound.plays ? "video_kept" : "video_off";
  if (sound.reason === "auto") return "auto_kept";
  return null;
}

/** What a recording does with the moments nothing happens. @i18n-keys */
export const IDLE_MODE_LABELS = {
  compress: "Speed them up",
  cut: "Cut them out",
  off: "Keep them",
} as const;

/** @i18n-keys */
export const CAMERA_MODE_LABELS = {
  auto: "Follow the action",
  static: "Still",
  manual: "Keyframes",
} as const;

/** How the automatic camera moves (`timeline.CAMERA_PACES`). @i18n-keys */
export const CAMERA_PACE_LABELS = {
  calm: "Calm: smooth moves",
  dynamic: "Dynamic: quicker moves",
} as const;

/** Seconds of each pace's zoom in (`timeline.CAMERA_PACES`), what `move_seconds` overrides. */
export const CAMERA_ZOOM_SECONDS: Record<keyof typeof CAMERA_PACE_LABELS, number> = {
  calm: 1.1,
  dynamic: 0.8,
};

/** The actions an automatic camera zooms on. @i18n-keys */
export const ZOOM_ON_LABELS = {
  click: "Clicks",
  type: "Typing",
  select: "Selections",
  hover: "Hovers",
  highlight: "Highlights",
} as const;

/** @i18n-keys */
export const CURSOR_STYLE_LABELS = {
  arrow: "Arrow",
  dot: "Dot",
  halo: "Halo",
} as const;

/** @i18n-keys */
export const CLICK_EFFECT_LABELS = {
  ripple: "Ripple",
  pulse: "Pulse",
  none: "None",
} as const;

/** @i18n-keys */
export const FRAME_STYLE_LABELS = {
  window: "Browser window",
  none: "Full bleed",
} as const;

/** @i18n-keys */
export const WATERMARK_LABELS = {
  none: "None",
  top_left: "Top left",
  top_right: "Top right",
  bottom_left: "Bottom left",
  bottom_right: "Bottom right",
} as const;

/** @i18n-keys */
export const CAPTION_STYLE_LABELS = {
  pill: "Pill",
  bar: "Bar",
  outline: "Outline",
} as const;

/** @i18n-keys */
export const CAPTION_POSITION_LABELS = {
  bottom: "Bottom",
  top: "Top",
} as const;

/** @i18n-keys */
export const THEME_MODE_LABELS = {
  dark: "Dark",
  light: "Light",
} as const;

/** The options of a label table, worded by *t*. */
export function choicesOf<T extends string>(table: Record<T, string>, t: (key: string) => string) {
  return (Object.entries(table) as [T, string][]).map(([value, label]) => ({ value, label: t(label) }));
}

/** Whether a template's lock covers *path* (a lock on `captions` covers `captions.style`). */
export function isLockedBy(locked: readonly string[] | undefined, path: string): boolean {
  return !!locked?.some((entry) => path === entry || path.startsWith(`${entry}.`));
}

/** When a recording's or an AI shot's media expire, as the library says it. */
export function retentionDays(expiresAt: string | null | undefined, now = Date.now()): number | null {
  if (!expiresAt) return null;
  const ms = Date.parse(expiresAt) - now;
  return Number.isFinite(ms) ? Math.max(0, Math.ceil(ms / 86_400_000)) : null;
}

/** What a video template's lock keeps, by path; any other path shows as written. @i18n-keys */
const LOCK_LABELS = {
  "theme.palette": "Colours and logo",
  "theme.background": "Backdrop",
  "theme.watermark": "Watermark",
  "theme.mode": "Colour scheme",
  "theme.motion": "Motion",
  "theme.motion.personality": "Motion personality",
  "captions.style": "Caption style",
  "captions.position": "Caption position",
  "captions": "Captions",
  "default_transition": "Transitions",
  "voiceover.style": "Voice style",
  "voiceover.voice": "Voice",
  "scenes.title.style": "Title card style",
  "scenes.outro.style": "Closing card style",
  "scenes.recording.frame": "Browser window",
  "scenes.recording.cursor": "Cursor",
  "scenes.recording.cursor.style": "Cursor style",
  "scenes.recording.camera": "Camera",
} as const;

export function lockLabel(path: string): string | null {
  return (LOCK_LABELS as Record<string, string>)[path] ?? null;
}

/**
 * The built-in tracks' words, as the backend's `score.STYLES` writes
 * them (`test_video_studio_music.py` keeps the two equal): the library shows
 * them in the reader's language. @i18n-keys
 */
const MUSIC_TRACK_WORDS = {
  uplifting: "uplifting",
  calm: "calm",
  tense: "tense",
  inspiring: "inspiring",
  relaxed: "relaxed",
  energetic: "energetic",
  reflective: "reflective",
  "corporate electronic": "corporate electronic",
  "ambient tech": "ambient tech",
  "dark minimal tech": "dark minimal tech",
  cinematic: "cinematic",
  "lo-fi": "lo-fi",
  "upbeat pop electronic": "upbeat pop electronic",
  "minimal piano": "minimal piano",
} as const;

/** What each built-in track is, by its id: the backend's descriptions. @i18n-keys */
const MUSIC_TRACK_DESCRIPTIONS = {
  "bright-horizon":
    "Optimistic and clean: warm pads, a plucked arpeggio, a light four-on-the-floor in the lifts. Product launches, demos and explainers that should feel confident.",
  "deep-focus":
    "Calm and spacious: soft electric piano chords over a deep pad, a gentle pulse. Leaves the narration all the room: product walkthroughs and long explanations.",
  signal:
    "Dark and focused: a filtered sixteenth-note bass pulse, a low drone, ticking hats and impacts on the turns. Threat briefings, security research, incident stories.",
  ascent:
    "A cinematic build: ensemble strings, a piano ostinato, taiko drums and a big hit at the peak. Brand films, keynote openers and launch films that need scale.",
  "late-notes":
    "Warm and laid back: jazzy electric piano sevenths, a swung dusty beat and vinyl crackle. Social clips, tips and friendly explainers.",
  momentum:
    "Punchy and bright: offbeat chord stabs, a bouncing bass, claps and a driving kick. Social teasers, event recaps and launch countdowns.",
  "quiet-light":
    "Intimate and human: a felt piano's broken chords over a faint pad, no drums. Customer stories, testimonials and heartfelt brand moments.",
} as const;

export function musicTrackWord(word: string): string {
  return (MUSIC_TRACK_WORDS as Record<string, string>)[word] ?? word;
}

export function musicTrackDescription(id: string, fallback: string): string {
  return (MUSIC_TRACK_DESCRIPTIONS as Record<string, string>)[id] ?? fallback;
}

/** The backdrops a video draws behind its cards and recordings. @i18n-keys */
export const BACKGROUND_LABELS = {
  aurora: "Aurora",
  gradient: "Gradient",
  mesh: "Mesh",
  grid: "Grid",
  mosaic: "Mosaic",
  solid: "Solid",
} as const;

/** What a video template keeps every video to, as the rules panel shows it. */
export interface VideoRulesView {
  aspect_ratios: string[];
  locked: string[];
  intro: boolean;
  outro: boolean;
  background: string | null;
  purposes?: VideoTemplatePurpose[];
  /** The art direction of its AI shots. */
  shots?: VideoShotDirection | null;
  /** How its videos move (`theme.motion`, or its personality alone), or `null` for the platform's own. */
  motion?: VideoMotionStyle | string | null;
}

/** The rules of a video template from its stored `extra_metadata.video`, or `null` when it declares none. */
export function rulesFromMetadata(extraMetadata: unknown): VideoRulesView | null {
  const video = (extraMetadata as { video?: Record<string, unknown> } | null)?.video;
  if (!video || typeof video !== "object") return null;
  const strings = (value: unknown) => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : []);
  const required = (bookend: unknown) =>
    !!bookend && typeof bookend === "object" && (bookend as { required?: unknown }).required !== false;
  const theme = ((video.defaults as Record<string, unknown> | undefined)?.theme ?? {}) as Record<string, unknown>;
  const shots = video.shots && typeof video.shots === "object" ? (video.shots as Record<string, unknown>) : null;
  const motion = (theme.motion ?? null) as VideoMotionStyle | null;
  return {
    motion: motion && typeof motion === "object" ? motion : null,
    aspect_ratios: strings(video.aspect_ratios).length ? strings(video.aspect_ratios) : ["16:9", "9:16", "1:1", "4:5"],
    locked: strings(video.locked),
    intro: required(video.intro),
    outro: required(video.outro),
    background: typeof theme.background === "string" ? theme.background : null,
    purposes: strings(video.purposes).filter((p): p is VideoTemplatePurpose => p in TEMPLATE_PURPOSE_LABELS),
    shots: shots && { style: typeof shots.style === "string" ? shots.style : undefined, avoid: strings(shots.avoid) },
  };
}
