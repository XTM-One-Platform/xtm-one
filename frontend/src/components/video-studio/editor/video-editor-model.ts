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
 * The Video Studio editor's edits, as pure functions of the spec -
 * `video-editor-model.test.ts` (`yarn test:unit`).
 *
 * The editor edits the spec the author wrote (`project.spec`) and nothing
 * else: every change is a new spec the backend validates on save (the tools'
 * validator) and plans for the preview (the render's planner). Times on the
 * timeline are OUTPUT seconds; a recording's or a clip's trim is SOURCE
 * seconds - the planned scene's `time_map` turns one into the other, idle
 * compression and speed included, so a cut lands on the frame under the
 * playhead.
 */

export type SceneType = "title" | "statement" | "recording" | "slide" | "figure" | "clip" | "image" | "outro" | "motion";

export interface EditorScene {
  id?: string;
  type: SceneType;
  [key: string]: unknown;
}

export interface EditorSpec {
  title: string;
  scenes: EditorScene[];
  [key: string]: unknown;
}

/** A planned scene as the timeline reads it (`timeline.plan`). */
interface PlannedScene {
  id: string;
  start: number;
  end: number;
  time_map?: [number, number][];
}

/** The shortest still a scene may be (`spec.py`: `duration` >= 1). */
const MIN_SCENE_SECONDS = 1;
/** How close to a scene's edge a cut may land (s): a sliver is not a scene. */
const SPLIT_MARGIN = 0.2;
/** The planner's reading pace (`timeline.WORDS_PER_SECOND`). */
const WORDS_PER_SECOND = 2.6;

/** Seconds a still scene lasts when the author set none (`spec.py` defaults). */
export const DEFAULT_DURATION: Record<SceneType, number> = {
  title: 4,
  statement: 4,
  outro: 5,
  slide: 5,
  figure: 6,
  image: 4,
  clip: 4,
  recording: 10,
  motion: 5,
};

/** The built-in compositions of a motion scene (`spec.MOTION_COMPOSITIONS`), and the text each one needs. */
export const MOTION_COMPOSITIONS = {
  kinetic_title: "title",
  logo_reveal: null,
  stat_counter: "figure",
  list_build: "items",
  split_statement: "lines",
  quote: "title",
  constellation: "title",
} as const;
export type MotionComposition = keyof typeof MOTION_COMPOSITIONS;

/** The patch switching a motion scene to *composition*, its needed text filled from what the scene already says. */
export function motionCompositionPatch(scene: EditorScene, composition: MotionComposition, text: string): Record<string, unknown> {
  const patch: Record<string, unknown> = { composition };
  const needs = MOTION_COMPOSITIONS[composition];
  const said = typeof scene.title === "string" && scene.title ? scene.title : text;
  if (needs === "title" && !scene.title) patch.title = said;
  if (needs === "figure" && !scene.figure) patch.figure = "100%";
  if (needs === "items" && !(Array.isArray(scene.items) && scene.items.length)) patch.items = [said];
  if (needs === "lines" && !(Array.isArray(scene.lines) && scene.lines.length >= 2)) patch.lines = twoLines(said);
  return patch;
}

/** *text* as the two to four lines of a split statement: its own lines, else its words halved outside an `*accent*`. */
function twoLines(text: string): string[] {
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  if (lines.length >= 2) return lines.slice(0, 4);
  const words = text.trim().split(/\s+/);
  const cuts = words.map((_, i) => i).filter((i) => i > 0 && (words.slice(0, i).join(" ").match(/\*/g)?.length ?? 0) % 2 === 0);
  if (!cuts.length) return [text.trim(), text.trim()];
  const cut = cuts.reduce((best, i) => (Math.abs(i - words.length / 2) < Math.abs(best - words.length / 2) ? i : best));
  return [words.slice(0, cut).join(" "), words.slice(cut).join(" ")];
}

/** Seconds a figure's step stays on screen before the next one when the author set none (`spec.DEFAULT_FIGURE_DWELL`). */
export const DEFAULT_FIGURE_DWELL = 0.8;

/** The planner's estimate of a narration's length (`timeline.estimate_narration_seconds`). */
export function estimateNarrationSeconds(text: string | null | undefined, speed = 1): number {
  const words = (text ?? "").match(/[\p{L}\p{N}_]+/gu)?.length ?? 0;
  return words ? words / (WORDS_PER_SECOND * Math.max(speed, 0.5)) + 0.3 : 0;
}

/** `m:ss.t` (tenths), the timeline's clock. */
export function formatTimecode(seconds: number): string {
  const safe = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
  const minutes = Math.floor(safe / 60);
  const rest = safe - minutes * 60;
  const whole = Math.floor(rest);
  const tenths = Math.min(9, Math.floor((rest - whole) * 10));
  return `${minutes}:${String(whole).padStart(2, "0")}.${tenths}`;
}

/** The source time of output time *x* on a time map (the compositor's `mapTime`). */
export function mapTime(points: readonly [number, number][] | undefined, x: number): number {
  if (!points || points.length === 0) return x;
  if (x <= points[0][0]) return points[0][1];
  const last = points[points.length - 1];
  if (x >= last[0]) return last[1];
  let lo = 0;
  let hi = points.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] <= x) lo = mid;
    else hi = mid;
  }
  const a = points[lo];
  const b = points[hi];
  return b[0] === a[0] ? b[1] : a[1] + ((b[1] - a[1]) * (x - a[0])) / (b[0] - a[0]);
}

type Point = readonly [number, number];

/** The rect `[left, top, width, height]` a picture of `srcW` x `srcH` covers in a `w` x `h` box (`object-fit: cover`, the compositor's `coverRect`). */
export function coverRect(w: number, h: number, srcW: number, srcH: number): [number, number, number, number] {
  const s = Math.max(w / Math.max(srcW, 1), h / Math.max(srcH, 1));
  return [(w - srcW * s) / 2, (h - srcH * s) / 2, srcW * s, srcH * s];
}

/** The CSS `matrix3d` drawing a `w` x `h` box (`transform-origin: 0 0`) onto the quad `p`, in pixels (the compositor's `quadMatrix`). */
export function quadMatrix(p: readonly Point[], w: number, h: number): string {
  const [[x0, y0], [x1, y1], [x2, y2], [x3, y3]] = p;
  const dx1 = x1 - x2;
  const dx2 = x3 - x2;
  const dy1 = y1 - y2;
  const dy2 = y3 - y2;
  const sx = x0 - x1 + x2 - x3;
  const sy = y0 - y1 + y2 - y3;
  let g = 0;
  let k = 0;
  if (Math.abs(sx) > 1e-9 || Math.abs(sy) > 1e-9) {
    const den = dx1 * dy2 - dx2 * dy1;
    g = (sx * dy2 - dx2 * sy) / den;
    k = (dx1 * sy - sx * dy1) / den;
  }
  const a = (x1 - x0 + g * x1) / w;
  const b = (x3 - x0 + k * x3) / h;
  const d = (y1 - y0 + g * y1) / w;
  const e = (y3 - y0 + k * y3) / h;
  return `matrix3d(${a},${d},0,${g / w},${b},${e},0,${k / h},0,0,1,0,${x0},${y0},0,1)`;
}

/**
 * A screen insert's plane on footage drawn `w` x `h`, as the compositor's static preview places it
 * (`previewInsert`): the box its source is fitted in - the quad's mean side lengths, so `cover` crops and
 * `contain` letterboxes as on the screen - and the transform laying that box on the quad (0-1 across and down
 * the footage). `null` while the footage has no size.
 */
export function insertPlane(quad: readonly Point[], w: number, h: number): { width: number; height: number; transform: string } | null {
  if (quad.length !== 4 || !(w > 0 && h > 0)) return null;
  const p = quad.map(([x, y]) => [x * w, y * h] as const);
  const width = (Math.hypot(p[1][0] - p[0][0], p[1][1] - p[0][1]) + Math.hypot(p[2][0] - p[3][0], p[2][1] - p[3][1])) / 2;
  const height = (Math.hypot(p[3][0] - p[0][0], p[3][1] - p[0][1]) + Math.hypot(p[2][0] - p[1][0], p[2][1] - p[1][1])) / 2;
  if (!(width > 0 && height > 0)) return null;
  return { width, height, transform: quadMatrix(p, width, height) };
}

export interface HeldMedia {
  url: string;
  /** The file the URL plays (`preview_media_ids`), else the URL itself. */
  file: string;
}

/**
 * The URL each key of a plan's media is shown from: the one it was shown from while it plays the
 * same file and that URL has not failed (`lost`), else the plan's. A plan mints its URLs again every token
 * window, and a `<video>` handed another URL of the same file loads it again, blank until it has - the
 * stage holds its URLs the same way.
 */
export function holdMedia(
  held: Readonly<Record<string, HeldMedia>>,
  media: Readonly<Record<string, string>>,
  ids: Readonly<Record<string, string>>,
  lost: ReadonlySet<string>,
): Record<string, HeldMedia> {
  const next: Record<string, HeldMedia> = {};
  for (const [key, url] of Object.entries(media)) {
    const file = ids[key] ?? url;
    const before = held[key];
    next[key] = before && before.file === file && !lost.has(before.url) ? before : { url, file };
  }
  return next;
}

/** The index of the scene on screen at *t*: the last one started (a transition shows the incoming one). */
export function sceneIndexAt(scenes: readonly { start: number; end: number }[], t: number): number {
  let found = 0;
  for (let i = 0; i < scenes.length; i += 1) {
    if (scenes[i].start <= t + 1e-6) found = i;
    else break;
  }
  return found;
}

/** A scene id the spec does not use yet (`spec.SCENE_ID_PATTERN`). */
export function newSceneId(spec: EditorSpec, random: () => number = Math.random): string {
  const taken = new Set(spec.scenes.map((s) => s.id));
  for (;;) {
    const id = `s${Math.floor(random() * 36 ** 6).toString(36).padStart(6, "0")}`;
    if (!taken.has(id)) return id;
  }
}

/** Every scene with an id (the backend names the ones it gets without): the editor selects by id. */
export function ensureSceneIds(spec: EditorSpec): EditorSpec {
  if (spec.scenes.every((s) => s.id)) return spec;
  const next: EditorSpec = { ...spec, scenes: [...spec.scenes] };
  next.scenes = next.scenes.map((scene) => (scene.id ? scene : { ...scene, id: newSceneId(next) }));
  return next;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** *base* with *patch* merged in: objects merge, a `null` or `undefined` value removes the key. */
export function mergePatch<T extends Record<string, unknown>>(base: T, patch: Record<string, unknown>): T {
  const out: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(patch)) {
    if (value === null || value === undefined) delete out[key];
    else if (isPlainObject(value) && isPlainObject(out[key])) out[key] = mergePatch(out[key] as Record<string, unknown>, value);
    else out[key] = value;
  }
  return out as T;
}

export function sceneIndex(spec: EditorSpec, id: string): number {
  return spec.scenes.findIndex((s) => s.id === id);
}

export function updateScene(spec: EditorSpec, id: string, patch: Record<string, unknown>): EditorSpec {
  const index = sceneIndex(spec, id);
  if (index < 0) return spec;
  const scenes = [...spec.scenes];
  scenes[index] = mergePatch(scenes[index], patch);
  return { ...spec, scenes };
}

/** The spec's own fields (title, format, theme, voice-over...), merged the same way. */
export function updateVideo(spec: EditorSpec, patch: Record<string, unknown>): EditorSpec {
  const { scenes: _ignored, ...rest } = patch;
  return { ...mergePatch(spec, rest), scenes: spec.scenes };
}

/** The fields of a video's music that name what plays: one source at a time. */
const MUSIC_SOURCE_FIELDS = ["track", "music_id", "file_id", "prompt", "model", "provider_id"] as const;

/** The patch making *source* the video's music: its levels kept, whatever played before dropped. */
export function musicSourcePatch(
  music: Record<string, unknown> | null | undefined,
  source: { track: string } | { music_id: string } | { file_id: string },
): Record<string, unknown> {
  const dropped = Object.keys(music ?? {})
    .filter((key) => key === "enabled" || (MUSIC_SOURCE_FIELDS as readonly string[]).includes(key))
    .map((key) => [key, null]);
  return { music: { ...Object.fromEntries(dropped), ...source } };
}

/** The gain of the music's ducking envelope at *t* (`[seconds, gain]` points, linear between them). */
export function duckGainAt(points: readonly (readonly [number, number])[] | null | undefined, t: number): number {
  if (!points || points.length === 0) return 1;
  if (t <= points[0][0]) return points[0][1];
  for (let i = 1; i < points.length; i += 1) {
    const [t1, g1] = points[i];
    if (t <= t1) {
      const [t0, g0] = points[i - 1];
      return t1 > t0 ? g0 + ((g1 - g0) * (t - t0)) / (t1 - t0) : g1;
    }
  }
  return points[points.length - 1][1];
}

/** A narration the preview can play: where it starts and how long it lasts, in output seconds. */
export interface SpokenLine {
  scene_id: string;
  start: number;
  seconds: number;
}

/** How far into each spoken line `t` is: `null` for a line not speaking at `t` (narrations never overlap). */
export function spokenOffsets(lines: readonly SpokenLine[], t: number): Map<string, number | null> {
  return new Map(lines.map((line) => [line.scene_id, t >= line.start && t < line.start + line.seconds ? t - line.start : null]));
}

export function insertScene(spec: EditorSpec, scene: EditorScene, at = spec.scenes.length): EditorSpec {
  const scenes = [...spec.scenes];
  const withId = scene.id ? scene : { ...scene, id: newSceneId(spec) };
  scenes.splice(Math.max(0, Math.min(at, scenes.length)), 0, withId);
  return { ...spec, scenes };
}

/** Without the scene - a video keeps at least one (`spec.py`: `scenes` min 1). */
export function removeScene(spec: EditorSpec, id: string): EditorSpec {
  if (spec.scenes.length <= 1) return spec;
  return { ...spec, scenes: spec.scenes.filter((s) => s.id !== id) };
}

interface PlannedFigure {
  start: number;
  end: number;
  /** A figure scene, however it plays (its still, or its build as a clip: `timeline.plan`). */
  figure?: unknown;
  /** Seconds into a figure's scene from which every step is in, the whole figure framed: set when its build plays. */
  preview_figure_whole?: unknown;
}

/** What a planned scene is, as its author wrote it: a figure playing its build is planned as a clip. */
export function plannedType<T extends string>(scene: { type: T; figure?: unknown }): T | "figure" {
  return scene.figure === true ? "figure" : scene.type;
}

/** Whether a planned scene is a figure playing its build: what the timeline skims. */
export function playsBuild(scene: PlannedFigure): boolean {
  return scene.figure === true && typeof scene.preview_figure_whole === "number";
}

/**
 * Where selecting the scene at *index* puts the playhead: its start - a
 * figure whose build plays, the moment it is whole, before the next
 * scene starts coming in.
 */
export function sceneLanding(scenes: readonly PlannedFigure[], index: number): number {
  const scene = scenes[index];
  if (!playsBuild(scene)) return scene.start + 0.01;
  const next = scenes[index + 1]?.start ?? scene.end;
  return Math.max(scene.start + 0.01, Math.min(scene.start + (scene.preview_figure_whole as number), next - 0.05));
}

/** The scene a jump of *delta* scenes from *t* lands on; back from inside a scene goes to its own start first. */
export function sceneJump(scenes: readonly { start: number; end: number }[], t: number, delta: -1 | 1): number {
  if (scenes.length === 0) return 0;
  const index = sceneIndexAt(scenes, t);
  if (delta < 0 && t - scenes[index].start > 0.3) return index;
  return Math.max(0, Math.min(scenes.length - 1, index + delta));
}

/** *newId* is the copy's id when the caller must know it before the edit lands. */
export function duplicateScene(spec: EditorSpec, id: string, newId?: string): { spec: EditorSpec; id: string | null } {
  const index = sceneIndex(spec, id);
  if (index < 0) return { spec, id: null };
  const copy: EditorScene = { ...structuredClone(spec.scenes[index]), id: newId ?? newSceneId(spec) };
  return { spec: insertScene(spec, copy, index + 1), id: copy.id ?? null };
}

/** What a template's opening (closing) card says - never how it looks, which the template keeps dressing. */
const BOOKEND_WORDS = ["title", "subtitle", "kicker", "cta", "url", "narration"] as const;

/**
 * The video's own copy of the opening or closing card its template adds,
 * placed first (last) where it replaces the template's (`templates.apply_template`).
 * Null for any other scene.
 */
export function customizeBookend(spec: EditorSpec, planned: EditorScene, newId: string): EditorSpec | null {
  if (planned.type !== "title" && planned.type !== "outro") return null;
  const scene: EditorScene = { id: newId, type: planned.type };
  for (const key of BOOKEND_WORDS) {
    if (typeof planned[key] === "string" && planned[key]) scene[key] = planned[key];
  }
  return insertScene(spec, scene, planned.type === "title" ? 0 : spec.scenes.length);
}

export function moveScene(spec: EditorSpec, id: string, to: number): EditorSpec {
  const from = sceneIndex(spec, id);
  if (from < 0) return spec;
  const scenes = [...spec.scenes];
  const [scene] = scenes.splice(from, 1);
  scenes.splice(Math.max(0, Math.min(to, scenes.length)), 0, scene);
  return { ...spec, scenes };
}

type TimedOverlay = { start?: number; at_step?: number; [key: string]: unknown };

/** The overlays of a scene split at *t*: the first part keeps those that start before, the second the rest, re-timed. */
function splitOverlays(overlays: unknown, t: number): [TimedOverlay[], TimedOverlay[]] {
  const list = Array.isArray(overlays) ? (overlays as TimedOverlay[]) : [];
  const first: TimedOverlay[] = [];
  const second: TimedOverlay[] = [];
  for (const overlay of list) {
    if (overlay.at_step !== undefined || (overlay.start ?? 0) < t) first.push(overlay);
    else second.push({ ...overlay, start: Math.round(((overlay.start ?? 0) - t) * 100) / 100 });
  }
  return [first, second];
}

const round2 = (value: number) => Math.round(value * 100) / 100;

/**
 * The spec with scene *id* cut in two at *t* seconds into it (output time),
 * or `null` when *t* is too close to an edge. The first part keeps the
 * narration, the transition in and the overlays that start before the cut;
 * the second starts on a cut and takes the rest. A recording or a clip is cut
 * in its SOURCE: the planned `time_map` says which source second is under
 * the playhead.
 */
export function splitScene(
  spec: EditorSpec,
  id: string,
  t: number,
  planned: PlannedScene | undefined,
  newId?: string,
): { spec: EditorSpec; id: string } | null {
  const index = sceneIndex(spec, id);
  if (index < 0 || !planned) return null;
  const scene = spec.scenes[index];
  const length = planned.end - planned.start;
  if (t < SPLIT_MARGIN || t > length - SPLIT_MARGIN) return null;
  const [overlaysA, overlaysB] = splitOverlays(scene.overlays, t);
  const secondId = newId ?? newSceneId(spec);
  let first: EditorScene;
  let second: EditorScene;
  if (scene.type === "recording" || scene.type === "clip") {
    const trimStart = typeof scene.trim_start === "number" ? scene.trim_start : 0;
    const source = round2(mapTime(planned.time_map, t));
    const trimEnd = typeof scene.trim_end === "number" ? scene.trim_end : null;
    if (source <= trimStart + SPLIT_MARGIN / 2 || (trimEnd !== null && source >= trimEnd - SPLIT_MARGIN / 2)) return null;
    first = { ...scene, trim_end: source, overlays: overlaysA };
    delete first.hold_end;
    second = { ...structuredClone(scene), id: secondId, trim_start: source, overlays: overlaysB };
    delete second.highlights;
  } else {
    if (t < MIN_SCENE_SECONDS || length - t < MIN_SCENE_SECONDS) return null;
    first = { ...scene, duration: round2(t), overlays: overlaysA };
    second = { ...structuredClone(scene), id: secondId, duration: round2(length - t), overlays: overlaysB };
  }
  delete second.narration;
  delete second.narration_delay;
  second.transition = { type: "cut" };
  if (overlaysA.length === 0) delete first.overlays;
  if (overlaysB.length === 0) delete second.overlays;
  const scenes = [...spec.scenes];
  scenes.splice(index, 1, first, second);
  return { spec: { ...spec, scenes }, id: secondId };
}

/**
 * A new scene of *type*, ready to edit (the fields the spec requires filled); a statement's *text* is its first
 * line, a motion scene's the title of its kinetic title.
 */
export function blankScene(type: "title" | "outro" | "statement" | "motion", text: string): EditorScene {
  if (type === "statement") return { type, lines: [text], duration: DEFAULT_DURATION.statement };
  if (type === "motion") return { type, composition: "kinetic_title", title: text, duration: DEFAULT_DURATION.motion };
  return { type, title: text, duration: DEFAULT_DURATION[type] };
}

/** A statement's lines as the editor writes them, one per line of text (`spec.StatementScene`: 1-4). */
export function statementLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 4);
}

/** The AI shot a scene plays (a clip scene's, or the one behind a statement), or `null`. */
export function sceneClipId(scene: EditorScene | undefined): string | null {
  if (!scene) return null;
  if (scene.type === "clip" && typeof scene.clip_id === "string") return scene.clip_id;
  const backdrop = scene.backdrop as { clip_id?: unknown } | undefined;
  if (scene.type === "statement" && typeof backdrop?.clip_id === "string") return backdrop.clip_id;
  return null;
}

/** The recordings and AI shots the video's scenes use (`spec.referenced_ids`'s `take` / `clip`). */
export function videoMedia(spec: EditorSpec | null | undefined): { takes: Set<string>; clips: Set<string> } {
  const takes = new Set<string>();
  const clips = new Set<string>();
  for (const scene of spec?.scenes ?? []) {
    if (scene.type === "recording" && typeof scene.take_id === "string") takes.add(scene.take_id);
    if (scene.type === "clip" && typeof scene.clip_id === "string") clips.add(scene.clip_id);
    for (const key of ["backdrop", "animate"] as const) {
      const nested = scene[key] as { clip_id?: unknown } | undefined;
      if (nested && typeof nested === "object" && typeof nested.clip_id === "string") clips.add(nested.clip_id);
    }
  }
  return { takes, clips };
}

// ── Undo / redo ───────────────────────────────────────────────────────────

export interface History<T> {
  past: T[];
  present: T;
  future: T[];
}

/** How many steps back the editor keeps. */
const HISTORY_LIMIT = 100;

export function historyInit<T>(present: T): History<T> {
  return { past: [], present, future: [] };
}

/** A new present; *replace* folds it into the current step (a run of keystrokes is one undo). */
export function historyPush<T>(history: History<T>, present: T, replace = false): History<T> {
  if (Object.is(present, history.present)) return history;
  if (replace) return { ...history, present, future: [] };
  return { past: [...history.past, history.present].slice(-HISTORY_LIMIT), present, future: [] };
}

export function historyUndo<T>(history: History<T>): History<T> {
  if (history.past.length === 0) return history;
  const present = history.past[history.past.length - 1];
  return { past: history.past.slice(0, -1), present, future: [history.present, ...history.future] };
}

export function historyRedo<T>(history: History<T>): History<T> {
  if (history.future.length === 0) return history;
  const [present, ...future] = history.future;
  return { past: [...history.past, history.present], present, future };
}
