/**
 * The Video Studio editor's edits - `yarn test:unit`.
 *
 * A cut lands on the frame under the playhead: a recording is cut in its
 * source through the planned time map (idle compression and speed included),
 * a still is cut by duration; the second part starts on a cut and the
 * narration stays with the first.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  VIDEO_KIND_EXAMPLES,
  VIDEO_KIND_GROUPS,
  VIDEO_KIND_LABELS,
  VIDEO_KIND_REQUESTS,
  VIDEO_OPEN_LABELS,
  exampleBrief,
  isTemplatePurpose,
  NEW_VIDEO_CHOICES_UNSET,
  newVideoChoiceLines,
  newVideoChoicesSet,
  newVideoRequest,
  newVideoSpecChoices,
  sceneSoundReason,
  templatesForKind,
  videoOpenAction,
} from "../video-format.ts";
import {
  blankScene,
  coverRect,
  customizeBookend,
  duckGainAt,
  duplicateScene,
  ensureSceneIds,
  estimateNarrationSeconds,
  formatTimecode,
  historyInit,
  historyPush,
  historyRedo,
  historyUndo,
  holdMedia,
  insertPlane,
  mapTime,
  mergePatch,
  motionCompositionPatch,
  moveScene,
  musicSourcePatch,
  plannedType,
  playsBuild,
  quadMatrix,
  removeScene,
  sceneClipId,
  sceneIndexAt,
  sceneJump,
  sceneLanding,
  splitScene,
  spokenOffsets,
  statementLines,
  updateScene,
  updateVideo,
  videoMedia,
  type EditorSpec,
} from "./video-editor-model.ts";

const spec = (): EditorSpec => ({
  title: "Demo",
  scenes: [
    { id: "a", type: "title", title: "Hello", duration: 4, narration: "Welcome to the demo." },
    {
      id: "b",
      type: "recording",
      take_id: "00000000-0000-0000-0000-000000000001",
      trim_start: 2,
      narration: "Here is the product.",
      hold_end: 1,
      highlights: [{ at_step: 1 }],
      overlays: [
        { type: "callout", text: "First", start: 1 },
        { type: "callout", text: "Second", start: 6 },
      ],
    },
    { id: "c", type: "outro", title: "Thanks" },
  ],
});

test("a video's media are the recordings and shots its scenes use, backdrops and animated stills included", () => {
  const media = videoMedia({
    title: "x",
    scenes: [
      { id: "a", type: "recording", take_id: "t1" },
      { id: "b", type: "clip", clip_id: "c1" },
      { id: "c", type: "statement", lines: ["Hi"], backdrop: { clip_id: "c2" } },
      { id: "d", type: "image", file_id: "f", animate: { clip_id: "c3" } },
      { id: "e", type: "clip", file_id: "uploaded" },
    ],
  });
  assert.deepEqual([...media.takes], ["t1"]);
  assert.deepEqual([...media.clips].sort(), ["c1", "c2", "c3"]);
  assert.deepEqual(videoMedia(null), { takes: new Set(), clips: new Set() });
});

test("the clock reads minutes, seconds and tenths", () => {
  assert.equal(formatTimecode(0), "0:00.0");
  assert.equal(formatTimecode(65.37), "1:05.3");
  assert.equal(formatTimecode(-3), "0:00.0");
});

test("a time map turns output seconds into source seconds", () => {
  const points: [number, number][] = [[0, 2], [4, 6], [5, 16], [9, 20]];
  assert.equal(mapTime(points, 2), 4);
  // An idle stretch compressed tenfold: one output second covers ten source seconds.
  assert.equal(mapTime(points, 4.5), 11);
  assert.equal(mapTime(points, 99), 20);
  assert.equal(mapTime(undefined, 3), 3);
});

/** Where a CSS `matrix3d` (column-major, `transform-origin: 0 0`) puts the point (x, y) of its box. */
function project(matrix: string, x: number, y: number): [number, number] {
  const m = (matrix.match(/^matrix3d\((.*)\)$/)?.[1] ?? "").split(",").map(Number);
  const w = m[3] * x + m[7] * y + m[15];
  return [(m[0] * x + m[4] * y + m[12]) / w, (m[1] * x + m[5] * y + m[13]) / w];
}

function assertNear(actual: [number, number], expected: [number, number]) {
  assert.ok(Math.abs(actual[0] - expected[0]) < 1e-6 && Math.abs(actual[1] - expected[1]) < 1e-6, `${actual} is not ${expected}`);
}

test("a picture covering a box overflows it on the axis it is longer on, centred", () => {
  // A 16:9 poster in a block wider than it: cut top and bottom.
  assert.deepEqual(coverRect(320, 90, 1280, 720), [0, -45, 320, 180]);
  // In a narrow block: cut left and right.
  assert.deepEqual(coverRect(80, 90, 1280, 720), [-40, 0, 160, 90]);
});

test("a screen insert's box lands corner on corner on its quad, under perspective", () => {
  const quad: [number, number][] = [[0.1, 0.2], [0.55, 0.1], [0.6, 0.9], [0.05, 0.8]];
  const plane = insertPlane(quad, 200, 100);
  assert.ok(plane);
  assertNear(project(plane.transform, 0, 0), [20, 20]);
  assertNear(project(plane.transform, plane.width, 0), [110, 10]);
  assertNear(project(plane.transform, plane.width, plane.height), [120, 90]);
  assertNear(project(plane.transform, 0, plane.height), [10, 80]);
  // The box is the quad's mean side lengths: a source is fitted to the surface's own proportions.
  const top = Math.hypot(90, -10);
  const bottom = Math.hypot(110, 10);
  assert.ok(Math.abs(plane.width - (top + bottom) / 2) < 1e-9);
});

test("a rectangle needs no perspective, and footage with no size has no plane yet", () => {
  const matrix = quadMatrix([[10, 20], [50, 20], [50, 40], [10, 40]], 40, 20);
  assert.equal(matrix, "matrix3d(1,0,0,0,0,1,0,0,0,0,1,0,10,20,0,1)");
  assert.equal(insertPlane([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], 0, 90), null);
});

test("a thumbnail keeps the URL it shows while it plays the same file, and takes the plan's for another file or once it failed", () => {
  const first = holdMedia({}, { "poster/s1": "/p?t=1", "insert/s1.0": "/v?t=1" }, { "poster/s1": "P", "insert/s1.0": "V" }, new Set());
  // The next token window: the same files under newer tokens.
  const again = holdMedia(first, { "poster/s1": "/p?t=2", "insert/s1.0": "/v?t=2" }, { "poster/s1": "P", "insert/s1.0": "V" }, new Set());
  assert.equal(again["poster/s1"].url, "/p?t=1");
  assert.equal(again["insert/s1.0"].url, "/v?t=1");
  // Another take under the key, a URL that stopped answering, a key the plan dropped.
  const later = holdMedia(again, { "poster/s1": "/p2?t=2", "insert/s1.0": "/v?t=2" }, { "poster/s1": "P2", "insert/s1.0": "V" }, new Set(["/v?t=1"]));
  assert.deepEqual(later, { "poster/s1": { url: "/p2?t=2", file: "P2" }, "insert/s1.0": { url: "/v?t=2", file: "V" } });
  assert.deepEqual(holdMedia(later, {}, {}, new Set()), {});
  // Without file ids, a key is held by its URL alone.
  assert.equal(holdMedia(first, { logo: "/l?t=2" }, {}, new Set()).logo.url, "/l?t=2");
});

test("the scene on screen is the last one started", () => {
  const planned = [{ start: 0, end: 4 }, { start: 3.4, end: 10 }, { start: 10, end: 15 }];
  assert.equal(sceneIndexAt(planned, 0), 0);
  assert.equal(sceneIndexAt(planned, 3.5), 1);
  assert.equal(sceneIndexAt(planned, 14), 2);
});

test("a scene jump goes back to the scene's own start first", () => {
  const planned = [{ start: 0, end: 4 }, { start: 4, end: 10 }, { start: 10, end: 15 }];
  assert.equal(sceneJump(planned, 6, -1), 1);
  assert.equal(sceneJump(planned, 4.1, -1), 0);
  assert.equal(sceneJump(planned, 6, 1), 2);
  assert.equal(sceneJump(planned, 14, 1), 2);
  assert.equal(sceneJump([], 3, 1), 0);
});

test("selecting a figure whose build plays lands on it built in whole, before the next scene comes in", () => {
  const planned = [
    { type: "title", start: 0, end: 4 },
    { type: "clip", figure: true, preview_figure_whole: 12.6, start: 3.4, end: 20.4 },
    { type: "clip", figure: true, preview_figure_whole: 9, start: 19.8, end: 26 },
    { type: "image", figure: true, start: 26, end: 32 },
  ];
  assert.equal(sceneLanding(planned, 0), 0.01);
  assert.equal(sceneLanding(planned, 1), 3.4 + 12.6);
  // Whole only once the next scene is already coming in: just before it.
  assert.ok(Math.abs(sceneLanding(planned, 2) - (26 - 0.05)) < 1e-9);
  // A figure still showing its still (no build yet) lands on its start, like any scene.
  assert.equal(sceneLanding(planned, 3), 26.01);
  assert.deepEqual(planned.map(playsBuild), [false, true, true, false]);
  // Planned as a clip, it is still a figure for the author.
  assert.deepEqual(planned.map((scene) => plannedType(scene)), ["title", "figure", "figure", "figure"]);
});

test("a recording is cut in its source, under the playhead", () => {
  const planned = { id: "b", start: 4, end: 14, time_map: [[0, 2], [4, 6], [5, 16], [10, 21]] as [number, number][] };
  const result = splitScene(spec(), "b", 4.5, planned);
  assert.ok(result);
  const [, first, second] = result.spec.scenes;
  assert.equal(first.trim_end, 11);
  assert.equal(second.trim_start, 11);
  assert.equal(second.id, result.id);
  assert.notEqual(second.id, "b");
  // The narration and the transition in stay with the first part, which no longer holds its end.
  assert.equal(first.narration, "Here is the product.");
  assert.equal(second.narration, undefined);
  assert.deepEqual(second.transition, { type: "cut" });
  assert.equal(first.hold_end, undefined);
  // The overlays follow their time; the second part's are re-timed from the cut.
  assert.deepEqual((first.overlays as { text: string }[]).map((o) => o.text), ["First"]);
  assert.deepEqual(second.overlays, [{ type: "callout", text: "Second", start: 1.5 }]);
  assert.equal(second.highlights, undefined);
});

test("a still is cut by duration, and never into a sliver", () => {
  const planned = { id: "a", start: 0, end: 4 };
  const result = splitScene(spec(), "a", 1.5, planned);
  assert.ok(result);
  assert.equal(result.spec.scenes[0].duration, 1.5);
  assert.equal(result.spec.scenes[1].duration, 2.5);
  assert.equal(splitScene(spec(), "a", 0.5, planned), null);
  assert.equal(splitScene(spec(), "a", 3.9, planned), null);
  assert.equal(splitScene(spec(), "missing", 2, planned), null);
});

test("scenes are duplicated, moved and removed by id, and a video keeps one", () => {
  const dup = duplicateScene(spec(), "a");
  assert.equal(dup.spec.scenes.length, 4);
  assert.equal(dup.spec.scenes[1].id, dup.id);
  assert.equal(dup.spec.scenes[1].title, "Hello");
  assert.deepEqual(moveScene(spec(), "c", 0).scenes.map((s) => s.id), ["c", "a", "b"]);
  assert.deepEqual(removeScene(spec(), "b").scenes.map((s) => s.id), ["a", "c"]);
  const single: EditorSpec = { title: "x", scenes: [{ id: "z", type: "title", title: "Only" }] };
  assert.equal(removeScene(single, "z"), single);
});

test("a template's card becomes the video's own: its words, placed where it replaces the template's", () => {
  const authored: EditorSpec = { title: "x", scenes: [{ id: "r", type: "recording", take_id: "t" }] };
  const closing = { id: "outro", type: "outro" as const, title: "Uncover threats.\n*Take action.*", url: "filigran.io", style: "aurora", duration: 5 };
  const withClosing = customizeBookend(authored, closing, "own");
  assert.ok(withClosing);
  // The words only: the template keeps dressing the copy with its look.
  assert.deepEqual(withClosing.scenes[withClosing.scenes.length - 1], { id: "own", type: "outro", title: "Uncover threats.\n*Take action.*", url: "filigran.io" });
  const opening = { id: "intro", type: "title" as const, title: "Demo", kicker: "Filigran", subtitle: "" };
  const withOpening = customizeBookend(authored, opening, "first");
  assert.deepEqual(withOpening?.scenes[0], { id: "first", type: "title", title: "Demo", kicker: "Filigran" });
  assert.equal(customizeBookend(authored, { id: "r2", type: "recording" }, "nope"), null);
});

test("a patch merges objects and a null removes a field", () => {
  const merged = mergePatch({ camera: { mode: "auto", max_zoom: 2 }, speed: 2 }, { camera: { mode: "static" }, speed: null });
  assert.deepEqual(merged, { camera: { mode: "static", max_zoom: 2 } });
  const edited = updateScene(spec(), "a", { title: "Hi", narration: null });
  assert.equal(edited.scenes[0].title, "Hi");
  assert.equal(edited.scenes[0].narration, undefined);
  const video = updateVideo(spec(), { title: "Renamed", captions: { style: "bar" }, scenes: [] });
  assert.equal(video.title, "Renamed");
  assert.equal(video.scenes.length, 3);
});

test("scenes without an id get one", () => {
  const filled = ensureSceneIds({ title: "x", scenes: [{ type: "title", title: "A" }, { id: "k", type: "outro", title: "B" }] });
  assert.match(String(filled.scenes[0].id), /^s[0-9a-z]{6}$/);
  assert.equal(filled.scenes[1].id, "k");
});

test("the narration is paced like the planner", () => {
  assert.equal(estimateNarrationSeconds(""), 0);
  assert.ok(Math.abs(estimateNarrationSeconds("one two three four five six seven eight nine ten one two three") - 5.3) < 0.01);
});

test("undo and redo walk the edits; a run of keystrokes is one step", () => {
  let history = historyInit("a");
  history = historyPush(history, "ab");
  history = historyPush(history, "abc", true);
  history = historyPush(history, "abcd");
  assert.deepEqual(history.past, ["a", "abc"]);
  history = historyUndo(history);
  assert.equal(history.present, "abc");
  history = historyUndo(history);
  assert.equal(history.present, "a");
  history = historyRedo(history);
  assert.equal(history.present, "abc");
  history = historyPush(history, "x");
  assert.deepEqual(history.future, []);
});

test("a statement is written one line per line of text", () => {
  assert.deepEqual(blankScene("statement", "Your *message*"), { type: "statement", lines: ["Your *message*"], duration: 4 });
  assert.deepEqual(statementLines("Too much *noise*.\n\n  Not enough *context*.  \nthree\nfour\nfive"), [
    "Too much *noise*.",
    "Not enough *context*.",
    "three",
    "four",
  ]);
});

test("a motion scene starts as a kinetic title and keeps its words across compositions", () => {
  const scene = blankScene("motion", "Your *message* here");
  assert.deepEqual(scene, { type: "motion", composition: "kinetic_title", title: "Your *message* here", duration: 5 });
  assert.deepEqual(motionCompositionPatch(scene, "quote", "x"), { composition: "quote" });
  assert.deepEqual(motionCompositionPatch(scene, "list_build", "x"), { composition: "list_build", items: ["Your *message* here"] });
  assert.deepEqual(motionCompositionPatch(scene, "stat_counter", "x"), { composition: "stat_counter", figure: "100%" });
  assert.deepEqual(motionCompositionPatch(scene, "logo_reveal", "x"), { composition: "logo_reveal" });
  // A split statement halves the words, never inside an accent.
  assert.deepEqual(motionCompositionPatch(scene, "split_statement", "x").lines, ["Your", "*message* here"]);
  assert.deepEqual(motionCompositionPatch({ type: "motion", title: "One\nTwo\nThree" }, "split_statement", "x").lines, ["One", "Two", "Three"]);
  assert.deepEqual(motionCompositionPatch({ type: "motion", title: "*All accent*" }, "split_statement", "x").lines, ["*All accent*", "*All accent*"]);
  // What the scene already says is kept.
  assert.deepEqual(motionCompositionPatch({ type: "motion", title: "A", items: ["b"] }, "list_build", "x"), { composition: "list_build" });
  assert.deepEqual(motionCompositionPatch({ type: "motion" }, "kinetic_title", "Fallback"), { composition: "kinetic_title", title: "Fallback" });
});

test("the AI shot a scene shows: a clip's, or the one behind a statement", () => {
  assert.equal(sceneClipId({ type: "clip", clip_id: "c1" }), "c1");
  assert.equal(sceneClipId({ type: "statement", lines: ["x"], backdrop: { clip_id: "c2" } }), "c2");
  assert.equal(sceneClipId({ type: "statement", lines: ["x"], backdrop: { file_id: "f" } }), null);
  assert.equal(sceneClipId({ type: "clip", file_id: "f" }), null);
  assert.equal(sceneClipId(undefined), null);
});

test("a kind of video suggests the templates made for it, built-ins first, and opens the request", () => {
  const rows = [
    { id: "mine", purposes: ["brand_film"], is_builtin: false },
    { id: "demo", purposes: ["product_demo"], is_builtin: true },
    { id: "film", purposes: ["brand_film", "launch"], is_builtin: true },
  ];
  assert.deepEqual(templatesForKind(rows, "brand_film").map((r) => r.id), ["film", "mine", "demo"]);
  assert.deepEqual(templatesForKind(rows, null).map((r) => r.id), ["mine", "demo", "film"]);
  assert.equal(newVideoRequest("Make this video a brand film:", "  Two minutes about us  "), "Make this video a brand film:\n\nTwo minutes about us");
  assert.equal(newVideoRequest(null, "Cats"), "Cats");
});

test("new video: a choice left to the assistant says and sets nothing; a choice set is one line and, where it can, on the spec", () => {
  const t = (id: string, values?: Record<string, string | number>) =>
    Object.entries(values ?? {}).reduce((text, [key, value]) => text.replace(`{${key}}`, String(value)), id.replace(/\{seconds, plural.*\}\}/, "{seconds} seconds"));
  assert.deepEqual(newVideoChoiceLines(NEW_VIDEO_CHOICES_UNSET, t), []);
  assert.deepEqual(newVideoSpecChoices(NEW_VIDEO_CHOICES_UNSET), {});
  assert.equal(newVideoRequest("Make this video a brand film:", "Us", []), "Make this video a brand film:\n\nUs");

  const none = { ...NEW_VIDEO_CHOICES_UNSET, music: "none" as const, narration: "none" as const, captions: "off" as const };
  assert.deepEqual(newVideoChoiceLines(none, t), ["Music: none.", "Narration: none.", "Captions: none."]);
  assert.deepEqual(newVideoSpecChoices(none), { music: { enabled: false }, voiceover: { enabled: false }, captions: { enabled: false } });

  const set = {
    ...NEW_VIDEO_CHOICES_UNSET,
    music: "track" as const,
    musicTrack: "deep-focus",
    musicTrackName: "Deep Focus",
    narration: "on" as const,
    narrationLanguage: "French",
    narrationVoice: "warm",
    captions: "file" as const,
    seconds: 45,
    pace: "dynamic" as const,
  };
  const lines = newVideoChoiceLines(set, t);
  assert.deepEqual(lines, [
    "Music: the built-in track Deep Focus.",
    "Narration: yes, in French.",
    "Voice: warm.",
    "Captions: a subtitle file only, not drawn on the video.",
    "Length: about 45 seconds.",
    "Pace: dynamic.",
  ]);
  assert.deepEqual(newVideoSpecChoices(set), { music: { track: "deep-focus" }, captions: { enabled: true, burn_in: false }, pace: "dynamic" });
  assert.equal(newVideoRequest(null, "Cats", lines.slice(0, 1)), "Cats\n\nMusic: the built-in track Deep Focus.");

  // A track composed by AI is said with its mood and left to the assistant to start; without a mood it says nothing.
  const ai = { ...NEW_VIDEO_CHOICES_UNSET, music: "ai" as const, musicMood: "warm piano", captions: "burned" as const, pace: "calm" as const };
  assert.deepEqual(newVideoChoiceLines(ai, t), ["Music: a track composed for this video - warm piano.", "Captions: burned into the video.", "Pace: calm."]);
  assert.deepEqual(newVideoSpecChoices(ai), { captions: { enabled: true, burn_in: true }, pace: "calm" });
  assert.deepEqual(newVideoChoiceLines({ ...ai, musicMood: " " }, t).length, 2);

  // The summary counts choices, not request lines: narration with a voice is one choice (UX #11540 r1 A1).
  assert.equal(newVideoChoicesSet(NEW_VIDEO_CHOICES_UNSET), 0);
  assert.equal(lines.length, 6);
  assert.equal(newVideoChoicesSet(set), 5);
  assert.equal(newVideoChoicesSet({ ...ai, musicMood: "" }), 2);
});

test("every kind of video opens its request, and a creative one has no template made for it", () => {
  const kinds = VIDEO_KIND_GROUPS.flatMap((group) => group.kinds);
  assert.equal(new Set(kinds).size, kinds.length);
  for (const kind of kinds) {
    assert.ok(VIDEO_KIND_LABELS[kind] && VIDEO_KIND_REQUESTS[kind] && VIDEO_KIND_EXAMPLES[kind], kind);
  }
  assert.deepEqual(VIDEO_KIND_GROUPS.find((group) => group.id === "creative")?.kinds.filter(isTemplatePurpose), []);
  assert.ok(VIDEO_KIND_GROUPS.find((group) => group.id === "work")?.kinds.every(isTemplatePurpose));
  const rows = [{ id: "demo", purposes: ["product_demo"], is_builtin: true }];
  assert.deepEqual(templatesForKind(rows, "fun").map((r) => r.id), ["demo"]);
  assert.equal(exampleBrief("A cat astronaut on a candy planet..."), "A cat astronaut on a candy planet");
  assert.equal(exampleBrief("Un chat astronaute\u2026"), "Un chat astronaute");
  assert.equal(exampleBrief("No ellipsis"), "No ellipsis");
});

test("choosing a music source keeps its levels and drops what played before", () => {
  const withUpload = updateVideo(spec(), { music: { file_id: "f1", volume: 0.3, duck_db: 12 } });
  const withTrack = updateVideo(withUpload, musicSourcePatch(withUpload.music as Record<string, unknown>, { track: "signal" }));
  assert.deepEqual(withTrack.music, { volume: 0.3, duck_db: 12, track: "signal" });
  const ai = updateVideo(withTrack, musicSourcePatch(withTrack.music as Record<string, unknown>, { music_id: "m1" }));
  assert.deepEqual(ai.music, { volume: 0.3, duck_db: 12, music_id: "m1" });
  // A video without music gets the source alone, no null left behind.
  assert.deepEqual(updateVideo(spec(), musicSourcePatch(undefined, { track: "ascent" })).music, { track: "ascent" });
});

test("the preview's music follows the render's ducking envelope", () => {
  const points: [number, number][] = [[0, 1], [7.65, 1], [8, 0.3162], [20, 0.3162], [20.7, 1], [30, 1]];
  assert.equal(duckGainAt(points, 3), 1);
  assert.ok(Math.abs(duckGainAt(points, 7.825) - (1 + 0.3162) / 2) < 1e-9);
  assert.equal(duckGainAt(points, 12), 0.3162);
  assert.equal(duckGainAt(points, 40), 1);
  assert.equal(duckGainAt(null, 5), 1);
});

test("the preview plays each spoken narration from its own start, one at a time", () => {
  const lines = [
    { scene_id: "a", start: 0.35, seconds: 4.2 },
    { scene_id: "b", start: 6.1, seconds: 3 },
  ];
  assert.deepEqual([...spokenOffsets(lines, 0.2)], [["a", null], ["b", null]]);
  const inA = spokenOffsets(lines, 2.35);
  assert.ok(Math.abs((inA.get("a") ?? 0) - 2) < 1e-9 && inA.get("b") === null);
  // Its end is the next line's business: nothing speaks between two narrations.
  assert.deepEqual([...spokenOffsets(lines, 4.55)], [["a", null], ["b", null]]);
  assert.equal(spokenOffsets(lines, 6.1).get("b"), 0);
});

test("a video's card offers the editor to who may change it and a view to a reader", () => {
  assert.equal(VIDEO_OPEN_LABELS[videoOpenAction({ can_manage: true })], "Open in the editor");
  assert.equal(VIDEO_OPEN_LABELS[videoOpenAction({ can_manage: false })], "View now");
  assert.equal(videoOpenAction({}), "view");
});

test("a clip scene's sound choice is explained only when the scene left it to the rule", () => {
  assert.equal(sceneSoundReason({ plays: false, reason: "music" }), "music");
  assert.equal(sceneSoundReason({ plays: true, reason: "auto" }), "auto_kept");
  assert.equal(sceneSoundReason({ plays: false, reason: "video" }), "video_off");
  assert.equal(sceneSoundReason({ plays: true, reason: "video" }), "video_kept");
  assert.equal(sceneSoundReason({ plays: false, reason: "silent" }), "silent");
  assert.equal(sceneSoundReason({ plays: true, reason: "scene" }), null);
  assert.equal(sceneSoundReason(undefined), null);
});
